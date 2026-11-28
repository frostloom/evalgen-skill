---
name: evalscope-evalgen
description: >-
  Generate EvalScope business-agent eval sets from a local project and its
  knowledge base. Read-only scans selected source/docs, produces eval_set.v1.jsonl
  with reference answers, expected/forbidden facts, rubrics and end-state checks,
  discovers and probes the business agent's HTTP API, and hands execution to
  EvalScope (directly with core or through an optional management import).
  Trigger on:
  generate eval set, create evaluation tasks from a repo, project eval, evalgen,
  import eval set, 生成评测集, 根据源码出题, 知识库出题, 业务 Agent 评测,
  EvalScope 项目评测, 导入评测集. Do NOT trigger for: model training, serving,
  or writing benchmark adapters.
---

# EvalScope EvalGen

This skill is an **external authoring tool** for EvalScope. It does not modify
EvalScope. It reads a user-selected local project and knowledge base, writes
an `eval_set.v1.jsonl`, and EvalScope consumes that file through the
`project_eval` benchmark. Core builds can run the file directly; importing it
into the Eval Sets management page is optional.

Read only the reference file for the current step; do not preload everything.
Commands below use `scripts/...` relative to this skill directory; when your
working directory is the user's project, run them with the absolute path to
this skill's `scripts/` folder.

| Step | Reference |
|---|---|
| Task planning (required before generation) | [task-planning.md](references/task-planning.md) |
| Output format | [jsonl-contract.md](references/jsonl-contract.md) |
| Source / knowledge-base scope | [source-scope.md](references/source-scope.md) |
| Checks and read-only state API | [checks.md](references/checks.md) |
| Agent API envelope | [agent-api.md](references/agent-api.md) |
| Trace capability levels | [trace-levels.md](references/trace-levels.md) |

## Install

Copy this directory into the coding agent's skill-discovery path:

```bash
git clone https://github.com/frostloom/evalgen-skill.git ~/.claude/skills/evalscope-evalgen
```

For Claude Code that path is usually `~/.claude/skills/evalscope-evalgen/`;
other agents use their own skill path. The skill only needs Python 3.11+ and its
bundled scripts (standard library only). `check_evalscope.py` additionally
requires an EvalScope build that contains `evalscope.project_eval` (the
`feat/project-eval` branch or the patch); the released PyPI package does not
provide it.

## Required inputs

Ask for these before generating:

- **project root** — the local project to evaluate;
- **knowledge base** — one or more document directories or files under the
  project root; copy or mount external docs under the root, because the scanner
  never reads outside it;
- **output directory** — where `eval_set.v1.jsonl` and `manifest.json` go;
- **task count and difficulty ratio** — ask the user (default 50 tasks,
  `4:4:2`); they may freely adjust both;
- **system under test** — HTTP agent URL, or a local service; and a read-only
  state endpoint or SQLite snapshot when end-state checks need one;
- **agent start command** — how to start the business agent when the probe
  fails; include the expected health path if it is not `/health`;
- **EvalScope service and eval-set root** — only for the optional management
  import: the running service URL (default `http://127.0.0.1:9010`) and the
  directory passed as `--eval-set-root`. Core users skip this and pass the
  JSONL through `TaskConfig`/CLI;
- **state evidence** — a read-only state response, SQLite copy, or a short
  description of seed data (balances, orders, catalog, subscriptions). Use it to
  declare `metadata.env_assumptions` and to design checks that match the real
  environment;
- **runtime state directory** — the directory holding the business DB/files
  that local checks read (the same path passed to `check_evalscope.py
  --project-root`); record it as `metadata.env_assumptions.state_dir` and pass
  it to the import script as `--state-dir`. Remote-only HTTP checks do not
  need it;
- **trusted remote state prefix** — for a separated state API, the explicit
  `--check-url-prefix` value used by the skill and the matching
  `dataset_args.project_eval.check_url_prefixes` value used by EvalScope;
- **trace level** — L0/L1/L2 if known; otherwise `auto`.

## Workflow

### 1. Scan selected materials (read-only)

```bash
python scripts/scan_sources.py \
  --root <project-root> \
  --include src \
  --knowledge-dir docs \
  --summary \
  --out artifacts/scan.json
```

The scanner enforces the allowlist, default excludes, sensitive-file denylist,
symlink rejection, file/byte/character limits, redaction and SHA-256.
`--knowledge-dir` accepts a directory or a single file, is scanned
automatically and is marked `knowledge: true`; it does not need to be repeated
in `--include`. The default excerpt budget is 300,000 characters total and
20,000 per file, split 50/50 between knowledge and code; adjust with
`--knowledge-share` (0–1) and the budget flags. Code claims any knowledge share
that goes unused, so a small knowledge base does not starve the source.
Packaging metadata (`*.egg-info`, `*.dist-info`) is excluded, and
docs/observability files are deferred behind business code so a large logging
layer cannot push out the agent's own logic. Show the summary (selected
files, sent chars, sensitive rejects, omitted reasons) to the user and get
confirmation before generating tasks. **Two warnings in that summary are work to
do, not noise:** files that did not fit the total budget, and files cut at the
per-file cap (`main.py 488/908 lines`). A citation past a file's cut is rejected
by the validator, because the model never saw that text. **Read only `scan.json`
for project context; never read raw project files directly.**

### 2. Connect to the business agent (required before generation)

Ask for the agent base URL (or start command). Probe the candidates found in
`scan.json`:

```bash
python scripts/discover_agent.py \
  --scan artifacts/scan.json \
  --base-url <agent-url> \
  --continuity \
  --json-out artifacts/agent.json
```

The script reads only `scan.json` and probes local candidates; it never starts
a service. If it reports `online`, use the recommended URL. If it reports
offline, stop and ask the user to start the agent; show the start command they
provided. Continue only when the user explicitly accepts offline generation.
In offline mode, skip the live dry-run and import the set without `--verified`;
the EvalScope page will show it as imported but not verified.

**Authentication.** A `401`/`403` is not "offline": the service is up and wants
a credential. The probe says so explicitly and names the variable to set
(`--api-key`, or `AGENT_API_KEY` for the adapter). Ask the user for the token;
never write it into `scan.json`, the eval set, `env_assumptions`, or a check URL.
Record only *that* a credential is needed and which variable carries it.

**Session continuity.** `--continuity` sends two real turns and reports whether
the agent keeps a conversation. This decides whether multi-turn tasks are
writable at all:

- `caller-session` — the caller supplies the session id; multi-turn tasks work.
- `server-mints-per-request` — the agent returns an id but mints a new one per
  request, so two POSTs are two conversations. A `确认` turn cannot reach the
  previous turn's pending action. **Write single-turn tasks only**; if the
  agent has a dedicated confirm/decision endpoint, a multi-turn confirm task
  must drive that endpoint, which `exec.type: agent_api` does not do.
- `none-detected` — assume stateless and write single-turn tasks.

Writing a confirm/cancel task against a stateless-per-request agent produces a
task that cannot pass however correct the agent is. Do not do it.

If the discovered API does not match the `/v1/eval/chat` envelope, do not modify
the business code or EvalScope. Use `exec.type: http` only when the request body
is static. Otherwise an adapter is required — and **do not guess where the state
lives**. Detect it, scaffold from the report, then prove it:

```bash
# 1. What storage does this project actually use? Read-only; connects to nothing.
python scripts/detect_state_source.py \
  --scan artifacts/scan.json --json-out artifacts/state.json

# 2. Scaffold the envelope around the real agent API. The scaffold contains no
#    storage code: StateSource.read/reset are left unimplemented on purpose.
python scripts/scaffold_adapter.py \
  --state artifacts/state.json \
  --out <run-dir>/adapter \
  --agent-url <agent URL> \
  --chat-path <real chat path> \
  --request-field <field carrying the question> \
  --request-session-field <field carrying the session id> \
  --response-reply-field <field carrying the reply> \
  --response-steps-field <field carrying tool steps, if any>

# 3. Implement StateSource.read/reset from the schema named in the scaffold,
#    then prove the adapter tells the truth.
python scripts/check_adapter.py \
  --adapter-url http://127.0.0.1:9115 --agent-url <agent URL> \
  --expect-state-key <business field> [--expect-state-key <another>]
```

Pass the credential to the adapter through `AGENT_API_KEY` in its environment,
never in the generated source.

Read the agent's real response body before scaffolding, and pass the fields you
actually saw. When the reply and trace are nested inside an envelope, add
`--response-root-field <envelope>` (for example `state` or `data`); without it
the adapter reads the top level and answers 502 rather than returning an empty
reply. A step item that names itself with none of `name`, `tool`, `node`,
`action`, `step` or `label` is reported as an error for the same reason: a step
list that is empty by accident silently turns every `process` criterion into
`not_evaluable`.

`--expect-state-key` is what separates real end state from adapter bookkeeping.
Without it, an adapter that returns only its own counters passes every other
check while every end-state assertion is fiction. Name the business fields the
state must expose (dotted paths allowed); the check fails when they are absent.

`detect_state_source.py` reports the engines, tables and state endpoints found in
the scanned excerpts. It never assumes one: a project may be SQLite, MySQL,
Postgres, Redis, a file, or an existing status endpoint, and the report says
which. If it finds nothing, widen the scan or ask the user how the end state can
be observed — do not invent a state source. `scaffold_adapter.py` refuses to run
in that case; `--allow-unknown-engine` exists only for the situation where the
user has already told you how the state is observed.

**The adapter is only correct if `check_adapter.py` passes.** It runs a real
turn and fails when the state does not follow it, when a fresh session inherits
another session's rows, when reset does not restore the pre-turn state, or when
reset wipes a different session. A failing check means the checks in the eval set
would be measuring the adapter instead of the agent.

Run the adapter as its own process and record its start command in
`metadata.env_assumptions`.

If `StateSource` needs a database driver (MySQL, Postgres, …), create
`<run-dir>/adapter/.venv`, install only that driver into it, and run the adapter
with that interpreter. Do not install into the project's environment, into
EvalScope's, or into the skill's — the adapter is the only thing that needs it,
and it must stay removable.

### 3. Build source analysis

From the scanner excerpts only, produce a short structured analysis:

- modules, routes, tools, data models, entry points;
- business rules and state transitions;
- observable end state (database/API/file);
- open questions / unsupported facts.

Every fact must cite `path:line` or `doc#heading`, and the reference must exist
in `scan.json`; the validator enforces this. Do not invent unsupported rules;
mark them as inference or ask the user.

### 4. Discuss and approve a task plan (required)

This step is mandatory: always ask the user, even if they said "use the
defaults". Do not generate tasks yet. Ask only:

1. how many tasks (default 50);
2. the difficulty ratio (default `easy:medium:hard = 4:4:2`).

The user may freely adjust both values; accept any positive count and any
three non-negative difficulty weights. Expected-failure tasks are included
automatically at 20% (at least one when the count is 5 or more), so do not ask
the user to enumerate categories. If the user wants a different failure share,
pass `--failure-ratio`.

Choose a fresh run directory for every generation:
`<eval-set-root>/<project>-<plan_id>/`. Do not overwrite or reuse an earlier
set; users may want several different sets.

```bash
python scripts/plan_eval_set.py \
  --project <name> --count 50 --difficulty 4:4:2 \
  --out <run-dir>/eval_plan.json
```

Show the resulting counts. When the user explicitly approves, set
`approved_by_user: true` and `approved_at`. Generate nothing while it is
`false`. See [task-planning.md](references/task-planning.md).

### 5. Generate tasks

For each task write:

- `question` (and `messages` for multi-turn);
- `reference_answer` (the expected business outcome, derived from source/docs);
- `expected_facts` / `forbidden_facts`;
- `rubric` with `must_pass` / `weight` / `evidence_requirement`;
- `checks` — read-only file / sqlite / http / http_json / json_path;
- `exec` — agent API or HTTP, plus an optional loopback `reset_url`;
- `source_refs` and materials with SHA-256;
- `metadata.plan_id`, `metadata.difficulty`, `metadata.expect_failure`;
- `metadata.source_analysis` (required: at least one `code_refs` or `doc_refs`
  entry and a `facts` list).

Generation rules (the validator enforces them):

- `expected_facts` are user-visible business facts only. Never put tool names
  (`get_balance`), code identifiers, source references, or implementation rules
  into them, and use the same wording in `reference_answer` — the judge checks
  the answer against these strings.
- Absence assertions must be scoped to what is absent. Do not use a bare
  `count(*)` for "nothing happened"; filter to the write tool or a state
  discriminator such as `confirmed=1`. Never compare a full human-readable
  result string; assert a structured column or the final state.
- Every table and tool name in a SQL check must appear in the scanned excerpts,
  and SQL checks must stay read-only (`SELECT`/CTE).
- `messages` may contain only `user` turns; the runner ignores scripted
  assistant turns. A turn that says `确认` or `取消` must follow an explicit
  imperative request that carries the needed parameters; a question such as
  "退 50 元可以吗？" does not reliably create a pending operation for the
  confirmation to act on.
- When a task uses a loopback or trusted remote state URL, declare
  `metadata.env_assumptions` with the agent URL and the state/seed assumptions.
- Every task declares `metadata.env_assumptions.agent_url`; `state_dir` is
  required when file/JSON/SQLite checks are present, not for remote-only HTTP
  checks. The import script reads them to prefill the Eval Sets page. When an
  adapter is used, `agent_url` is the **adapter's** URL — that is what
  EvalScope calls.
- When checks read SQLite or files, set `metadata.env_assumptions.state_dir` to
  the runtime state directory; the import page uses it to prefill the state
  path used by end-state checks.

Every task needs at least one `must_pass` outcome criterion and at least one
final-state check. The schema cannot link a criterion to a check; at runtime,
if a task has a must_pass outcome criterion and ANY check fails, the task is
zeroed. Include only checks that must pass for that outcome. Also include at
least one answer criterion. Use [jsonl-contract.md](references/jsonl-contract.md).
Write one task per line to `<run-dir>/eval_set.v1.jsonl`. Each generation gets
a new directory; the file is not an incremental update of an earlier set.

### 6. Validate (required)

```bash
python scripts/validate_eval_set.py \
  --eval-set <run-dir>/eval_set.v1.jsonl \
  --scan artifacts/scan.json \
  --plan <run-dir>/eval_plan.json \
  --root <project-root> \
  --strict
# Add --check-url-prefix <prefix> for remote state checks.

python scripts/check_evalscope.py \
  --eval-set <run-dir>/eval_set.v1.jsonl \
  --project-root <runtime state directory> \
  --source-root <project-root>
# Use --check-url-prefix <prefix> for remote HTTP state checks; --project-root
# is optional when every check is remote.
```

`--project-root` is the runtime state directory that holds the state DB or
files the checks read; `--source-root` is the project root used to verify
materials and source references.

When validation fails, change the generation inputs (scan scope, task wording,
checks, facts) and regenerate the set; do not hand-edit individual tasks after
generation.

`validate_eval_set.py` checks the EvalScope `EvalTask` contract, source
references (file/line/heading), material hashes, source changes, the approved
plan, excerpt coverage, check paths/URLs and the standard rubric. A reference to
a file that produced no excerpt fails: re-scan with a larger `--max-total-chars`
instead of citing unseen code. `check_evalscope.py` is the authoritative smoke
test: it validates the schema with EvalScope's own loader, resolves every check
path against `--project-root`, and converts every task through the
`project_eval` adapter. It requires an EvalScope build that contains
`evalscope.project_eval` (the `feat/project-eval` branch or the patch); the
released PyPI package does not include it. **Do not hand off until validation,
the EvalScope smoke test and the dry-run pass.**

### 7. Dry-run against the live agent (required when the agent is online)

Static checks cannot tell whether the Agent reaches the expected end state,
whether a confirmation turn creates a pending operation, or whether the assumed
seed data exists. Run one task per interaction shape (read, write + confirm,
cancel, rejection/adversarial) against the real Agent:

```bash
python scripts/check_evalscope.py \
  --eval-set <run-dir>/eval_set.v1.jsonl \
  --project-root <runtime state directory> \
  --source-root <project-root> \
  --run --agent-url <agent URL> \
  --task <read-task-id> --task <write-task-id> \
  --task <cancel-task-id> --task <rejection-task-id>
# Add --check-url-prefix <prefix> for a remote state API; omit --project-root
# when every check is remote.
```

The dry-run executes the Agent, prints the reply, tool steps, conversation and
every end-state check, and returns non-zero when any check fails. It does not
score, call a judge, or write eval results. Fix the generation inputs (task
wording, parameters, checks, `metadata.env_assumptions`) until the selected
templates pass; do not hand-edit a one-off set after generation. Configured
reset URLs are called before each task.

It also reports **fact coverage**: for every task, whether each
`expected_facts` entry appears somewhere in the conversation the judge will read,
and whether the Agent said any `forbidden_facts`. A green check tally with a
missing fact is not a pass — the judge would fail a correct answer, so treat a
`fact check:` line as work to do on the task wording or the facts, not as noise.
`forbidden_facts` are matched against the Agent's own turns only; a question that
names the forbidden claim ("what is the shipping address") is not a violation.

`--agent-url` is the URL EvalScope will actually call, so when the set goes
through an adapter it must be the **adapter's** URL (e.g.
`http://127.0.0.1:9115`), not the business agent's. Passing the raw agent here
tests a path the scoring run will never use and hides adapter bugs that
`check_adapter.py` would catch.

If the dry-run fails because the environment lacks the assumed data, either seed
the environment or generate tasks for the actual state — never silently weaken
the check.

If the agent is offline and the user explicitly accepted offline generation,
skip this step and do not pass `--verified` at import time.

The dry-run writes audit/state rows into the Agent's runtime state. Before the
full EvalScope scoring run, restart the Agent with an empty state directory (or
clear the audit log); otherwise end-state checks may be satisfied by dry-run
rows instead of the scored run.

### 8. Build manifest

```bash
python scripts/build_manifest.py \
  --scan artifacts/scan.json \
  --eval-set <run-dir>/eval_set.v1.jsonl \
  --out <run-dir>/manifest.json
```

The manifest records source digest, material hashes, task versions and the
generation timestamp. Editing a task increments its `version`; keep prior run
files unchanged.

### 9. Import into a management-enabled EvalScope (optional)

This step requires the management API; the core `project_eval` build has no
Eval Sets import endpoint. With core, skip it and run the JSONL directly with
`TaskConfig`/`eval` (`local_path`, `agent_url`, and `project_root` for local
checks; `check_url_prefixes` for remote state APIs).

The service must be running with the same root:

```bash
mkdir -p <eval-set-root> ./outputs
evalscope service --host 127.0.0.1 --port 9010 --outputs ./outputs \
  --enable-project-eval --eval-set-root <eval-set-root> \
  --state-root <runtime state directory>
```

`--outputs` must already exist; the CLI rejects a missing directory. `--state-root`
is what lets the import accept `--state-dir` (it defaults to the eval-set root).

Then import the generated file:

```bash
python scripts/import_to_evalscope.py \
  --eval-set <run-dir>/eval_set.v1.jsonl \
  --eval-set-root <eval-set-root> \
  --service-url http://127.0.0.1:9010 \
  --name <project>-<plan_id> \
  --agent-url <agent URL> \
  --state-dir <runtime state directory> \
  --verified
```

`--agent-url` and `--state-dir` are stored on the imported entry so the Eval
Sets page prefills the business Agent URL and runtime state directory; the user
only edits them when the environment changes. Both are also read from
`metadata.env_assumptions` when the flags are omitted. `agent_url` is required;
`state_dir` is required for file/JSON/SQLite checks, while remote-only HTTP
checks may omit it. Pass `--state-dir` when the management service still needs
a state root for the entry.

Before importing, the script asks the service for its allowed state roots
(`GET /api/v1/project-eval/state-roots`) and fails with the exact `--state-root`
to use when `--state-dir` is outside them. That is the same rule the run
endpoint enforces with a 403, checked here so the user never hits it later.

Omit `--verified` for an offline run. Import is allowed while the business
agent is offline; the EvalScope page shows the set as imported and enables the
run button only after a connection probe succeeds. The script never starts a
run. If the service is not running, it prints the exact start command and
leaves the file in place.

The direct `TaskConfig` route must set
`dataset_args.project_eval.project_root` when the task has file/JSON/SQLite
checks; remote-only HTTP checks do not need it. For a remote state API, also
set `dataset_args.project_eval.check_url_prefixes`. The evaluator owns
execution, trace, judge and report; this skill only authored the set.

## Safety invariants

- Read only through `scan_sources.py`; do not `Read`/`cat` arbitrary project paths.
- Never include `.env`, keys, certificates, credentials or private keys.
- Never execute project code, install dependencies, or run shell commands from
  generated task inputs. Installing a driver for the generated adapter is the one
  exception, and it must happen in an isolated environment you create
  (`<run-dir>/adapter/.venv`) with the user's explicit approval — never into the
  project's own environment and never into EvalScope's.
- Checks must be read-only; command checks are not supported.
- Dry-run calls only the Agent's HTTP API and the read-only checks; never run
  project commands or mutate state outside the Agent's own actions. Reset URLs
  stay loopback-only.
- `discover_agent.py` reads only `scan.json` and probes candidate URLs; it never
  reads project files directly, installs dependencies, or starts a service.
- `import_to_evalscope.py` only posts the generated file to the EvalScope
  service; it never starts a run or executes the Agent.
- Offline generation requires explicit user confirmation; never skip the
  dry-run silently.
- Do not include secrets in `source_refs`, `materials`, `expected_facts` or the
  judge prompt.
- The scanner excerpts are sent to the local coding-agent model for planning and
  generation. The eval set and Judge receive the reference answer,
  expected/forbidden facts, rubric criteria and check evidence, not raw source.
  Keep secrets out of all of them; task/Rubric/check editing and versioning stay
  outside EvalScope.
- Do not claim trace evidence that the agent did not return.

## Quality bar

- The approved plan controls count, difficulty counts and the expected-failure
  count; do not silently change them.
- Every task has a reference answer, expected facts, at least one final-state
  check and `metadata.source_analysis` with a code/doc reference.
- **A correct answer must not fail on wording.** `expected_facts` are the
  *content* the answer must convey, not a string to echo: the judge is semantic,
  so a fact is satisfied by any phrasing that carries it. Never write an `answer`
  criterion that pins a literal sentence ("must contain 「已完成」") or that simply
  restates an expected fact — the validator rejects both shapes, because they
  turn a synonym, a reordering, or an extra sentence into a wrong answer. Name
  the behaviour instead (query / clarify / refuse / preview / confirm / report
  status) and forbid only contradictions and fabricated amounts or state.
- Every rubric has at least one `must_pass` outcome criterion and at least one
  additional `answer` criterion. Any failed check zeroes a task with a
  must_pass outcome, so include only checks that must pass.
- **Vary what `must_pass` asserts.** If most tasks share one mandatory shape —
  "intent is X and nothing was written" — the set cannot separate a correct
  agent from one that merely avoids side effects, and a high pass rate measures
  only the lower-weighted answer criteria. Make the mandatory criterion specific
  to each task: a field value that must change, a record that must be created, a
  status transition, an amount. The validator warns when 80% or more of tasks
  share one shape.
- Expected-failure tasks must carry `forbidden_facts`. The default plan includes
  them; an explicit `--failure-ratio 0` is honored only when the user asks for
  an all-pass set.
- Every `expected_facts` entry is user-visible, contains no tool or code
  identifier, and appears verbatim (modulo whitespace) in `reference_answer`.
- Absence checks name the write tool or state discriminator they assert
  (`confirmed=1`, a status column, a write tool); they never rely on a bare
  row count and never compare full human-readable result text.
- Multi-turn scripts use user turns only, and every `确认`/`取消` turn follows a
  parameter-complete imperative request from the user **and** an agent whose
  session-continuity probe allows multi-turn (`discover_agent.py --continuity`).
  Against a `server-mints-per-request` agent, write single-turn tasks.
- `answer` criteria name the required next step or behavior (query, clarify,
  refuse, preview, confirm, report status) and forbid contradictions or
  fabricated amounts/state; they do not require literal equality with the
  reference answer and allow non-contradictory extras.
- Every loopback or explicitly trusted remote URL in `exec` or checks is
  declared in `metadata.env_assumptions` together with the state/seed
  assumptions it needs.
- The dry-run covers one task per interaction shape and its evidence has been
  reviewed before handoff, including the fact-coverage report — a task whose
  `expected_facts` the Agent never says will fail a correct answer at judge time.
- Every generation writes a new `<eval-set-root>/<project>-<plan_id>/` directory;
  never overwrite or merge an earlier set.
- The imported entry name is the project and plan id (or a user-provided
  `--name`); multiple sets remain distinguishable in Eval Sets.
- An offline set is imported without `--verified`; do not describe it as
  dry-run checked or ready to run until the service probe succeeds.
- After a dry-run, explicitly tell the user to restart the Agent with a fresh
  state directory before the full scoring run; the dry-run state is not
  seed-clean.
- Any generated adapter lives under `<run-dir>/adapter/`, runs as its own
  process, and never changes the business code or EvalScope core.
- The adapter's state source comes from `detect_state_source.py`, never from a
  guess; a project whose storage the scan cannot see is an open question for the
  user, not a default.
- `check_adapter.py` passes before any task is generated against the adapter: a
  wrong adapter makes every end-state check meaningless while the run still
  looks successful.
- `must_pass` is reserved for criteria that are truly mandatory.
- Prefer `http_json` against a read-only state API for separately deployed
  agents; use `sqlite` only when EvalScope can read the business DB.
- If there is no trace, omit `process` criteria or accept that they will be
  `not_evaluable`; do not fabricate tool calls.
