# EvalScope EvalGen

An external **coding-agent Skill** that turns a local project and its knowledge
base into an EvalScope `eval_set.v1.jsonl`. It does not modify EvalScope.

The business Agent stays as it is: expose it over HTTP, let EvalScope call it,
and use read-only state checks (SQLite or a status API) for the final outcome.

## Install as a Skill

Copy this folder into your coding agent's skill-discovery path. For Claude Code:

```bash
cp -R skills/evalscope-evalgen ~/.claude/skills/
```

Other agents use their own skill path. The skill only needs Python 3.11+ and
its bundled standard-library scripts. `check_evalscope.py` additionally needs an
EvalScope build that contains `evalscope.project_eval` (the `feat/project-eval`
branch or the patch); the released PyPI package does not provide it.

## Quick start

Run the bundled scripts with their absolute path when your shell is in the
target project (the skill may live under `~/.claude/skills/`).

```bash
# 1. Read-only scan; the agent reads scan.json, not raw project files.
#    --knowledge-dir is scanned automatically (it must stay under --root).
#    It accepts a directory or a single file; the 120k-char budget is split
#    50/50 between knowledge and code by default (--knowledge-share).
python scripts/scan_sources.py \
  --root /path/to/project \
  --include app \
  --knowledge-dir docs \
  --out artifacts/scan.json

# 2. Discover and probe the business agent from scan.json.
#    If offline, stop and ask the user to start it; offline generation needs
#    explicit confirmation and skips the dry-run.
python scripts/discover_agent.py \
  --scan artifacts/scan.json \
  --base-url http://127.0.0.1:8080 \
  --json-out artifacts/agent.json

# 2b. If the agent's API is not the /v1/eval/* envelope, detect its storage,
#     scaffold an adapter, implement StateSource.read/reset, then prove it.
python scripts/detect_state_source.py \
  --scan artifacts/scan.json --json-out artifacts/state.json
python scripts/scaffold_adapter.py --state artifacts/state.json \
  --out artifacts/my-agent-plan/adapter --agent-url http://127.0.0.1:9105
python scripts/check_adapter.py --adapter-url http://127.0.0.1:9115 \
  --agent-url http://127.0.0.1:9105

# 3. Discuss and approve a plan (ask the user; default 50 tasks, 4:4:2).
#    Use a fresh run directory under the EvalScope eval-set root.
python scripts/plan_eval_set.py \
  --project my-agent --count 50 --difficulty 4:4:2 \
  --out artifacts/my-agent-plan/eval_plan.json
# after the user approves, set approved_by_user=true in eval_plan.json

# 4. Author <run-dir>/eval_set.v1.jsonl from scan.json + eval_plan.json.
#    Each task has reference_answer / expected+forbidden facts / rubric /
#    checks, metadata.source_analysis, and metadata.plan_id / difficulty /
#    expect_failure. Each generation gets a new run directory.

# 5. Validate and build a manifest.
python scripts/validate_eval_set.py \
  --eval-set artifacts/my-agent-plan/eval_set.v1.jsonl \
  --scan artifacts/scan.json --plan artifacts/my-agent-plan/eval_plan.json \
  --root /path/to/project --strict

# Authoritative EvalScope check: schema, project_root, checks and adapter.
# Requires an EvalScope build with evalscope.project_eval.
python scripts/check_evalscope.py \
  --eval-set artifacts/my-agent-plan/eval_set.v1.jsonl \
  --project-root /path/to/runtime-state \
  --source-root /path/to/project

# 6. Dry-run one task per interaction shape (read, write+confirm, cancel,
#    rejection) against the real Agent; prints evidence, does not score.
#    Restart the Agent with a fresh state directory before the full run.
python scripts/check_evalscope.py \
  --eval-set artifacts/my-agent-plan/eval_set.v1.jsonl \
  --project-root /path/to/runtime-state \
  --source-root /path/to/project \
  --run --agent-url http://127.0.0.1:8080 \
  --task <read-task-id> --task <write-task-id> \
  --task <cancel-task-id> --task <rejection-task-id>

# 7. Record source digest and task versions.
python scripts/build_manifest.py \
  --scan artifacts/scan.json \
  --eval-set artifacts/my-agent-plan/eval_set.v1.jsonl \
  --out artifacts/my-agent-plan/manifest.json

# 8. Import into EvalScope. Omit --verified for an offline run.
python scripts/import_to_evalscope.py \
  --eval-set artifacts/my-agent-plan/eval_set.v1.jsonl \
  --eval-set-root artifacts \
  --service-url http://127.0.0.1:9010 \
  --name my-agent-plan \
  --agent-url http://127.0.0.1:8080 \
  --state-dir /path/to/runtime-state \
  --verified
```

`--agent-url` and `--state-dir` are stored on the imported entry and prefilled
on the Eval Sets page.

## Hand off to EvalScope

```bash
mkdir -p outputs
evalscope service --host 127.0.0.1 --port 9010 --outputs ./outputs \
  --enable-project-eval --eval-set-root artifacts \
  --state-root /path/to/runtime-state
```

Import the JSONL with `import_to_evalscope.py` or in **Eval Sets**. The set can
be imported while the business Agent is offline; the page shows it as imported
and enables **Run** only after a connection probe succeeds. A dry-run-passed set
is imported with `--verified` and shows a verified badge. EvalScope owns the
Agent call, trace, rubric judge, checks and report.

The direct `TaskConfig` route must set
`dataset_args.project_eval.project_root` to the project root; otherwise relative
check paths resolve against the current directory and fail silently.

## Layout

```text
SKILL.md
references/
  task-planning.md       # discuss count/type/quality with the user
  jsonl-contract.md      # output schema and examples
  source-scope.md        # allowlist, excludes, sensitive files, limits
  checks.md              # check types and read-only state API
  agent-api.md           # REST agent envelope for L0/L1/L2
  trace-levels.md        # L0/L1/L2 and degradation
scripts/
  scan_sources.py        # read-only snapshot with hashes and excerpts
  discover_agent.py      # routes/ports/health probe from scan.json
  detect_state_source.py # where the agent keeps state (sqlite/mysql/http/file)
  scaffold_adapter.py    # generate the /v1/eval/* envelope around the real API
  check_adapter.py       # prove the adapter reports state truthfully
  plan_eval_set.py       # draft plan; approval is explicit
  validate_eval_set.py   # schema/path/material/source/plan validation
  check_evalscope.py     # EvalScope loader + adapter smoke test + per-task dry-run
  import_to_evalscope.py # import a generated set into a running service
  build_manifest.py      # source digest + task versions
examples/minimal/        # one-task example
tests/                   # standard-library unittest suite
```

## Design boundaries

- **EvalScope does not read source.** This skill performs the source/KB scan
  and hands over a JSONL contract.
- **The Agent does not need code changes.** EvalScope calls its HTTP API; checks
  use SQLite or a read-only state endpoint.
- **Trace is optional.** Use L2 when the Agent returns steps; degrade to L0 and
  mark `process` criteria `not_evaluable` when it does not.
- **Security is in the scanner.** The agent must not read project files
  directly; it only reads `scan.json`, which excludes `.env`, keys, secrets,
  symlinks and over-budget files.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests live with the skill because it is installed and copied as a
standalone artifact; EvalScope's own package tests remain under `tests/`.
