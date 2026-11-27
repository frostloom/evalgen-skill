#!/usr/bin/env python3
"""Import a generated eval set into a running EvalScope service.

The service only accepts JSONL files below one of its ``--eval-set-root``
directories. This script validates that relationship locally, checks that the
service is reachable, and then calls the import API. It never runs the eval set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _service_url(value: str) -> str:
    value = (value or '').strip().rstrip('/')
    if not value:
        return 'http://127.0.0.1:9010'
    if '://' not in value:
        value = f'http://{value}'
    return value


def _request_json(url: str, payload: dict | None, timeout: float) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json', 'User-Agent': 'evalscope-evalgen-import'},
        method='GET' if payload is None else 'POST',
    )

    def _decode(body: str, fallback: dict) -> dict:
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return fallback
        return parsed if isinstance(parsed, dict) else fallback

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
            return response.status, _decode(body, {'error': 'service returned a non-object JSON body'})
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        return exc.code, _decode(body, {'error': body or str(exc)})
    except (URLError, HTTPException, TimeoutError, OSError) as exc:
        return 0, {'error': str(exc)}


def _start_command(service_url: str, eval_set_root: str) -> str:
    parsed = urlsplit(service_url)
    port = parsed.port or 9010
    return (
        'evalscope service --host 127.0.0.1 '
        f'--port {port} --outputs ./outputs --enable-project-eval '
        f'--eval-set-root {eval_set_root}'
    )


def _task_env_assumptions(eval_set: Path) -> tuple[dict, list[str]]:
    """Read metadata.env_assumptions and reject tasks that disagree.

    The service stores one agent_url / project_root per imported entry, so a set
    whose tasks declare different values cannot be represented faithfully. Return
    the shared env plus one message per inconsistency.
    """
    env: dict = {}
    problems: list[str] = []
    first_seen: dict[str, tuple[str, str]] = {}
    try:
        lines = eval_set.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        return {}, [f'cannot read eval set: {exc}']
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError:
            continue
        metadata = task.get('metadata') if isinstance(task, dict) else None
        task_env = metadata.get('env_assumptions') if isinstance(metadata, dict) else None
        if not isinstance(task_env, dict):
            continue
        task_id = str(task.get('id', f'line {index}'))
        if not env:
            env = task_env
        for key in ('agent_url', 'state_dir'):
            value = task_env.get(key)
            if not isinstance(value, str) or not value:
                continue
            if key not in first_seen:
                first_seen[key] = (value, task_id)
            elif first_seen[key][0] != value:
                problems.append(
                    f'{key} differs: task {first_seen[key][1]} says {first_seen[key][0]!r}, '
                    f'task {task_id} says {value!r}; one imported entry can only hold one value'
                )
    return env, problems


def _check_state_root(service: str, state_dir: Path) -> str | None:
    """Ask the service whether it will accept this state dir before importing.

    The service only accepts a project_root under one of its --state-root
    directories, and reports that only when a run starts. Checking here turns a
    later 403 into an actionable message at import time.

    Returns an error string when the state dir is rejected *or* when the check
    could not be performed. Failing closed matters: a silent pass here would
    push the same 403 back to the moment the user presses Run.
    """
    status, body = _request_json(f'{service}/api/v1/project-eval/state-roots', None, 5.0)
    if status != 200 or not isinstance(body, dict):
        return (
            f'could not verify state roots: GET {service}/api/v1/project-eval/state-roots '
            f'returned {status}. Is this a project_eval-enabled EvalScope service?'
        )
    roots = body.get('roots')
    if not isinstance(roots, list) or not roots:
        return (
            f'the service reports no state roots, so no project_root can be accepted; restart it '
            f'with --state-root {os.path.realpath(str(state_dir))}'
        )
    resolved = os.path.realpath(str(state_dir))
    for root in roots:
        if not isinstance(root, str):
            continue
        root_resolved = os.path.realpath(root)
        if os.path.commonpath([root_resolved, resolved]) == root_resolved:
            return None
    return (
        f'state dir {resolved} is outside the service state roots '
        f'{[os.path.realpath(r) for r in roots if isinstance(r, str)]}; '
        f'restart the service with --state-root {resolved} (or a parent of it)'
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--eval-set', required=True)
    parser.add_argument('--eval-set-root', required=True)
    parser.add_argument('--service-url', default='http://127.0.0.1:9010')
    parser.add_argument('--name', default=None, help='display name; defaults to the parent directory name')
    parser.add_argument('--agent-url', default=None, help='business agent URL to prefill on the Eval Sets page')
    parser.add_argument(
        '--state-dir',
        default=None,
        help='runtime state directory containing agent.db; prefilled as project_root',
    )
    parser.add_argument('--verified', action='store_true', help='mark the set as dry-run verified')
    parser.add_argument('--timeout', type=float, default=10.0)
    args = parser.parse_args()

    service = _service_url(args.service_url)
    eval_set = Path(args.eval_set).resolve()
    root = Path(args.eval_set_root).resolve()
    if not eval_set.is_file() or eval_set.suffix != '.jsonl':
        print(f'error: eval set must be an existing .jsonl file: {eval_set}')
        return 2
    try:
        common = os.path.commonpath([str(root), str(eval_set)])
    except ValueError:
        common = ''
    if common != str(root):
        print(f'error: eval set is outside the eval-set root: {eval_set}')
        print(f'       root: {root}')
        print('       Move the file under the root or start the service with this root:')
        print(f'       {_start_command(service, str(root))}')
        return 2

    status, health = _request_json(f'{service}/health', None, args.timeout)
    if status != 200:
        print(f'error: EvalScope service is not reachable at {service} (status {status})')
        print('       start it first:')
        print(f'       {_start_command(service, str(root))}')
        return 2

    env, env_problems = _task_env_assumptions(eval_set)
    if env_problems:
        print('error: the eval set declares inconsistent environment assumptions:')
        for problem in env_problems:
            print(f'       {problem}')
        print('       regenerate the set so every task agrees, or split it into separate sets')
        return 2
    state_dir = args.state_dir or env.get('state_dir') or env.get('project_root')
    if state_dir:
        problem = _check_state_root(service, Path(str(state_dir)))
        if problem:
            print(f'error: {problem}')
            return 2
    else:
        print('warning: no --state-dir and no metadata.env_assumptions.state_dir; the Eval Sets page '
              'will fall back to the eval-set directory as project_root')
    agent_url = args.agent_url or env.get('agent_url')
    if not agent_url:
        print('warning: no --agent-url and no metadata.env_assumptions.agent_url; the Eval Sets page '
              'will require the user to type it')

    name = (args.name or '').strip() or eval_set.parent.name or eval_set.stem
    payload = {
        'path': str(eval_set),
        'name': name,
        'verified': bool(args.verified),
    }
    if agent_url:
        payload['agent_url'] = agent_url.strip()
    if state_dir:
        payload['project_root'] = os.path.realpath(str(state_dir))
    status, body = _request_json(f'{service}/api/v1/project-eval/import', payload, args.timeout)
    if not isinstance(body, dict):
        body = {'error': f'unexpected response body: {body!r}'}
    if status == 403:
        print(f'error: the service rejected the path: {body.get("error", "forbidden")}')
        print('       the eval-set root used by the service does not include this file:')
        print(f'       {_start_command(service, str(root))}')
        return 2
    if status != 200:
        print(f'error: import failed (status {status}): {body.get("error", body)}')
        return 1
    if 'id' not in body:
        print(f'error: the service returned an unexpected body; is this an EvalScope service? {body}')
        return 1

    print(f'imported: {body.get("name", name)}')
    print(f'  id: {body.get("id", "?")}')
    print(f'  tasks: {body.get("task_count", "?")}')
    print(f'  path: {body.get("path", eval_set)}')
    if body.get('agent_url'):
        print(f'  agent: {body["agent_url"]}')
    if body.get('project_root'):
        print(f'  state: {body["project_root"]}')
    print(f'  open: {service}/eval-sets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
