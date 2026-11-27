#!/usr/bin/env python3
"""Prove an evaluation adapter reports the business state truthfully.

An adapter that answers the envelope but reads the wrong table (or caches, or
scopes by the wrong key) makes every end-state check meaningless: the run
succeeds and the score is fiction. This script exercises the adapter the way
EvalScope will and fails when the adapter and the agent disagree.

Checks, in order:
  1. ``GET /health`` answers 200.
  2. ``POST /v1/eval/chat`` returns a string reply for a real message.
  3. ``GET /v1/eval/state`` changes after the turn — an unchanged state means
     the adapter is not reading what the agent wrote.
  4. The state is keyed by session: a second session must not inherit the
     first one's rows.
  5. ``POST /v1/eval/reset`` clears that session only, leaving the other one
     intact (EvalScope runs checks at judge time, so a global reset would
     erase other tasks' evidence).

Usage:
    python scripts/check_adapter.py \
      --adapter-url http://127.0.0.1:9115 \
      --agent-url http://127.0.0.1:9105 \
      --message "查一下我的余额"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any


def _request(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={'Content-Type': 'application/json', 'User-Agent': 'evalgen-adapter-check'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode('utf-8', errors='replace')
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = {'error': payload}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {'error': str(exc)}


def _state(adapter: str, session_id: str) -> tuple[int, Any]:
    return _request('GET', f'{adapter}/v1/eval/state?session_id={session_id}')


def _fingerprint(state: Any) -> str:
    """Stable view of a state payload, ignoring the session id it was asked for."""
    if isinstance(state, dict):
        state = {key: value for key, value in state.items() if key != 'session_id'}
    return json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)


def _missing_keys(state: Any, keys: list[str]) -> list[str]:
    """Keys the author declared as business state that the payload lacks.

    A dotted key is followed one level at a time, so ``order.status`` checks the
    nested field rather than a literal dotted name.
    """
    missing = []
    for key in keys:
        current: Any = state
        found = True
        for part in key.split('.'):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if not found:
            missing.append(key)
    return missing


def run_checks(
    adapter: str,
    agent_url: str,
    message: str,
    expect_keys: list[str] | None = None,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    session_a = f'check-{uuid.uuid4().hex[:8]}'
    session_b = f'check-{uuid.uuid4().hex[:8]}'

    status, health = _request('GET', f'{adapter}/health')
    results.append(('health', status == 200, f'GET /health -> {status}'))
    if status != 200:
        return results

    status, before = _state(adapter, session_a)
    if status != 200:
        results.append(('state readable', False, f'GET /v1/eval/state -> {status}: {before}'))
        return results
    results.append(('state readable', True, 'GET /v1/eval/state returned JSON'))

    status, chat = _request(
        'POST', f'{adapter}/v1/eval/chat',
        {'session_id': session_a, 'message': message}, timeout=120.0,
    )
    reply = chat.get('reply') if isinstance(chat, dict) else None
    ok = status == 200 and isinstance(reply, str) and bool(reply.strip())
    results.append(('chat replies', ok, f'POST /v1/eval/chat -> {status}, reply={str(reply)[:60]!r}'))
    if not ok:
        return results

    status, after = _state(adapter, session_a)
    changed = status == 200 and _fingerprint(before) != _fingerprint(after)
    results.append((
        'state follows the turn', changed,
        'state unchanged after a real turn — the adapter is not reading what the agent wrote'
        if not changed else 'state changed after the turn',
    ))

    status_b, state_b = _state(adapter, session_b)
    # A fresh session must look like the pre-turn state: no rows from session A.
    isolated = status_b == 200 and _fingerprint(state_b) == _fingerprint(before)
    results.append((
        'state is per session', isolated,
        'a fresh session already sees another session\'s state — the adapter is not '
        'scoping by session_id'
        if not isolated else 'a fresh session starts empty',
    ))

    # Give session B its own row, so the reset scope below is observable.
    _request(
        'POST', f'{adapter}/v1/eval/chat',
        {'session_id': session_b, 'message': message}, timeout=120.0,
    )
    status_b, state_b = _state(adapter, session_b)
    results.append((
        'second session is independent', status_b == 200 and _fingerprint(state_b) != _fingerprint(before),
        'session B shows no state after its own turn'
        if status_b != 200 or _fingerprint(state_b) == _fingerprint(before)
        else 'session B recorded its own state',
    ))

    # The state must carry the business state, not adapter-local bookkeeping.
    # An adapter that returns only its own counters passes every check above
    # while every end-state assertion is fiction, so the author declares a
    # business field the state must contain and this asserts it is there.
    if expect_keys:
        missing = _missing_keys(state_b, expect_keys)
        results.append((
            'state carries the business fields', not missing,
            f'state is missing {missing}; it may be adapter bookkeeping rather than the business '
            f'end state, which would make every end-state check meaningless'
            if missing else f'state exposes {", ".join(expect_keys)}',
        ))

    status, reset = _request('POST', f'{adapter}/v1/eval/reset?session_id={session_a}')
    ok_reset = status == 200
    results.append(('reset accepted', ok_reset, f'POST /v1/eval/reset -> {status}: {reset}'))
    if not ok_reset:
        return results

    status, cleared = _state(adapter, session_a)
    cleared_ok = status == 200 and _fingerprint(cleared) == _fingerprint(before)
    results.append((
        'reset clears this session', cleared_ok,
        'reset did not restore the pre-turn state for this session'
        if not cleared_ok else 'reset restored the pre-turn state',
    ))

    status_b2, state_b2 = _state(adapter, session_b)
    kept = status_b2 == 200 and _fingerprint(state_b2) == _fingerprint(state_b)
    results.append((
        'reset leaves other sessions alone', kept,
        'reset wiped another session — EvalScope runs checks at judge time, so this '
        'erases other tasks\' evidence'
        if not kept else 'other sessions untouched',
    ))

    # Clean up the session this script created.
    _request('POST', f'{adapter}/v1/eval/reset?session_id={session_b}')
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--adapter-url', required=True)
    parser.add_argument('--agent-url', default=None, help='for the report only; the adapter owns the call')
    parser.add_argument('--message', default='查一下我的余额', help='a message the agent can answer')
    parser.add_argument(
        '--expect-state-key',
        action='append',
        default=[],
        help='business field the state must expose (repeatable; dotted paths allowed). '
             'Without it, an adapter that returns only its own counters passes every check',
    )
    args = parser.parse_args()

    adapter = args.adapter_url.strip().rstrip('/')
    results = run_checks(adapter, args.agent_url or '', args.message, args.expect_state_key)
    for name, ok, detail in results:
        print(f'{"ok" if ok else "FAILED":7s} {name}: {detail}')
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f'adapter check: {len(results) - len(failed)}/{len(results)} passed; failed: {", ".join(failed)}')
        return 1
    print(f'adapter check: {len(results)}/{len(results)} passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
