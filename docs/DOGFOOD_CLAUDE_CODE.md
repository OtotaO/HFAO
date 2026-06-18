# Dogfood: route Claude Code telemetry into a self-hosted HFAO

Claude Code natively emits OpenTelemetry, and HFAO ingests OTLP. So the highest-leverage
dogfood is to point your own Claude Code fleet at a local/self-hosted HFAO and watch your
own agent sessions land in the observatory.

This guide is grounded in what the v1.0.0 ingest plane **actually** accepts today
(`packages/hfao/ingest/`) and what Claude Code **actually** emits
([code.claude.com/docs/en/monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)).
Every claim cites a file. Where a Claude Code attribute does not yet map cleanly, that is
stated explicitly rather than glossed (see [Known mapping gaps](#known-mapping-gaps)).

---

## TL;DR

1. `docker compose up` — boots OTLP ingest on `:4318`, MCP on `:4319/mcp`, cockpit on `:7860`
   (`docker-compose.yml`).
2. In Claude Code's `settings.json`, set the `env` block below — enable telemetry and point
   the **OTLP traces** exporter at `http://localhost:4318/v1/traces`.
3. Run `claude`. Open the cockpit at `http://localhost:7860` or run `hfao query` to see your
   own sessions.

Use the **Traces (beta)** signal, not metrics or logs. Reasoning in
[Which Claude Code signal to send](#which-claude-code-signal-to-send).

---

## What HFAO's ingest plane accepts

HFAO exposes a Granian OTLP/HTTP server (`packages/hfao/ingest/server.py`):

- `POST /v1/traces` — OTLP protobuf **or** JSON. Parsed by
  `parse_traces` in `packages/hfao/ingest/otlp_http.py`, normalized by
  `normalize` in `packages/hfao/ingest/normalize.py`.
- `POST /v1/logs` — OTLP logs. Parsed by `parse_logs`, but **only log records whose name
  starts with `gen_ai.` are kept** (`otlp_http.py` line 54); everything else is dropped.
  In practice the only log records HFAO turns into data are `gen_ai.evaluation.result`
  events, which become eval `Score` rows (`normalize.normalize_scores`).
- `GET /health` — liveness.

The normalizer branches on attribute namespace (`normalize.py` line 78–82):

- any `openinference.*` attribute → OpenInference mapping (§5.3),
- else any `gen_ai.*` attribute → **OTel GenAI** mapping (§5.2),
- else → generic `SPAN`.

Claude Code's spans carry `gen_ai.system` and `gen_ai.request.model` (see below), so they
land in the **OTel GenAI** branch (`_from_otel_genai`).

### Resource / span attributes HFAO extracts

From `normalize._build_common` and `_from_otel_genai`:

| HFAO field        | Read from (first non-empty wins)                                              |
| ----------------- | ----------------------------------------------------------------------------- |
| `project_id`      | resource/span `hfao.project_id`, else `HFAO_PROJECT` config default           |
| `environment`     | `deployment.environment` (resource or span), else `hfao.environment`          |
| `release`         | `service.version`, else `hfao.release`                                         |
| `session_id`      | `session.id` → `gen_ai.conversation.id` → `a2a.context_id` → `hfao.session_id` |
| `user_id`         | `user.id` → `enduser.id`                                                       |
| `model`           | `gen_ai.response.model` → `gen_ai.request.model`                              |
| `model_parameters`| `gen_ai.request.{temperature,top_p,top_k,max_tokens,...}`                     |
| token usage       | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` / `...cache_*`     |
| tool calls        | `gen_ai.tool.name` (+ `gen_ai.tool.call.{id,arguments,result,error}`)         |

> Note: HFAO does **not** read `service.name`. Claude Code sets `service.name=claude-code`
> on every resource, but HFAO keys project isolation off `hfao.project_id` (or the server's
> `HFAO_PROJECT` default), not `service.name`. To get your Claude Code traces into a named
> project, set `hfao.project_id` via `OTEL_RESOURCE_ATTRIBUTES` (shown below) or run a
> dedicated ingest instance whose `HFAO_PROJECT` is `claude-code`.

---

## Which Claude Code signal to send

Claude Code emits **three** OTel signals (per its monitoring docs):

| Signal      | Exporter env            | HFAO endpoint that would receive it | Useful for dogfood?                                  |
| ----------- | ----------------------- | ----------------------------------- | --------------------------------------------------- |
| **Traces**  | `OTEL_TRACES_EXPORTER`  | `POST /v1/traces`                   | **Yes** — spans normalize into Observations         |
| Metrics     | `OTEL_METRICS_EXPORTER` | _none_ (HFAO has no `/v1/metrics`)  | No — HFAO does not expose a metrics endpoint        |
| Logs/events | `OTEL_LOGS_EXPORTER`    | `POST /v1/logs`                     | No — Claude Code's events are named `claude_code.*`, not `gen_ai.*`, so `parse_logs` drops them all (`otlp_http.py` line 54) |

So the dogfood path is **Traces (beta)**. Claude Code's trace spans
(`claude_code.interaction`, `claude_code.llm_request`, `claude_code.tool`, …) carry the OTel
GenAI semantic-convention attributes HFAO's `_from_otel_genai` already understands:

- `gen_ai.system` = `anthropic`
- `gen_ai.request.model` (= `model`)
- `gen_ai.response.id` (= `request_id`)
- `gen_ai.response.finish_reasons`
- `gen_ai.tool.call.id` on tool spans

Tracing is **off by default** in Claude Code and gated behind a beta flag, so the env block
below sets both `CLAUDE_CODE_ENABLE_TELEMETRY=1` and `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`.

> Metrics and logs are not wasted if you also run a general OTel collector — but they do not
> land in HFAO. If you want Claude Code's cost/token **metrics**, send them to Prometheus or a
> collector separately; HFAO derives cost/tokens from spans, not from the metrics signal.

---

## Step 1 — run HFAO's OTLP ingest locally

The single-binary Docker shape (`docker-compose.yml`) is the least-friction path. DuckDB
hot tier, SQLite control plane, in-memory ingest buffer, local body offload:

```bash
docker compose up
# ingest  → http://localhost:4318/v1/traces   (OTLP/HTTP, §5.1)
# cockpit → http://localhost:7860             (Gradio UI)
# mcp     → http://localhost:4319/mcp         (Streamable HTTP, §9.1)
```

To land Claude Code traces in a dedicated project, set `HFAO_PROJECT=claude-code` for the
`ingest` service (env block in `docker-compose.yml`), or stamp `hfao.project_id` per-trace
via `OTEL_RESOURCE_ATTRIBUTES` (Step 2). Smoke-test the endpoint is up:

```bash
curl -fsS http://localhost:4318/health    # → {"status":"ok"}
```

### Without Docker

The ingest server is a plain ASGI/Granian module:

```bash
# DuckDB hot tier + SQLite control plane (defaults from Appendix A)
export HFAO_BACKEND=duckdb
export HFAO_DUCKDB_PATH="$PWD/hfao.duckdb"
export HFAO_PROJECT=claude-code
export HFAO_INGEST_PORT=4318
hfao migrate                       # init schema (idempotent)
python -m hfao.ingest.server       # boots Granian on :4318 (server.py `_main`)
```

`hfao migrate` and `python -m hfao.ingest.server` are defined in `packages/hfao/cli.py`
and `packages/hfao/ingest/server.py` respectively.

---

## Step 2 — point Claude Code at HFAO

Claude Code is configured purely through environment variables, which you can set in your
shell or — cleaner for a fleet — in the `env` block of a Claude Code `settings.json`
(user `~/.claude/settings.json`, project `.claude/settings.json`, or an org-managed
settings file).

### Ready-to-paste `settings.json` `env` snippet

```jsonc
{
  "env": {
    // 1. Turn telemetry on, and turn on the trace beta (traces are off by default).
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",

    // 2. Send ONLY traces to HFAO. Metrics/logs have no HFAO endpoint (see table above).
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",

    // 3. OTLP/HTTP to HFAO's ingest. HFAO speaks http/protobuf and http/json on :4318.
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://localhost:4318/v1/traces",

    // 4. Tag every trace into a named HFAO project (HFAO keys on hfao.project_id,
    //    NOT service.name). Comma-separated, no spaces in values.
    "OTEL_RESOURCE_ATTRIBUTES": "hfao.project_id=claude-code,deployment.environment=dogfood",

    // 5. Faster feedback while you verify the loop (defaults: 5000ms for traces).
    "OTEL_TRACES_EXPORT_INTERVAL": "1000"
  }
}
```

Notes on each:

- **`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`** — HFAO's `parse_traces` accepts
  `application/x-protobuf`, `application/protobuf`, and `application/json`
  (`otlp_http.py` `_parse_proto`). `http/protobuf` is the most efficient of the three and is
  fully supported. `grpc` is **not** supported by the HTTP ingest server — use one of the
  HTTP protocols.
- **Per-signal endpoint** — `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` includes the `/v1/traces`
  path. If you instead set the generic `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`,
  the OTel SDK appends `/v1/traces` itself; either works.
- **`OTEL_RESOURCE_ATTRIBUTES`** — Claude Code attaches these to the OTLP resource block,
  which HFAO flattens onto every span (`otlp_http.parse_traces`) and reads via
  `normalize._ids` / `_build_common`. Setting `hfao.project_id` here is what routes your
  Claude Code traces into the `claude-code` project regardless of the server's
  `HFAO_PROJECT` default. Values cannot contain spaces.
- **Auth header (if you front HFAO with a gateway)** — add
  `"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <token>"`. The bare ingest server
  itself does not require a token on `/v1/traces`.

### Or, shell-only

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces
export OTEL_RESOURCE_ATTRIBUTES="hfao.project_id=claude-code,deployment.environment=dogfood"
claude
```

---

## Step 3 — query the captured traces

Drive a couple of Claude Code turns, then inspect what landed. The exporter batches on
`OTEL_TRACES_EXPORT_INTERVAL` (1s above), and HFAO's writer drains every ~2s
(`server.py` `_BATCH_MAX_AGE_S`), so allow a few seconds.

### No-auth surfaces (read DuckDB directly)

```bash
HFAO_PROJECT=claude-code hfao query 20     # last 20 traces as a table (cli.py `query`)
HFAO_PROJECT=claude-code hfao dashboard    # one-shot storage + ingest health
open http://localhost:7860                 # cockpit: Traces / Live tail tabs
```

`hfao query` shows trace id, op name (the span name, e.g. `claude_code.interaction`), span
count, latency, tokens, cost, status, and session id.

### MCP read surface (for Claude/Cursor to query)

The point of dogfooding is to let Claude itself query the observatory over MCP. The MCP
server (`packages/hfao/mcp_server/`) is at `http://localhost:4319/mcp` and exposes the §9.2
read tools — the ones relevant here:

- `list_traces(project, where, limit)` — `tools.py` line 85
- `get_trace(project, trace_id)` — observations + scores + causal edges, line 94
- `search_traces(project, query, limit)` — keyword search over bodies + span names, line 107
- `get_cost_by(project, group_by, window)` — cost rollup by date/user/agent/model, line 249
- `get_causal_attribution(project, trace_id)` — ranked decisive-error **hypotheses**, line 115

**Auth:** MCP calls require an API key (`Authorization: Bearer hfao_pat_...`) bound to a
workspace, and every tool enforces workspace→project isolation
(`packages/hfao/mcp_server/auth.py`, `tools.authorize_project`). There is no CLI command to
mint a PAT in v1.0.0 — create a workspace/project/key from the cockpit's Settings tab
(`http://localhost:7860`), then register HFAO as an MCP server in your client. Example for a
second Claude Code instance acting as the "analyst":

```jsonc
// .mcp.json (or "mcpServers" in settings.json) on the analyst instance
{
  "mcpServers": {
    "hfao": {
      "type": "http",
      "url": "http://localhost:4319/mcp",
      "headers": { "Authorization": "Bearer hfao_pat_..." }
    }
  }
}
```

Then ask it: _"Use the hfao tools: list the last 10 traces in project `claude-code`, then
`get_trace` on the slowest one and explain what happened."_

---

## Known mapping gaps

The dogfood works today via Traces, and the trace structure, model, project, session, and
status all normalize cleanly. Two Claude Code attribute shapes do **not** yet map onto the
v1.0.0 normalizer, so the corresponding HFAO fields stay empty for Claude Code spans. These
are documented here honestly rather than patched into the v1.0.0 ingest plane (per
`CLAUDE.md`: no §5.2 mapping change without a `SPEC.md` §16 entry and human review first):

1. **Token usage is not extracted.** `_from_otel_genai` reads tokens from
   `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` /
   `gen_ai.usage.cache_read.input_tokens` / `gen_ai.usage.cache_creation.input_tokens`
   (`normalize.py` lines 268–276). Claude Code's `claude_code.llm_request` span puts the
   same numbers in the **bare** keys `input_tokens`, `output_tokens`, `cache_read_tokens`,
   `cache_creation_tokens` (no `gen_ai.usage.` prefix). Result: `usage.total_tokens` reads
   `0` and `hfao query`/cost rollups show no tokens or cost for Claude Code traces.

2. **Observation type falls through to `SPAN`.** `_OTEL_OP_TO_TYPE`
   (`normalize.py` lines 31–39) maps `gen_ai.operation.name` → type. Claude Code's
   `claude_code.llm_request` span sets neither `gen_ai.operation.name` nor a recognized span
   name, so it normalizes as a generic `SPAN` instead of a `GENERATION`. Traces are still
   captured and queryable; only the per-observation type label is coarser.

Neither gap blocks the dogfood — sessions, span trees, models, and timing all land. They
only affect token/cost aggregation and the `GENERATION` type label for Claude Code spans.

### Proposed fix (gated on §16, do not apply blind)

A small, purely **additive** fallback in `_from_otel_genai` would close both gaps without
touching the existing OTel-GenAI / OpenInference contract:

- when `gen_ai.usage.input_tokens` is absent, fall back to bare `input_tokens` (and the
  output / cache equivalents);
- map the span name `claude_code.llm_request` → `GENERATION` when `gen_ai.operation.name`
  is absent.

Because each branch fires **only** when the canonical `gen_ai.*` key is missing, it cannot
regress any existing OTel-GenAI or OpenInference input (covered by
`tests/acceptance/test_ac_5_wire.py::test_otel_genai_chat_completion_round_trip`). It is
nonetheless a change to the §5.2 mapping, which `CLAUDE.md` governs — so it must land as a
`SPEC.md` §16 open-question entry, get human review, then ship **with** a wire-level
acceptance test asserting a Claude-Code-shaped span yields the right `type` and token usage.
Until then, this guide stays docs-only and the v1.0.0 ingest plane is untouched.
