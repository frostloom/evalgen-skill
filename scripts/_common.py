"""Shared path and URL rules for the EvalGen scripts."""

from __future__ import annotations

import os
import posixpath
import socket
from collections.abc import Iterable
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

HTTP_PREFIXES = ('http://127.0.0.1', 'http://localhost')
HEALTH_PATHS = ('/health', '/healthz', '/livez', '/readyz', '/ping')
SENSITIVE_DIRS = {'.ssh', '.aws', '.gnupg', '.docker', '.git'}
SENSITIVE_NAMES = {'.env', '.npmrc', '.pypirc', '.netrc'}
SENSITIVE_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.kdbx', '.env')
SENSITIVE_PREFIXES = ('id_rsa', 'id_ed25519', 'id_ecdsa', 'credentials')
# Status codes that mean "you reached the service but did not authenticate".
AUTH_STATUSES = (401, 403)


def auth_hint(status: int | None, env_var: str = '') -> str:
    """Explain a 401/403 instead of reporting a generic failure.

    Without this, a missing credential looks like an unreachable agent and the
    author wastes a round trip discovering that the service simply wanted a
    token.
    """
    if status not in AUTH_STATUSES:
        return ''
    variable = env_var or 'AGENT_API_KEY'
    return (
        f'the agent answered {status}: it is reachable but requires authentication. '
        f'Supply the credential through {variable} (the value must not be written into the eval set) '
        f'and re-run'
    )


def is_http_url(url: object) -> bool:
    """Accept an http(s) URL with a host, no userinfo and a valid port."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return port is None or 1 <= port <= 65535


def _prefix_allows(url: str, prefix: str) -> bool:
    """Match a trusted remote prefix by scheme, host, effective port and path."""
    try:
        target = urlsplit(url)
        allowed = urlsplit(prefix)
        target_port = target.port
        allowed_port = allowed.port
    except ValueError:
        return False
    if target.scheme not in {'http', 'https'} or allowed.scheme not in {'http', 'https'}:
        return False
    if not target.hostname or not allowed.hostname:
        return False
    if target.username is not None or target.password is not None:
        return False
    if allowed.username is not None or allowed.password is not None:
        return False
    if target.scheme != allowed.scheme or target.hostname.lower() != allowed.hostname.lower():
        return False
    effective_target = target_port if target_port is not None else (443 if target.scheme == 'https' else 80)
    if allowed_port is not None and effective_target != allowed_port:
        return False
    target_path = posixpath.normpath(unquote(target.path or '/'))
    allowed_path = posixpath.normpath(unquote(allowed.path or '/'))
    if allowed_path == '/':
        return True
    return target_path == allowed_path or target_path.startswith(allowed_path.rstrip('/') + '/')


def is_allowed_http(url: object, prefixes: Iterable[str] = ()) -> bool:
    """Accept loopback HTTP by default, or an explicitly trusted remote prefix.

    The prefix match alone is not enough: ``http://localhost:80@evil.com/x``
    starts with the loopback prefix but connects to ``evil.com``. Parse every
    URL and require an exact host with no userinfo. Remote prefixes are explicit
    opt-in, matching EvalScope's ``check_url_prefixes`` contract.
    """
    if not isinstance(url, str) or not url:
        return False
    # Keep the skill's explicit lowercase-scheme contract; parsed schemes are lowercased.
    if not url.startswith(('http://', 'https://')):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}:
        if parsed.username is not None or parsed.password is not None:
            return False
        return port is None or 1 <= port <= 65535
    return any(_prefix_allows(url, prefix) for prefix in prefixes)


def is_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and '..' not in path.parts


def assert_under_root(relative: str, root: str | Path) -> Path:
    """Resolve a relative path and reject escapes and symlinks under root."""
    if not is_relative_path(relative):
        raise ValueError(f'path must stay under project root: {relative!r}')
    root_resolved = Path(root).resolve()
    candidate = root_resolved / relative
    if candidate.is_symlink():
        raise ValueError(f'path must not be a symlink: {relative!r}')
    resolved = candidate.resolve()
    try:
        common = os.path.commonpath([str(root_resolved), str(resolved)])
    except ValueError as exc:
        raise ValueError(f'path is outside project root: {relative!r}') from exc
    if common != str(root_resolved):
        raise ValueError(f'path is outside project root: {relative!r}')
    return candidate


def is_sensitive_path(relative: str | Path) -> bool:
    path = Path(relative)
    for part in path.parts:
        name = part.lower()
        if name in SENSITIVE_DIRS or name.startswith('secrets'):
            return True
    name = path.name.lower()
    if name in SENSITIVE_NAMES or name.startswith('.env'):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return name.startswith(SENSITIVE_PREFIXES)


def probe_agent(
    base_url: str,
    *,
    timeout: float = 2.0,
    health_paths: tuple[str, ...] = HEALTH_PATHS,
) -> dict:
    """Check whether a local agent HTTP service is reachable.

    A health endpoint is preferred; any HTTP response still proves the service
    is listening. When no health endpoint answers, a TCP connect is used.
    """
    value = (base_url or '').strip().rstrip('/')
    if not value:
        return {'online': False, 'url': '', 'mode': 'none', 'health': None, 'error': 'agent URL is required'}
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return {
            'online': False,
            'url': value,
            'mode': 'none',
            'health': None,
            'error': 'agent URL must be http:// or https://',
        }
    try:
        port = parsed.port
    except ValueError as exc:
        return {'online': False, 'url': value, 'mode': 'none', 'health': None, 'error': str(exc)}
    for path in health_paths:
        url = f'{value}{path}'
        try:
            request = Request(url, method='GET', headers={'User-Agent': 'evalscope-evalgen-probe'})
            with urlopen(request, timeout=timeout) as response:
                return {
                    'online': True,
                    'url': value,
                    'mode': 'health',
                    'health': {'url': url, 'status': response.status},
                    'error': None,
                }
        except HTTPError as exc:
            # A 401/403 on the health path means the service is up; the health
            # probe simply is not public. Report it as online so the caller does
            # not chase a startup problem that does not exist.
            auth = auth_hint(exc.code)
            return {
                'online': True,
                'url': value,
                'mode': 'health',
                'health': {'url': url, 'status': exc.code},
                'auth_required': bool(auth),
                'error': None,
                'note': auth or None,
            }
        except (URLError, HTTPException, TimeoutError, OSError):
            continue
    port = port or (443 if parsed.scheme == 'https' else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
        return {'online': True, 'url': value, 'mode': 'tcp', 'health': None, 'error': None}
    except OSError as exc:
        return {'online': False, 'url': value, 'mode': 'tcp', 'health': None, 'error': str(exc)}


__all__ = [
    'AUTH_STATUSES',
    'HEALTH_PATHS',
    'HTTP_PREFIXES',
    'assert_under_root',
    'auth_hint',
    'is_allowed_http',
    'is_http_url',
    'is_relative_path',
    'is_sensitive_path',
    'probe_agent',
]
