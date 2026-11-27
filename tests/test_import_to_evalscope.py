import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'import_to_evalscope.py'


class ImportToEvalScopeTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_rejects_path_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'root'
            root.mkdir()
            outside = base / 'outside.jsonl'
            outside.write_text('{}\n', encoding='utf-8')
            result = self._run(
                '--eval-set',
                str(outside),
                '--eval-set-root',
                str(root),
                '--service-url',
                'http://127.0.0.1:1',
                '--timeout',
                '0.5',
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('outside the eval-set root', result.stdout)

    def test_reports_unreachable_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'root'
            root.mkdir()
            eval_set = root / 'eval_set.v1.jsonl'
            eval_set.write_text('{}\n', encoding='utf-8')
            result = self._run(
                '--eval-set',
                str(eval_set),
                '--eval-set-root',
                str(root),
                '--service-url',
                'http://127.0.0.1:1',
                '--timeout',
                '0.5',
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('not reachable', result.stdout)
        self.assertIn('--eval-set-root', result.stdout)

    def test_non_json_and_non_object_responses_fail_cleanly(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        bodies = {
            'html': b'<html>proxy</html>',
            'list': json.dumps([1, 2, 3]).encode(),
        }
        for name, body in bodies.items():
            with self.subTest(name):
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header('content-length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def do_POST(self):
                        self.do_GET()

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    port = server.server_address[1]
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        eval_set = root / 'eval_set.v1.jsonl'
                        eval_set.write_text('{}\n', encoding='utf-8')
                        result = self._run(
                            '--eval-set',
                            str(eval_set),
                            '--eval-set-root',
                            str(root),
                            '--service-url',
                            f'http://127.0.0.1:{port}',
                            '--timeout',
                            '2',
                        )
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_state_dir_outside_service_roots_fails_before_import(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        calls: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                if self.path.startswith('/api/v1/project-eval/state-roots'):
                    body = json.dumps({'roots': ['/allowed/state'], 'eval_set_roots': ['/sets']}).encode()
                else:
                    body = json.dumps({'status': 'ok'}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                calls.append(self.path)
                self.send_response(200)
                self.send_header('content-length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                eval_set = root / 'eval_set.v1.jsonl'
                eval_set.write_text('{}\n', encoding='utf-8')
                bad_state = root / 'not-allowed'
                bad_state.mkdir()
                result = self._run(
                    '--eval-set', str(eval_set),
                    '--eval-set-root', str(root),
                    '--service-url', f'http://127.0.0.1:{port}',
                    '--state-dir', str(bad_state),
                    '--timeout', '2',
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('outside the service state roots', result.stdout)
        self.assertIn('--state-root', result.stdout)
        self.assertFalse(any(path.endswith('/import') for path in calls))

    def test_state_dir_comes_from_env_assumptions(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        posted: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'roots': [str(Path(tempfile.gettempdir()))], 'eval_set_roots': []}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                posted.append(json.loads(self.rfile.read(length) or b'{}'))
                body = json.dumps({'id': 'abc', 'task_count': 1, 'name': 'x'}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                eval_set = root / 'eval_set.v1.jsonl'
                task = {
                    'id': 't1',
                    'metadata': {
                        'env_assumptions': {
                            'agent_url': 'http://127.0.0.1:9115',
                            'state_dir': tempfile.gettempdir(),
                        }
                    },
                }
                eval_set.write_text(json.dumps(task) + '\n', encoding='utf-8')
                result = self._run(
                    '--eval-set', str(eval_set),
                    '--eval-set-root', str(root),
                    '--service-url', f'http://127.0.0.1:{port}',
                    '--timeout', '2',
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(posted, 'import was not called')
        self.assertEqual(posted[-1].get('agent_url'), 'http://127.0.0.1:9115')
        self.assertEqual(posted[-1].get('project_root'), os.path.realpath(tempfile.gettempdir()))

    def _service(self, roots):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        posted: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/api/v1/project-eval/state-roots'):
                    payload = {'roots': roots, 'eval_set_roots': []}
                else:
                    payload = {'status': 'ok'}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                posted.append(json.loads(self.rfile.read(length) or b'{}'))
                body = json.dumps({'id': 'abc', 'task_count': 1, 'name': 'x'}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, posted

    def test_conflicting_env_across_tasks_is_rejected(self):
        server, posted = self._service([tempfile.gettempdir()])
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                eval_set = root / 'eval_set.v1.jsonl'
                tasks = [
                    {'id': 't1', 'metadata': {'env_assumptions': {
                        'agent_url': 'http://127.0.0.1:9115', 'state_dir': tempfile.gettempdir()}}},
                    {'id': 't2', 'metadata': {'env_assumptions': {
                        'agent_url': 'http://127.0.0.1:9999', 'state_dir': tempfile.gettempdir()}}},
                ]
                eval_set.write_text('\n'.join(json.dumps(t) for t in tasks) + '\n', encoding='utf-8')
                result = self._run(
                    '--eval-set', str(eval_set),
                    '--eval-set-root', str(root),
                    '--service-url', f'http://127.0.0.1:{port}',
                    '--timeout', '2',
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('inconsistent environment assumptions', result.stdout)
        self.assertIn('agent_url differs', result.stdout)
        self.assertEqual(posted, [], 'import must not run with conflicting env')

    def test_empty_state_roots_fails_closed(self):
        server, posted = self._service([])
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                eval_set = root / 'eval_set.v1.jsonl'
                task = {'id': 't1', 'metadata': {'env_assumptions': {
                    'agent_url': 'http://127.0.0.1:9115', 'state_dir': tempfile.gettempdir()}}}
                eval_set.write_text(json.dumps(task) + '\n', encoding='utf-8')
                result = self._run(
                    '--eval-set', str(eval_set),
                    '--eval-set-root', str(root),
                    '--service-url', f'http://127.0.0.1:{port}',
                    '--timeout', '2',
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('reports no state roots', result.stdout)
        self.assertEqual(posted, [])


if __name__ == '__main__':
    unittest.main()
