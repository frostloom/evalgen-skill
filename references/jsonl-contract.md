# JSONL contract

One JSON object per line, UTF-8, no trailing commas. The file is consumed by
EvalScope's `project_eval` benchmark.

## Minimal task

```json
{
  "id": "refund-001",
  "version": 1,
  "question": "客户想把最近一笔充值退 100 元，可以吗？",
  "system": "",
  "messages": [],
  "reference_answer": "先预览退款 100 元；确认后执行一次，返回退回 100 元、余额 1900 元。",
  "expected_facts": ["退款 100 元", "余额 1900 元"],
  "forbidden_facts": ["重复退款", "退款 0 元"],
  "pass_threshold": 0.75,
  "rubric": [
    {"id": "c1", "criterion": "确认后审计中出现一次已确认的退款", "weight": 1,
     "must_pass": true, "evidence_requirement": "outcome"},
    {"id": "c2", "criterion": "先预览后确认，没有跳过确认", "weight": 2,
     "must_pass": false, "evidence_requirement": "process"},
    {"id": "c3", "criterion": "说明退款金额和结果", "weight": 2,
     "must_pass": false, "evidence_requirement": "answer"}
  ],
  "checks": [
    {"id": "db-1", "type": "sqlite", "dsn": "agent.db",
     "query": "select confirmed from agent_audit_log where session_id='refund-001' order by id desc limit 1",
     "expect": 1}
  ],
  "exec": {
    "type": "agent_api",
    "protocol": "rest_steps",
    "agent_url_env": "EVAL_AGENT_URL",
    "reset_url": "http://127.0.0.1:8080/v1/eval/reset",
    "trace": {"mode": "auto"},
    "session": {"id_from": "sample_id"}
  },
  "source_refs": ["internal/agent/tools.go:484", "docs/policy.md#退款"],
  "origin": "fact",
  "materials": [{"path": "internal/agent/tools.go", "sha256": "..."}],
  "metadata": {
    "plan_id": "plan-20261126061056",
    "difficulty": "medium",
    "expect_failure": false,
    "uncertainties": [],
    "env_assumptions": {
      "agent_url": "http://127.0.0.1:8080",
      "reset_url": "http://127.0.0.1:8080/v1/eval/reset",
      "state": {"balance_cent": 200000}
    },
    "source_analysis": {
      "code_refs": [{"path": "internal/agent/tools.go", "line": 484, "fact": "refund_recharge requires confirmation"}],
      "doc_refs": [{"path": "docs/policy.md", "line": 12, "heading": "退款", "fact": "refund is capped at balance"}],
      "facts": ["refund amount is capped at the current balance"],
      "scope_digest": "..."
    }
  }
}
```

## Fields

| Field | Rule |
|---|---|
| `id` | Unique string; used as session id by the adapter |
| `version` | Integer >= 1; increment on edit |
| `question` | Required non-empty string; `messages` are an optional multi-turn supplement |
| `messages` | Optional multi-turn messages; when present they replace the single-turn question; `system` is still forwarded |
| `reference_answer` | Gold business outcome; shown as Expected Answer and used by the judge |
| `expected_facts` / `forbidden_facts` | User-visible facts only; no tool names, code identifiers or source refs; every expected fact must appear in `reference_answer` |
| `pass_threshold` | 0–1; default 0.6, use 0.75 for multi-criteria tasks |
| `rubric` | Non-empty; each criterion needs `id`, `criterion`, `weight`, `must_pass`, `evidence_requirement` |
| `checks` | At least one final-state check is required; read-only and structured (see below) |
| `exec` | `agent_api` or `http`; see below |
| `source_refs` | `path:line` or `doc#heading`, and the file must have an excerpt in scan.json |
| `materials` | Actual files with a 64-char hex SHA-256, no secrets |
| `metadata.source_analysis` | Required; structured facts + at least one code/doc ref + scope digest |
| `metadata.env_assumptions` | Required with loopback URLs; declares agent URL and state/seed assumptions |
| `metadata.plan_id` / `metadata.difficulty` / `metadata.expect_failure` | Must match the approved `eval_plan.json` |
| `origin` | `fact` or `inference` |

## Evidence requirement

- `answer` — judged from the agent's final answer;
- `outcome` — judged from end-state checks;
- `process` — requires trace/tool calls. If trace is unavailable it becomes
  `not_evaluable`; never invent it.

`json_path`, `sqlite` and `http_json` checks must include `expect`; EvalScope
compares the observed value to it and a missing value would compare against
`null`. `http` checks use `expect_status` (default 200) instead.

## Validator lint rules

`validate_eval_set.py --strict` rejects:

- expected facts containing snake_case identifiers, `path:line` references, or
  wording that is absent from `reference_answer`;
- SQL checks that use a bare `count(*)` for absence, compare `result`/`preview`/
  `args` text, compare a long human-readable string, are not read-only, or
  mention tables or `tool='...'` literals missing from `scan.json`;
- `messages` with non-user roles, a `确认`/`取消` turn after a question, or a
  confirmed-write/cancel check without the matching turn;
- loopback URLs without `metadata.env_assumptions` (warning; `--strict` makes
  it an error).

`check_evalscope.py --run --task <id>` executes selected tasks once, prints the
reply, tool steps, conversation and every check result, and fails when a check
fails. It also reports fact coverage: an `expected_facts` entry that appears
nowhere in the conversation the judge reads, and any `forbidden_facts` the Agent
actually said. A passing check tally does not excuse a missing fact — the judge
would fail a correct answer. Use it before handoff; fix the generation inputs
rather than editing the generated file.

Answer criteria are semantic: name the required next step or behavior (query,
clarify, refuse, preview, confirm, report status) and forbid contradictory or
fabricated facts. Do not require literal equality with `reference_answer` and do
not fail a correct answer for adding non-contradictory business context.

## `exec`

```json
{"type": "agent_api", "protocol": "rest_steps", "agent_url_env": "EVAL_AGENT_URL",
 "reset_url": "http://127.0.0.1:8080/v1/eval/reset",
 "trace": {"mode": "auto"}, "session": {"id_from": "sample_id"}}
```

```json
{"type": "http", "base_url_env": "EVAL_AGENT_URL",
 "request": {"method": "POST", "path": "/chat", "json": {"message": "hi"}},
 "trace": {"mode": "none"}, "session": {"id_from": "sample_id"}}
```

`reset_url` must be `http://127.0.0.1[:port]` or `http://localhost[:port]` and
is called before every task; use it only when the business state is safe to
reset. Check URLs must use the same loopback forms; uppercase schemes, userinfo,
IPv6 loopback and lookalike hosts are rejected. `exec.trace.mode` accepts
`auto`/`inline`/`none`; `fetch` is rejected because this build records only steps
returned inline with the reply. `exec.session.id_from` must be `sample_id`.

The REST agent envelope is in [agent-api.md](agent-api.md).

## Manifest

`build_manifest.py` writes:

```json
{
  "generated_at": "...",
  "source_scope_digest": "...",
  "file_hashes": {"internal/...": "..."},
  "task_versions": {"refund-001": 1}
}
```

Keep old eval-set files and manifests when editing; runs remain tied to the
version they used.
