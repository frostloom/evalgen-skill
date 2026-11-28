# Agent API envelope

`exec.type: agent_api` with `protocol: rest_steps` calls a small REST contract on
the business agent. The agent keeps its own business logic; this is only the
evaluation endpoint.

Export the base URL before running EvalScope:

```bash
export EVAL_AGENT_URL=http://127.0.0.1:8080
```

`exec.agent_url_env` names the variable (default `EVAL_AGENT_URL`).

## Discovery

Before generating, run `scripts/discover_agent.py --scan artifacts/scan.json`.
It extracts route, port, health-path and eval-envelope hints from the read-only
snapshot, probes the candidates, and never starts a service. If the Agent
exposes a different chat path, use a separate thin adapter or the single-request
`exec.type: http` form. EvalScope also probes `/health` (or a TCP connect)
before enabling **Run** on the Eval Sets page.

## Different API shape

`exec.type: http` supports only a static request body; it cannot inject the
question or session id. When the business API needs those, an adapter is
required.

**Do not assume where the state lives.** Run `scripts/detect_state_source.py` on
`scan.json` first. It reports the storage engines, tables and state endpoints the
scan actually shows — SQLite, MySQL, Postgres, Redis, a file, an existing status
endpoint, or nothing. The choice of adapter shape follows from that report:

| Detected | What the adapter must do |
|---|---|
| SQLite | EvalScope `sqlite` checks can read it directly; the adapter may only need the chat envelope, plus a reset if tasks share the database |
| MySQL / Postgres | Checks cannot read it; `/v1/eval/state` must query it read-only |
| An existing status endpoint | Confirm it is read-only and keyed by session id, then forward it |
| A file | `json_path`/`file_*` checks may read it directly if the path is stable and under the project root |
| Nothing found | Widen the scan, or ask the user how the end state can be observed — never invent a source |

Then:

```bash
python scripts/scaffold_adapter.py --state artifacts/state.json --out <run-dir>/adapter ...
python scripts/check_adapter.py --adapter-url http://127.0.0.1:9115 --agent-url <agent URL>
```

The scaffold wires the chat proxy to the real API and leaves
`StateSource.read`/`reset` unimplemented, because only the author can see the
real schema. `check_adapter.py` is what makes the adapter trustworthy: it fails
when the state does not follow a real turn, when a fresh session inherits another
session's rows, or when reset clears the wrong scope. Without that check a wrong
adapter silently turns every end-state check into a false pass or a false fail.

The adapter is a separate process; it must not change the business code or
EvalScope core. Record its start command in `metadata.env_assumptions`.

## Request

One request per user message:

```http
POST {EVAL_AGENT_URL}/v1/eval/chat
Content-Type: application/json
```

```json
{"session_id": "task-id", "message": "客户想退 100 元，可以吗？", "system": "你是客服。"}
```

`session_id` is the eval task id, so the business side can key its audit/state
by task. A non-empty `system` is forwarded on every request; multi-turn tasks
send one user message per request.

## Response

```json
{
  "reply": "已退回 100 元，当前余额 1900 元。",
  "steps": [
    {"type": "tool", "name": "refund_recharge",
     "arguments": {"amount_cent": 10000}, "result": "ok", "elapsed_ms": 12}
  ],
  "trace_id": "trace-123",
  "state": {"balance_cent": 190000},
  "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
}
```

Rules:

- `reply` is required and must be a string.
- `steps`, `tool_calls` or `trace` is optional; when absent the run is L0
  (answer + end state) and `process` criteria become `not_evaluable`.
- A step needs `name` or `tool`; `arguments` (object) or `args` (JSON string),
  `result`, `elapsed_ms`/`duration_ms` and `type` are optional.
- `state` and `usage` are optional.
- Multi-turn tasks send one request per `user` message; assistant messages are
  not sent to this endpoint. `system` is forwarded with every request when set.

## Other protocols

- `openai_compat` calls `{base_url}/chat/completions` and records only the final
  answer (L0).
- `exec.type: http` posts the custom `request` and records only the final
  response (L0); use it for an agent that already has an evaluation endpoint.

For a separately deployed agent, pair this with the read-only state API in
[checks.md](checks.md). Do not change business logic just to return trace; a
proxy or OTel instrumentation is acceptable when the user wants L2.
