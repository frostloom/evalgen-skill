#!/usr/bin/env python3
"""Detect where a business agent keeps its state, from a read-only scan.

The adapter a project needs depends on how its state is stored, and that is not
knowable in advance: it may be SQLite, MySQL/Postgres, an HTTP status API, a
file on disk, or nothing readable at all. This script reports the candidates it
finds in ``scan.json`` together with the evidence, and never connects to any of
them. The coding agent uses the report to decide what the adapter must read.

Usage:
    python scripts/detect_state_source.py --scan artifacts/scan.json --json-out artifacts/state.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Each pattern is (kind, label, compiled regex). Order matters: the first match
# in a file wins, so more specific engines are listed before generic ones.
DRIVERS = (
    ('mysql', 'MySQL DSN', re.compile(r'(?:root|user|admin):[^@\s"\']*@tcp\([^)]*\)/[A-Za-z0-9_]+')),
    ('mysql', 'go-sql-driver', re.compile(r'github\.com/go-sql-driver/mysql')),
    ('mysql', 'pymysql', re.compile(r'\bpymysql\b')),
    ('postgres', 'Postgres DSN', re.compile(r'postgres(?:ql)?://[^\s"\']+')),
    ('postgres', 'psycopg', re.compile(r'\bpsycopg2?\b')),
    ('sqlite', 'SQLite connect', re.compile(r'sqlite3\.connect\(|sqlite\.Open\(|"file:[^"]+\.db')),
    ('redis', 'Redis client', re.compile(r'\bgo-redis\b|redis\.NewClient|ioredis|\bredis\.Redis\b')),
    ('mongo', 'MongoDB client', re.compile(r'\bmongodb://|pymongo|go\.mongodb\.org')),
)
# Table/schema declarations are the best hint for what a state endpoint should read.
TABLE_RE = re.compile(
    r'CREATE TABLE (?:IF NOT EXISTS )?[`"]?([A-Za-z_][\w]*)',
    re.IGNORECASE,
)
ORM_MODEL_RE = re.compile(r'gorm:"[^"]*"|TableName\(\)\s+string\s*\{\s*return\s+"([\w]+)"')
STATE_PATH_RE = re.compile(
    r'["\'](/(?:v\d+/)?(?:eval/)?(?:state|status|health|healthz|inspect|debug)[^"\']*)["\']'
)
FILE_STATE_RE = re.compile(r'(?:state|data|store)\.(?:json|db|sqlite3?|sqlite)\b')


def _files(scan: dict) -> list[dict]:
    files = scan.get('files')
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _line_of(text: str, index: int) -> int:
    return text.count('\n', 0, index) + 1


def detect(scan: dict) -> dict:
    engines: dict[str, list[dict]] = {}
    tables: dict[str, dict] = {}
    state_paths: set[str] = set()
    state_files: set[str] = set()

    for item in _files(scan):
        path = str(item.get('path') or '')
        excerpt = item.get('excerpt')
        if not path or not isinstance(excerpt, str) or not excerpt:
            continue
        for kind, label, pattern in DRIVERS:
            match = pattern.search(excerpt)
            if not match:
                continue
            entry = {
                'kind': kind,
                'evidence': label,
                'source': f'{path}:{_line_of(excerpt, match.start())}',
                'sample': match.group(0)[:120],
            }
            bucket = engines.setdefault(kind, [])
            if not any(existing['source'] == entry['source'] and existing['evidence'] == label for existing in bucket):
                bucket.append(entry)
        for match in TABLE_RE.finditer(excerpt):
            name = match.group(1)
            tables.setdefault(name, {
                'name': name,
                'source': f'{path}:{_line_of(excerpt, match.start())}',
            })
        for match in ORM_MODEL_RE.finditer(excerpt):
            name = match.group(1)
            if name:
                tables.setdefault(name, {
                    'name': name,
                    'source': f'{path}:{_line_of(excerpt, match.start())}',
                })
        for match in STATE_PATH_RE.finditer(excerpt):
            state_paths.add(match.group(1))
        for match in FILE_STATE_RE.finditer(excerpt):
            state_files.add(match.group(0))

    ordered = {
        kind: sorted(engines[kind], key=lambda entry: entry['source'])
        for kind in ('mysql', 'postgres', 'sqlite', 'redis', 'mongo')
        if kind in engines
    }
    primary = next((kind for kind in ('sqlite', 'postgres', 'mysql') if kind in ordered), '')
    if not primary and state_paths:
        primary = 'http'
    if not primary and state_files:
        primary = 'file'

    return {
        'scan_root': scan.get('root', ''),
        'storage_engines': ordered,
        'primary_engine': primary,
        'tables': sorted(tables.values(), key=lambda entry: entry['name']),
        'state_endpoints': sorted(state_paths),
        'state_files': sorted(state_files),
        'notes': _notes(ordered, primary, state_paths, state_files),
    }


def _notes(engines: dict, primary: str, state_paths: set[str], state_files: set[str]) -> list[str]:
    notes = [
        'An adapter is required unless the agent already answers /v1/eval/chat and exposes a '
        'read-only state endpoint.',
    ]
    if not engines and not state_paths and not state_files:
        notes.append(
            'No storage engine or state endpoint was found in the scanned excerpts. Either the '
            'state lives outside the scanned scope, or it is not readable: widen the scan, or ask '
            'the user how the end state can be observed.'
        )
        return notes
    if primary in {'mysql', 'postgres'}:
        notes.append(
            f'State lives in {primary}. EvalScope checks cannot read it directly (only sqlite, '
            'http_json and files are supported), so the adapter must expose /v1/eval/state by '
            'querying it read-only.'
        )
    elif primary == 'sqlite':
        notes.append(
            'State lives in SQLite. EvalScope can read it directly through sqlite checks, so the '
            'adapter only needs the chat envelope unless a per-task reset or a derived view is '
            'needed.'
        )
    elif primary == 'http':
        notes.append(
            'A state endpoint is mentioned; confirm it is read-only and keyed by the session id '
            'before relying on it.'
        )
    elif primary == 'file':
        notes.append(
            'State may live in a file; EvalScope file/json_path checks can read it if the path is '
            'stable and under the project root.'
        )
    if state_paths:
        notes.append(f'Candidate state endpoints: {", ".join(sorted(state_paths))}')
    return notes


def print_report(result: dict) -> None:
    print(f"scan root: {result['scan_root'] or '(unknown)'}")
    print(f"primary engine: {result['primary_engine'] or '(none detected)'}")
    for kind, entries in result['storage_engines'].items():
        print(f'  {kind}:')
        for entry in entries[:5]:
            print(f"    {entry['evidence']}  ({entry['source']})")
    if result['tables']:
        print('tables: ' + ', '.join(entry['name'] for entry in result['tables'][:20]))
    if result['state_endpoints']:
        print('state endpoints: ' + ', '.join(result['state_endpoints']))
    if result['state_files']:
        print('state files: ' + ', '.join(result['state_files']))
    for note in result['notes']:
        print(f'note: {note}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--scan', required=True)
    parser.add_argument('--json-out', default=None)
    args = parser.parse_args()

    path = Path(args.scan)
    if not path.is_file():
        print(f'error: scan not found: {path}')
        return 2
    try:
        scan = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'error: could not read scan: {exc}')
        return 2
    if not isinstance(scan, dict):
        print('error: scan must be a JSON object')
        return 2

    result = detect(scan)
    print_report(result)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
