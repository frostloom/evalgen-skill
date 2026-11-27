#!/usr/bin/env python3
"""Smoke-test an eval set with EvalScope's own loader and project_eval adapter.

The default mode does not call a model or the business Agent. It verifies that
EvalScope's schema accepts the file, that the project_eval adapter can convert
every task into a Sample, and that check paths/URLs resolve against the project
root. With ``--run`` it executes selected tasks against the Agent, prints the
reply, tool steps, conversation and every check result, and never scores.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from _common import assert_under_root, is_allowed_http, is_sensitive_path, probe_agent


def preflight_checks(
    tasks: Any,
    project_root: str | Path,
    source_root: str | Path | None = None,
) -> list[str]:
    """Resolve check paths and URLs against the project root without running them."""
    root = Path(project_root)
    if not root.is_dir():
        raise ValueError(f'project root is not a directory: {root}')
    root = root.resolve()
    sources = Path(source_root).resolve() if source_root is not None else None
    if sources is not None and not sources.is_dir():
        raise ValueError(f'source root is not a directory: {sources}')
    warnings: list[str] = []
    for task in tasks:
        task_id = getattr(task, 'id', '?')
        for material in getattr(task, 'materials', []):
            if is_sensitive_path(material.path):
                raise ValueError(f'{task_id}: sensitive material path denied: {material.path}')
            if sources is not None:
                assert_under_root(material.path, sources)
                if not (sources / material.path).is_file():
                    raise ValueError(f'{task_id}: material not found under source_root: {material.path}')
        for check in getattr(task, 'checks', []):
            ctype = getattr(check, 'type', '')
            check_id = getattr(check, 'id', '?')
            if ctype in {'file_exists', 'file_content', 'json_path'}:
                if is_sensitive_path(check.path):
                    raise ValueError(f'{task_id}/{check_id}: sensitive path denied: {check.path}')
                assert_under_root(check.path, root)
                if ctype in {'file_content', 'json_path'} and not (root / check.path).is_file():
                    warnings.append(f'{task_id}/{check_id}: path not present yet: {check.path}')
            elif ctype == 'sqlite':
                if is_sensitive_path(check.dsn):
                    raise ValueError(f'{task_id}/{check_id}: sensitive path denied: {check.dsn}')
                assert_under_root(check.dsn, root)
                if not (root / check.dsn).is_file():
                    warnings.append(f'{task_id}/{check_id}: sqlite file not present yet: {check.dsn}')
            elif ctype in {'http', 'http_json'}:
                if not is_allowed_http(check.url):
                    raise ValueError(f'{task_id}/{check_id}: URL must use http://127.0.0.1 or http://localhost')
        exec_spec = getattr(task, 'exec', None)
        reset_url = getattr(exec_spec, 'reset_url', '') if exec_spec is not None else ''
        if reset_url and not is_allowed_http(reset_url):
            raise ValueError(f'{task_id}: reset_url must use http://127.0.0.1 or http://localhost')
    return warnings


def select_tasks(tasks: Any, task_ids: list[str] | None = None, limit: int | None = None) -> list[Any]:
    """Pick the tasks to dry-run, preserving the requested order."""
    tasks = list(tasks)
    if task_ids:
        lookup = {task.id: task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in lookup]
        if missing:
            raise ValueError(f'unknown task id(s): {", ".join(missing)}')
        selected = [lookup[task_id] for task_id in task_ids]
    else:
        selected = tasks
    if limit is not None:
        if limit < 1:
            raise ValueError('limit must be >= 1')
        selected = selected[:limit]
    return selected


def _default_client_factory(exec_spec: Any, base_url: str, model: str, api_key: str) -> Any:
    from evalscope.benchmarks.project_eval.agent_clients import build_client

    return build_client(exec_spec, base_url=base_url, model=model, api_key=api_key)


def _default_check_runner(task: Any, project_root: Path) -> list[Any]:
    from evalscope.project_eval.checks import run_checks

    return run_checks(task, project_root)


def _default_reset(reset_url: str) -> None:
    import requests

    response = requests.post(reset_url, timeout=10)
    response.raise_for_status()


def _normalise(text: str) -> str:
    return ' '.join(str(text).split())


def _fact_coverage(task: Any, record: dict[str, Any]) -> dict[str, list[str]]:
    """Check expected/forbidden facts against what the judge will read.

    The judge sees the whole multi-turn conversation plus the final reply, so a
    fact satisfied by an earlier *assistant* turn counts.

    Forbidden facts are checked against the agent's own words only. A task's
    question often names the thing the agent must not claim ("what is the
    shipping address", "say you transferred me to a human"), so including user
    turns would flag every correct refusal as a violation.
    """
    said: list[str] = [record.get('reply') or '']
    for message in record.get('conversation') or []:
        if str(message.get('role') or '') == 'user':
            continue
        said.append(str(message.get('content') or ''))
    agent_text = _normalise(' '.join(said))
    full_text = _normalise(' '.join(
        [agent_text] + [str(message.get('content') or '') for message in record.get('conversation') or []]
    ))
    missing = [
        fact for fact in (getattr(task, 'expected_facts', None) or [])
        if _normalise(fact) and _normalise(fact) not in full_text
    ]
    present = [
        fact for fact in (getattr(task, 'forbidden_facts', None) or [])
        if _normalise(fact) and _normalise(fact) in agent_text
    ]
    return {'missing': missing, 'forbidden': present}


def run_dry_run(
    tasks: Any,
    agent_url: str,
    project_root: str | Path,
    *,
    client_factory: Any = None,
    check_runner: Any = None,
    reset: Any = None,
    model: str = '',
    api_key: str = '',
) -> list[dict[str, Any]]:
    """Execute tasks once against the agent and report check evidence without scoring."""
    client_factory = client_factory or _default_client_factory
    check_runner = check_runner or _default_check_runner
    reset = reset or _default_reset
    base_url = agent_url.strip().rstrip('/')
    if not base_url:
        raise ValueError('agent URL is required')
    root = Path(project_root)
    records: list[dict[str, Any]] = []
    for task in tasks:
        record: dict[str, Any] = {
            'task_id': task.id,
            'reply': '',
            'steps': [],
            'conversation': [],
            'checks': [],
            'facts': {'missing': [], 'forbidden': []},
            'error': None,
        }
        try:
            exec_spec = task.exec
            reset_url = getattr(exec_spec, 'reset_url', '') or ''
            if reset_url:
                if not is_allowed_http(reset_url):
                    raise ValueError(f'reset_url must use http://127.0.0.1 or http://localhost: {reset_url}')
                reset(reset_url)
            client = client_factory(exec_spec, base_url, model, api_key)
            if getattr(exec_spec, 'type', 'agent_api') == 'http':
                turn = client.call(exec_spec.request)
            else:
                turn = client.chat(task, task.id)
            record['reply'] = getattr(turn, 'reply', '') or ''
            record['steps'] = [getattr(step, 'name', '') for step in getattr(turn, 'steps', []) or []]
            record['conversation'] = list(getattr(turn, 'conversation', []) or [])
            record['facts'] = _fact_coverage(task, record)
            checks = check_runner(task, root)
            record['checks'] = [
                {
                    'id': getattr(check, 'check_id', '?'),
                    'ok': bool(getattr(check, 'ok', False)),
                    'evidence': getattr(check, 'evidence', {}) or {},
                    'error': getattr(check, 'error', None),
                }
                for check in checks
            ]
        except Exception as exc:  # noqa: BLE001 - reported as dry-run evidence
            record['error'] = f'{type(exc).__name__}: {exc}'
        records.append(record)
    return records


def print_dry_run(records: list[dict[str, Any]]) -> bool:
    """Print dry-run evidence; return True when every check passed."""
    for record in records:
        print(f'task {record["task_id"]}')
        if record['error']:
            print(f'  error: {record["error"]}')
            continue
        print(f'  reply: {record["reply"][:200]}')
        if record['steps']:
            print(f'  tools: {", ".join(record["steps"])}')
        for message in record['conversation'][:8]:
            role = str(message.get('role', '?'))
            content = str(message.get('content', '')).replace('\n', ' ')
            print(f'  {role}: {content[:160]}')
        for check in record['checks']:
            status = 'ok' if check['ok'] else 'FAILED'
            print(f'  check {check["id"]}: {status}')
            if check.get('error'):
                print(f'    error: {check["error"]}')
            elif not check['ok']:
                print('    evidence: ' + json.dumps(check.get('evidence'), ensure_ascii=False, default=str))
        facts = record.get('facts') or {}
        if facts.get('missing'):
            print(f'  facts missing from the conversation: {facts["missing"]}')
        if facts.get('forbidden'):
            print(f'  facts the agent actually said (forbidden): {facts["forbidden"]}')
    failed = [record for record in records if record['error'] or any(not check['ok'] for check in record['checks'])]
    print(f'dry-run: {len(records) - len(failed)}/{len(records)} task(s) matched all checks')
    fact_problems = [
        record for record in records
        if (record.get('facts') or {}).get('missing') or (record.get('facts') or {}).get('forbidden')
    ]
    if fact_problems:
        print(
            f'fact check: {len(fact_problems)}/{len(records)} task(s) have a fact the judge will not '
            f'find in the conversation — a correct answer would fail at judge time'
        )
    print(
        'note: the dry-run may have written audit/state rows; restart the Agent with a fresh '
        'state directory before the full EvalScope scoring run'
    )
    return not failed


def _index_warning(evalscope_module: Any) -> str | None:
    index_path = Path(evalscope_module.__file__).parent / 'benchmarks' / '_index.json'
    if not index_path.is_file():
        return 'benchmarks/_index.json is missing; adapter discovery may import every adapter'
    try:
        index = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return f'could not read {index_path}; adapter discovery may be slow'
    entry = index.get('project_eval') if isinstance(index, dict) else None
    if not isinstance(entry, str) or not entry.endswith('.project_eval_adapter'):
        return (
            'project_eval is missing or stale in benchmarks/_index.json; adapter discovery '
            'will import every adapter (run make docs-update-index in EvalScope)'
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--eval-set', required=True)
    parser.add_argument('--project-root', required=True, help='root for check paths (runtime state directory)')
    parser.add_argument('--source-root', help='root for materials/source files when it differs from --project-root')
    parser.add_argument(
        '--run', action='store_true', help='execute selected tasks and print evidence (dry-run; no score)'
    )
    parser.add_argument('--agent-url', default=None, help='business agent base URL (default: EVAL_AGENT_URL)')
    parser.add_argument('--task', action='append', default=[], help='task id to dry-run (repeatable)')
    parser.add_argument(
        '--limit', type=int, default=None, help='max tasks to dry-run (default: 1 when --task is not given)'
    )
    parser.add_argument('--model', default=None, help='agent model name (default: EVAL_AGENT_MODEL)')
    parser.add_argument('--api-key', default=None, help='agent API key (default: EVAL_AGENT_API_KEY)')
    args = parser.parse_args()

    path = Path(args.eval_set)
    if not path.is_file():
        print(f'error: eval set not found: {path}')
        return 1
    project_root = Path(args.project_root).resolve()
    source_root = Path(args.source_root).resolve() if args.source_root else None

    try:
        import evalscope
    except ImportError as exc:
        print(
            f'error: EvalScope is not installed in this Python ({exc}); install the '
            'project_eval-enabled EvalScope build and re-run'
        )
        return 2

    try:
        from evalscope.api.registry import get_benchmark
        from evalscope.config import TaskConfig
    except ImportError as exc:
        print(f'error: EvalScope at {evalscope.__file__} is missing required modules: {exc}')
        return 2

    try:
        from evalscope.project_eval.schema import load_eval_set
    except ImportError as exc:
        name = getattr(exc, 'name', '') or ''
        if name.startswith('evalscope.project_eval'):
            print(
                f'error: EvalScope is installed at {evalscope.__file__} but this build has no '
                f'evalscope.project_eval ({exc}); use the feat/project-eval branch or apply '
                'the project_eval patch, then re-run'
            )
        else:
            print(
                f'error: evalscope.project_eval could not be imported due to a dependency '
                f'error ({exc}); fix the environment and re-run'
            )
        return 2

    try:
        tasks = load_eval_set(path)
    except Exception as exc:  # noqa: BLE001 - schema errors are the diagnostic
        print(f'error: eval set failed EvalScope schema validation: {type(exc).__name__}: {exc}')
        return 1

    try:
        warnings = preflight_checks(tasks, project_root, source_root)
    except Exception as exc:  # noqa: BLE001 - path/URL diagnostics
        print(f'error: project_root/check preflight failed: {exc}')
        return 1

    index_warning = _index_warning(evalscope)
    if index_warning:
        warnings.append(index_warning)
    for warning in warnings:
        print(f'warning: {warning}')

    config = TaskConfig(
        model='evalgen-smoke',
        eval_type='mock_llm',
        datasets=['project_eval'],
        dataset_args={
            'project_eval': {
                'local_path': str(path),
                'project_root': str(project_root),
            }
        },
        eval_batch_size=1,
    )
    try:
        adapter = get_benchmark('project_eval', config)
    except Exception as exc:  # noqa: BLE001 - registry/import diagnostics
        print(f'error: EvalScope could not resolve the project_eval adapter: {type(exc).__name__}: {exc}')
        return 1

    try:
        dataset, _fewshot = adapter.load_from_disk()
        subsets = list(dataset.keys())
        samples = [sample for subset in subsets for sample in dataset[subset]]
        sample_ids = {sample.metadata.get('id') for sample in samples}
        expected_ids = {task.id for task in tasks}
        if len(samples) != len(tasks):
            print(f'error: adapter produced {len(samples)} samples for {len(tasks)} tasks')
            return 1
        if sample_ids != expected_ids:
            missing = sorted(expected_ids - sample_ids)
            extra = sorted(sample_ids - expected_ids)
            print(f'error: task ids mismatch; missing={missing}, extra={extra}')
            return 1
    except Exception as exc:  # noqa: BLE001 - adapter diagnostics
        print(f'error: project_eval adapter could not load the eval set: {type(exc).__name__}: {exc}')
        return 1

    print(
        f'OK: EvalScope project_eval loaded {len(tasks)} task(s) in subset(s) {subsets} '
        f'from project_root={project_root}'
    )
    if not args.run:
        return 0

    agent_url = (args.agent_url or os.environ.get('EVAL_AGENT_URL', '')).strip()
    if not agent_url:
        print('error: --run needs --agent-url or EVAL_AGENT_URL')
        return 2
    probe = probe_agent(agent_url)
    if not probe['online']:
        print(f'error: the business Agent is not reachable at {agent_url}: {probe["error"]}')
        print('       start the Agent first, then re-run the dry-run')
        return 2
    limit = args.limit
    if limit is None and not args.task:
        limit = 1
    try:
        selected = select_tasks(tasks, args.task, limit)
    except ValueError as exc:
        print(f'error: {exc}')
        return 2
    records = run_dry_run(
        selected,
        agent_url,
        project_root,
        model=args.model or os.environ.get('EVAL_AGENT_MODEL', 'business-agent'),
        api_key=args.api_key or os.environ.get('EVAL_AGENT_API_KEY', 'EMPTY'),
    )
    return 0 if print_dry_run(records) else 1


if __name__ == '__main__':
    sys.exit(main())
