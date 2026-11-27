import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from discover_agent import discover  # noqa: E402

SCAN = {
    'root': 'project',
    'files': [
        {
            'path': 'cmd/server/main.go',
            'excerpt': 'mux.HandleFunc("/v1/eval/chat", chat)\n'
            'mux.HandleFunc("/v1/eval/reset", reset)\n'
            'addr := ":9201"\n',
        },
        {
            'path': 'docs/run.md',
            'excerpt': 'Start it on http://127.0.0.1:9201 and check /health.\n',
        },
        {
            'path': 'app/main.py',
            'excerpt': '@app.post("/chat")\ndef chat():\n    pass\n',
        },
    ],
}


class DiscoverAgentTest(unittest.TestCase):
    def test_extracts_routes_ports_and_envelope(self):
        with patch(
            'discover_agent.probe_agent',
            return_value={
                'online': True,
                'url': 'http://127.0.0.1:9201',
                'mode': 'health',
                'health': {'status': 200},
                'error': None,
            },
        ):
            result = discover(SCAN, ['http://127.0.0.1:9201'])
        paths = {route['path'] for route in result['routes']}
        self.assertIn('/v1/eval/chat', paths)
        self.assertIn('/v1/eval/reset', paths)
        self.assertIn('/chat', paths)
        self.assertIn(9201, result['ports'])
        self.assertTrue(result['eval_envelope']['chat'])
        self.assertTrue(result['eval_envelope']['reset'])
        self.assertEqual(result['recommended'], 'http://127.0.0.1:9201')

    def test_reports_no_reachable_agent(self):
        with patch(
            'discover_agent.probe_agent',
            return_value={
                'online': False,
                'url': 'http://127.0.0.1:9201',
                'mode': 'tcp',
                'health': None,
                'error': 'connection refused',
            },
        ):
            result = discover(SCAN)
        self.assertFalse(result['online'])
        self.assertEqual(result['recommended'], '')

    def test_falls_back_to_common_ports(self):
        def fake_probe(url, **_kwargs):
            return {
                'online': False,
                'url': url,
                'mode': 'tcp',
                'health': None,
                'error': 'connection refused',
            }

        with patch('discover_agent.probe_agent', side_effect=fake_probe):
            result = discover({'files': []})
        urls = [probe['url'] for probe in result['candidates']]
        self.assertIn('http://127.0.0.1:8000', urls)
        self.assertIn('http://127.0.0.1:9201', urls)

    def test_health_candidate_outranks_tcp_only_ports(self):
        scan = {
            'files': [
                {
                    'path': 'internal/agent/evalcore.go',
                    'excerpt': 'mysql 127.0.0.1:3307\nredis 127.0.0.1:6381\n',
                }
            ]
        }

        def fake_probe(url, **_kwargs):
            if url.endswith(':9301'):
                return {
                    'online': True,
                    'url': url,
                    'mode': 'health',
                    'health': {'status': 200},
                    'error': None,
                }
            return {
                'online': True,
                'url': url,
                'mode': 'tcp',
                'health': None,
                'error': None,
            }

        with patch('discover_agent.probe_agent', side_effect=fake_probe):
            result = discover(scan, ['http://127.0.0.1:9301'])
        self.assertEqual(result['recommended'], 'http://127.0.0.1:9301')
        self.assertEqual(result['non_http_ports'], {'3307': 'database', '6381': 'redis'})
        self.assertNotIn(3307, result['ports'])

    def test_scheme_less_base_url_is_normalised(self):
        with patch('discover_agent.probe_agent', side_effect=lambda url, timeout=2.0: {
            'online': False, 'url': url, 'mode': 'tcp', 'health': None, 'error': 'refused'
        }):
            result = discover({'files': []}, ['127.0.0.1:9201', 'localhost:8000'])
        urls = [probe['url'] for probe in result['candidates']]
        self.assertIn('http://127.0.0.1:9201', urls)
        self.assertIn('http://localhost:8000', urls)

    def test_invalid_port_in_excerpt_does_not_crash(self):
        scan = {'files': [{'path': 'a.go', 'excerpt': 'ListenAndServe("127.0.0.1:99999", nil)'}]}
        result = discover(scan, [], timeout=0.1)
        self.assertIn('candidates', result)
        self.assertEqual(result['recommended'], '')

    def test_health_path_in_backticks_is_found(self):
        scan = {'files': [{'path': 'deploy.md', 'excerpt': 'serves `127.0.0.1:9105/healthz`'}]}
        result = discover(scan, [], timeout=0.05)
        self.assertEqual(result['health_paths'], ['/healthz'])

    def test_bare_host_port_is_an_explicit_candidate(self):
        scan = {'files': [{'path': 'deploy.md', 'excerpt': 'agent listens on 127.0.0.1:9105'}]}
        result = discover(scan, [], timeout=0.05)
        self.assertIn('http://127.0.0.1:9105', result['explicit_urls'])
        self.assertIn(9105, result['ports'])

    def test_money_amount_is_not_a_port(self):
        scan = {
            'files': [
                {
                    'path': 'eval/cases/security.json',
                    'excerpt': '"args": "{\\"amount_money\\":8888}"',
                }
            ]
        }
        result = discover(scan, [], timeout=0.05)
        self.assertEqual(result['ports'], [])


class SessionContinuityTest(unittest.TestCase):
    """A confirm/cancel task cannot pass against a stateless-per-request agent."""

    def _serve(self, handler_cls):
        import threading
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def test_agent_that_mints_a_thread_per_request_is_reported(self):
        import json
        import uuid
        from http.server import BaseHTTPRequestHandler
        from discover_agent import probe_session_continuity

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                self.rfile.read(length)
                body = json.dumps({'answer': 'ok', 'thread_id': uuid.uuid4().hex}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        result = probe_session_continuity(self._serve(Handler), chat_path='/chat',
                                          request_field='question', reply_field='answer')
        self.assertEqual(result['continuity'], 'server-mints-per-request')
        self.assertFalse(result['multi_turn_possible'])
        self.assertEqual(result['session_field'], 'thread_id')
        self.assertIn('cannot reach the previous turn', result['note'])

    def test_agent_that_remembers_a_nonce_is_multi_turn_capable(self):
        import json
        import re
        from http.server import BaseHTTPRequestHandler
        from discover_agent import probe_session_continuity

        remembered: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                question = str(body.get('question') or '')
                match = re.search(r'EVALGEN[0-9A-F]{8}', question)
                if match:
                    remembered['nonce'] = match.group(0)
                    reply = '已记住'
                elif 'nonce' in remembered:
                    reply = f'你让我记住的是 {remembered["nonce"]}'
                else:
                    reply = '不知道'
                payload = {'answer': reply}
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        result = probe_session_continuity(self._serve(Handler), chat_path='/chat')
        self.assertIn(result['continuity'], ('server-session', 'caller-session'))
        self.assertTrue(result['multi_turn_possible'])
        self.assertTrue(result['remembered_nonce'])

    def test_agent_that_echoes_the_session_but_forgets_is_not_multi_turn(self):
        """Echoing the id is not continuation: the probe tests recall, not field values."""
        import json
        from http.server import BaseHTTPRequestHandler
        from discover_agent import probe_session_continuity

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                payload = {'reply': '已记住', 'session_id': body.get('session_id', 'session-1')}
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        result = probe_session_continuity(self._serve(Handler), chat_path='/chat',
                                          token='session-1')
        self.assertFalse(result['multi_turn_possible'])
        self.assertFalse(result['remembered_nonce'])

    def test_auth_required_is_distinguished_from_offline(self):
        from http.server import BaseHTTPRequestHandler
        from discover_agent import probe_session_continuity

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(401)
                self.send_header('content-length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        result = probe_session_continuity(self._serve(Handler), chat_path='/chat')
        self.assertEqual(result['continuity'], 'unknown')
        self.assertTrue(result['auth_required'])
        self.assertIn('requires authentication', result['error'])


if __name__ == '__main__':
    unittest.main()
