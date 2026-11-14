"""JSON HTTP API implementing SCD operations on ClickHouse."""
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.db import Database, configure

WRITE_LOCK = threading.Lock()


class ApiError(Exception):
    """An expected HTTP error."""
    def __init__(self, status, message):
        self.status = status
        self.message = message


def invalid(name, kind='field'):
    """Raise a consistent validation error."""
    raise ApiError(400, f'invalid "{name}" {kind}')


def uint(value, name, bits=64, kind='field'):
    """Validate an unsigned integer, excluding booleans and floats."""
    if type(value) is not int or not 0 <= value < 2 ** bits:
        invalid(name, kind)
    return value


def day(value, name, kind='parameter'):
    """Validate a strict ISO calendar date."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        invalid(name, kind)
    try:
        date.fromisoformat(value)
    except ValueError:
        invalid(name, kind)
    return value


def timestamp(previous=None):
    """Advance second-resolution versions even for rapid successive requests."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    if previous:
        now = max(now, datetime.fromisoformat(previous) + timedelta(seconds=1))
    return now.strftime('%Y-%m-%d %H:%M:%S')


class Handler(BaseHTTPRequestHandler):
    """Route HTTP requests and translate expected errors to JSON."""
    db = None

    def reply(self, status, data=None):
        """Send JSON or an explicitly empty response body."""
        body = b'' if data is None else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        if data is not None:
            self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        """Read a bounded JSON object."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError()
            result = json.loads(self.rfile.read(size))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'invalid JSON body')

    def one(self, sql, params=None):
        """Fetch one required row or return the specified not-found error."""
        rows = self.db.rows(sql, params)
        if not rows:
            raise ApiError(404, 'Not found')
        return rows[0]

    def exists(self, table, identifier):
        """Check a foreign identifier in an application-owned table."""
        final = ' FINAL' if table in ('dim_users', 'dim_tariffs') else ''
        self.one(f'SELECT id FROM {table}{final} WHERE id = {{id:UInt64}} LIMIT 1',
                 {'id': identifier})

    def dispatch(self):
        """Handle a request with centralized exception mapping."""
        try:
            if self.command in ('POST', 'PATCH'):
                with WRITE_LOCK:
                    status, data = self.route()
            else:
                status, data = self.route()
            self.reply(status, data)
        except ApiError as error:
            self.reply(error.status, {'message': error.message})
        except Exception:
            logging.exception('Request failed')
            self.reply(500, {'message': 'Internal server error'})

    do_GET = dispatch
    do_PATCH = dispatch
    do_POST = dispatch

    def route(self):
        """Route the public lab API."""
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        method = self.command
        if url.path == '/ping' and method == 'GET':
            return 200, None
        if url.path == '/tariffs' and method == 'GET':
            target = day(query.get('date', [''])[0], 'date')
            return 200, self.one(
                'SELECT id,name,price_day_kwph,price_night_kwph,valid_from,valid_to '
                'FROM dim_tariffs FINAL WHERE toString(valid_from) <= {date:String} '
                'AND {date:String} < toString(valid_to) ORDER BY id,valid_from DESC LIMIT 1',
                {'date': target + ' 00:00:00'})
        match = re.fullmatch(r'/(users|realty)/(\d+)(?:/(readings|invoices|gauges))?', url.path)
        if not match:
            raise ApiError(404, 'Not found')
        entity, identifier, resource = match.groups()
        identifier = uint(int(identifier), 'id', kind='parameter')
        params = {'id': identifier}
        if entity == 'users' and resource is None:
            user = self.one('SELECT * FROM dim_users FINAL WHERE id={id:UInt64}', params)
            if method == 'GET':
                return 200, {key: user[key] for key in ('id', 'fio', 'phone', 'phone_prev')}
            if method == 'PATCH':
                body = self.body()
                if not any(key in body for key in ('fio', 'phone')):
                    invalid('fio')
                for key in ('fio', 'phone'):
                    if key in body and (not isinstance(body[key], str) or not body[key].strip()):
                        invalid(key)
                updates = []
                if 'fio' in body:
                    updates.append('fio={fio:String}')
                    params['fio'] = body['fio']
                if 'phone' in body and body['phone'] != user['phone']:
                    updates.extend(['phone={phone:String}', 'phone_prev={previous:String}'])
                    params.update(phone=body['phone'], previous=user['phone'])
                if updates:
                    self.db.execute('ALTER TABLE dim_users UPDATE ' + ','.join(updates) +
                                    ' WHERE id={id:UInt64} SETTINGS mutations_sync=1', params)
                return 204, None
        if entity != 'realty':
            raise ApiError(404, 'Not found')
        realty = self.one('SELECT id,address,square,type,number,rooms,floor,porch '
                          'FROM dim_realty WHERE id={id:UInt64}', params)
        if resource is None and method == 'GET':
            return 200, realty
        if resource == 'readings' and method == 'GET':
            start = day(query.get('date_from', [''])[0], 'date_from')
            end = day(query.get('date_to', [''])[0], 'date_to')
            if start > end:
                invalid('date_from', 'parameter')
            params.update(start=start, end=end)
            rows = self.db.rows('SELECT reading_date,user_id,tariff_id,gauge_id,day_kw,night_kw '
                                'FROM fact_meter_readings FINAL WHERE realty_id={id:UInt64} '
                                'AND toString(reading_date) BETWEEN {start:String} AND {end:String} '
                                'ORDER BY reading_date', params)
            return 200, {'readings': rows, 'count': len(rows)}
        if resource == 'readings' and method == 'POST':
            body = self.body()
            row = {'realty_id': identifier}
            for key in ('user_id', 'gauge_id', 'tariff_id', 'day_kw', 'night_kw'):
                row[key] = uint(body.get(key), key, 32 if key.endswith('_kw') else 64)
            row['reading_date'] = day(body.get('reading_date'), 'reading_date', 'field')
            if not '1970-01-01' <= row['reading_date'] <= '2149-06-06':
                invalid('reading_date')
            for table, key in (('dim_users', 'user_id'), ('dim_gauges', 'gauge_id'),
                               ('dim_tariffs', 'tariff_id')):
                self.exists(table, row[key])
            old = self.db.rows('SELECT created_at FROM fact_meter_readings FINAL '
                               'WHERE realty_id={id:UInt64} AND reading_date={date:Date}',
                               {'id': identifier, 'date': row['reading_date']})
            row['created_at'] = timestamp(old[0]['created_at'] if old else None)
            self.db.insert('fact_meter_readings', [row])
            return 201, None
        if resource == 'invoices' and method == 'GET':
            for key, default in (('limit', '10'), ('offset', '0')):
                value = query.get(key, [default])[0]
                if not re.fullmatch(r'[0-9]+', value):
                    invalid(key, 'parameter')
                params[key] = uint(int(value), key, kind='parameter')
            count = self.one('SELECT count() AS n FROM fact_invoice WHERE realty_id={id:UInt64}', params)['n']
            rows = self.db.rows('SELECT tariff_id,gauge_id,day_kw,night_kw,price,deadline,created_at '
                                'FROM fact_invoice WHERE realty_id={id:UInt64} '
                                'ORDER BY created_at DESC LIMIT {limit:UInt64} OFFSET {offset:UInt64}', params)
            return 200, {'invoices': rows, 'count': count}
        if resource == 'gauges' and method == 'POST':
            gauge = uint(self.body().get('gauge_id'), 'gauge_id')
            self.exists('dim_gauges', gauge)
            active = self.db.rows('SELECT * FROM fact_gauge_bindings FINAL '
                                  'WHERE realty_id={id:UInt64} AND sign=1', params)
            version = max((row['version'] for row in active), default=0) + 1
            rows = [dict(row, sign=-1) for row in active]
            rows.append({'gauge_id': gauge, 'realty_id': identifier,
                         'created_at': timestamp(), 'sign': 1, 'version': version})
            self.db.insert('fact_gauge_bindings', rows)
            return 201, None
        raise ApiError(405, 'Method not allowed')


def main():
    """Start one process; serialize writes to preserve SCD read-modify-write order."""
    configure()
    Handler.db = Database()
    ThreadingHTTPServer(('0.0.0.0', int(os.environ['APP_PORT'])), Handler).serve_forever()


if __name__ == '__main__':
    main()
