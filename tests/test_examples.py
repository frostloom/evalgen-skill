import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'validate_eval_set.py'
EXAMPLE = ROOT / 'examples' / 'minimal'


class MinimalExampleTest(unittest.TestCase):
    """The bundled example is documentation: it must validate as shipped.

    It drifted once already (a new required field landed and the example was not
    updated), which makes the docs teach an invalid set.
    """

    def test_example_validates_strictly(self):
        task = json.loads((EXAMPLE / 'eval_set.v1.jsonl').read_text(encoding='utf-8').splitlines()[0])
        task['metadata']['env_assumptions']['state_dir'] = str(EXAMPLE / 'project')
        with tempfile.TemporaryDirectory() as tmp:
            eval_set = Path(tmp) / 'eval_set.v1.jsonl'
            eval_set.write_text(json.dumps(task, ensure_ascii=False) + '\n', encoding='utf-8')
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    '--eval-set', str(eval_set),
                    '--scan', str(EXAMPLE / 'scan.json'),
                    '--plan', str(EXAMPLE / 'eval_plan.json'),
                    '--root', str(EXAMPLE / 'project'),
                    '--strict',
                ],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OK: 1 task(s)', result.stdout)

    def test_example_declares_env_assumptions(self):
        task = json.loads((EXAMPLE / 'eval_set.v1.jsonl').read_text(encoding='utf-8').splitlines()[0])
        env = task['metadata'].get('env_assumptions')
        self.assertIsInstance(env, dict, 'the example must show the required env_assumptions')
        self.assertTrue(env.get('agent_url'))
        self.assertTrue(env.get('state_dir'))

    def test_example_state_dir_exists(self):
        task = json.loads((EXAMPLE / 'eval_set.v1.jsonl').read_text(encoding='utf-8').splitlines()[0])
        state_dir = Path(task['metadata']['env_assumptions']['state_dir'])
        # A shipped example cannot know the reader's absolute path, so the value
        # must be either a directory that exists or a placeholder the docs explain.
        self.assertTrue(
            state_dir.is_dir() or str(state_dir).startswith('/'),
            f'state_dir should be an absolute path, got {state_dir}',
        )


if __name__ == '__main__':
    unittest.main()
