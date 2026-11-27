#!/usr/bin/env python3
"""Discover candidate business-agent endpoints from a read-only scan.

The scanner produces `scan.json`; this script only reads that snapshot. It
extracts HTTP routes, health paths, ports and explicit loopback URLs, then
probes the candidates. It never starts a service and never reads project files
directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from _common import auth_hint, probe_agent

ROUTE_PATTERNS = (
    re.compile(r'(?:HandleFunc|Handle)\(\s*"(/[^"]*)"'),
    re.compile(r'\.(?:GET|POST|PUT|PATCH|DELETE)\(\s*"(/[^"]*)"'),
    re.compile(r'\.(?:Get|Post|Put|Patch|Delete|Handle)\(\s*"(/[^"]*)"'),
    re.compile(r'@(?:app|router|bp|blueprint)\.(?:get|post|put|patch|delete|route)\(\s*["\'](/[^"\']*)'),
    re.compile(r'@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*)?["\'](/[^"\']*)'),
    re.compile(r'\.(?:get|post|put|patch|delete)\(\s*["\'](/[^"\']*)'),
)
HEALTH_RE = re.compile(r'(?<![A-Za-z/])(/(?:health|healthz|livez|readyz|ping))\b')
PORT_PATTERNS = (
    re.compile(r'(?:PORT|port)\s*[:=]\s*["\']?(\d{2,5})'),
    re.compile(r'https?://(?:127\.0\.0\.1|localhost):(\d{2,5})'),
    re.compile(r'listen\([^)]*:(\d{2,5})'),
    re.compile(r'--port\s+(\d{2,5})'),
    re.compile(r'(?<![\w:."])(?:127\.0\.0\.1|localhost):(\d{2,5})\b'),
)
LOOPBACK_RE = re.compile(r'(?:https?://)?(?:127\.0\.0\.1|localhost)(?::\d{2,5})?(?:/[^\s`"\']*)?')
EVAL_PATHS = ('/v1/eval/chat', '/v1/eval/state', '/v1/eval/reset')
COMMON_PORTS = (8000, 8080, 3000, 5000, 9201)
NON_HTTP_HINTS = {
    'database': ('tcp(', 'mysql', 'postgres', 'postgresql', 'mongodb', 'mongo'),
    'redis': ('redis', 'goredis', 'redigo'),
    'broker': ('kafka', 'amqp', 'rabbitmq'),
}


def _line_number(text: str, index: int) -> int:
    return text.count('\n', 0, index) + 1


def _origin(url: str) -> str:
    value = (url or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = f'http://{value}'
    parsed = urlsplit(value.rstrip('/'))
    if not parsed.hostname:
        return ''
    try:
        parsed.port
    except ValueError:
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, '', '', '')).rstrip('/')


def _files(scan: dict) -> list[dict]:
    files = scan.get('files')
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _classify_port(excerpt: str, start: int, end: int) -> str:
    """Use the port's own line first to avoid crossing into a MySQL/Redis line."""
    line_start = excerpt.rfind('\n', 0, start) + 1
    line_end = excerpt.find('\n', end)
    if line_end == -1:
        line_end = len(excerpt)
    line = excerpt[line_start:line_end].lower()
    for kind, tokens in NON_HTTP_HINTS.items():
        if any(token in line for token in tokens):
            return kind
    window = excerpt[max(0, start - 80) : min(len(excerpt), end + 80)].lower()
    for kind, tokens in NON_HTTP_HINTS.items():
        if any(token in window for token in tokens):
            return kind
    return 'http'


def discover(
    scan: dict,
    extra_urls: list[str] | None = None,
    *,
    timeout: float = 2.0,
    chat_probe: bool = False,
    api_key: str = '',
    chat_path: str = '/v1/eval/chat',
    request_field: str = 'question',
    reply_field: str = 'answer',
    session_field: str = '',
) -> dict:
    routes: dict[tuple[str, str], dict] = {}
    health_paths: set[str] = set()
    ports: set[int] = set()
    non_http_ports: dict[str, str] = {}
    explicit_urls: set[str] = set()
    eval_paths: set[str] = set()

    for item in _files(scan):
        path = str(item.get('path') or '')
        excerpt = str(item.get('excerpt') or '')
        if not path or not excerpt:
            continue
        for pattern in ROUTE_PATTERNS:
            for match in pattern.finditer(excerpt):
                route = match.group(1)
                if not route.startswith('/'):
                    continue
                key = (route, path)
                routes.setdefault(
                    key,
                    {
                        'path': route,
                        'source': f'{path}:{_line_number(excerpt, match.start())}',
                    },
                )
        for match in HEALTH_RE.finditer(excerpt):
            health_paths.add(match.group(1))
        for pattern in PORT_PATTERNS:
            for match in pattern.finditer(excerpt):
                try:
                    port = int(match.group(1))
                except (TypeError, ValueError):
                    continue
                if not 1 <= port <= 65535:
                    continue
                kind = _classify_port(excerpt, match.start(), match.end())
                if kind == 'http':
                    ports.add(port)
                    non_http_ports.pop(str(port), None)
                else:
                    non_http_ports.setdefault(str(port), kind)
        for match in LOOPBACK_RE.finditer(excerpt):
            explicit_urls.add(_origin(match.group(0)))
        for eval_path in EVAL_PATHS:
            if eval_path in excerpt:
                eval_paths.add(eval_path)

    candidates: list[str] = []
    for url in extra_urls or []:
        if url.strip():
            candidates.append(_origin(url.strip()))
    candidates.extend(sorted(explicit_urls))
    candidates.extend(f'http://127.0.0.1:{port}' for port in sorted(ports))
    if not candidates:
        candidates.extend(f'http://127.0.0.1:{port}' for port in COMMON_PORTS)
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)

    probes = []
    for candidate in deduped:
        probe = probe_agent(candidate, timeout=timeout)
        if chat_probe and probe.get('online'):
            probe['chat'] = _probe_chat(candidate, timeout, api_key=api_key, chat_path=chat_path)
        probes.append(probe)
    recommended = next(
        (probe['url'] for probe in probes if probe.get('online') and probe.get('mode') == 'health'),
        '',
    )
    if not recommended:
        recommended = next((probe['url'] for probe in probes if probe.get('online')), '')
    continuity = None
    if recommended and chat_probe:
        continuity = probe_session_continuity(
            recommended,
            timeout=max(timeout, 10.0),
            api_key=api_key,
            chat_path=chat_path,
            request_field=request_field,
            reply_field=reply_field,
            session_field=session_field,
        )
    return {
        'scan_root': scan.get('root', ''),
        'routes': sorted(routes.values(), key=lambda item: (item['path'], item['source'])),
        'health_paths': sorted(health_paths),
        'ports': sorted(ports),
        'non_http_ports': {port: kind for port, kind in sorted(non_http_ports.items(), key=lambda item: int(item[0]))},
        'explicit_urls': sorted(explicit_urls),
        'eval_envelope': {
            'chat': '/v1/eval/chat' in eval_paths,
            'state': '/v1/eval/state' in eval_paths,
            'reset': '/v1/eval/reset' in eval_paths,
        },
        'candidates': probes,
        'recommended': recommended,
        'online': bool(recommended),
        'session_continuity': continuity,
    }


def _probe_chat(base_url: str, timeout: float, api_key: str = '', chat_path: str = '/v1/eval/chat') -> dict:
    url = f'{base_url.rstrip("/")}{chat_path}'
    payload = json.dumps({'session_id': '__evalgen_probe__', 'message': 'ping'}).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'User-Agent': 'evalscope-evalgen-probe'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    request = Request(url, data=payload, headers=headers, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            return {
                'ok': isinstance(data, dict) and isinstance(data.get('reply'), str),
                'status': response.status,
                'reply': str(data.get('reply', ''))[:120] if isinstance(data, dict) else '',
                'auth_required': False,
            }
    except HTTPError as exc:
        auth = auth_hint(exc.code)
        return {
            'ok': False,
            'status': exc.code,
            'error': str(exc),
            'auth_required': bool(auth),
            'note': auth or None,
        }
    except (URLError, HTTPException, TimeoutError, OSError) as exc:
        return {'ok': False, 'status': None, 'error': str(exc), 'auth_required': False}


SESSION_ID_FIELDS = ('session_id', 'thread_id', 'conversation_id', 'chat_id', 'sessionId', 'threadId')


def _session_field(data: dict) -> str:
    """Find the field the agent uses to continue a conversation."""
    for field in SESSION_ID_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return field
    nested = data.get('state')
    if isinstance(nested, dict):
        for field in SESSION_ID_FIELDS:
            value = nested.get(field)
            if isinstance(value, str) and value.strip():
                return field
    return ''


def probe_session_continuity(
    base_url: str,
    *,
    timeout: float = 10.0,
    api_key: str = '',
    chat_path: str = '/chat',
    request_field: str = 'question',
    reply_field: str = 'answer',
    session_field: str = '',
    token: str = '',
) -> dict:
    """Probe whether the agent actually carries context across two turns.

    Multi-turn tasks are only writable when the agent keeps a conversation
    alive. Three shapes exist and the source alone cannot tell them apart:

    * ``caller-session`` — the caller supplies the session id and the agent
      continues that conversation, so a confirm/cancel turn can reach the
      previous turn's pending action;
    * ``server-mints-per-request`` — the agent returns an id but mints a new one
      per request, so two POSTs are two conversations and a ``确认`` turn can
      never reach the pending action;
    * ``unknown`` — the agent gave no evidence either way.

    The test is behavioural, not a field comparison: a unique nonce is stated
    in turn one, and turn two asks for it back. An agent that echoes the nonce
    is continuing the conversation; one that does not, is not. Comparing the
    ids the agent returned is not enough — an agent can reuse an id while
    still starting a fresh conversation, and it can mint a new id per request
    while keeping a caller-supplied one as the real key.

    Writing a confirm/cancel task against anything but ``caller-session``
    produces a task that cannot pass, however correct the agent is.
    """
    base = base_url.strip().rstrip('/')
    if not base:
        return {'continuity': 'unknown', 'error': 'agent URL is required'}
    headers = {'Content-Type': 'application/json', 'User-Agent': 'evalscope-evalgen-probe'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    def call(payload: dict) -> dict:
        request = Request(
            f'{base}{chat_path}',
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8') or '{}')

    nonce = f'EVALGEN{uuid.uuid4().hex[:8].upper()}'
    first_payload = {request_field: f'请记住这个编号：{nonce}。只回复“已记住”即可。'}
    if token:
        first_payload['session_id'] = token
    try:
        first = call(first_payload)
    except HTTPError as exc:
        return {
            'continuity': 'unknown',
            'status': exc.code,
            'auth_required': bool(auth_hint(exc.code)),
            'error': auth_hint(exc.code) or str(exc),
        }
    except (URLError, HTTPException, TimeoutError, OSError) as exc:
        return {'continuity': 'unknown', 'error': str(exc)}

    if not isinstance(first, dict):
        return {'continuity': 'unknown', 'error': 'agent reply is not a JSON object'}

    # Which field carries the conversation? A caller-supplied token wins when
    # the agent names it; otherwise use whatever id the agent returned.
    returned = session_field or _session_field(first)
    echoed_id = str(first.get(returned) or '') if returned else ''
    carries_caller_token = bool(token) and echoed_id == token

    second_payload = {request_field: '刚才让你记住的编号是什么？'}
    if token and carries_caller_token:
        second_payload['session_id'] = token
    elif returned and echoed_id and not token:
        # Hand the agent back its own id: if it accepts it as the key, the
        # conversation continues; if it ignores it, it mints another.
        second_payload[returned] = echoed_id
    try:
        second = call(second_payload)
    except (HTTPError, URLError, HTTPException, TimeoutError, OSError) as exc:
        second = {'error': str(exc)}

    second_reply = str(second.get(reply_field) or '') if isinstance(second, dict) else ''
    remembered = nonce.lower() in second_reply.lower()
    second_id = str(second.get(returned) or '') if returned and isinstance(second, dict) else ''

    if remembered:
        continuity = 'caller-session' if carries_caller_token else 'server-session'
    elif returned and second_id and second_id != echoed_id:
        continuity = 'server-mints-per-request'
    elif returned:
        continuity = 'stateless-per-request'
    else:
        continuity = 'stateless-per-request'

    reply_field_ok = isinstance(first.get(reply_field), str)
    return {
        'continuity': continuity,
        'session_field': returned,
        'caller_token_echoed': carries_caller_token,
        'first_session': echoed_id,
        'second_session': second_id,
        'remembered_nonce': remembered,
        'reply_field': reply_field,
        'reply_field_ok': reply_field_ok,
        'multi_turn_possible': continuity in ('caller-session', 'server-session'),
        'note': _continuity_note(continuity, returned, reply_field_ok, reply_field),
    }


def _continuity_note(continuity: str, session_field: str, reply_ok: bool = True, reply_field: str = '') -> str:
    if not reply_ok:
        return (
            f'the reply field {reply_field!r} was not a string in the agent response; pass '
            '--response-reply-field for this agent and re-run, because the continuity verdict '
            'cannot be trusted without a readable reply'
        )
    if continuity == 'caller-session':
        return (
            f'the caller supplies {session_field or "a session id"} and the agent continues that '
            'conversation; multi-turn confirm/cancel tasks are writable'
        )
    if continuity == 'server-session':
        return (
            f'the agent keeps the conversation alive across turns using {session_field or "its own id"}; '
            'multi-turn tasks are writable if you send that id back on every turn'
        )
    if continuity == 'server-mints-per-request':
        return (
            f'the agent returns {session_field or "a session id"} but mints a new one per request, so two '
            'POSTs are two separate conversations. A "确认" turn cannot reach the previous turn\'s pending '
            'action over this endpoint: write single-turn tasks, or drive the agent\'s dedicated '
            'confirm/decision endpoint, which exec.type=agent_api does not do'
        )
    return (
        'the agent did not recall a nonce stated one turn earlier, so it is stateless per request. '
        'Write single-turn tasks only: a confirm/cancel turn cannot reach the previous turn\'s state'
    )


def print_discovery(result: dict) -> None:
    print(f'scan root: {result["scan_root"]}')
    print('routes:')
    if result['routes']:
        for route in result['routes'][:20]:
            print(f'  {route["path"]}  ({route["source"]})')
    else:
        print('  (none found)')
    print('health paths: ' + (', '.join(result['health_paths']) or '(none found)'))
    print('ports: ' + (', '.join(str(port) for port in result['ports']) or '(none found)'))
    if result.get('non_http_ports'):
        print('non-http ports: ' + ', '.join(f'{port} ({kind})' for port, kind in result['non_http_ports'].items()))
    envelope = result['eval_envelope']
    print(
        'eval envelope: ' + ', '.join(name for name, present in envelope.items() if present)
        if any(envelope.values())
        else 'eval envelope: (none)'
    )
    print('candidates:')
    for probe in result['candidates']:
        if probe.get('online'):
            state = 'online' if probe.get('mode') == 'health' else 'tcp-open'
        else:
            state = 'offline'
        detail = probe.get('error') or (probe.get('health') or {}).get('status') or ''
        chat = probe.get('chat')
        chat_note = ''
        if isinstance(chat, dict):
            if chat.get('auth_required'):
                chat_note = ', chat=auth-required'
            else:
                chat_note = f', chat={"ok" if chat.get("ok") else "failed"}'
        print(f'  {probe.get("url", "?")}: {state}{chat_note} {detail}'.rstrip())
        if probe.get('note'):
            print(f'    note: {probe["note"]}')
        if isinstance(chat, dict) and chat.get('note'):
            print(f'    chat note: {chat["note"]}')
    continuity = result.get('session_continuity')
    if continuity:
        print(f'session continuity: {continuity.get("continuity", "unknown")}')
        if continuity.get('note'):
            print(f'  {continuity["note"]}')
    if result['online']:
        print(f'recommended: {result["recommended"]}')
    else:
        print('no reachable agent; start the service first, then re-run this command')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--scan', required=True, help='scan.json produced by scan_sources.py')
    parser.add_argument('--base-url', action='append', default=[], help='extra agent base URL to probe (repeatable)')
    parser.add_argument('--timeout', type=float, default=2.0)
    parser.add_argument('--chat-probe', action='store_true', help='also POST a harmless ping to the chat endpoint')
    parser.add_argument(
        '--api-key',
        default='',
        help='bearer token for the agent, read from the environment by the caller; never written to the report',
    )
    parser.add_argument('--chat-path', default='/v1/eval/chat', help='chat endpoint path for the chat probe')
    parser.add_argument(
        '--request-field',
        default='question',
        help='field carrying the user message in the agent request body (default: question)',
    )
    parser.add_argument(
        '--response-reply-field',
        default='answer',
        help='field carrying the reply in the agent response body (default: answer)',
    )
    parser.add_argument(
        '--session-field',
        default='',
        help='field carrying the conversation id; auto-detected when omitted',
    )
    parser.add_argument(
        '--continuity',
        action='store_true',
        help='probe whether the agent keeps a conversation across two turns (needed before writing '
             'confirm/cancel multi-turn tasks)',
    )
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

    result = discover(
        scan,
        args.base_url,
        timeout=args.timeout,
        chat_probe=args.chat_probe or args.continuity,
        api_key=args.api_key,
        chat_path=args.chat_path,
        request_field=args.request_field,
        reply_field=args.response_reply_field,
        session_field=args.session_field,
    )
    print_discovery(result)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if result['online'] else 1


if __name__ == '__main__':
    sys.exit(main())
