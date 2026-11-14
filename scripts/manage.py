"""Create schema, load deterministic CSV batches, or drop lab tables."""
import sys
import time
from pathlib import Path

from app.db import Database
from scripts.generate import TABLES

ROOT = Path(__file__).resolve().parents[1]


def schema(db):
    """Create the configured database and all seven tables."""
    db.execute(f'CREATE DATABASE IF NOT EXISTS {db.name}', database=False)
    for statement in (ROOT / 'sql/schema.sql').read_text(encoding='utf-8').split(';'):
        if statement.strip():
            db.execute(statement)


def load(db):
    """Replace dataset tables before streaming CSV in batches of 100,000 rows."""
    missing = [table for table in TABLES if not (ROOT / 'data' / (table+'.csv')).is_file()]
    if missing:
        raise SystemExit('Run make gen first. Missing: ' + ', '.join(missing))
    for table in TABLES:
        db.execute(f'TRUNCATE TABLE {table}')
        with (ROOT / 'data' / (table+'.csv')).open('rb') as source:
            batch = []
            for line in source:
                batch.append(line)
                if len(batch) == 100000:
                    db.execute(f'INSERT INTO {table} FORMAT CSV', payload=b''.join(batch))
                    batch.clear()
            if batch:
                db.execute(f'INSERT INTO {table} FORMAT CSV', payload=b''.join(batch))
        print('Loaded ' + table, flush=True)


def main():
    """Run the explicit management command and report elapsed time."""
    started = time.monotonic()
    command = sys.argv[1] if len(sys.argv) == 2 else ''
    if command not in ('schema', 'load', 'teardown'):
        raise SystemExit('Usage: python -m scripts.manage schema|load|teardown')
    db = Database()
    if command == 'schema':
        schema(db)
    elif command == 'load':
        load(db)
    else:
        for table in reversed(TABLES):
            db.execute(f'DROP TABLE IF EXISTS {table} SYNC')
    print(f'{command}: {time.monotonic()-started:.2f}s', flush=True)


if __name__ == '__main__':
    main()
