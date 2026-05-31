# HFAO — Hugging Face Agent Observatory

> Open-source, standards-native agent observability. OpenTelemetry GenAI + OpenInference on ingest, MCP-native query surface, closed eval-trace loop in a single system.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![SPEC v1.0.0](https://img.shields.io/badge/SPEC-v1.0.0-green.svg)](SPEC.md)

HFAO is the observability backend agents debug *themselves* with. Point any framework — LangGraph, OpenAI Agents SDK, Claude Agent SDK, smolagents, CrewAI, AutoGen, DSPy, LlamaIndex, Haystack, raw `openai` / `anthropic` SDKs — at HFAO with one line, and get traces, scored evaluations, cost rollups, causal failure attribution, NL-defined monitors, and a Model Context Protocol surface every MCP client (Claude Desktop, Cursor, your own agent) can query.

```python
import hfao
hfao.init(project="my-agent")   # one line; auto-detects installed instrumentations
```

---

## The three pillars

Commercial agent-observability vendors (LangSmith, Langfuse, Phoenix, Braintrust, Weave, Helicone) all do tracing + datasets + evals + monitoring. HFAO matches them on every line item. The reason to use HFAO is **three things they cannot easily copy** (see [SPEC §1.1](SPEC.md), Q-9 resolution):

### 1. Standards-nativeness done right

Every span HFAO ingests speaks **OpenTelemetry GenAI** ([experimental semconv](https://opentelemetry.io/docs/specs/semconv/gen-ai/)) and/or **OpenInference**. No proprietary wire format. No SDK lock-in. If your agent is already emitting OTLP, you're already done — point the OTLP exporter at `http://localhost:4318/v1/traces` and HFAO normalizes both attribute namespaces into a canonical schema at ingest.

Commercial vendors hedge on this because true standards-nativeness commoditizes their backend. HFAO has no reason to hedge.

### 2. MCP-native queryability

Every observability primitive HFAO stores — traces, observations, scores, causal edges, costs, prompts, datasets — is queryable by any [MCP](https://modelcontextprotocol.io/) client. Boot up `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hfao": {
      "url": "http://localhost:4319/mcp",
      "headers": { "Authorization": "Bearer hfao_pat_..." }
    }
  }
}
```

…and Claude can now ask `list_decisive_errors`, `get_causal_attribution`, `compare_runs`, `run_eval`, `get_cost_by` over your live traces. Your agent can debug yesterday's failure the same way you can. The MCP surface is documented in [SPEC §9.2](SPEC.md).

### 3. Closed eval-trace loop

Traces → dataset items → evaluator inputs → scores → monitor triggers → traces, in **one system with one schema**, not glued across three SaaS products. A failed production trace becomes a golden-set item with one click in the cockpit (or one `hfao` CLI call). The next eval run scores against it. A regression flips a monitor. The monitor's alert links back to the trace it's protecting against.

---

## What's in the box

### Insight surfaces

| Surface | What it produces | Lives in |
|---|---|---|
| **Causal attribution** ([§8.1](SPEC.md)) | Ranked decisive-error hypotheses per failing trace with confidence + evidence + per-edge `replay_supported` flag. *Hypotheses, not verdicts.* | `hfao.compute.causal` |
| **Eval engine** ([§8.2](SPEC.md)) | 8 built-in evaluators (`exact_match`, `regex_match`, `json_schema_match`, `levenshtein_ratio`, `llm_judge`, `latency_p95`, `cost_per_call`, `tool_use_correct`) + CI gates + judge calibration | `hfao.compute.eval` |
| **Cost rollups** ([§8.3](SPEC.md)) | Daily cost-per-(user, agent, model, prompt) pivot, refreshed every 60s | `hfao.compute.cost` |
| **NL→SQL monitors** ([§8.4](SPEC.md)) | "Alert when error rate > 5% over 1h" → frozen SQL → threshold breach → webhook | `hfao.compute.monitor` |
| **Cockpit** ([§10](SPEC.md)) | Single-file Gradio UI: Home, Traces, Trace detail, Live tail, Datasets, Prompts, Evals, Annotations, Monitors, Costs, Settings, Ask HFAO | `apps/cockpit/cockpit.py` |
| **MCP server** ([§9](SPEC.md)) | FastMCP Streamable HTTP at `:4319/mcp` — every read tool + gated `score_observation` write | `hfao.mcp_server` |
| **Retention** ([§6.4](SPEC.md)) | Per-project hot-tier + body purge on a configurable cadence | `hfao.compute.retention` |

### Deployment shapes

One codebase. Three shapes per [SPEC §6.1](SPEC.md):

| Shape | Hot tier | Control plane | Warm tier |
|---|---|---|---|
| **Single-file (HF Space)** — `pip install hfao && hfao up` | DuckDB embedded | SQLite | optional HF Buckets via DuckLake |
| **Docker Compose** — `docker compose up` | ClickHouse | Postgres | HF Buckets via DuckLake |
| **Kubernetes** (Helm chart) | ClickHouse Cloud | managed Postgres | HF Buckets / S3 / R2 |

---

## Quickstart

```bash
pip install hfao              # or `uv pip install hfao`
hfao up                        # → cockpit at :7860, OTLP at :4318, MCP at :4319/mcp
```

Then in your agent code:

```python
import hfao
hfao.init(project="my-agent")

# Your existing agent code — LangGraph, OpenAI Agents SDK, Claude Agent SDK,
# smolagents, CrewAI, AutoGen, DSPy, LlamaIndex, Haystack, raw openai / anthropic.
# Every span auto-flows through the OpenInference / OTel GenAI instrumentor
# already installed for your framework.
```

The cockpit shows the trace within 2 seconds. The MCP server lets Claude/Cursor query it.

### CI integration

```bash
hfao eval run goldens --evaluators exact_match,levenshtein_ratio \
    --gate "exact_match>=0.9"
# Exits 1 if the gate fails — drop into any CI workflow.
```

### Warm-tier export

```bash
hfao parquet export ./warm --from 2026-05-01 --to 2026-05-31 \
    --hf-bucket f8n-ai/hfao-warm
```

Hourly partitions land at `hf://buckets/f8n-ai/hfao-warm/hfao/v1/events/project_id=…/year=…/month=…/day=…/hour=…/part-0.parquet`, readable from any DuckDB via the standard DuckLake catalog.

### Retention

```bash
hfao retention set my-agent --hot-days 30 --bodies-days 90
hfao retention run                  # one-shot pass; or run as a daemon worker
```

---

## Framework support

**Tier 1 — full acceptance coverage** (replay-supported per §12.2):
LangGraph · OpenAI Agents SDK · Claude Agent SDK · smolagents · raw LLM SDKs (openai · anthropic · mistral · groq · bedrock · vertex · google-genai)

**Tier 2 — generic harness path** (counterfactual replay unsupported per §12.2):
CrewAI · AutoGen · DSPy · LlamaIndex · Haystack · Pydantic AI · Google ADK · AWS Strands · LiteLLM · MCP-as-instrumentation

Tier 2 frameworks land via a [shared harness](tests/acceptance/test_ac_12_tier2_harness.py) — community PRs adding a new instrumentor extend the harness's catalog rather than writing per-framework AC tests.

---

## Architecture

```
agent code              ─OTLP/HTTP→  Granian server (:4318)  ─→  normalize  ─→  Redis Streams
                                                                                    │
                                                                                    ▼
DuckDB (single-binary) / ClickHouse (Docker / K8s)      ←──── batched writer
       │                          │                                                 │
       │                          │                                                 ▼
       ▼                          ▼                                       on-demand Parquet
HF Buckets warm tier         compute plane                                  (`hfao parquet export`,
(DuckLake catalog)        ┌──────────────────┐                              auto-sync in v1.1)
                          │ causal attribution (§8.1)
                          │ eval engine (§8.2)
                          │ cost rollups (§8.3)
                          │ monitor engine (§8.4)
                          │ retention (§6.4)
                          └──────────────────┘
                                                                                    │
                                                                                    ▼
cockpit (Gradio :7860)  ────────────────────────────────────────────────  MCP (:4319/mcp)
```

Storage is **protocol-abstracted** ([§6.2](SPEC.md)): every backend implements one `StorageBackend` protocol and no SQL is allowed outside `packages/hfao/storage/`. Swapping DuckDB → ClickHouse is a config flip, not a code change. The cockpit, MCP server, eval engine, monitor engine, retention worker all depend on the protocol, never on a concrete backend.

---

## Status

This repository is built against [SPEC.md](SPEC.md) v1.0.0. Implementation is on schedule:

| Milestone | Tag | What's done |
|---|---|---|
| **M1 — Walking skeleton** | `v0.1.0` | ✅ OTLP ingest, DuckDB hot tier, cockpit, MCP `list_traces`/`get_trace`, single-binary deploy |
| **M2 — Phase 1 feature parity + Experiment primitive** | `v0.5.0` | 🚧 Causal attribution + eval engine + cost + monitors + retention + parquet export ✅; experiment runner pending Q-10a |
| **M3 — Phase 2 differentiation** | `v1.0.0` | ⏳ Counterfactual replay, Helm chart, marquee examples |

The [§16 Open Questions](SPEC.md) ledger is the source of truth for every deviation from the original plan. Treat that file as the audit trail for "why does v1 look like this?"

---

## License

Apache-2.0. See [LICENSE](LICENSE).

The full design rationale lives in [SPEC.md](SPEC.md). Contributor onboarding: read [CLAUDE.md](CLAUDE.md) first — the "no spec deviation without a §16 entry" rule is the single most important constraint in this codebase.
