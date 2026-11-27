import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from detect_state_source import detect  # noqa: E402

SCAFFOLD = SCRIPTS / 'scaffold_adapter.py'
DETECT = SCRIPTS / 'detect_state_source.py'


def scan(files):
    return {'root': 'project', 'files': files}


class DetectStateSourceTest(unittest.TestCase):
    def test_mysql_dsn_is_detected(self):
        result = detect(scan([
            {'path': 'internal/db.go', 'excerpt': 'dsn := "root:root@tcp(127.0.0.1:3307)/app?charset=utf8mb4"'},
        ]))
        self.assertEqual(result['primary_engine'], 'mysql')
        self.assertTrue(result['storage_engines']['mysql'])

    def test_sqlite_is_detected(self):
        result = detect(scan([
            {'path': 'app/db.py', 'excerpt': 'conn = sqlite3.connect("data/app.db")'},
        ]))
        self.assertEqual(result['primary_engine'], 'sqlite')

    def test_postgres_is_detected(self):
        result = detect(scan([
            {'path': 'app/db.py', 'excerpt': 'url = "postgresql://user:pw@localhost/app"'},
        ]))
        self.assertEqual(result['primary_engine'], 'postgres')

    def test_state_endpoint_is_detected(self):
        result = detect(scan([
            {'path': 'app/routes.py', 'excerpt': '@app.get("/v1/status")'},
        ]))
        self.assertEqual(result['primary_engine'], 'http')
        self.assertIn('/v1/status', result['state_endpoints'])

    def test_tables_are_reported(self):
        result = detect(scan([
            {'path': 'scripts/schema.sql', 'excerpt': 'CREATE TABLE IF NOT EXISTS agent_audit_log ('},
        ]))
        self.assertIn('agent_audit_log', [entry['name'] for entry in result['tables']])

    def test_nothing_detected_says_so(self):
        result = detect(scan([{'path': 'README.md', 'excerpt': 'hello world'}]))
        self.assertEqual(result['primary_engine'], '')
        self.assertTrue(any('No storage engine' in note for note in result['notes']))

    def test_no_assumed_engine(self):
        # A scan with only Redis must not claim sqlite/mysql.
        result = detect(scan([{'path': 'app/cache.go', 'excerpt': 'redis.NewClient(&redis.Options{})'}]))
        self.assertEqual(result['primary_engine'], '')
        self.assertIn('redis', result['storage_engines'])

    def test_cli_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan_path = Path(tmp) / 'scan.json'
            out_path = Path(tmp) / 'state.json'
            scan_path.write_text(json.dumps(scan([
                {'path': 'a.py', 'excerpt': 'sqlite3.connect("x.db")'},
            ])), encoding='utf-8')
            result = subprocess.run(
                [sys.executable, str(DETECT), '--scan', str(scan_path), '--json-out', str(out_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(out_path.read_text(encoding='utf-8'))
        self.assertEqual(report['primary_engine'], 'sqlite')


class ScaffoldAdapterTest(unittest.TestCase):
    def _scaffold(self, tmp, engine='mysql'):
        state_path = Path(tmp) / 'state.json'
        state_path.write_text(json.dumps({
            'primary_engine': engine,
            'tables': [{'name': 'agent_audit_log', 'source': 'a.sql:1'}],
            'state_endpoints': [],
        }), encoding='utf-8')
        out = Path(tmp) / 'adapter'
        result = subprocess.run(
            [
                sys.executable, str(SCAFFOLD),
                '--state', str(state_path), '--out', str(out),
                '--project', 'demo', '--agent-url', 'http://127.0.0.1:9105',
                '--chat-path', '/chat',
            ],
            capture_output=True, text=True,
        )
        return result, out

    def test_scaffold_generates_compilable_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, out = self._scaffold(tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            adapter = out / 'eval_adapter.py'
            self.assertTrue(adapter.is_file())
            compiled = subprocess.run(
                [sys.executable, '-m', 'py_compile', str(adapter)],
                capture_output=True, text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_scaffold_has_no_storage_engine_assumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, out = self._scaffold(tmp, engine='postgres')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source = (out / 'eval_adapter.py').read_text(encoding='utf-8')
        for module in ('pymysql', 'psycopg', 'sqlite3', 'redis'):
            self.assertNotIn(f'import {module}', source)
        self.assertIn('NotImplementedError', source)

    def test_scaffold_reports_detected_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, out = self._scaffold(tmp, engine='postgres')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source = (out / 'eval_adapter.py').read_text(encoding='utf-8')
            readme = (out / 'README.md').read_text(encoding='utf-8')
        self.assertIn('postgres', source)
        self.assertIn('agent_audit_log', source)
        self.assertIn('postgres', readme)

    def test_scaffold_refuses_non_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'adapter'
            out.mkdir()
            (out / 'existing.txt').write_text('x', encoding='utf-8')
            state_path = Path(tmp) / 'state.json'
            state_path.write_text(json.dumps({'primary_engine': 'sqlite'}), encoding='utf-8')
            result = subprocess.run(
                [sys.executable, str(SCAFFOLD), '--state', str(state_path), '--out', str(out)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('not empty', result.stdout)

    def test_missing_state_report_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(SCAFFOLD), '--state', str(Path(tmp) / 'missing.json'),
                 '--out', str(Path(tmp) / 'adapter')],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('not found', result.stdout)

    def test_unknown_engine_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.json'
            state_path.write_text(json.dumps({'primary_engine': ''}), encoding='utf-8')
            out = Path(tmp) / 'adapter'
            result = subprocess.run(
                [sys.executable, str(SCAFFOLD), '--state', str(state_path), '--out', str(out)],
                capture_output=True, text=True,
            )
            created = out.exists()
        self.assertEqual(result.returncode, 2)
        self.assertIn('did not identify a state source', result.stdout)
        self.assertFalse(created, 'no adapter directory should be created')

    def test_unknown_engine_allowed_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.json'
            state_path.write_text(json.dumps({'primary_engine': ''}), encoding='utf-8')
            out = Path(tmp) / 'adapter'
            result = subprocess.run(
                [sys.executable, str(SCAFFOLD), '--state', str(state_path), '--out', str(out),
                 '--allow-unknown-engine'],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((out / 'eval_adapter.py').is_file())


def _load_adapter(tmp, *extra):
    """Scaffold into tmp and import the generated module."""
    state_path = Path(tmp) / 'state.json'
    state_path.write_text(json.dumps({'primary_engine': 'sqlite'}), encoding='utf-8')
    out = Path(tmp) / 'adapter'
    result = subprocess.run(
        [sys.executable, str(SCAFFOLD), '--state', str(state_path), '--out', str(out), *extra],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    spec = importlib.util.spec_from_file_location(f'adapter_{uuid.uuid4().hex}', out / 'eval_adapter.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StepMappingTest(unittest.TestCase):
    """A step list that is empty by accident makes every `process` criterion fiction."""

    def test_node_style_steps_are_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(tmp, '--response-steps-field', 'events')
        steps = module._steps({'events': [
            {'node': 'route_request', 'status': 'completed', 'duration_ms': 0.2},
            {'node': 'lookup_order', 'status': 'completed', 'duration_ms': 21.8},
        ]})
        self.assertEqual([step['name'] for step in steps], ['route_request', 'lookup_order'])
        self.assertEqual(steps[0]['result'], {'status': 'completed'})
        self.assertEqual(steps[0]['elapsed_ms'], 0.2)

    def test_tool_style_steps_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(tmp, '--response-steps-field', 'trace')
        steps = module._steps({'trace': [
            {'tool': 'get_balance', 'args': {'user': 'a'}, 'result': {'balance': 1}},
        ]})
        self.assertEqual(steps[0]['name'], 'get_balance')
        self.assertEqual(steps[0]['arguments'], {'user': 'a'})

    def test_unrecognised_step_shape_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(tmp, '--response-steps-field', 'events')
        with self.assertRaises(RuntimeError):
            module._steps({'events': [{'unknown_key': 'x'}]})

    def test_nested_envelope_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(
                tmp, '--response-root-field', 'state',
                '--response-reply-field', 'answer',
                '--response-steps-field', 'events',
            )
        payload = module._payload({'thread_id': 't', 'state': {
            'answer': 'done', 'events': [{'node': 'lookup_order', 'status': 'completed'}],
        }})
        self.assertEqual(payload['answer'], 'done')
        self.assertEqual(module._steps(payload)[0]['name'], 'lookup_order')

    def test_missing_envelope_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(tmp, '--response-root-field', 'state')
        with self.assertRaises(RuntimeError):
            module._payload({'answer': 'done'})

    def test_no_envelope_reads_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_adapter(tmp, '--response-reply-field', 'reply')
        self.assertEqual(module._payload({'reply': 'hi'})['reply'], 'hi')


if __name__ == '__main__':
    unittest.main()
