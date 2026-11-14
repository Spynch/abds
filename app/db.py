"""Small ClickHouse HTTP client with typed query parameters."""
import base64
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def configure():
    """Read the sole configuration file without overriding container variables."""
    path = Path(__file__).resolve().parents[1] / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


class Database:
    """Execute synchronous SQL requests against one configured database."""

    def __init__(self):
        configure()
        self.name = os.environ["CLICKHOUSE_DATABASE"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.name):
            raise ValueError("Invalid database identifier")
        self.url = (f"http://{os.environ['CLICKHOUSE_HOST']}:"
                    f"{os.environ['CLICKHOUSE_HTTP_PORT']}/")
        credentials = (os.environ['CLICKHOUSE_USER'] + ':' +
                       os.environ['CLICKHOUSE_PASSWORD']).encode()
        self.auth = 'Basic ' + base64.b64encode(credentials).decode()

    def execute(self, sql, params=None, payload=None, database=True):
        """Execute SQL; stream optional CSV payload in a separate request body."""
        options = {"query": sql, "output_format_json_quote_64bit_integers": 0,
                   "date_time_output_format": "simple", "session_timezone": "UTC"}
        if database:
            options["database"] = self.name
        for key, value in (params or {}).items():
            options['param_' + key] = value
        body = b'' if payload is None else payload
        request = Request(self.url + '?' + urlencode(options), data=body,
                          headers={"Authorization": self.auth}, method="POST")
        try:
            with urlopen(request, timeout=180) as response:
                return response.read()
        except HTTPError as error:
            with error:
                detail = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'ClickHouse: {detail}') from error

    def rows(self, sql, params=None):
        """Return JSON rows with numeric identifiers and ISO timestamps."""
        return [json.loads(line) for line in
                self.execute(sql + ' FORMAT JSONEachRow', params).splitlines()]

    def insert(self, table, rows):
        """Insert application-owned rows as JSONEachRow."""
        payload = '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows)
        self.execute(f'INSERT INTO {table} FORMAT JSONEachRow',
                     payload=payload.encode('utf-8'))
