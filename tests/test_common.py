import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from _common import is_allowed_http, probe_agent  # noqa: E402


class IsAllowedHttpTest(unittest.TestCase):
    def test_plain_loopback_urls_pass(self):
        for url in (
            'http://127.0.0.1',
            'http://127.0.0.1:9201',
            'http://localhost',
            'http://localhost:9010/v1/eval/state',
        ):
            self.assertTrue(is_allowed_http(url), url)

    def test_userinfo_cannot_smuggle_a_remote_host(self):
        for url in (
            'http://localhost:80@evil.com/state',
            'http://127.0.0.1@evil.com/state',
            'http://user:pass@127.0.0.1/state',
        ):
            self.assertFalse(is_allowed_http(url), url)

    def test_lookalike_hosts_fail(self):
        for url in (
            'http://127.0.0.1.evil.com/x',
            'http://127.0.0.10/x',
            'http://localhost.evil.com/x',
            'https://127.0.0.1:9201',
            'http://[::1]:9201',
            'http://127.0.0.1:99999',
        ):
            self.assertFalse(is_allowed_http(url), url)

    def test_explicit_remote_prefix_is_opt_in(self):
        prefix = ('https://state.example',)
        self.assertTrue(is_allowed_http('https://state.example/v1/state', prefix))
        self.assertFalse(is_allowed_http('https://state.example.evil/v1/state', prefix))
        self.assertFalse(is_allowed_http('https://state.example/api/../admin', ('https://state.example/api',)))


class ProbeAgentTest(unittest.TestCase):
    def test_invalid_port_reports_offline(self):
        for url in ('http://127.0.0.1:99999', 'http://127.0.0.1:abc'):
            result = probe_agent(url, timeout=0.2)
            self.assertFalse(result['online'], url)
            self.assertTrue(result['error'], url)

    def test_scheme_less_url_is_rejected(self):
        self.assertFalse(probe_agent('127.0.0.1:9201', timeout=0.2)['online'])

    def test_live_health_endpoint_is_online(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('content-length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            result = probe_agent(f'http://127.0.0.1:{port}', timeout=2)
            self.assertTrue(result['online'])
            self.assertEqual(result['mode'], 'health')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
