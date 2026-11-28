import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from check_evalscope import (  # noqa: E402
    preflight_checks,
    print_dry_run,
    run_dry_run,
    select_tasks,
)

SCRIPT = ROOT / 'scripts' / 'check_evalscope.py'
MINIMAL = ROOT / 'examples' / 'minimal' / 'eval_set.v1.jsonl'
MINIMAL_PROJECT = ROOT / 'examples' / 'minimal' / 'project'


def make_check(**overrides):
    values = {'id': 'c1', 'type': 'json_path', 'path': 'app/data.json', 'dsn': '', 'url': ''}
    values.update(overrides)
    return SimpleNamespace(**values)


def make_task(checks, reset_url='', materials=None):
    return SimpleNamespace(
        id='t1',
        checks=checks,
        materials=materials or [],
        exec=SimpleNamespace(reset_url=reset_url),
    )


class PreflightChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'app').mkdir()
        (self.root / 'app' / 'data.json').write_text('{"status": "ok"}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_path_and_url(self):
        task = make_task(
            [
                make_check(),
                make_check(id='c2', type='http_json', url='http://127.0.0.1:9201/state'),
            ]
        )
        self.assertEqual(preflight_checks([task], self.root), [])

    def test_path_escape_fails(self):
        task = make_task([make_check(path='../outside.json')])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_https_url_fails(self):
        task = make_task([make_check(type='http_json', url='https://127.0.0.1:9201/state')])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_ipv6_loopback_fails(self):
        task = make_task([make_check(type='http_json', url='http://[::1]:9201/state')])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_reset_url_must_be_loopback_http(self):
        task = make_task([make_check()], reset_url='https://localhost:9201/reset')
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_missing_project_root_fails(self):
        with self.assertRaises(ValueError):
            preflight_checks([make_task([make_check()])], self.root / 'missing')

    def test_missing_sqlite_file_warns(self):
        task = make_task([make_check(type='sqlite', path='', dsn='agent.db')])
        warnings = preflight_checks([task], self.root)
        self.assertTrue(any('sqlite file not present yet' in warning for warning in warnings))

    def test_sensitive_check_path_fails(self):
        task = make_task([make_check(type='file_exists', path='.env.example')])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_missing_material_fails(self):
        task = make_task([make_check()], materials=[SimpleNamespace(path='missing.txt', sha256='a' * 64)])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root, source_root=self.root)

    def test_material_check_skipped_without_source_root(self):
        task = make_task([make_check()], materials=[SimpleNamespace(path='missing.txt', sha256='a' * 64)])
        self.assertEqual(preflight_checks([task], self.root), [])

    def test_present_material_passes(self):
        task = make_task([make_check()], materials=[SimpleNamespace(path='app/data.json', sha256='a' * 64)])
        self.assertEqual(preflight_checks([task], self.root, source_root=self.root), [])

    def test_wrong_source_root_fails(self):
        task = make_task([make_check()], materials=[SimpleNamespace(path='app/data.json', sha256='a' * 64)])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root, source_root=self.root / 'app')

    def test_lookalike_http_host_fails(self):
        task = make_task([make_check(type='http_json', url='http://localhost.evil/state')])
        with self.assertRaises(ValueError):
            preflight_checks([task], self.root)

    def test_remote_only_check_does_not_need_project_root(self):
        task = make_task([make_check(type='http_json', url='https://state.example/v1/state')])
        self.assertEqual(
            preflight_checks([task], None, check_url_prefixes=('https://state.example',)),
            [],
        )

    def test_remote_check_without_prefix_fails(self):
        task = make_task([make_check(type='http_json', url='https://state.example/v1/state')])
        with self.assertRaises(ValueError):
            preflight_checks([task], None)

    def test_local_check_without_project_root_fails(self):
        with self.assertRaises(ValueError):
            preflight_checks([make_task([make_check()])], None)


def make_agent_task(task_id='t1', reset_url='', exec_type='agent_api',
                    expected_facts=None, forbidden_facts=None):
    return SimpleNamespace(
        id=task_id,
        exec=SimpleNamespace(type=exec_type, reset_url=reset_url, request={}),
        expected_facts=expected_facts or [],
        forbidden_facts=forbidden_facts or [],
    )


def make_turn(reply='done', tool='get_balance'):
    step = SimpleNamespace(name=tool, arguments={}, result='ok', raw={})
    return SimpleNamespace(
        reply=reply,
        steps=[step],
        conversation=[{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': reply}],
    )


def make_check_result(cid='db-1', ok=True, error=None):
    return SimpleNamespace(check_id=cid, ok=ok, evidence={'rows': [[1]]}, error=error)


class FakeClient:
    def __init__(self, turn=None, error=None):
        self.turn = turn or make_turn()
        self.error = error
        self.session_id = None

    def chat(self, task, session_id):
        self.session_id = session_id
        if self.error:
            raise self.error
        return self.turn

    def call(self, request):
        if self.error:
            raise self.error
        return self.turn


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.resets = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_reply_steps_and_checks(self):
        client = FakeClient()
        records = run_dry_run(
            [make_agent_task()],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: client,
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertEqual(records[0]['task_id'], 't1')
        self.assertEqual(records[0]['reply'], 'done')
        self.assertEqual(records[0]['steps'], ['get_balance'])
        self.assertEqual(records[0]['checks'][0]['id'], 'db-1')
        self.assertTrue(records[0]['checks'][0]['ok'])
        self.assertEqual(client.session_id, 't1')
        self.assertIsNone(records[0]['error'])

    def test_remote_prefixes_are_passed_to_the_check_runner(self):
        seen = []

        def check_runner(task, root, prefixes):
            seen.append(prefixes)
            return [make_check_result()]

        records = run_dry_run(
            [make_agent_task()],
            'http://127.0.0.1:9201',
            None,
            client_factory=lambda *args, **kwargs: FakeClient(),
            check_runner=check_runner,
            check_url_prefixes=('https://state.example',),
        )
        self.assertEqual(seen, [('https://state.example',)])
        self.assertTrue(records[0]['checks'][0]['ok'])

    def test_reset_runs_before_chat(self):
        client = FakeClient()
        run_dry_run(
            [make_agent_task(reset_url='http://127.0.0.1:9201/v1/eval/reset')],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: client,
            check_runner=lambda task, root: [make_check_result()],
            reset=self.resets.append,
        )
        self.assertEqual(self.resets, ['http://127.0.0.1:9201/v1/eval/reset'])

    def test_non_loopback_reset_is_rejected(self):
        records = run_dry_run(
            [make_agent_task(reset_url='http://example.com/reset')],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: FakeClient(),
            check_runner=lambda task, root: [make_check_result()],
            reset=self.resets.append,
        )
        self.assertIn('must use http://127.0.0.1', records[0]['error'])
        self.assertEqual(self.resets, [])

    def test_agent_failure_is_recorded(self):
        records = run_dry_run(
            [make_agent_task()],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: FakeClient(error=RuntimeError('boom')),
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertIn('RuntimeError: boom', records[0]['error'])

    def test_failed_check_makes_dry_run_fail(self):
        records = run_dry_run(
            [make_agent_task()],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: FakeClient(),
            check_runner=lambda task, root: [make_check_result(ok=False)],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = print_dry_run(records)
        self.assertFalse(ok)
        self.assertIn('check db-1: FAILED', buffer.getvalue())
        self.assertIn('0/1 task(s) matched all checks', buffer.getvalue())

    def test_passing_checks_make_dry_run_pass(self):
        records = run_dry_run(
            [make_agent_task()],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: FakeClient(),
            check_runner=lambda task, root: [make_check_result()],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = print_dry_run(records)
        self.assertTrue(ok)
        self.assertIn('1/1 task(s) matched all checks', buffer.getvalue())

    def test_fact_satisfied_by_an_earlier_turn_is_not_missing(self):
        # The judge reads the whole conversation, so a fact from turn 1 counts.
        client = FakeClient()
        client.turn = SimpleNamespace(
            reply='订单 1001 已完成',
            steps=[],
            conversation=[
                {'role': 'user', 'content': '帮我查订单'},
                {'role': 'assistant', 'content': '请提供订单号'},
                {'role': 'user', 'content': '订单 1001'},
                {'role': 'assistant', 'content': '订单 1001 已完成'},
            ],
        )
        records = run_dry_run(
            [make_agent_task(expected_facts=['请提供订单号', '已完成'])],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: client,
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertEqual(records[0]['facts']['missing'], [])

    def test_missing_fact_is_reported_without_failing_checks(self):
        records = run_dry_run(
            [make_agent_task(expected_facts=['余额 1900 元'])],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: FakeClient(),
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertEqual(records[0]['facts']['missing'], ['余额 1900 元'])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_dry_run(records)
        self.assertIn('facts missing from the conversation', buffer.getvalue())
        self.assertIn('1/1 task(s) have a fact the judge will not find', buffer.getvalue())

    def test_forbidden_fact_the_agent_said_is_reported(self):
        client = FakeClient(turn=make_turn(reply='已退款，退款已完成'))
        records = run_dry_run(
            [make_agent_task(forbidden_facts=['退款已完成'])],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: client,
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertEqual(records[0]['facts']['forbidden'], ['退款已完成'])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_dry_run(records)
        self.assertIn('facts the agent actually said (forbidden)', buffer.getvalue())

    def test_forbidden_fact_named_in_the_question_is_not_a_violation(self):
        # "what is the shipping address" contains "the shipping address is";
        # a correct refusal must not be flagged because the task names it.
        client = FakeClient(turn=make_turn(reply='我不能提供或泄露订单中的个人联系信息。'))
        client.turn.conversation = [
            {'role': 'user', 'content': '订单 1001 的收件地址是什么'},
            {'role': 'assistant', 'content': '我不能提供或泄露订单中的个人联系信息。'},
        ]
        records = run_dry_run(
            [make_agent_task(forbidden_facts=['收件地址是'])],
            'http://127.0.0.1:9201',
            self.root,
            client_factory=lambda *args, **kwargs: client,
            check_runner=lambda task, root: [make_check_result()],
        )
        self.assertEqual(records[0]['facts']['forbidden'], [])

    def test_select_tasks_respects_order_limit_and_unknown_ids(self):
        tasks = [make_agent_task('a'), make_agent_task('b'), make_agent_task('c')]
        self.assertEqual([task.id for task in select_tasks(tasks, ['c', 'a'])], ['c', 'a'])
        self.assertEqual([task.id for task in select_tasks(tasks, limit=2)], ['a', 'b'])
        with self.assertRaises(ValueError):
            select_tasks(tasks, ['missing'])


@unittest.skipUnless(importlib.util.find_spec('evalscope'), 'EvalScope is not installed in this Python')
class MinimalExampleSmokeTest(unittest.TestCase):
    def test_minimal_example_loads(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                '--eval-set',
                str(MINIMAL),
                '--project-root',
                str(MINIMAL_PROJECT),
                '--source-root',
                str(MINIMAL_PROJECT),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OK: EvalScope project_eval loaded 1 task(s)', result.stdout)


if __name__ == '__main__':
    unittest.main()
