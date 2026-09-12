"""Validate CSV files against an explicit contract and load a local warehouse."""
from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def load_contract(path):
    contract = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(contract, dict) or set(contract) != {'name', 'key', 'fields'}:
        raise ValueError('contract requires exactly name, key, and fields')
    if not isinstance(contract['name'], str) or not contract['name'].strip():
        raise ValueError('contract name must be a nonempty string')
    fields = contract['fields']
    if not isinstance(fields, list) or not fields:
        raise ValueError('contract fields must be a nonempty list')
    names = []
    for field in fields:
        if not isinstance(field, dict) or not {'name', 'type', 'required'} <= field.keys():
            raise ValueError('each field requires name, type, and required')
        if set(field) - {'name', 'type', 'required', 'min', 'enum'}:
            raise ValueError('unsupported field constraint')
        if not isinstance(field['name'], str) or not re.fullmatch(r'[a-z][a-z0-9_]*', field['name']):
            raise ValueError('field names must be lower-case identifiers')
        if field['type'] not in ('string', 'integer', 'decimal', 'date'):
            raise ValueError('unsupported field type')
        if type(field['required']) is not bool:
            raise ValueError('required must be boolean')
        if 'enum' in field and (field['type'] != 'string' or
            not isinstance(field['enum'], list) or not field['enum'] or
            any(not isinstance(x, str) for x in field['enum'])):
            raise ValueError('enum requires a nonempty list of strings on a string field')
        if 'min' in field:
            if field['type'] not in ('integer', 'decimal'):
                raise ValueError('min is only supported on numeric fields')
            try:
                minimum = Decimal(str(field['min']))
            except InvalidOperation as error:
                raise ValueError('invalid minimum') from error
            if not minimum.is_finite():
                raise ValueError('minimum must be finite')
        names.append(field['name'])
    if len(names) != len(set(names)):
        raise ValueError('duplicate field names')
    if contract['key'] not in names:
        raise ValueError('key must name a contract field')
    key_field = next(f for f in fields if f['name'] == contract['key'])
    if key_field['type'] != 'string' or not key_field['required']:
        raise ValueError('key must be a required string')
    return contract


def parse_value(raw, field):
    value = raw.strip()
    if not value:
        if field['required']:
            raise ValueError('required value is missing')
        return None
    kind = field['type']
    if kind == 'integer':
        if not re.fullmatch(r'[+-]?\d+', value):
            raise ValueError('expected integer')
        parsed = int(value)
    elif kind == 'decimal':
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise ValueError('expected decimal') from error
        if not parsed.is_finite():
            raise ValueError('decimal must be finite')
    elif kind == 'date':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('expected YYYY-MM-DD')
        parsed = date.fromisoformat(value).isoformat()
    else:
        parsed = value
    if 'min' in field and parsed < Decimal(str(field['min'])):
        raise ValueError(f"must be at least {field['min']}")
    if 'enum' in field and parsed not in field['enum']:
        raise ValueError('value is outside the allowed set')
    # Preserve decimals exactly rather than introducing binary floating point.
    return str(parsed) if kind == 'decimal' else parsed


def validate(raw_bytes, contract):
    reader = csv.reader(io.StringIO(raw_bytes.decode('utf-8-sig'), newline=''), strict=True)
    header = next(reader, None)
    expected = [f['name'] for f in contract['fields']]
    if header is None or len(header) != len(set(header)) or set(header) != set(expected):
        raise ValueError(f'CSV header must contain exactly {expected}')
    accepted, rejected, seen = [], [], set()
    for values in reader:
        line = reader.line_num  # Last physical line of this CSV record.
        if len(values) != len(header):
            rejected.append({'line': line, 'errors': ['column count mismatch'], 'raw': values})
            continue
        raw = dict(zip(header, values))
        parsed, errors = {}, []
        for field in contract['fields']:
            try:
                parsed[field['name']] = parse_value(raw[field['name']], field)
            except (ValueError, InvalidOperation) as error:
                errors.append(f"{field['name']}: {error}")
        key = parsed.get(contract['key'])
        if key is not None:
            if key in seen:
                errors.append('duplicate key within this file')
            seen.add(key)  # First occurrence reserves the key, even if invalid.
        if errors:
            rejected.append({'line': line, 'errors': errors, 'raw': raw})
        else:
            accepted.append(parsed)
    return accepted, rejected


DDL = """
CREATE TABLE IF NOT EXISTS contracts (
    name TEXT PRIMARY KEY, hash TEXT NOT NULL, definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, contract_name TEXT NOT NULL, content_hash TEXT NOT NULL,
    accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(contract_name, content_hash)
);
CREATE TABLE IF NOT EXISTS records (
    contract_name TEXT NOT NULL, record_key TEXT NOT NULL, body TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    PRIMARY KEY(contract_name, record_key)
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    line INTEGER NOT NULL, errors TEXT NOT NULL, raw TEXT NOT NULL
);
"""


def ingest(csv_path, contract_path, database, dry_run=False, strict=False):
    contract = load_contract(contract_path)
    csv_path = Path(csv_path)
    if csv_path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('local demo limit is 20 MiB per file')
    data = csv_path.read_bytes()
    accepted, rejected = validate(data, contract)
    summary = {'accepted': len(accepted), 'rejected': len(rejected),
               'errors': rejected, 'dry_run': dry_run, 'skipped': False}
    if strict and rejected:
        raise ValueError(f'{len(rejected)} invalid records; strict import made no changes')
    if dry_run:
        return summary
    definition = json.dumps(contract, sort_keys=True, separators=(',', ':'))
    contract_hash = fingerprint(definition.encode())
    content_hash = fingerprint(data)
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database, timeout=10)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(DDL)
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT hash FROM contracts WHERE name=?', (contract['name'],)).fetchone()
        if old and old[0] != contract_hash:
            raise ValueError('contract changed; use a new versioned contract name')
        db.execute('INSERT OR IGNORE INTO contracts VALUES (?,?,?)',
                   (contract['name'], contract_hash, definition))
        batch = db.execute('SELECT id FROM batches WHERE contract_name=? AND content_hash=?',
                           (contract['name'], content_hash)).fetchone()
        if batch:
            summary.update(batch_id=batch[0], skipped=True)
            db.rollback()
            return summary
        batch_id = db.execute('''INSERT INTO batches
            (contract_name,content_hash,accepted,rejected) VALUES (?,?,?,?)''',
            (contract['name'], content_hash, len(accepted), len(rejected))).lastrowid
        for record in accepted:
            db.execute('''INSERT INTO records VALUES (?,?,?,?)
                ON CONFLICT(contract_name,record_key) DO UPDATE
                SET body=excluded.body,batch_id=excluded.batch_id''',
                (contract['name'], record[contract['key']], json.dumps(record), batch_id))
        db.executemany('INSERT INTO quarantine VALUES (?,?,?,?)', [
            (batch_id, row['line'], json.dumps(row['errors']), json.dumps(row['raw']))
            for row in rejected
        ])
        db.commit()
        summary['batch_id'] = batch_id
        return summary
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def report(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        return {
            'batches': [dict(row) for row in db.execute('SELECT * FROM batches ORDER BY id')],
            'record_count': db.execute('SELECT count(*) FROM records').fetchone()[0],
            'quarantined_count': db.execute('SELECT count(*) FROM quarantine').fetchone()[0],
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='var/warehouse.db')
    commands = parser.add_subparsers(dest='command', required=True)
    load = commands.add_parser('ingest')
    load.add_argument('csv')
    load.add_argument('--contract', required=True)
    load.add_argument('--dry-run', action='store_true')
    load.add_argument('--strict', action='store_true')
    commands.add_parser('report')
    args = parser.parse_args()
    try:
        result = report(args.db) if args.command == 'report' else ingest(
            args.csv, args.contract, args.db, args.dry_run, args.strict)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, csv.Error, sqlite3.Error) as error:
        parser.exit(2, f'Error: {error}\n')


if __name__ == '__main__':
    main()
