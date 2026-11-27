# Checks and read-only state API

Checks assert the business end state, not the model's self-report. They run
after the agent call and are read-only.

## Supported check types

| Type | Fields | Meaning |
|---|---|---|
| `file_exists` | `path` | file exists under the project root |
| `file_content` | `path`, `contains` | text contains substring |
| `json_path` | `path`, `json_path`, `expect` | dot path in a JSON file equals expected |
| `sqlite` | `dsn`, `query`, `expect` | read-only query; first row/first column equals expected |
| `http` | `method`, `url`, `expect_status` | GET/HEAD only; HTTP status equals expected |
| `http_json` | `url`, `json_path`, `expect` | GET JSON and compare a dot path |

`dsn`/`path` are relative to `project_root`; they must stay under it and must not
point to sensitive files (`.env`, `.ssh`, key files, `secrets*`) or symlinks;
EvalScope denies those at runtime.
`expect` is required for `json_path`, `sqlite` and `http_json`; missing it would
compare the observed value against `null`. HTTP URLs must use
`http://127.0.0.1[:port]` or `http://localhost[:port]`; uppercase schemes,
userinfo, IPv6 loopback and lookalike hosts such as `localhost.example.com` are
rejected. Command/exit-code checks are not supported by this skill.

## Read-only state API contract

For a separately deployed business agent, ask the business side to expose a
read-only endpoint:

```http
GET /v1/eval/state?session_id=<task id>
```

```json
{
  "audit": [
    {"tool": "refund_recharge", "confirmed": true, "result": "..."}
  ],
  "balance_cent": 190000,
  "orders": [{"id": "T-1001", "status": "refunded"}],
  "pending": null
}
```

Rules:

- GET only; no writes; no credentials in query strings;
- stable field names; return the state after the task;
- include tool/action audit entries with `confirmed` when confirmation matters;
- if the agent is local, a read-only SQLite query is also acceptable.

Use `http_json` against this endpoint, e.g.:

```json
{"id": "state-1", "type": "http_json",
 "url": "http://127.0.0.1:8080/v1/eval/state?session_id=refund-001",
 "json_path": "audit.0.confirmed", "expect": true}
```

## Assertion rules

The validator rejects checks that cannot distinguish a correct run from an
incorrect one:

- **Scope absence assertions.** "Nothing happened" must assert the absence of
  the specific write or state change: filter by the write tool or a state
  discriminator (`confirmed=1`, a status column). A bare `count(*)` over the
  session also counts reads and preview attempts, so it fails correct behavior.
- **Never compare human-readable text.** Do not select `result`, `preview` or
  `args` and compare a sentence. Assert a structured column (`tool`, `status`,
  `confirmed`, `amount_cent`) or the final state. Short enum values are fine.
- **Ground table and tool names in the scan.** Every table and every
  `tool='...'` literal in a SQL check must appear in the scanned excerpts; the
  validator enforces this.
- **Keep SQL read-only.** Only `SELECT` or CTE queries are accepted.
- **Prefer state over rows.** For balances, caps and final status, use
  `http_json` against the read-only state endpoint when the database is not the
  source of truth; when it is accessible, query the state column directly.
- **Checks must be necessary.** A task with a `must_pass` outcome is zeroed by
  ANY failed check, so include only checks whose failure means the outcome is
  wrong. A legitimate read or preview attempt is not a state change.
- **Stay inside the task's session.** Use the task's own `id` in the SQL/URL;
  never reference another session's state.

## Environment assumptions

When a task calls a loopback URL (`reset_url`, `http` or `http_json` checks),
declare `metadata.env_assumptions` on the task, for example:

```json
"env_assumptions": {
  "agent_url": "http://127.0.0.1:8080",
  "reset_url": "http://127.0.0.1:8080/v1/eval/reset",
  "state": {"balance_cent": 200000, "paid_orders": ["T-1001"]}
}
```

The dry-run uses the same assumptions; if the environment does not match them,
seed it or change the generated task, not the check.

## Choosing checks

- Prefer an explicit final-state check per task.
- For “nothing happened” cases, assert no confirmed write for the relevant tool.
- For “confirmed” writes, check the confirmed audit entry or the status field.
- For boundaries, check the capped amount / final balance, not only the answer.
- Do not check implementation details that are not business-observable.
