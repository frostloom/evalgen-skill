import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'check_adapter.py'


def make_adapter(mode: str):
    """A fake adapter. mode selects which behaviour to break."""
    sessions: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                return self._send({'status': 'ok'})
            if self.path.startswith('/v1/eval/state'):
                session = self.path.split('session_id=')[-1]
                if mode == 'shared-state':
                    count = sum(sessions.values())
                elif mode == 'frozen-state':
                    count = 0
                else:
                    count = sessions.get(session, 0)
                return self._send({'session_id': session, 'audit_count': count})
            return self._send({'error': 'not found'}, status=404)

        def do_POST(self):
            length = int(self.headers.get('content-length', '0'))
            body = json.loads(self.rfile.read(length) or b'{}')
            if self.path.startswith('/v1/eval/reset'):
                session = self.path.split('session_id=')[-1]
                if mode == 'global-reset':
                    sessions.clear()
                else:
                    sessions.pop(session, None)
                return self._send({'reset': True})
            if self.path == '/v1/eval/chat':
                session = str(body.get('session_id') or '')
                if mode == 'no-state':
                    pass
                else:
                    sessions[session] = sessions.get(session, 0) + 1
                return self._send({'reply': '已处理', 'steps': []})
            return self._send({'error': 'not found'}, status=404)

        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class CheckAdapterTest(unittest.TestCase):
    def _run(self, mode: str):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_adapter(mode))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            return subprocess.run(
                [sys.executable, str(SCRIPT), '--adapter-url', f'http://127.0.0.1:{port}',
                 '--message', '查余额'],
                capture_output=True, text=True, timeout=60,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_good_adapter_passes(self):
        result = self._run('good')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("9/9 passed", result.stdout)

    def test_state_that_never_changes_is_caught(self):
        result = self._run('frozen-state')
        self.assertEqual(result.returncode, 1)
        self.assertIn('state follows the turn', result.stdout)
        self.assertIn('state unchanged', result.stdout)

    def test_unscoped_state_is_caught(self):
        result = self._run('shared-state')
        self.assertEqual(result.returncode, 1)
        self.assertIn('state is per session', result.stdout)

    def test_global_reset_is_caught(self):
        result = self._run('global-reset')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('reset leaves other sessions alone', result.stdout)

    def test_adapter_without_state_change_is_caught(self):
        result = self._run('no-state')
        self.assertEqual(result.returncode, 1)
        self.assertIn('state follows the turn', result.stdout)

    def test_unreachable_adapter_fails(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--adapter-url', 'http://127.0.0.1:1'],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('health', result.stdout)


if __name__ == '__main__':
    unittest.main()
