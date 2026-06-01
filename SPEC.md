# HFAO — SPEC.md

## Hugging Face Agent Observatory · Definitive build specification

| Field | Value |
| :---- | :---- |
| **Project** | HFAO (Hugging Face Agent Observatory) |
| **Organisation** | `SUM Equities` |
| **License** | Apache-2.0 |
| **Repo** | `github.com/ototao/hfao` |
| **Python package** | `hfao` |
| **TS packages** | `@hfao/sdk-ts`, `@hfao/console` |
| **Container registry** | `ghcr.io/f8n-ai/hfao-*` |
| **Reader** | Claude Code, executing autonomously |
| **Spec version** | 1.0.0 (locked; bump only on PR-approved deviation) |
| **Date** | April 2026 |

**Reader contract.** This document is the single source of truth for v1. Anywhere this document conflicts with chat history, README drafts, or external articles, **this document wins**. Where this document is silent, use the conventions referenced (OTel GenAI, OpenInference, MCP). Where conventions also disagree, prefer **OpenInference** (richer instrumentation coverage), then **OTel GenAI**, then internal HFAO defaults. Do not invent silently — if a true ambiguity surfaces, stop and post to §16 Open Questions.

---

## Table of contents

0. Meta & reader contract (above)
1. Goals & non-goals
2. Architecture
3. Repository layout
4. Data model
5. Wire protocol
6. Storage plane
7. Ingest plane
8. Computation plane (causal attribution, evals, costs, monitors)
9. MCP server (HFAO-as-MCP)
10. Cockpit (Gradio 6, single-file)
11. Console (SvelteKit, analyst surface)
12. Framework integrations
13. Auth & multi-tenancy
14. Acceptance harness (per-module tests, baked in)
15. Implementation plan (Claude Code execution order, git commits)
16. Open questions for human decision

---

## 1\. Goals & non-goals

### 1.1 Goals

1. **Match LangSmith / Langfuse / Phoenix / Braintrust / Weave / Helicone** on tracing, datasets, evals, prompts, annotation, cost, and monitoring.
2. **Exceed them on three pillars commercial competitors cannot easily copy** (per Q-9 resolution, 2026-04-19):
   - **(a) Standards-nativeness done right** — OTel GenAI \+ OpenInference on ingest, no proprietary wire format, full OTLP compatibility. Commercial vendors hedge this because it commoditizes their backend; HFAO has no reason to hedge.
   - **(b) MCP-native queryability** — every observability primitive (traces, scores, causal edges, costs, prompts, datasets, experiments) is queryable by any MCP client. The observability backend agents debug themselves with.
   - **(c) Closed eval-trace loop** — traces become dataset items become evaluator inputs become scores become monitor triggers become traces, in a single system with a single schema, not glued across three SaaS products.
   Causal attribution and the Experiment primitive (§16 Q-10) are Phase 1 features that live inside these pillars; they are not themselves the pillars.
3. **Three deployment shapes** from one codebase: single-file HF Space (DuckDB embedded), Docker Compose self-host (ClickHouse), Kubernetes enterprise (ClickHouse Cloud).
4. **OTel-native ingest from day one** — no proprietary wire format. Accept OTel GenAI experimental \+ OpenInference; emit either on export.
5. **Single-file elegance where it earns its place** (cockpit, MCP server, ingest worker), conventional engineering everywhere else.

### 1.2 Non-goals

- Infra/APM tracing (no Datadog/Grafana host-metrics overlap).
- User-product analytics (no Mixpanel overlap).
- Training-time experiment tracking (link out to Trackio; do not duplicate).
- LLM gateway / proxy (Helicone owns this; we live downstream of LiteLLM).
- Browser session recording (Laminar's niche; out of v1).
- Closed-source CI gating à la Braintrust (our advantage is OSS).

### 1.3 Definition of done for v1

- `pip install hfao && hfao up` boots a working observatory at `localhost:7860` (cockpit) \+ `localhost:4318` (OTLP/HTTP) \+ `localhost:4319/mcp` (HFAO MCP) within 20s on a laptop.
- A user instruments any framework in §12 with one line of code, runs an agent, and sees the trace in \<2s.
- A failed trace renders a ranked list of "decisive error" hypotheses with evidence, surfaced in UI and over MCP.
- Same bits, deployed via `docker compose up`, run on ClickHouse without code changes.
- All §14 acceptance tests pass green in CI.

---

## 2\. Architecture

flowchart LR

  subgraph User\["User Code"\]

    APP\[Agent App\<br/\>LangGraph / OpenAI Agents /\<br/\>Claude Agent SDK / CrewAI / smolagents / ...\]

    SDK\[hfao SDK\<br/\>OTel \+ OpenInference setup\]

    APP \--\>|imports| SDK

  end

  subgraph Ingest\["Ingest Plane"\]

    OTLP\_HTTP\["OTLP/HTTP :4318\<br/\>(Granian)"\]

    OTLP\_GRPC\["OTLP/gRPC :4317"\]

    NORM\[Normalizer\<br/\>OTel GenAI \+ OpenInference\<br/\>→ HFAO canonical\]

    REDIS\[(Redis Streams\<br/\>buffer)\]

    OTLP\_HTTP \--\> NORM

    OTLP\_GRPC \--\> NORM

    NORM \--\> REDIS

  end

  SDK \--\>|OTLP| OTLP\_HTTP

  subgraph Storage\["Storage Plane (per shape)"\]

    DUCK\[(DuckDB events\<br/\>+ DuckLake catalog)\]

    CH\[(ClickHouse events\<br/\>ReplacingMergeTree)\]

    PG\[(Postgres / SQLite\<br/\>control plane)\]

    HFB\[("HF Buckets\<br/\>Parquet warm tier")\]

    REDIS \--\> DUCK

    REDIS \--\> CH

    DUCK \--\>|hourly Parquet sync| HFB

    CH \--\>|hourly Parquet sync| HFB

  end

  subgraph Compute\["Computation Plane"\]

    CAUSAL\[Causal Attribution\<br/\>static \+ counterfactual \+ judge\]

    EVAL\[Eval Engine\<br/\>online \+ offline\]

    COST\[Cost Rollups\<br/\>materialized views\]

    MON\[Monitor/Alert Engine\]

    CAUSAL \--- DUCK

    CAUSAL \--- CH

    EVAL \--- DUCK

    EVAL \--- CH

    COST \--- DUCK

    COST \--- CH

    MON \--- DUCK

    MON \--- CH

  end

  subgraph UI\["UI Plane"\]

    COCKPIT\[Cockpit\<br/\>Gradio 6, single file\<br/\>:7860\]

    CONSOLE\[Analyst Console\<br/\>SvelteKit \+ TanStack \+ Svelte Flow\<br/\>:5173\]

    MCP\[HFAO MCP Server\<br/\>FastMCP Streamable HTTP\<br/\>:4319/mcp\]

  end

  COCKPIT \--- DUCK

  COCKPIT \--- CH

  COCKPIT \--- PG

  COCKPIT \-.-\>|gr.launch mcp\_server=True| MCP

  CONSOLE \--- DUCK

  CONSOLE \--- CH

  CONSOLE \--- PG

  CLIENT\[Claude / Cursor / any MCP client\] \--\>|MCP| MCP

  MCP \--- DUCK

  MCP \--- CH

**One-paragraph summary.** Agent code instruments itself with the standard OTel SDK plus the relevant OpenInference instrumentor, configured by a one-line `hfao.init()` call. Spans flow over OTLP/HTTP into a Granian server, get normalized into the HFAO canonical schema by msgspec, buffer in Redis Streams, and write in batches to either DuckDB (single-binary shape) or ClickHouse (Docker/K8s shape). Postgres (or SQLite for single-binary) holds control-plane state. Hourly, closed Parquet shards sync to HF Buckets via DuckLake catalogs for warm-tier retention and shareable datasets. The Gradio cockpit is the live admin UI and exposes every read tool as an MCP server. The SvelteKit console is the heavy analyst surface (100k-row tables, DAG viz, deep links). A causal-attribution worker re-processes any failed trace asynchronously and writes its hypotheses into a `causal_edges` table that both UIs and the MCP server consume.

---

## 3\. Repository layout

hfao/

├── README.md

├── SPEC.md                              \# this document

├── LICENSE                              \# Apache-2.0

├── pyproject.toml                       \# uv-managed; package \= hfao

├── uv.lock

├── docker-compose.yml                   \# Docker shape

├── docker-compose.clickhouse.yml        \# K8s/CH shape extras

├── helm/                                \# K8s shape

│   └── hfao/Chart.yaml

├── docker/

│   ├── cockpit.Dockerfile

│   ├── ingest.Dockerfile

│   ├── worker.Dockerfile

│   └── console.Dockerfile

├── packages/

│   ├── hfao/                            \# primary Python package

│   │   ├── \_\_init\_\_.py                  \# exports init(), session(), prompt()

│   │   ├── sdk/                         \# user-facing SDK

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── init.py                  \# hfao.init()

│   │   │   ├── context.py               \# session(), prompt()

│   │   │   ├── score.py                 \# hfao.score()

│   │   │   └── decorators.py            \# @hfao.observe

│   │   ├── ingest/                      \# OTLP server

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── server.py                \# Granian \+ ASGI

│   │   │   ├── otlp\_http.py             \# POST /v1/traces

│   │   │   ├── otlp\_grpc.py             \# gRPC stub

│   │   │   ├── normalize.py             \# OTel/OpenInference → canonical

│   │   │   ├── redact.py                \# PII redaction

│   │   │   └── buffer.py                \# Redis Streams

│   │   ├── storage/                     \# storage abstraction

│   │   │   ├── \_\_init\_\_.py              \# StorageBackend protocol

│   │   │   ├── duckdb\_backend.py

│   │   │   ├── clickhouse\_backend.py

│   │   │   ├── ducklake\_warm.py         \# DuckLake catalog → HF Buckets

│   │   │   ├── parquet\_sync.py          \# hourly shard sync

│   │   │   ├── ddl/

│   │   │   │   ├── duckdb.sql

│   │   │   │   └── clickhouse.sql

│   │   │   └── control\_plane.py         \# Postgres/SQLite (projects, keys, prompts, datasets)

│   │   ├── schema/                      \# canonical msgspec Structs

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── events.py

│   │   │   ├── scores.py

│   │   │   ├── prompts.py

│   │   │   ├── datasets.py

│   │   │   ├── annotations.py

│   │   │   ├── causal.py

│   │   │   └── otlp.py                  \# OTLP protobuf-shaped Structs

│   │   ├── compute/

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── causal/

│   │   │   │   ├── \_\_init\_\_.py

│   │   │   │   ├── static.py            \# Stage 1

│   │   │   │   ├── counterfactual.py    \# Stage 2 (Phase 2\)

│   │   │   │   ├── judge.py             \# Stage 3

│   │   │   │   └── pipeline.py

│   │   │   ├── eval/

│   │   │   │   ├── \_\_init\_\_.py

│   │   │   │   ├── engine.py            \# Evaluator protocol

│   │   │   │   ├── builtin.py           \# exact, regex, schema, llm\_judge

│   │   │   │   ├── calibration.py       \# judge ↔ human alignment

│   │   │   │   └── runner.py            \# online sampler \+ offline CLI

│   │   │   ├── cost.py                  \# rollup queries

│   │   │   └── monitor.py               \# NL-rule → query → alert

│   │   ├── mcp\_server/                  \# HFAO-as-MCP

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── server.py                \# FastMCP Streamable HTTP

│   │   │   ├── tools.py                 \# exact tool surface

│   │   │   └── auth.py

│   │   ├── auth/

│   │   │   ├── \_\_init\_\_.py

│   │   │   ├── hf\_oauth.py              \# HF Spaces SSO

│   │   │   ├── oidc.py                  \# generic OIDC

│   │   │   ├── api\_keys.py

│   │   │   └── rbac.py

│   │   ├── instrumentations/            \# framework-specific extras

│   │   │   ├── langgraph\_extra.py       \# checkpointer hooks

│   │   │   ├── openai\_agents\_extra.py   \# TracingProcessor

│   │   │   ├── claude\_agent\_extra.py    \# PreToolUse / PostToolUse

│   │   │   ├── adk\_extra.py

│   │   │   ├── strands\_extra.py

│   │   │   └── transformers\_agents\_extra.py

│   │   ├── cli.py                       \# \`hfao up\`, \`hfao migrate\`, \`hfao eval\`

│   │   └── config.py                    \# env-driven settings

│   └── hfao-py-tests/                   \# pytest suite

├── apps/

│   ├── cockpit/                         \# Gradio 6 single file

│   │   ├── cockpit.py                   \# THE single file

│   │   ├── components/                  \# custom Gradio components

│   │   │   ├── span\_tree.py

│   │   │   ├── trace\_chat.py

│   │   │   └── live\_tail.py

│   │   └── assets/

│   │       └── tailwind.css             \# scoped CSS for gr.HTML

│   └── console/                         \# SvelteKit (Phase 2\)

│       ├── package.json

│       ├── svelte.config.js

│       ├── vites.config.ts

│       ├── src/

│       │   ├── routes/

│       │   │   ├── \+layout.svelte

│       │   │   ├── \+page.svelte

│       │   │   └── projects/\[pid\]/

│       │   │       ├── traces/+page.svelte         \# 100k-row table

│       │   │       ├── traces/\[tid\]/+page.svelte   \# detail \+ DAG

│       │   │       ├── datasets/+page.svelte

│       │   │       ├── prompts/+page.svelte

│       │   │       ├── evals/+page.svelte

│       │   │       └── annotations/+page.svelte

│       │   ├── lib/

│       │   │   ├── api/                  \# OpenAPI-generated client

│       │   │   ├── components/

│       │   │   │   ├── TraceTable.svelte \# TanStack Table

│       │   │   │   ├── DAG.svelte        \# Svelte Flow

│       │   │   │   ├── DiffView.svelte   \# Monaco

│       │   │   │   └── FacetBar.svelte

│       │   │   ├── stores/

│       │   │   └── duckdb/               \# DuckDB-WASM faceting

│       └── tests/                        \# Playwright

├── examples/                            \# one Space per integration

│   ├── smolagents-quickstart/

│   ├── crewai-research-team/

│   ├── openai-agents-handoff/

│   ├── claude-agent-coder/

│   ├── langgraph-rag/

│   └── multi-framework-a2a/             \# the marquee causal-attribution demo

├── tests/

│   ├── conftest.py

│   ├── acceptance/                      \# one file per §14 module

│   └── perf/

├── scripts/

│   ├── seed\_demo.py                     \# generates fake traces

│   ├── verify\_otlp.py                   \# OTel collector smoke

│   └── ducklake\_to\_hfbuckets.py

└── .github/workflows/

    ├── test.yml

    ├── docker-publish.yml

    └── space-deploy.yml                 \# one-click HF Space updates

**Claude Code instruction.** Create the tree exactly. Do not introduce additional top-level directories. Do not collapse `packages/hfao` into a flat layout — the `packages/` shell exists so a future `packages/hfao-rs` (Rust workers, if profile demands) drops in cleanly.

---

## 4\. Data model

### 4.1 Canonical msgspec Structs

All ingest and storage goes through the canonical schema. OTel/OpenInference attribute names are translated **at ingest** by the normalizer (§5); downstream code only sees the canonical names.

`packages/hfao/schema/events.py`:

from \_\_future\_\_ import annotations

from typing import Literal

from msgspec import Struct, field

from datetime import datetime

ObservationType \= Literal\[

    "AGENT", "GENERATION", "TOOL", "RETRIEVAL",

    "EMBEDDING", "EVAL", "GUARDRAIL", "HANDOFF", "SPAN", "EVENT"

\]

Status \= Literal\["ok", "error", "unset"\]

Level \= Literal\["DEFAULT", "DEBUG", "WARNING", "ERROR"\]

class TokenUsage(Struct, frozen=True, kw\_only=True):

    prompt\_tokens: int \= 0

    completion\_tokens: int \= 0

    cache\_read\_tokens: int \= 0

    cache\_creation\_tokens: int \= 0

    total\_tokens: int \= 0

class CostBreakdown(Struct, frozen=True, kw\_only=True):

    input\_cost\_usd: float \= 0.0

    output\_cost\_usd: float \= 0.0

    total\_cost\_usd: float \= 0.0

class ToolCall(Struct, frozen=True, kw\_only=True):

    id: str

    name: str

    arguments: str          \# JSON string; keep as string at storage layer

    result: str | None \= None

    error: str | None \= None

class Observation(Struct, kw\_only=True):

    \# Identity

    project\_id: str

    trace\_id: str

    observation\_id: str

    parent\_observation\_id: str | None \= None

    \# Routing / context

    session\_id: str | None \= None      \# OpenInference session.id / OTel gen\_ai.conversation.id / A2A contextId

    user\_id: str | None \= None

    environment: str \= "production"

    release: str | None \= None

    \# What

    name: str

    type: ObservationType

    level: Level \= "DEFAULT"

    \# When

    start\_time: datetime

    end\_time: datetime | None \= None

    duration\_ms: int | None \= None

    \# Status

    status: Status \= "unset"

    status\_message: str | None \= None

    \# Payload (large bodies offloaded; see §6)

    input: str | None \= None              \# JSON string OR pointer ref

    output: str | None \= None             \# JSON string OR pointer ref

    input\_ref: str | None \= None          \# s3://... if offloaded

    output\_ref: str | None \= None

    \# Generation-specific

    model: str | None \= None

    model\_parameters: dict\[str, str\] \= field(default\_factory=dict)

    usage: TokenUsage \= field(default\_factory=TokenUsage)

    cost: CostBreakdown \= field(default\_factory=CostBreakdown)

    \# Tool-specific

    tool\_definitions: dict\[str, str\] \= field(default\_factory=dict)

    tool\_calls: list\[ToolCall\] \= field(default\_factory=list)

    tool\_call\_names: list\[str\] \= field(default\_factory=list)

    \# Agent-specific

    agent\_id: str | None \= None

    agent\_role: str | None \= None

    handoff\_target\_agent\_id: str | None \= None

    \# Prompt linkage

    prompt\_name: str | None \= None

    prompt\_version: int | None \= None

    prompt\_label: str | None \= None

    \# Free-form

    metadata: dict\[str, str\] \= field(default\_factory=dict)

    tags: list\[str\] \= field(default\_factory=list)

    \# Bookkeeping

    event\_version: int \= 1

    ingested\_at: datetime

`packages/hfao/schema/scores.py`:

class Score(Struct, kw\_only=True):

    project\_id: str

    trace\_id: str

    observation\_id: str | None \= None

    name: str

    value: float | None \= None

    string\_value: str | None \= None

    source: Literal\["ANNOTATION", "LLM\_JUDGE", "HEURISTIC", "EXTERNAL"\]

    comment: str | None \= None

    judge\_model: str | None \= None

    calibration\_bias: float \= 0.0

    timestamp: datetime

    annotator\_id: str | None \= None

    eval\_run\_id: str | None \= None

`packages/hfao/schema/causal.py` (HFAO-unique):

EdgeType \= Literal\[

    "DATAFLOW", "HANDOFF", "TOOL\_DEPENDENCY",

    "PROMPT\_CONDITIONING", "DECISIVE\_ERROR",

\]

Method \= Literal\["STATIC", "COUNTERFACTUAL\_REPLAY", "LLM\_JUDGE", "SPECTRUM"\]

class CausalEdge(Struct, kw\_only=True):

    project\_id: str

    trace\_id: str

    source\_observation\_id: str

    target\_observation\_id: str

    edge\_type: EdgeType

    confidence: float                       \# 0.0 – 1.0

    method: Method

    evidence: str                           \# human-readable explanation

    replay\_supported: bool                  \# FRAMEWORK SUPPORTS COUNTERFACTUAL?

    judge\_model: str | None \= None

    computed\_at: datetime

`packages/hfao/schema/prompts.py`:

class PromptVersion(Struct, kw\_only=True):

    project\_id: str

    name: str

    version: int                            \# immutable, monotonic

    content: str

    config: dict\[str, str\] \= field(default\_factory=dict)

    type: Literal\["text", "chat"\]

    created\_at: datetime

    created\_by: str

    commit\_message: str | None \= None

class PromptLabel(Struct, kw\_only=True):    \# mutable label → version pointer

    project\_id: str

    name: str

    label: str                              \# "production", "staging", custom

    version: int

    updated\_at: datetime

`packages/hfao/schema/datasets.py`:

class Dataset(Struct, kw\_only=True):

    project\_id: str

    id: str

    name: str

    description: str | None \= None

    created\_at: datetime

class DatasetItem(Struct, kw\_only=True):

    project\_id: str

    dataset\_id: str

    id: str

    input: str

    expected\_output: str | None \= None

    metadata: dict\[str, str\] \= field(default\_factory=dict)

    source\_trace\_id: str | None \= None

    source\_observation\_id: str | None \= None

    created\_at: datetime

`packages/hfao/schema/annotations.py`:

class AnnotationQueue(Struct, kw\_only=True):

    project\_id: str

    id: str

    name: str

    filter\_query: str                       \# SQL WHERE clause; auto-routes new traces

    score\_schema: list\[str\]

    created\_at: datetime

class AnnotationItem(Struct, kw\_only=True):

    queue\_id: str

    trace\_id: str

    observation\_id: str | None \= None

    assigned\_to: str | None \= None

    status: Literal\["pending", "in\_progress", "completed", "skipped"\]

    completed\_at: datetime | None \= None

### 4.2 DuckDB DDL (single-binary shape)

`packages/hfao/storage/ddl/duckdb.sql`:

CREATE TABLE IF NOT EXISTS events (

  project\_id              VARCHAR NOT NULL,

  trace\_id                VARCHAR NOT NULL,

  observation\_id          VARCHAR NOT NULL,

  parent\_observation\_id   VARCHAR,

  session\_id              VARCHAR,

  user\_id                 VARCHAR,

  environment             VARCHAR DEFAULT 'production',

  release                 VARCHAR,

  name                    VARCHAR NOT NULL,

  type                    VARCHAR NOT NULL,

  level                   VARCHAR DEFAULT 'DEFAULT',

  start\_time              TIMESTAMP NOT NULL,

  end\_time                TIMESTAMP,

  duration\_ms             INTEGER,

  status                  VARCHAR DEFAULT 'unset',

  status\_message          VARCHAR,

  input                   VARCHAR,

  output                  VARCHAR,

  input\_ref               VARCHAR,

  output\_ref              VARCHAR,

  model                   VARCHAR,

  model\_parameters        MAP(VARCHAR, VARCHAR),

  prompt\_tokens           INTEGER DEFAULT 0,

  completion\_tokens       INTEGER DEFAULT 0,

  cache\_read\_tokens       INTEGER DEFAULT 0,

  cache\_creation\_tokens   INTEGER DEFAULT 0,

  total\_tokens            INTEGER DEFAULT 0,

  input\_cost\_usd          DOUBLE DEFAULT 0,

  output\_cost\_usd         DOUBLE DEFAULT 0,

  total\_cost\_usd          DOUBLE DEFAULT 0,

  tool\_definitions        MAP(VARCHAR, VARCHAR),

  tool\_calls              VARCHAR\[\],

  tool\_call\_names         VARCHAR\[\],

  agent\_id                VARCHAR,

  agent\_role              VARCHAR,

  handoff\_target\_agent\_id VARCHAR,

  prompt\_name             VARCHAR,

  prompt\_version          INTEGER,

  prompt\_label            VARCHAR,

  metadata                MAP(VARCHAR, VARCHAR),

  tags                    VARCHAR\[\],

  event\_version           BIGINT NOT NULL,

  ingested\_at             TIMESTAMP NOT NULL,

  PRIMARY KEY (project\_id, trace\_id, observation\_id, event\_version)

);

CREATE INDEX IF NOT EXISTS events\_by\_time     ON events(project\_id, start\_time);

CREATE INDEX IF NOT EXISTS events\_by\_session  ON events(project\_id, session\_id);

CREATE INDEX IF NOT EXISTS events\_by\_user     ON events(project\_id, user\_id);

CREATE INDEX IF NOT EXISTS events\_by\_trace    ON events(project\_id, trace\_id);

CREATE INDEX IF NOT EXISTS events\_by\_status   ON events(project\_id, status, start\_time);

CREATE INDEX IF NOT EXISTS events\_by\_model    ON events(project\_id, model);

CREATE OR REPLACE VIEW events\_current AS

SELECT \* FROM (

  SELECT \*, ROW\_NUMBER() OVER (PARTITION BY project\_id, trace\_id, observation\_id ORDER BY event\_version DESC) AS rn

  FROM events

) WHERE rn \= 1;

CREATE TABLE IF NOT EXISTS scores (

  project\_id        VARCHAR NOT NULL,

  trace\_id          VARCHAR NOT NULL,

  observation\_id    VARCHAR,

  name              VARCHAR NOT NULL,

  value             DOUBLE,

  string\_value      VARCHAR,

  source            VARCHAR NOT NULL,

  comment           VARCHAR,

  judge\_model       VARCHAR,

  calibration\_bias  DOUBLE DEFAULT 0,

  timestamp         TIMESTAMP NOT NULL,

  annotator\_id      VARCHAR,

  eval\_run\_id       VARCHAR,

  PRIMARY KEY (project\_id, trace\_id, COALESCE(observation\_id,''), name, timestamp)

);

CREATE TABLE IF NOT EXISTS causal\_edges (

  project\_id              VARCHAR NOT NULL,

  trace\_id                VARCHAR NOT NULL,

  source\_observation\_id   VARCHAR NOT NULL,

  target\_observation\_id   VARCHAR NOT NULL,

  edge\_type               VARCHAR NOT NULL,

  confidence              DOUBLE NOT NULL,

  method                  VARCHAR NOT NULL,

  evidence                VARCHAR,

  replay\_supported        BOOLEAN NOT NULL,

  judge\_model             VARCHAR,

  computed\_at             TIMESTAMP NOT NULL,

  PRIMARY KEY (project\_id, trace\_id, source\_observation\_id, target\_observation\_id, method)

);

CREATE TABLE IF NOT EXISTS cost\_daily (

  project\_id  VARCHAR NOT NULL,

  date        DATE NOT NULL,

  user\_id     VARCHAR,

  agent\_id    VARCHAR,

  model       VARCHAR,

  prompt\_name VARCHAR,

  total\_cost\_usd DOUBLE,

  total\_tokens   BIGINT,

  call\_count     BIGINT,

  PRIMARY KEY (project\_id, date, COALESCE(user\_id,''), COALESCE(agent\_id,''), COALESCE(model,''), COALESCE(prompt\_name,''))

);

### 4.3 ClickHouse DDL (Docker / K8s shape)

`packages/hfao/storage/ddl/clickhouse.sql`:

CREATE TABLE IF NOT EXISTS events (

  project\_id              LowCardinality(String),

  trace\_id                String,

  observation\_id          String,

  parent\_observation\_id   String,

  session\_id              String,

  user\_id                 String,

  environment             LowCardinality(String) DEFAULT 'production',

  release                 LowCardinality(String),

  name                    String,

  type                    LowCardinality(String),

  level                   LowCardinality(String) DEFAULT 'DEFAULT',

  start\_time              DateTime64(3, 'UTC'),

  end\_time                Nullable(DateTime64(3, 'UTC')),

  duration\_ms             UInt32 DEFAULT 0,

  status                  LowCardinality(String) DEFAULT 'unset',

  status\_message          String,

  input                   String CODEC(ZSTD(3)),

  output                  String CODEC(ZSTD(3)),

  input\_ref               String,

  output\_ref              String,

  model                   LowCardinality(String),

  model\_parameters        Map(LowCardinality(String), String),

  prompt\_tokens           UInt32 DEFAULT 0,

  completion\_tokens       UInt32 DEFAULT 0,

  cache\_read\_tokens       UInt32 DEFAULT 0,

  cache\_creation\_tokens   UInt32 DEFAULT 0,

  total\_tokens            UInt32 DEFAULT 0,

  input\_cost\_usd          Float64 DEFAULT 0,

  output\_cost\_usd         Float64 DEFAULT 0,

  total\_cost\_usd          Float64 DEFAULT 0,

  tool\_definitions        Map(LowCardinality(String), String),

  tool\_calls              Array(String),

  tool\_call\_names         Array(LowCardinality(String)),

  agent\_id                LowCardinality(String),

  agent\_role              LowCardinality(String),

  handoff\_target\_agent\_id LowCardinality(String),

  prompt\_name             LowCardinality(String),

  prompt\_version          UInt32,

  prompt\_label            LowCardinality(String),

  metadata                Map(LowCardinality(String), String),

  tags                    Array(LowCardinality(String)),

  event\_version           UInt64,

  ingested\_at             DateTime64(3, 'UTC'),

  INDEX idx\_session    (project\_id, session\_id) TYPE bloom\_filter GRANULARITY 1,

  INDEX idx\_user       (project\_id, user\_id)    TYPE bloom\_filter GRANULARITY 1,

  INDEX idx\_status     (status)                 TYPE set(8)       GRANULARITY 1,

  INDEX idx\_tools      tool\_call\_names          TYPE bloom\_filter GRANULARITY 1,

  INDEX idx\_tags       tags                     TYPE bloom\_filter GRANULARITY 1,

  INDEX idx\_agent      (project\_id, agent\_id)   TYPE bloom\_filter GRANULARITY 1

)

ENGINE \= ReplacingMergeTree(event\_version)

PARTITION BY toYYYYMM(start\_time)

ORDER BY (project\_id, toStartOfHour(start\_time), trace\_id, observation\_id)

SETTINGS index\_granularity \= 8192;

CREATE TABLE IF NOT EXISTS scores (

  project\_id       LowCardinality(String),

  trace\_id         String,

  observation\_id   String,

  name             LowCardinality(String),

  value            Nullable(Float64),

  string\_value     String,

  source           LowCardinality(String),

  comment          String,

  judge\_model      LowCardinality(String),

  calibration\_bias Float32 DEFAULT 0,

  timestamp        DateTime64(3, 'UTC'),

  annotator\_id     String,

  eval\_run\_id      String,

  event\_version    UInt64

)

ENGINE \= ReplacingMergeTree(event\_version)

PARTITION BY toYYYYMM(timestamp)

ORDER BY (project\_id, toStartOfHour(timestamp), trace\_id, observation\_id, name);

CREATE TABLE IF NOT EXISTS causal\_edges (

  project\_id            LowCardinality(String),

  trace\_id              String,

  source\_observation\_id String,

  target\_observation\_id String,

  edge\_type             LowCardinality(String),

  confidence            Float32,

  method                LowCardinality(String),

  evidence              String,

  replay\_supported      Bool,

  judge\_model           LowCardinality(String),

  computed\_at           DateTime64(3, 'UTC'),

  event\_version         UInt64

)

ENGINE \= ReplacingMergeTree(event\_version)

PARTITION BY toYYYYMM(computed\_at)

ORDER BY (project\_id, trace\_id, source\_observation\_id, target\_observation\_id, method);

CREATE MATERIALIZED VIEW IF NOT EXISTS cost\_daily\_mv

ENGINE \= SummingMergeTree

PARTITION BY toYYYYMM(date)

ORDER BY (project\_id, date, user\_id, agent\_id, model, prompt\_name)

AS SELECT

  project\_id,

  toDate(start\_time) AS date,

  user\_id,

  agent\_id,

  model,

  prompt\_name,

  sum(total\_cost\_usd) AS total\_cost\_usd,

  sum(total\_tokens)   AS total\_tokens,

  count()             AS call\_count

FROM events

GROUP BY project\_id, date, user\_id, agent\_id, model, prompt\_name;

### 4.4 Parquet schema (warm tier on HF Buckets via DuckLake)

Hourly closed-partition Parquet shards mirror the canonical schema 1:1. Partition path:

hf://buckets/{org}/{bucket}/hfao/v1/events/

  project\_id={project\_id}/year={YYYY}/month={MM}/day={DD}/hour={HH}/

    part-{shard\_id}.parquet

DuckLake catalog: `hf://buckets/{org}/{bucket}/hfao/v1/_catalog/ducklake.duckdb`

Read-back from any DuckDB:

ATTACH 'ducklake:hf://buckets/{org}/{bucket}/hfao/v1/\_catalog/ducklake.duckdb' AS warm;

SELECT count() FROM warm.events WHERE project\_id \= '...' AND start\_time \> now() \- INTERVAL 30 DAY;

### 4.5 Cross-store invariants

1. **Schema name parity.** Every column name is identical across DuckDB, ClickHouse, and Parquet. Type widening rules: DuckDB `INTEGER` ≡ CH `UInt32` ≡ Parquet `INT32`. DuckDB `BIGINT` ≡ CH `UInt64` ≡ Parquet `INT64`. DuckDB `DOUBLE` ≡ CH `Float64` ≡ Parquet `DOUBLE`.
2. **Dedup.** ClickHouse uses `ReplacingMergeTree(event_version)`. DuckDB uses `events_current` view. Parquet shards are write-once; updates produce a new shard with bumped `event_version`.
3. **Body offload.** Any `input`/`output` body \>64KB at ingest is offloaded; columns store empty string; `*_ref` stores the URI.
4. **No `NULL` in primary key columns** — use empty string sentinels.

### 4.6 §4 acceptance criteria

\# tests/acceptance/test\_ac\_4\_data\_model.py

def test\_msgspec\_struct\_roundtrip(): ...

def test\_duckdb\_ddl\_applies\_clean(tmp\_path): ...

def test\_clickhouse\_ddl\_applies\_clean(ch): ...

def test\_parquet\_schema\_matches\_struct(): ...

def test\_event\_version\_dedup\_duckdb(): ...

def test\_event\_version\_dedup\_clickhouse(): ...

def test\_body\_offload\_at\_64kb(): ...

def test\_no\_null\_in\_pk(): ...

All eight pass → §4 done. Commit: `feat(schema): canonical msgspec + DuckDB + ClickHouse + Parquet DDL`.

---

## 5\. Wire protocol

### 5.1 OTLP endpoints

- `POST /v1/traces` (OTLP/HTTP, protobuf body, `Content-Type: application/x-protobuf`).
- `POST /v1/traces` with JSON body (`Content-Type: application/json`) — accepted for SDK convenience.
- gRPC `:4317` — exposed but optional; HTTP is the supported path for v1.
- `POST /v1/logs` — accepted; logs containing `gen_ai.*` event names (`gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.tool.message`, `gen_ai.choice`, `gen_ai.evaluation.result`) are merged into the parent span's `input`/`output`/`scores`.

### 5.2 OTel GenAI → canonical mapping

The normalizer reads the gate `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` for upstream stability semantics but **always accepts** experimental shapes.

| OTel GenAI attribute | Canonical field |
| :---- | :---- |
| `gen_ai.system` | `model_parameters["system"]` |
| `gen_ai.operation.name` | `name` (also drives `type` mapping below) |
| `gen_ai.request.model` | `model` |
| `gen_ai.response.model` | `model` (overwrites if both present) |
| `gen_ai.request.temperature` / `top_p` / `max_tokens` / `stop_sequences` | `model_parameters["..."]` |
| `gen_ai.usage.input_tokens` | `usage.prompt_tokens` |
| `gen_ai.usage.output_tokens` | `usage.completion_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `usage.cache_read_tokens` |
| `gen_ai.usage.cache_creation.input_tokens` | `usage.cache_creation_tokens` |
| `gen_ai.conversation.id` | `session_id` |
| `gen_ai.agent.id` / `gen_ai.agent.name` | `agent_id` / `agent_role` |
| `gen_ai.tool.name` / `gen_ai.tool.call.id` | populates a `ToolCall` in `tool_calls` |
| `gen_ai.input.messages` (JSON) | `input` |
| `gen_ai.output.messages` (JSON) | `output` |
| Span event `gen_ai.evaluation.result` | row in `scores` |

`type` derivation:

- `create_agent` → `AGENT`
- `invoke_agent` → `AGENT`
- `chat` / `text_completion` / `generate_content` → `GENERATION`
- `embeddings` → `EMBEDDING`
- `execute_tool` → `TOOL`

### 5.3 OpenInference → canonical mapping

| OpenInference attribute | Canonical field |
| :---- | :---- |
| `openinference.span.kind` | `type` (translate per table below) |
| `input.value` / `input.mime_type` | `input` |
| `output.value` / `output.mime_type` | `output` |
| `llm.model_name` | `model` |
| `llm.invocation_parameters` (JSON) | `model_parameters` |
| `llm.token_count.prompt` / `.completion` / `.total` | `usage.*` |
| `llm.input_messages.{i}.message.{role,content,tool_calls}` | reconstructed into `input` |
| `llm.output_messages.{i}.*` | reconstructed into `output` |
| `tool.name` / `tool.parameters` | `ToolCall` in `tool_calls` |
| `session.id` | `session_id` |
| `user.id` | `user_id` |
| `tag.tags` | `tags` |
| `metadata` | `metadata` (flattened to string→string) |

Span-kind translation: `LLM`→`GENERATION`, `CHAIN`→`SPAN`, `RETRIEVER`→`RETRIEVAL`, `EMBEDDING`→`EMBEDDING`, `AGENT`→`AGENT`, `TOOL`→`TOOL`, `RERANKER`→`RETRIEVAL` (with metadata flag), `EVALUATOR`→`EVAL`, `GUARDRAIL`→`GUARDRAIL`.

### 5.4 MCP `_meta` context propagation

HFAO injects/extracts W3C `traceparent` in MCP `_meta`:

{

  "\_meta": {

    "traceparent": "00-{trace\_id}-{span\_id}-01",

    "tracestate": "...",

    "hfao.session\_id": "{session\_id}",

    "hfao.project\_id": "{project\_id}"

  }

}

Server: extract on `CallToolRequest`, reparent the local span. Client: inject before send. Library: `packages/hfao/sdk/init.py` patches the official `mcp` SDK `Client.call_tool` and `Server.tool` decorator at `hfao.init()` time. Prefer the OpenInference `openinference-instrumentation-mcp` package when installed; HFAO's patch is a fallback.

### 5.5 A2A `contextId` mapping

- `a2a.context_id` → canonical `session_id` (and OTel `gen_ai.conversation.id`).
- `a2a.task_id` → canonical metadata `metadata["a2a.task_id"]`, and root-span attribute.
- `a2a.agent_card_url` → canonical metadata `metadata["a2a.agent_card_url"]` for DAG hover-card.

### 5.6 Normalizer pseudocode

`packages/hfao/ingest/normalize.py`:

def normalize(otlp\_span: ResourceSpan) \-\> list\[Observation\]:

    """One OTLP span → 1+ canonical Observations.

    \- 1:1 in the common case.

    \- 1:N if a span carries multiple events that should split out (rare).

    """

    project\_id \= resolve\_project(otlp\_span.resource\_attributes)

    common \= build\_common\_fields(otlp\_span)

    if has\_attr(otlp\_span, "openinference.span.kind"):

        return \[from\_openinference(otlp\_span, common)\]

    elif has\_attr\_prefix(otlp\_span, "gen\_ai."):

        return \[from\_otel\_genai(otlp\_span, common)\]

    else:

        return \[from\_generic(otlp\_span, common)\]

The `build_common_fields` step also resolves MCP `_meta` and A2A attributes if present.

### 5.7 §5 acceptance criteria

\# tests/acceptance/test\_ac\_5\_wire.py

def test\_otlp\_http\_protobuf\_accept(): ...

def test\_otlp\_http\_json\_accept(): ...

def test\_otel\_genai\_chat\_completion\_round\_trip(): ...

def test\_openinference\_llm\_round\_trip(): ...

def test\_mcp\_meta\_traceparent\_extracted(): ...

def test\_a2a\_context\_id\_becomes\_session(): ...

def test\_unknown\_span\_falls\_through\_as\_SPAN(): ...

def test\_log\_event\_evaluation\_becomes\_score(): ...

Commit: `feat(ingest): OTLP endpoints + dual-emit normalizer (OTel GenAI + OpenInference)`.

---

## 6\. Storage plane

### 6.1 Storage matrix per deployment shape

| Shape | Hot tier | Control plane | Warm tier | Notes |
| :---- | :---- | :---- | :---- | :---- |
| **Single-file (HF Space)** | DuckDB embedded (`/data/hfao.duckdb`) | SQLite (`/data/control.db`) | optional HF Buckets via DuckLake | Default v1 ship; this **is** the existing `cockpit.py` evolved |
| **Docker Compose** | ClickHouse | Postgres | HF Buckets via DuckLake | Default for self-host |
| **Kubernetes** | ClickHouse Cloud or CH Operator | managed Postgres | HF Buckets / S3 / R2 via DuckLake | Helm chart |

**Rule.** `StorageBackend` protocol in `storage/__init__.py` is the boundary. `cockpit.py`, `ingest/server.py`, MCP tools, eval engine all depend on the protocol; concrete backends are pluggable. **No SQL string is allowed outside `storage/`.**

### 6.2 `StorageBackend` protocol

\# packages/hfao/storage/\_\_init\_\_.py

from typing import Protocol, Iterable

from datetime import datetime

from hfao.schema.events import Observation

from hfao.schema.scores import Score

from hfao.schema.causal import CausalEdge

class StorageBackend(Protocol):

    def init\_schema(self) \-\> None: ...

    def write\_events(self, events: Iterable\[Observation\]) \-\> int: ...

    def write\_scores(self, scores: Iterable\[Score\]) \-\> int: ...

    def write\_causal\_edges(self, edges: Iterable\[CausalEdge\]) \-\> int: ...

    def get\_trace(self, project\_id: str, trace\_id: str) \-\> list\[Observation\]: ...

    def list\_traces(self, project\_id: str, \*, where\_sql: str \= "1=1",

                    limit: int \= 50, offset: int \= 0\) \-\> list\[dict\]: ...

    def search\_traces\_text(self, project\_id: str, query: str, limit: int \= 50\) \-\> list\[dict\]: ...

    def get\_causal\_edges(self, project\_id: str, trace\_id: str) \-\> list\[CausalEdge\]: ...

    def get\_scores(self, project\_id: str, trace\_id: str) \-\> list\[Score\]: ...

    def cost\_rollup(self, project\_id: str, \*, date\_from: datetime,

                    date\_to: datetime, group\_by: list\[str\]) \-\> list\[dict\]: ...

    def execute\_readonly\_sql(self, project\_id: str, sql: str) \-\> list\[dict\]:

        """For monitor/alert engine and console SQL playground.

        MUST enforce project\_id scoping by query rewrite."""

Both `DuckDBBackend` and `ClickHouseBackend` implement this. Test parity: `tests/integration/test_backend_parity.py` runs the same suite against both with `pytest.mark.parametrize("backend", ["duckdb", "clickhouse"])`.

### 6.3 DuckLake warm-tier sync

Worker `storage/parquet_sync.py` runs every hour on a cron tick:

1. For each closed hour partition (older than `now() - 1h`):
   - DuckDB: `COPY (SELECT * FROM events_current WHERE start_time >= ? AND start_time < ?) TO 'hf://...' (FORMAT PARQUET)` via `huggingface_hub` fsspec.
   - ClickHouse: `SELECT * FROM events FINAL WHERE start_time >= ? AND start_time < ? FORMAT Parquet` then upload via `huggingface_hub.upload_file` (single multipart call per shard).
2. Register the new shard in the DuckLake catalog: `INSERT INTO __ducklake_data_files VALUES (...)`.
3. Verify shard count and row count via `SELECT count() FROM warm.events WHERE start_time >= ?`.
4. **Do not** delete the hot rows in v1 (retention §6.4 handles that separately).

### 6.4 Retention policy

Per-project, configured in control plane:

class RetentionPolicy(Struct):

    project\_id: str

    hot\_days: int \= 30          \# delete from hot tier after N days

    warm\_days: int \= 365        \# delete Parquet shards from HF Bucket after N days

    bodies\_days: int \= 90       \# purge offloaded body refs after N days

Enforced by a daily cron job (`worker.retention.run`).

### 6.5 PII redaction

Two layers, both configurable per project:

1. **Pre-validation regex** in `ingest/redact.py` — built-in patterns for emails, phone numbers (E.164), credit cards, SSN, AWS keys, Anthropic/OpenAI API keys, IP addresses. Replace with `[REDACTED:{kind}]`. Always-on.
2. **Microsoft Presidio** (optional dependency, install with `pip install hfao[presidio]`) — runs after regex; uses spaCy NER. Off by default.

class RedactionConfig(Struct):

    enabled: bool \= True

    builtin\_kinds: set\[str\] \= {"email","phone","cc","ssn","aws\_key","anthropic\_key","openai\_key"}

    use\_presidio: bool \= False

    presidio\_entities: list\[str\] \= \["PERSON","LOCATION"\]

    hash\_only: bool \= False     \# if True, store SHA256(value) instead of redacted value

### 6.6 Body offload

`input`/`output` \>64KB at ingest:

- Single-file shape: write to `/data/bodies/{project_id}/{trace_id}/{observation_id}.{io}.json.zst`.
- Docker shape: write to local MinIO bucket `s3://hfao-bodies/{project_id}/...`.
- K8s shape: write to configured S3/R2/HF Bucket.

`input_ref`/`output_ref` stores the URI. The cockpit and console fetch on-demand with a 1MB cap; longer bodies stream.

### 6.7 §6 acceptance criteria

\# tests/acceptance/test\_ac\_6\_storage.py

def test\_backend\_parity\_duckdb\_clickhouse(backend): ...       \# parametrized

def test\_ducklake\_sync\_round\_trip(tmp\_bucket): ...

def test\_retention\_purges\_old\_rows(): ...

def test\_redaction\_regex\_email(): ...

def test\_redaction\_presidio\_optional(): ...

def test\_body\_offload\_at\_64kb(): ...

def test\_readonly\_sql\_rejects\_writes(): ...

def test\_readonly\_sql\_enforces\_project\_scope(): ...

Commit: `feat(storage): DuckDB + ClickHouse backends with DuckLake warm tier`.

---

## 7\. Ingest plane

### 7.1 Server

`packages/hfao/ingest/server.py`:

def serve(config: HFAOConfig) \-\> None:

    """Granian-fronted ASGI app exposing OTLP/HTTP, OTLP/gRPC (optional),

    and a /health endpoint. Reads from a Redis Streams (or in-memory in

    single-binary mode) buffer and writes to the configured StorageBackend.

    Defaults:

      \- bind: 0.0.0.0:4318  (HTTP)

      \- workers: cpu\_count()

      \- max body size: 4 MiB

    """

In single-binary shape, the buffer is a `collections.deque` with backpressure; in Docker/K8s, Redis Streams.

### 7.2 Batching & flush

- Batch size: 10,000 observations or 32 MiB serialized, whichever first.
- Max batch age: 2 seconds.
- On batch failure: retry 3× with exponential backoff (100ms, 500ms, 2s); then write to `dead-letter/` directory (single-binary) or Redis stream `hfao:dlq` (Docker/K8s).

### 7.3 Backpressure

When the buffer is \>80% full:

- Return HTTP 429 with `Retry-After: 1` header.
- Increment `hfao_ingest_backpressure_total{project_id="..."}` Prometheus counter.

### 7.4 Schema versioning

Every observation carries `event_version` (monotonic per `(trace_id, observation_id)`). Re-ingest with higher version overwrites. The normalizer assigns versions from a per-process counter seeded at start.

### 7.5 §7 acceptance criteria

\# tests/acceptance/test\_ac\_7\_ingest.py

def test\_otlp\_http\_under\_load\_500\_rps(): ...        \# sustained 5min

def test\_429\_when\_buffer\_full(): ...

def test\_dlq\_on\_persistent\_storage\_failure(): ...

def test\_batch\_flush\_at\_size(): ...

def test\_batch\_flush\_at\_age(): ...

def test\_event\_version\_monotonic(): ...

Commit: `feat(ingest): Granian server + batched writer + backpressure`.

---

## 8\. Computation plane

### 8.1 Causal attribution pipeline

**Phase split.** Stage 1 (static) \+ Stage 3 (LLM-judge) ship in Phase 1\. Stage 2 (counterfactual replay) ships in Phase 2 (weeks 8–14). The `causal_edges` schema and pipeline scaffolding land in Phase 1; Stage 2 just isn't wired in yet. **The `replay_supported` flag is set correctly from day one** so the UI never claims replay for an unsupported framework.

`packages/hfao/compute/causal/pipeline.py`:

async def attribute\_failure(project\_id: str, trace\_id: str) \-\> list\[CausalEdge\]:

    """Run the full pipeline on a trace.

    Triggered by:

      \- any score with name='success' and value\<0.5

      \- any observation with status='error'

      \- manual user request

    """

    obs \= backend.get\_trace(project\_id, trace\_id)

    edges\_static \= stage1\_static(obs)

    edges \= list(edges\_static)

    if PHASE \>= 2:

        candidates \= rank\_for\_replay(obs, edges\_static)

        if candidates and replay\_supported\_for(obs):

            edges \+= await stage2\_counterfactual(obs, candidates)

    edges \+= await stage3\_judge(obs, edges)

    backend.write\_causal\_edges(edges)

    return edges

#### Stage 1 — static dataflow extraction

`packages/hfao/compute/causal/static.py`:

def stage1\_static(obs: list\[Observation\]) \-\> list\[CausalEdge\]:

    """Build a directed multigraph; emit edges for:

    \- parent/child span (always)

    \- explicit handoffs (handoff\_target\_agent\_id set, or A2A task lineage)

    \- tool dataflow (tool args contain a substring \>= MIN\_LEN of a prior obs.output)

    \- prompt conditioning (a GENERATION's input contains a prior sibling's output)

    \- retrieval-to-generation (RETRIEVAL output appears in next GENERATION input)

    """

Tunables:

- `MIN_LEN` (substring match): 32 chars.
- `MIN_SIM` (semantic match for prompt conditioning, optional embedding step): 0.85 cosine.
- `MAX_LOOKBACK_SPANS`: 50 (cap pairwise comparisons).

#### Stage 2 — counterfactual replay (Phase 2\)

`packages/hfao/compute/causal/counterfactual.py`:

async def stage2\_counterfactual(

    obs: list\[Observation\], candidates: list\[Observation\]

) \-\> list\[CausalEdge\]:

    """For each candidate, attempt to re-run the agent from that point with

    a minimally perturbed input. If the failure flips to success, mark as

    DECISIVE\_ERROR with method=COUNTERFACTUAL\_REPLAY.

    Replay drivers:

      \- LangGraph: from checkpointer thread\_id \+ config

      \- OpenAI Agents SDK: RunState.from\_string \+ Runner.run

      \- Claude Agent SDK: resume\_from

    Otherwise: skip silently and let Stage 3 cover it.

    """

#### Stage 3 — LLM judge

`packages/hfao/compute/causal/judge.py`:

async def stage3\_judge(obs: list\[Observation\],

                      hint\_edges: list\[CausalEdge\]) \-\> list\[CausalEdge\]:

    """Render top-k candidate trajectories (default k=5) and ask the configured

    judge model: 'Which agent and which step caused the failure, and why?'

    Returns one CausalEdge per (agent, step) hypothesis with confidence.

    Default judge: Anthropic Haiku via the user's configured key,

                   else OpenAI gpt-4o-mini, else HF Inference Provider model.

    Optional fine-tune: ship instructions in docs/causal/finetune.md for

    Qwen3-8B on the public TracerTraj-2.5K dataset.

    """

**Critical UX rule.** Every `CausalEdge` returned to a user (UI or MCP) carries `confidence`, `method`, `replay_supported`, and `evidence`. The UI **must** label `LLM_JUDGE`\-only edges as "hypothesis" not "cause." The MCP tool description carries the same disclaimer.

### 8.2 Eval engine

`packages/hfao/compute/eval/engine.py`:

from typing import Protocol, Any

class EvalContext(Struct):

    input: Any

    output: Any

    expected\_output: Any | None

    metadata: dict\[str, Any\]

class Evaluator(Protocol):

    name: str

    version: str

    def \_\_call\_\_(self, ctx: EvalContext) \-\> Score: ...

class EvalRun(Struct):

    id: str

    project\_id: str

    dataset\_id: str

    evaluators: list\[str\]

    status: Literal\["pending","running","done","failed"\]

    started\_at: datetime

    finished\_at: datetime | None

    summary: dict\[str, float\]   \# name → mean

Builtin evaluators (`compute/eval/builtin.py`):

- `exact_match`, `regex_match`, `json_schema_match`
- `levenshtein_ratio`
- `llm_judge` (configurable rubric)
- `latency_p95`, `cost_per_call`
- `tool_use_correct` (compares actual tool calls to expected)

Online evals: configured as triggers (`on_trace_close` \+ `where_sql` filter); sampled at a configurable rate (`sample_pct`).

Offline evals: `hfao eval run --dataset NAME --evaluators e1,e2 --runtime URL` runs the agent against every dataset item, captures the trace, runs evaluators, writes scores, prints a summary table. CI integration: `hfao eval run --gate "exact_match>=0.9"` exits non-zero on regression.

### 8.3 Cost rollups

DuckDB: `cost_daily` table refreshed by the rollup worker every 60s. ClickHouse: `cost_daily_mv` materialized view refreshes automatically.

Rollup dimensions: `(date, user_id, agent_id, model, prompt_name)`. Console pivots on any subset.

### 8.4 Monitor / alert engine

`packages/hfao/compute/monitor.py`:

class Monitor(Struct):

    project\_id: str

    id: str

    name: str

    nl\_description: str          \# "alert when error rate \> 5% over 1h"

    sql\_query: str               \# generated by NL→SQL on creation, then frozen

    threshold: float

    operator: Literal\["gt","lt","gte","lte","eq"\]

    window: str                  \# "5m","1h","24h"

    channels: list\[str\]          \# webhook urls, email

    enabled: bool \= True

Worker re-runs each enabled monitor on its window cadence and POSTs to channels on threshold breach. NL→SQL uses the configured judge model with a tightly schema-constrained prompt template.

### 8.5 §8 acceptance criteria

\# tests/acceptance/test\_ac\_8\_compute.py

def test\_static\_extracts\_handoff\_edge(): ...

def test\_static\_extracts\_tool\_dataflow\_edge(): ...

def test\_judge\_returns\_ranked\_hypotheses(): ...

def test\_replay\_supported\_false\_for\_crewai(): ...

def test\_eval\_run\_offline\_writes\_scores(): ...

def test\_eval\_run\_gate\_exits\_nonzero(): ...

def test\_cost\_rollup\_pivot\_by\_user\_and\_model(): ...

def test\_monitor\_nl\_to\_sql\_generation(): ...

def test\_monitor\_fires\_on\_threshold(): ...

Commits:

- `feat(compute): static causal DAG inference`
- `feat(compute): LLM-judge attribution`
- `feat(compute): eval engine + builtin evaluators`
- `feat(compute): cost rollups + monitor engine`

---

## 9\. MCP server (HFAO as MCP)

### 9.1 Transport

FastMCP Streamable HTTP at `:4319/mcp`. Auth: HTTP Basic with `(workspace_slug, api_key)` or `Authorization: Bearer hfao_pat_...`. Read-only mode toggled by `HFAO_MCP_READ_ONLY=true`.

### 9.2 Tool surface

\# packages/hfao/mcp\_server/tools.py

@mcp.tool()

async def list\_traces(

    project: str,

    where: str \= "1=1",     \# SQL WHERE; backend enforces project scoping

    limit: int \= 25,

) \-\> list\[TraceSummary\]: ...

@mcp.tool()

async def get\_trace(project: str, trace\_id: str) \-\> TraceDetail:

    """Returns observations \+ scores \+ causal edges \+ per-edge confidence."""

@mcp.tool()

async def search\_traces(project: str, query: str, limit: int \= 25\) \-\> list\[TraceSummary\]:

    """Semantic \+ keyword search over input/output bodies."""

@mcp.tool()

async def get\_causal\_attribution(project: str, trace\_id: str) \-\> CausalReport:

    """Returns ranked decisive-error hypotheses with evidence and a

    \`replay\_supported\` flag per framework. NOTE: Hypotheses, not verdicts."""

@mcp.tool()

async def list\_decisive\_errors(

    project: str, since: str \= "24h", min\_confidence: float \= 0.3

) \-\> list\[DecisiveError\]: ...

@mcp.tool()

async def compare\_runs(

    project: str, trace\_id\_a: str, trace\_id\_b: str

) \-\> RunComparison: ...

@mcp.tool()

async def run\_eval(

    project: str, dataset: str, evaluators: list\[str\], runtime\_url: str | None \= None

) \-\> EvalRun: ...

@mcp.tool()

async def get\_prompt(project: str, name: str, label: str \= "production") \-\> PromptVersion: ...

@mcp.tool()

async def list\_prompts(project: str) \-\> list\[PromptVersion\]: ...

@mcp.tool()

async def score\_observation(    \# gated by HFAO\_MCP\_READ\_ONLY

    project: str, trace\_id: str, observation\_id: str | None,

    name: str, value: float, comment: str | None \= None

) \-\> Score: ...

@mcp.tool()

async def get\_cost\_by(

    project: str, group\_by: list\[str\], window: str \= "7d"

) \-\> list\[CostRow\]: ...

@mcp.resource("hfao://traces/{project}/{trace\_id}")

async def trace\_resource(project: str, trace\_id: str) \-\> str: ...

@mcp.prompt()

async def explain\_failure(project: str, trace\_id: str) \-\> str:

    """Returns a templated prompt the client LLM can use to ask for an explanation."""

### 9.3 Auth

Three modes:

1. **API key** (default for self-host): `Authorization: Bearer hfao_pat_...`.
2. **HF OAuth** (HF Spaces deploys): exchange HF token for an HFAO session.
3. **OIDC** (enterprise): generic OIDC provider via env config.

Every tool resolves the workspace from auth and rewrites SQL to enforce `project_id IN (SELECT id FROM projects WHERE workspace_id = ?)`.

### 9.4 §9 acceptance criteria

\# tests/acceptance/test\_ac\_9\_mcp.py

def test\_mcp\_streamable\_http\_handshake(): ...

def test\_list\_traces\_requires\_auth(): ...

def test\_score\_observation\_blocked\_in\_readonly(): ...

def test\_get\_causal\_attribution\_returns\_replay\_flag(): ...

def test\_workspace\_isolation\_enforced(): ...

def test\_mcp\_resource\_uri\_resolves(): ...

Commit: `feat(mcp): HFAO-as-MCP server with full read+write tool surface`.

---

## 10\. Cockpit (Gradio 6, single-file)

### 10.1 File

`apps/cockpit/cockpit.py` — **the single file**. Imports from `hfao.*` only. Launches via `python -m hfao.cli up` which calls `apps.cockpit.cockpit:demo.launch(mcp_server=True, server_port=7860)`.

Theme: `gr.themes.Soft()` with HFAO accent. Tailwind via `gr.HTML` blocks scoped CSS.

### 10.2 Page-by-page spec

Every page is a `gr.Tab` in one `gr.Blocks`.

1. **Home** — workspace \+ project selector (`gr.Dropdown`); recent activity feed (rolling `gr.HTML` of last 20 traces); quick-stats cards (24h trace count, error rate, total cost).
2. **Traces** — list with `gr.Dataframe` (capped at 5K rows, deep-link to console for larger queries); filters: `time_range`, `status`, `model`, `agent_id`, `tag`, `where_sql` (SQL textbox, advanced toggle); row-click opens **Trace detail**.
3. **Trace detail** — `gr.Chatbot` rendering with metadata accordions for tool calls and CoT (this is where the §10.3 `span_tree.py` custom component lives). Side panel: scores, causal-edges table (with `confidence`, `method`, `replay_supported`), cost summary. Buttons: "Add to dataset", "Open in console", "Re-run causal attribution".
4. **Live tail** — `gr.Timer(1.0)` polling latest 20 traces; `gr.HTML` rendering colored status pills.
5. **Datasets** — list \+ create \+ items table; "Add observation to dataset" wired from Trace detail.
6. **Prompts** — registry; create new; bump version; move label; diff view (`gr.Code` two-column).
7. **Evals** — launch eval run; runs list with summary metrics; gate-failure highlighting.
8. **Annotations** — queue list; review mode (next-item, keyboard shortcuts via `gr.HTML` JS).
9. **Monitors** — list; "create monitor" with NL→SQL preview; alert history.
10. **Costs** — `gr.HTML` rendered chart \+ `gr.Dataframe` pivot; group-by selector.
11. **Settings** — project, retention, redaction, judge model, MCP key management.
12. **Ask HFAO** — `gr.ChatInterface` whose `fn` calls the HFAO MCP server locally; ships as a working "agent debugging copilot" the user can use from inside the cockpit.

### 10.3 Custom components

- `apps/cockpit/components/span_tree.py` — wraps `gr.HTML` with scoped JS that renders a span tree from a JSON list of observations, with expand/collapse, status colors, and lazy load.
- `apps/cockpit/components/trace_chat.py` — wraps `gr.Chatbot` with the metadata-accordion mapping for HFAO observation types (TOOL → tool accordion; AGENT → agent badge; HANDOFF → arrow \+ target).
- `apps/cockpit/components/live_tail.py` — `gr.HTML` \+ `gr.Timer(1.0)` ring buffer.

### 10.4 MCP export

Every event handler registered with `api_name="..."` becomes an MCP tool when launched with `mcp_server=True`. Naming convention: `api_name="cockpit.{verb}.{noun}"` and only handlers prefixed `cockpit.read.*` or `cockpit.write.*` get exported. The full HFAO MCP tool surface (§9.2) is mounted at `/mcp` instead of going through Gradio's auto-export — Gradio's MCP is for cockpit-level interactions only.

### 10.5 Auth

`gr.LoginButton` with HF OAuth in the HF-Space shape; `gr.OAuthProfile` resolves the workspace. Self-host: simple username/password login backed by control-plane Postgres/SQLite. Enterprise: header forwarding from an OIDC ingress.

### 10.6 §10 acceptance criteria

\# tests/acceptance/test\_ac\_10\_cockpit.py (uses gradio\_client \+ Playwright)

def test\_cockpit\_boots\_under\_5s(): ...

def test\_traces\_page\_renders\_seed\_data(): ...

def test\_trace\_detail\_renders\_tool\_accordion(): ...

def test\_live\_tail\_updates\_on\_new\_trace(): ...

def test\_dataset\_add\_from\_trace\_detail(): ...

def test\_prompt\_label\_move\_creates\_audit\_log(): ...

def test\_eval\_launch\_returns\_run\_id(): ...

def test\_monitor\_create\_nl\_preview(): ...

def test\_ask\_hfao\_returns\_grounded\_answer(): ...

Commit: `feat(cockpit): Gradio 6 single-file UI with MCP export`.

---

## 11\. Console (SvelteKit, Phase 2 — deferred to v2.0 per Q-11)

**Status.** Per §16 Q-11 (resolved 2026-04-19), the SvelteKit console is **cut from v1.0.0 scope** and returns as a v2.0 milestone, gated on real user demand — specifically, the first user who hits Gradio's 5K-row Dataframe wall. This section stays in SPEC.md as the authoritative reference for that future build; do not implement any of `apps/console/` before v2.0 is greenlit.

**Phase 2\.** Build only after Phase 1 ships and at least one user has hit Gradio's 5K-row Dataframe wall.

### 11.1 File structure

See §3 `apps/console/`.

### 11.2 Tech

- SvelteKit 2 \+ Svelte 5 runes.
- TanStack Table 8 for the 100K-row trace view.
- Svelte Flow (`@xyflow/svelte`) for DAGs.
- DuckDB-WASM for client-side faceting; falls back to server-side `execute_readonly_sql` for \>1M rows.
- Monaco Editor for SQL playground and trace diff.
- `shadcn-svelte` for components (decision: §16 Q-3).

### 11.3 Pages

- `/` — workspace dashboard (mirrors cockpit Home but with deep filters).
- `/projects/{pid}/traces` — 100K-row TanStack table; URL-state for every filter; saved views.
- `/projects/{pid}/traces/{tid}` — DAG view (Svelte Flow) with causal overlay; details pane mirrors cockpit Trace detail.
- `/projects/{pid}/datasets`, `/prompts`, `/evals`, `/annotations`, `/monitors`, `/costs`, `/settings` — analyst-grade versions of cockpit pages.
- `/projects/{pid}/sql` — SQL playground (read-only; backend enforces project scope).

### 11.4 API contract

A single OpenAPI spec at `/openapi.json` published from the FastAPI router that backs both cockpit and console. Console generates a typed client from it.

### 11.5 §11 acceptance criteria

\# tests/acceptance/test\_ac\_11\_console.py (Playwright)

def test\_console\_table\_loads\_100k\_rows\_under\_3s(): ...

def test\_dag\_renders\_500\_node\_trace(): ...

def test\_url\_state\_round\_trips(): ...

def test\_saved\_view\_persists\_across\_sessions(): ...

def test\_sql\_playground\_rejects\_writes(): ...

Commit: `feat(console): SvelteKit analyst surface (Phase 2)`.

---

## 12\. Framework integrations

### 12.1 Init contract

Every framework integration boils down to:

import hfao

hfao.init(project="my-project")   \# auto-detects installed instrumentations

`hfao.init()`:

1. Resolves config from env (`HFAO_BASE_URL`, `HFAO_API_KEY`, `HFAO_PROJECT`).
2. Sets up an OTel `TracerProvider` with an OTLP/HTTP exporter pointed at `HFAO_BASE_URL/v1/traces`.
3. Auto-instruments any installed OpenInference packages by introspecting `pkg_resources` (best-effort; explicit calls also supported).
4. Patches MCP `Client.call_tool` and `Server.tool` for `_meta` propagation if `mcp` is installed.
5. Returns a context object exposing `session()`, `prompt()`, `score()`, `observe`.

### 12.2 Per-framework matrix

| Framework | User code | Instrumentor | Notes |
| :---- | :---- | :---- | :---- |
| OpenAI / Anthropic / Mistral / Groq / Bedrock / Vertex / Google GenAI | `hfao.init()` (auto) | `openinference-instrumentation-{vendor}` | nothing extra |
| LangChain / LangGraph | `hfao.init()` (auto) | `openinference-instrumentation-langchain` | LangGraph emits via callbacks; HFAO maps `thread_id` → `session_id` via `instrumentations/langgraph_extra.py` |
| LlamaIndex / Haystack / DSPy | `hfao.init()` (auto) | respective `openinference-instrumentation-*` |  |
| CrewAI | `hfao.init()` (auto) | `openinference-instrumentation-crewai` | replay unsupported → `replay_supported=False` |
| OpenAI Agents SDK | `hfao.init()` (auto) \+ `agents.set_trace_processors([HFAOTracingProcessor()])` | `openinference-instrumentation-openai-agents` \+ `instrumentations/openai_agents_extra.py` (HandoffSpan, GuardrailSpan, MCPListToolsSpan, etc.) |  |
| Claude Agent SDK | `hfao.init()` (auto) | `openinference-instrumentation-claude-agent-sdk` \+ `instrumentations/claude_agent_extra.py` (PreToolUse, PostToolUse hooks) | replay supported via `resume_from` |
| AutoGen / AG2 | `hfao.init()` \+ `from hfao.instrumentations.autogen_extra import patch; patch()` | community OI package or fallback monkey-patch | replay unsupported |
| Pydantic AI | `hfao.init()` (auto) | native OTel | semconv shim |
| Google ADK | `hfao.init()` \+ ADK callbacks installed by `instrumentations/adk_extra.py` | native |  |
| AWS Strands | `hfao.init()` \+ Strands telemetry env var | native OTel |  |
| LiteLLM | `hfao.init()` (auto) | `openinference-instrumentation-litellm` |  |
| MCP clients/servers | `hfao.init()` (auto patches) | `openinference-instrumentation-mcp` preferred |  |
| HF Transformers Agents / smolagents | `hfao.init()` (auto) | `openinference-instrumentation-smolagents` \+ `instrumentations/transformers_agents_extra.py` | replay unsupported |

### 12.3 Per-framework example

`examples/smolagents-quickstart/app.py`:

import hfao

from smolagents import CodeAgent, HfApiModel

hfao.init(project="smol-demo")

agent \= CodeAgent(model=HfApiModel(), tools=\[\])

with hfao.session(user\_id="alice"):

    result \= agent.run("What is the capital of France?")

    print(result)

### 12.4 §12 acceptance criteria

\# tests/acceptance/test\_ac\_12\_integrations.py

@pytest.mark.parametrize("framework", \["openai","anthropic","langchain","langgraph",

    "crewai","openai\_agents","claude\_agent\_sdk","smolagents","litellm","mcp"\])

def test\_framework\_quickstart\_produces\_canonical\_trace(framework): ...

def test\_hfao\_init\_idempotent(): ...

def test\_hfao\_session\_propagates\_to\_session\_id(): ...

def test\_hfao\_prompt\_decorates\_generation\_span(): ...

def test\_replay\_supported\_flag\_correct\_per\_framework(): ...

Commits: one per framework: `feat(integrations): {framework}`.

---

## 13\. Auth & multi-tenancy

### 13.1 Workspace model

workspace

  └── project

        └── trace

              └── observation

A user belongs to one or more workspaces with a role. API keys are scoped to a workspace. Projects are children of workspaces. Traces are scoped to projects.

### 13.2 RBAC

Roles: `owner`, `admin`, `member`, `viewer`. Permissions: project CRUD, prompt write, eval launch, monitor write, redaction config, SSO config — gated per role. Stored in control plane.

### 13.3 SSO

- HF OAuth (Spaces): always available via `gr.LoginButton`.
- Generic OIDC: env-driven (`OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`).
- SAML: enterprise-only (Phase 2+).

### 13.4 API tokens

Bearer tokens prefixed `hfao_pat_`. Scope: workspace \+ role. Stored as SHA256 in control plane. Rotatable. Last-used timestamp tracked.

### 13.5 PII tenancy isolation

- Per-project encryption key for body offload (envelope encryption against a workspace KEK).
- No cross-project queries permitted via `execute_readonly_sql`.
- Audit log: every write, every settings change, every API key issuance.

### 13.6 §13 acceptance criteria

\# tests/acceptance/test\_ac\_13\_auth.py

def test\_workspace\_isolation\_in\_trace\_query(): ...

def test\_pat\_revocation\_invalidates\_immediately(): ...

def test\_role\_member\_cannot\_change\_redaction(): ...

def test\_oidc\_login\_round\_trip(): ...

def test\_audit\_log\_records\_settings\_change(): ...

Commit: `feat(auth): workspaces, RBAC, OIDC, API tokens, audit log`.

---

## 14\. Acceptance harness

Each module's AC tests live in `tests/acceptance/test_ac_{section}_{name}.py` (paths shown above). Claude Code MUST run, and pass, the full AC suite for a module before moving to the next module's commit.

### 14.1 Test runner

\# Run full suite

uv run pytest tests/acceptance \-v

\# Run one module's gate

uv run pytest tests/acceptance/test\_ac\_5\_wire.py \-v

\# Hard gate in CI

uv run pytest tests/acceptance \--strict-markers \-v \--cov=packages/hfao \--cov-fail-under=80

### 14.2 CI gating

`.github/workflows/test.yml`:

- On every PR: unit \+ integration \+ acceptance tests must pass.
- On `main` push: also build Docker images and push tagged `:edge`.
- Acceptance test report posted as PR comment.

### 14.3 Performance gates

`tests/perf/`:

- Ingest: sustain 5K spans/s on a single 4-core CI runner for 60s with p99 latency \< 100ms.
- Trace render: cockpit Trace detail loads a 200-span trace in \<500ms.
- Causal attribution Stage 1: \<2s on a 200-span trace.

---

## 15\. Implementation plan (Claude Code execution order)

### 15.1 Milestone gates

Three milestones, each ends with a tagged release.

| Milestone | Tag | Definition of done |
| :---- | :---- | :---- |
| **M1 — Walking skeleton** | `v0.1.0` | OTLP ingest works, DuckDB stores traces, cockpit renders a trace, MCP server returns `list_traces` and `get_trace`. Single-binary HF Space deploys. |
| **M2 — Phase 1 feature parity \+ Experiment primitive** | `v0.5.0` | All §1 goals 1–4 covered with Phase 1 scope. Static causal \+ LLM-judge attribution. ClickHouse backend behind a flag. Eval engine offline \+ online. Annotation queues. Cost rollups. Monitors. **Experiment runner emits Verdicts with paired statistics (§16 Q-10).** `hfao parquet export` CLI for manual warm-tier (§16 Q-13). |
| **M3 — Phase 2 differentiation** | `v1.0.0` | Counterfactual replay for LangGraph \+ OpenAI Agents SDK \+ Claude Agent SDK. Helm chart. Five integration examples shipped, including the Q-10 prompt bake-off demo. mkdocs-material docs site. README reframed around the Q-9 three pillars. **SvelteKit console deferred to v2.0 per §16 Q-11; DuckLake auto-sync worker deferred to v1.1 per §16 Q-13.** |

### 15.2 Week-by-week, commit-by-commit

**Notation.** `[T]` \= test, `[F]` \= feature, `[D]` \= docs, `[I]` \= infra. Each line is one commit.

**Week 1 — bootstrap**

- `[I]` repo skeleton, `pyproject.toml`, `uv.lock`, ruff, pyright, pytest config
- `[I]` Docker base images, GHA workflow
- `[F]` `hfao/schema/*` msgspec Structs (§4.1)
- `[T]` AC §4

**Week 2 — storage**

- `[F]` `hfao/storage/duckdb_backend.py` (§4.2 DDL, §6.2 protocol)
- `[F]` `hfao/storage/control_plane.py` (SQLite default)
- `[T]` AC §6 DuckDB rows
- `[F]` `hfao/storage/clickhouse_backend.py` (§4.3 DDL)
- `[T]` AC §6 ClickHouse rows \+ parity

**Week 3 — ingest (\+ Q-10 schema once greenlit)**

- `[F]` `hfao/ingest/normalize.py` (§5.6) with OpenInference \+ OTel GenAI mappers
- `[F]` `hfao/ingest/server.py` (§7.1)
- `[F]` `hfao/ingest/redact.py` (§6.5)
- `[F]` `hfao/ingest/buffer.py` (in-memory \+ Redis)
- `[T]` AC §5 \+ §7
- `[F]` body offload (§6.6)
- `[F]` `hfao/schema/experiments.py` — the four-object family from §16 Q-10a **(gated on Q-10a human review; lands only after the proposal is resolved)**
- `[T]` AC experiment/variant/pairing/verdict round-trip (gated identically)

**Week 4 — SDK \+ Tier 1 integrations (round 1\)** *(per Q-12 resolution)*

- `[F]` `hfao/sdk/init.py`, `context.py`, `score.py`, `decorators.py`
- `[F]` `hfao/instrumentations/langgraph_extra.py`
- `[F]` `hfao/instrumentations/openai_agents_extra.py`
- `[F]` `hfao/instrumentations/claude_agent_extra.py`
- `[F]` `hfao/instrumentations/transformers_agents_extra.py` (smolagents)
- `[T]` AC §12 for the four Tier 1 frameworks (LangGraph, OpenAI Agents SDK, Claude Agent SDK, smolagents) \+ auto-OpenInference LLM SDKs (openai / anthropic / mistral / groq / bedrock / vertex / google-genai)
- `[D]` quickstart per Tier 1 framework
- **Explicit non-goal:** no Tier 2 integration code this week (see Q-12). Tier 2 is CrewAI, AutoGen, DSPy, LlamaIndex, Haystack, Pydantic AI, Google ADK, AWS Strands, LiteLLM, MCP-as-instrumentation — all deferred to the Week 8 harness.

**Week 5 — cockpit (round 1\)**

- `[F]` `apps/cockpit/cockpit.py` Home \+ Traces \+ Trace detail \+ Live tail
- `[F]` `apps/cockpit/components/span_tree.py`, `trace_chat.py`, `live_tail.py`
- `[F]` `hfao/cli.py` (`hfao up`, `hfao migrate`, `hfao seed`)
- `[T]` AC §10 for these tabs
- 🏷 **TAG `v0.1.0` (M1)**

**Week 6 — cockpit (round 2\) \+ MCP \+ auth**

- `[F]` cockpit Datasets \+ Prompts \+ Evals \+ Annotations \+ Monitors \+ Costs \+ Settings \+ Ask HFAO
- `[F]` `hfao/mcp_server/server.py` \+ `tools.py` \+ `auth.py` (§9)
- `[T]` AC §10 remaining \+ §9
- `[F]` HF OAuth \+ API key auth (§13)
- `[T]` AC §13

**Week 7 — compute (Phase 1\)**

- `[F]` `hfao/compute/causal/static.py` (Stage 1\)
- `[F]` `hfao/compute/causal/judge.py` (Stage 3\)
- `[F]` `hfao/compute/causal/pipeline.py`
- `[T]` AC §8 causal subset
- `[F]` `hfao/compute/eval/*` \+ builtin evaluators
- `[T]` AC §8 eval subset
- `[F]` `hfao/compute/cost.py` \+ `monitor.py`
- `[T]` AC §8 cost \+ monitor
- `[D]` README \+ marketing copy refreshed to the Q-9 three-pillar framing **before** the v0.5.0 tag lands next week

**Week 8 — experiment runner \+ CLI parquet export \+ Tier 2 harness \+ retention** *(per Q-12 / Q-13)*

- `[F]` `hfao/compute/eval/runner.py` — executes an `Experiment` across its `Variant`s, emits `ExperimentRun`s, computes `Verdict`s. This is what makes HFAO genuinely better than Langfuse/Braintrust for comparative study (per Q-9 positioning).
- `[F]` `hfao/cli.py` — `hfao parquet export <from> <to> --hf-bucket <url>` one-shot command (per Q-13). `storage/parquet_sync.py` stays empty for v1.1.
- `[T]` Tier 2 test harness: a shared fixture that asserts any community-contributed instrumentor produces canonical traces. Not per-framework AC; one generic harness.
- `[F]` retention worker (§6.4)
- 🏷 **TAG `v0.5.0` (M2)**

**Weeks 9–10 — counterfactual replay \+ examples** *(replaces the former SvelteKit console weeks per Q-11)*

- `[F]` `hfao/compute/causal/counterfactual.py`
- `[F]` LangGraph replay driver
- `[F]` OpenAI Agents SDK replay driver
- `[F]` Claude Agent SDK replay driver
- `[T]` AC §8 counterfactual extension
- `[D]` `examples/multi-framework-a2a/` — the marquee causal demo
- `[D]` `examples/experiment-prompt-bake-off/` — the marquee Q-10 demo (HFAO's answer to "can I A/B prompts like Braintrust"; answer: yes, and with paired statistics)

**Week 11 — docs \+ polish \+ Helm** *(replaces weeks 11–12 consolidated)*

- `[D]` mkdocs-material docs site (per §16 Q-7 default)
- `[I]` Helm chart
- `[I]` HF Space one-click deploy workflow
- `[D]` README rewritten around the Q-9 three-pillar framing
- 🏷 **TAG `v1.0.0` (M3)**

**Net effect of the Q-9/Q-11/Q-12/Q-13/Q-14 rebalance.** v1.0.0 ships one week earlier (week 11 vs. the original week 12), without the SvelteKit console, with sharper positioning, and with the Experiment schema as the library-level hook that makes HFAO genuinely differentiated for comparative study. §11 (console) stays in this document as deferred-scope v2.0 reference, gated on the first user who actually hits Gradio's 5K-row wall.

### 15.3 Hard rules for Claude Code execution

1. **One commit per line in §15.2.** Conventional Commits format. Reference the spec section in the commit body.
2. **No spec deviation without a §16 entry first.** If a section is genuinely ambiguous, stop, append the question to §16 in a `docs(spec): open question` commit, and wait for human resolution.
3. **AC tests pass before the next commit.** If they fail, fix or revert in the same commit chain — never push red.
4. **Every PR runs the full AC suite.** No squash-merging across module boundaries.
5. **Type strictness.** `pyright --strict` clean for `packages/hfao/`. `tsc --strict` clean for `apps/console/`.
6. **No `# type: ignore` without a comment explaining why.** No `Any` outside the OTLP boundary.
7. **No new top-level dependencies without justification in the commit body.** Prefer stdlib \+ msgspec \+ duckdb \+ httpx \+ opentelemetry-\* \+ huggingface\_hub \+ gradio.

---

## 16\. Open questions for human decision

| ID | Question | Default if no answer | Decision deadline |
| :---- | :---- | :---- | :---- |
| Q-1 | Default LLM-judge model when user hasn't configured one — Anthropic Haiku? OpenAI gpt-4o-mini? HF Inference Provider Qwen3-8B? | Anthropic Haiku, fallback to OpenAI gpt-4o-mini, fallback to HF Inference Provider | Week 7 |
| Q-2 | Ship Qwen3-8B-TracerTraj fine-tune weights in repo, or document training and let users train themselves? | Document only; weights via HF Hub link from a separate `f8n-ai/hfao-judge-qwen3-8b` repo | Week 11 |
| Q-3 | SvelteKit console UI library — `shadcn-svelte` (richer, less mature) or raw Tailwind \+ headless components (less polish, more control)? | `shadcn-svelte` | Week 9 |
| Q-4 | Hosted SaaS: does HFAO ship a hosted offering at v1.0.0, or pure OSS only? | Pure OSS at v1.0.0; hosted is a separate `hfao-cloud` repo decision | Pre-launch |
| Q-5 | License for `hfao/compute/causal/*` — keep Apache-2.0 or carve out as AGPL to protect against proprietary forks of the differentiator? | Apache-2.0 (consistency wins over moat-building at this stage) | Week 7 |
| Q-6 | Default body offload destination in single-binary shape — local FS or embedded MinIO? | Local FS (simpler; MinIO adds a dep) | Week 2 |
| Q-7 | Docs site generator — mkdocs-material, Docusaurus, or Mintlify? | mkdocs-material | Week 12 |
| Q-8 | Telemetry-on-HFAO (anonymous usage stats from self-hosters) — opt-in, opt-out, or none? | Opt-in only; none in v1 | Pre-launch |
| Q-9 | Should §1.1 pillars be reframed to demote causal attribution from differentiator to feature, with standards-nativeness \+ MCP-native queryability \+ closed eval-trace loop as the three pillars? | Yes, reframe. Causal attribution remains a Phase 1 feature with identical code scope; positioning changes only. | Before week 3 |
| Q-10 | Add an Experiment / Variant / Pairing / Verdict object family to §4.1 as the primitive for systematic comparison — usable by HFAO eval CLI, CI gates, prompt A/Bs, model bake-offs, and external consumers (e.g. AgentXAgent) doing tournament orchestration? | Yes, but draft the full schema as a §16 proposal first (Q-10a below); do not merge to §4 until the shape is reviewed. The 40-line version initially proposed is insufficient — it lacks variant/pairing/verdict semantics that make the object uniquely useful vs. just tagging traces. | Before week 4 |
| Q-10a | What is the exact Experiment schema? | See draft below in §16 proposal block. Claude Code to iterate the draft as a `docs(spec): Q-10 experiment schema proposal` commit; human reviews before §4 gets touched. | Before week 4 |
| Q-11 | Cut SvelteKit console from v1.0.0 scope. Phase 1 / v1.0.0 ships Gradio-only. Console returns as v2.0, gated on real user demand (first user hits Gradio 5K-row wall). | Yes, cut. Frees weeks 9–10 of §15.2. §11 stays in SPEC as deferred-scope reference. | Before week 9 |
| Q-12 | Tier the §12.2 framework matrix. Tier 1 (v1.0.0 acceptance-bulletproof): LangGraph, OpenAI Agents SDK, Claude Agent SDK, smolagents, plus auto-OpenInference coverage for raw LLM SDKs (openai/anthropic/mistral/groq/bedrock/vertex/google-genai). Tier 2 (documented integration point \+ test harness only, not v1 AC): CrewAI, AutoGen, DSPy, LlamaIndex, Haystack, Pydantic AI, Google ADK, AWS Strands, LiteLLM, MCP-as-instrumentation. | Yes, tier. Axes: Tier 1 covers replay-supported ×3 \+ HF-native ×1. Tier 2 gets community-contribution path with the test harness §12 already mandates. | Before week 4 |
| Q-13 | Defer DuckLake auto-sync worker to v1.1. Ship `hfao parquet export <date-range> --to <hf-bucket>` CLI in v1.0.0 as the manual warm-tier path. Keep §4.4 partition-path conventions exactly as specified. | Yes. Cut `storage/parquet_sync.py` scheduled worker from week 8; implement `hfao parquet export` in `cli.py` as a one-shot command. Conventions in §4.4 stay — they cost nothing and preserve forward compatibility with auto-sync in v1.1. | Before week 8 |
| Q-14 | Architectural separation: HFAO ships as the open-source observability library / deployable; AgentXAgent (the arena platform) lives in a separate repository and imports `hfao` as a dependency. No arena-specific code, schema, or UI lives in this repo. | Yes. HFAO stays a general-purpose observatory. AgentXAgent becomes the marquee demo that proves HFAO is the substrate — the same playbook Langfuse/Supabase/Vercel ran with library-plus-product. | Before v1.0.0 launch narrative is written |
| Q-15 | Is `docs/` a permitted top-level directory? §3's repo layout does not list it, but §8.1 already references `docs/causal/finetune.md`, and the CLI demo deliverable needs `docs/hfao-cli.png` (portfolio screenshot) somewhere. Strict reading of CLAUDE.md treats a new top-level as deviation. | Yes — allow `docs/` as a top-level directory. It is already referenced in §8.1; §3's tree was non-exhaustive. Amend §3 to list `docs/` explicitly when the next spec-touching commit lands. Screenshots and generated artifacts live under `docs/`; prose docs land when the mkdocs-material site is built in week 11. | Before the CLI demo screenshot commit |
| Q-16 | §12.1 rule 4 / §5.4 say `hfao.init()` patches the `mcp` SDK's `Client.call_tool` and `Server.tool` decorator for `_meta` propagation. The preferred path is `openinference-instrumentation-mcp`; HFAO's patch is the fallback. Client-side inject is tractable now (the `meta=` kwarg is already in the SDK signature). Server-side extraction needs the §9 MCP server infrastructure that lands in Week 6. Do we ship the full HFAO fallback in Week 4 (risking coupling to §9 internals before they exist), split client-now / server-Week-6, or skip the HFAO fallback entirely and require `openinference-instrumentation-mcp` for non-OI deployments? | Split: Week 4 `hfao.sdk.init.py` logs a one-shot info-level notice telling users to install `openinference-instrumentation-mcp` and returns `mcp_patched=False`. The full client + server fallback lands in Week 6 alongside §9, when the `Server.tool` decorator semantics can be verified against the real MCP server. Rationale: full implementation in Week 4 would require locking patch behavior to MCP Python SDK internals months before those internals are exercised by our own §9 code, risking silent drift; the alternative (requiring `openinference-instrumentation-mcp`) is fine for v1.0.0 but removes the fallback commitment in §5.4. The split preserves the spec's contract while moving risky work to the week where it can be tested end-to-end. | Before Week 4 SDK commit lands |
| Q-17 | Week 6 (§15.2) lists two `[F]` commits in this order: (1) cockpit round-2 tabs (Datasets / Prompts / Evals / Annotations / Monitors / Costs / Settings / Ask HFAO), then (2) `mcp_server/` (§9). Three dependency problems make the listed order non-buildable as written: (a) the **Ask HFAO** tab (commit 1) calls the §9 MCP tool surface (commit 2) — an intra-week forward dependency; (b) the Datasets / Annotations / Monitors tabs need control-plane CRUD for `Dataset`/`DatasetItem`, `AnnotationQueue`/`AnnotationItem`, and `Monitor` that `storage/control_plane.py` does not yet implement (it has only workspace / project / api-key / prompt CRUD); (c) the Evals and Monitors tabs reference the eval engine (Week 7) and monitor engine (Week 7) that do not exist yet. Reordering the two Week-6 `[F]` commits, and adding control-plane CRUD, both count as deviation under CLAUDE.md. How should Week 6 proceed? | **Build the §9 MCP server first within Week 6** (commit 2 before commit 1); week boundaries are unchanged and only the two intra-week `[F]` commits swap order. Rationale: the MCP server is the M1 definition-of-done blocker (M1 requires `list_traces`/`get_trace` over MCP) and is a hard dependency of the Ask HFAO tab, so it must land first regardless. Then build cockpit round-2, **extending `storage/control_plane.py` with dataset / annotation-queue / monitor CRUD as part of that commit** (additive, matches the §4 schema Structs exactly — no schema deviation). Evals and Monitors tabs render **read-only views** of records (eval runs via `eval_run_id` on `scores`; monitors from the new control-plane table); the Week 7/8 compute engines populate them later without changing the tab contract. The `run_eval` MCP tool's executor lazily imports `hfao.compute.eval.runner` (Week 8) and raises a clear "lands in Week 8 per §15.2" error until then — AC §9 does not exercise it, so the suite stays green and honest. | Before the Week 6 MCP commit lands (i.e. now) |

**Claude Code instruction.** If an answer arrives, append it to §16 with a date stamp and a one-line rationale. Do not modify the table inline; preserve the original questions for audit.

### 16.1 Resolutions (append-only, per §16 instruction)

- **Q-9 resolved (2026-04-19)** — Yes, reframe. Standards-native ingest, MCP-native queryability, and the closed eval-trace loop are the three pillars. Causal attribution remains in Phase 1 with unchanged code scope and honest "hypotheses not verdicts" framing. *Rationale:* Stage 1 is table stakes, Stage 3 is easily copied, Stage 2 only covers three frameworks — the combo is good but not moat-grade. The three adopted pillars are each architecturally hard for commercial competitors to copy (LangSmith/Braintrust have business reasons not to ship clean MCP or abandon proprietary wire formats).
- **Q-10 resolved in principle; Q-10a pending (2026-04-19)** — Yes to the object family; no commit to §4 until Q-10a draft is reviewed. Claude Code to produce `docs(spec): Q-10 experiment schema proposal` next, containing the full Experiment/Variant/Pairing/Verdict Structs with rationale, drawing from the draft block in §16.2 below. Human reviews, iterates if needed, then greenlights the §4 commit.
- **Q-11 resolved (2026-04-19)** — Cut from v1.0.0. §11 stays in SPEC as v2.0 deferred scope. Weeks 9–10 of §15.2 freed; see §15 re-plan in the same commit that lands this resolution.
- **Q-12 resolved (2026-04-19)** — Tier as proposed. Tier 1 integrations get full per-framework AC tests in `test_ac_12_integrations.py`. Tier 2 gets the shared test harness only and a `CONTRIBUTING.md` path for community PRs.
- **Q-13 resolved (2026-04-19)** — Defer auto-sync; ship CLI export. `storage/parquet_sync.py` stays as an empty file for v1.1. §4.4 partition conventions untouched.
- **Q-14 resolved (2026-04-19)** — Separate repos. AgentXAgent is not an HFAO concern. The Experiment schema (Q-10) is the clean library-level primitive that lets external consumers including AgentXAgent build arena/tournament logic on top without HFAO carrying arena assumptions. Rationale mirrors the Langfuse/Supabase/Vercel pattern: library stays general, product stays focused, each benefits from the other without coupling.
- **Q-17 resolved (2026-05-29)** — Proceed under the proposed default, on the repo owner's explicit standing instruction to continue the Week 6 build. Order within Week 6: (1) MCP server (§9) first, (2) then cockpit round-2 with additive control-plane CRUD. Week boundaries unchanged; only the two intra-week `[F]` commits swap order. *Rationale:* MCP is the M1 DoD blocker and a hard dependency of the Ask HFAO tab; control-plane CRUD for datasets/annotation-queues/monitors is additive and matches the §4 Structs (no schema deviation); the `run_eval` MCP tool defers its executor to Week 8 with an explicit error rather than a misleading stub. Audit trail preserved per the §16 instruction — original Q-17 row left intact.
- **Q-10a resolved (2026-05-31)** — Greenlight the §16.2 draft as proposed, adopting each Option-A/Option-B recommendation §16.2.2 itself reaches:
  - **Q-10a.1 → Option A** (SHA256 of canonical JSON for `Variant.config_hash`). Keeps v1.0.0 dependency-free; `sha256:...` URI prefix leaves room for a future content-addressable store in v1.2 without a breaking change.
  - **Q-10a.2 → Option A** (one `Verdict` per evaluator, append-only). A thin `verdict_matrix()` aggregation helper covers the matrix view without losing the audit trail when a paired test or α changes.
  - **Q-10a.3 → Option B** (separate immutable `ExperimentDefinition` from runtime `Experiment` state). Mirrors the §4.1 `PromptVersion` / `PromptLabel` pattern users have already internalised — edits to a launched experiment's intent produce a new definition version, not in-place mutation.

  *Resulting object family for `packages/hfao/schema/experiments.py`:* `ExperimentDefinition` (immutable, FK target), `Experiment` (mutable runtime state with `definition_id` FK), `Variant`, `Pairing`, `Verdict`, `ExperimentRun`. AC test sketch per §16.2.4 applies as written. The Week 8 §15.2 `[F] hfao/schema/experiments.py` line is hereby unblocked; the experiment runner extension to `compute/eval/runner.py` lands in the same PR. v0.5.0 (M2) tag follows in a separate PR.

### 16.2 Q-10a draft — Experiment schema proposal block

The following is a **proposal**, not yet merged into §4. Claude Code must land this as its own `docs(spec): Q-10 experiment schema proposal` commit and wait for human review before touching `packages/hfao/schema/` or §4.1.

```python
# Proposed additions to packages/hfao/schema/experiments.py
# Pending Q-10a review. Do not implement until resolved.

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from msgspec import Struct, field


# An Experiment groups runs that share a task definition and a set of varied
# axes. It is the unit of systematic comparison: CI gates, prompt A/Bs, model
# bake-offs, and externally, tournament rounds.
#
# Design principles:
#   - held-constant vs. varied is explicit, not inferred from tags
#   - variants are first-class, not just metadata on runs
#   - pairings support proper paired statistics (same task + seed across variants)
#   - verdicts carry confidence intervals, not just means — n is usually small
#   - the experiment itself is replayable; individual run replay is in §8.1

VariantAxis = Literal[
    "prompt", "model", "tools", "topology", "agent_config", "system_prompt", "other"
]

ExperimentStatus = Literal["pending", "running", "complete", "aborted"]


class Variant(Struct, kw_only=True):
    """One side of a comparison. Multiple variants per experiment."""
    id: str                              # stable within experiment
    name: str                            # human label, e.g. "haiku-with-tools"
    axis: VariantAxis                    # what this variant changes
    config_hash: str                     # SHA256 of the variant's full config
    config: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))


class Pairing(Struct, kw_only=True):
    """A matched set of runs across variants: same task, same seed, different variant.

    Pairings enable paired statistics (Wilcoxon, paired t) instead of unpaired
    comparisons, which is important when n is small and task variance is high —
    the default condition for LLM evals.
    """
    id: str
    experiment_id: str
    dataset_item_id: str                 # the task being run
    seed: int                            # RNG / sampling seed held constant across variants
    run_ids_by_variant: dict[str, str]   # variant_id -> trace_id


class Verdict(Struct, kw_only=True):
    """Ranked outcome with confidence intervals, not just aggregate means."""
    experiment_id: str
    evaluator: str                       # the score used for ranking
    ranking: list[str]                   # variant_ids in winning order
    mean_by_variant: dict[str, float]
    ci_low_by_variant: dict[str, float]  # 95% CI lower, bootstrap
    ci_high_by_variant: dict[str, float]
    n_pairings: int
    paired_test: str                     # e.g. "wilcoxon_signed_rank"
    p_value: float | None = None
    computed_at: datetime


class Experiment(Struct, kw_only=True):
    project_id: str
    id: str
    name: str
    description: str | None = None
    dataset_id: str                      # what task(s) are being run
    evaluator_ids: list[str]             # which scores are computed
    variants: list[Variant]              # what's being compared
    held_constant: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    planned_runs_per_variant: int
    status: ExperimentStatus
    created_by: str
    created_at: datetime
    finished_at: datetime | None = None


class ExperimentRun(Struct, kw_only=True):
    """Links a single trace back to its experiment, variant, and pairing."""
    project_id: str
    experiment_id: str
    variant_id: str
    pairing_id: str | None = None        # null for unpaired runs
    trace_id: str
    seed: int
    started_at: datetime
```

**Open drafting questions for Q-10a review** (these are not rhetorical; each produces a meaningfully different schema and must be resolved before §4 is touched):

1. Is `config_hash` best as SHA256 of canonical JSON, or should it be a content-addressable store reference?
2. Should `Verdict` support multiple evaluators in one object, or one-Verdict-per-evaluator as drafted?
3. Do we need a separate `ExperimentDefinition` (pre-run, immutable contract) vs. `Experiment` (runtime state), or is one object enough?

#### 16.2.1 Rationale for the four-object family

Each object in the family pulls distinct weight; collapsing any pair produces measurably worse ergonomics or breaks the paired-statistics property that makes this useful for small-n LLM evals.

- **`Variant` as a first-class object** (not a tag on a run): a variant is the unit users configure, talk about, and iterate on. If we stored it as `metadata["variant"] = "haiku"`, we'd lose (a) the axis taxonomy, (b) `config_hash` for duplicate-detection across experiments, and (c) the invariant that every run in an experiment resolves to exactly one variant. Making it a Struct lets the runner validate those invariants once, at the boundary.
- **`Pairing` as its own object** (not inferred): the whole point is the *paired* comparison — same task, same seed, different variant. If pairings were reconstructed from tags, any runtime mismatch between variants (retries, skips, partial failures) would silently poison the statistics. An explicit `Pairing.run_ids_by_variant` map makes mis-pairing a schema error, not a silent data bug.
- **`Verdict` separate from `Experiment`**: an experiment has one intent; it can have many verdicts. You re-run verdicts when you add a new evaluator, change the paired test, or re-weight. Keeping verdicts as their own append-only object preserves the audit trail that "yesterday's ranking used Wilcoxon at α=0.05; today's uses paired-t at α=0.1" — a property users will ask for the first time a ranking flips.
- **`ExperimentRun` as a thin join**: one trace, one experiment context. Without it, the trace table would carry nullable experiment/variant/pairing columns forever. The join stays in control plane (Postgres/SQLite); the trace table stays clean.

#### 16.2.2 Drafting question analysis

**(1) `config_hash` — SHA256 of canonical JSON vs. content-addressable store reference.**

- *Option A — SHA256 of canonical JSON.* Simple, deterministic, no new service. Two variants are "the same" iff their config serializes to the same canonical JSON (sorted keys, no insignificant whitespace). Pros: no dependencies, reproducible across backends, trivial to compute in the SDK. Cons: the *contents* of large prompt configs bloat the `config` dict on every variant; a large system prompt is re-stored on every run.
- *Option B — content-addressable store reference.* `config_hash` points into `hfao-bodies` (or a sibling `hfao-configs` store); `config` becomes a reference and the full blob is fetched on demand. Pros: deduplicates large configs; aligns with the §6.6 body offload story. Cons: adds a store dependency to the eval flow, and introduces a new failure mode where a variant's config is deleted by retention while the experiment is still active.
- *Recommendation (pending review).* Option A for v1.0.0. Option B can be layered on later by making `config_hash` a URI prefix (`sha256:...` in v1.0.0, `content://...` in a hypothetical v1.2) without a breaking change. Users with giant prompts can still use the §6.6 body offload on the prompt registry side.

**(2) `Verdict` — single multi-evaluator object vs. one-per-evaluator.**

- *Option A — one Verdict per evaluator (as drafted).* Simple, composable. A `compare_runs` MCP tool or a UI table naturally iterates a list of Verdicts. Pros: clear unit of computation, clean append-only log. Cons: callers wanting the "for each variant, show mean-per-evaluator" matrix must join N Verdicts client-side.
- *Option B — single Verdict carrying a dict-of-evaluators.* The whole matrix is one object; clients render it directly. Pros: one network fetch for the full matrix. Cons: partial re-runs are awkward (you either append a new Verdict object or mutate the existing one, and the latter breaks audit-ability).
- *Recommendation (pending review).* Option A — keep the one-Verdict-per-evaluator shape. Add a thin aggregation helper (`verdict_matrix(experiment_id) -> dict[evaluator, Verdict]`) in the runner if the matrix view is frequent. The audit-trail argument is decisive: flipping a ranking when you change the paired test must be visible in the log, and Option B hides it behind a mutation.

**(3) `ExperimentDefinition` (immutable contract) vs. `Experiment` (runtime state) — one object or two?**

- *Option A — one `Experiment` object holding both.* What the draft shows. Fields like `status`, `finished_at`, `created_at` mix with `variants`, `held_constant`. Pros: one object to fetch, one table to query. Cons: the definition-vs-state split is blurred; if a user edits `description` or `variants` after launch, audit is fuzzy.
- *Option B — separate `ExperimentDefinition` + `ExperimentRun` (keep current `Experiment` as the state object).* A definition is immutable after launch; state is mutable. Pros: clean contract separation, aligns with the "prompt_versions are immutable, prompt_labels are mutable" pattern we've already committed to (§4.1 PromptVersion/PromptLabel).
- *Recommendation (pending review).* Option B. The Prompt object family already pays the cost of separating immutable content from mutable pointers, and users who've internalized that pattern will expect the same shape for experiments. Concretely: `ExperimentDefinition` holds `name`, `description`, `dataset_id`, `evaluator_ids`, `variants`, `held_constant`, `planned_runs_per_variant`, `created_by`, `created_at`. `Experiment` holds `project_id`, `id`, `definition_id` (FK), `status`, `started_at`, `finished_at`. `description` edits produce new definition versions, mirroring `PromptVersion`.

#### 16.2.3 Integration surface (preview)

Not part of Q-10a itself, but to help reviewers see the downstream cost:

- **CLI.** `hfao experiment create --config experiment.yaml`, `hfao experiment run <id>`, `hfao experiment verdict <id>`. The YAML contract freezes on the definition object.
- **MCP tools (§9.2 extension).** `list_experiments(project)`, `get_experiment(id)`, `get_verdict(experiment_id, evaluator)`, `compare_variants(experiment_id)`. All read-only; a write tool (`create_experiment`) stays gated by `HFAO_MCP_READ_ONLY`.
- **Cockpit / console.** One new "Experiments" tab. Experiment list → detail (variants table, pairings count, latest verdicts per evaluator). Verdict detail view renders confidence intervals as a forest plot via `gr.HTML` scoped CSS (cockpit) or Svelte Flow (console, when it returns in v2.0).
- **CI gate.** `hfao eval run --experiment <id> --gate "verdict.ranking[0] == 'baseline' OR verdict.p_value > 0.05"` exits non-zero when the challenger wins at the configured significance. This composes with §8.2's existing `--gate` flag shape.
- **Storage.** New DDL for `experiments`, `experiment_definitions`, `variants`, `pairings`, `verdicts`, `experiment_runs` in both `storage/ddl/duckdb.sql` and `clickhouse.sql`. Partitioning/PK strategy identical to `events` (project_id-leading, ORDER BY on the ID). No change to hot-path ingest.

#### 16.2.4 AC test sketch (for when Q-10a lands in §4)

Following §4.6's pattern, a `test_ac_4_experiments.py` would cover:

- `test_experiment_definition_immutable_after_launch` — edits produce a new version, old version still fetchable.
- `test_pairing_invariant_one_run_per_variant` — schema-level check that `run_ids_by_variant.keys()` equals the experiment's variant ids.
- `test_verdict_paired_test_bootstrap_ci` — Wilcoxon paired + percentile bootstrap CI against a fixture with a known truth.
- `test_verdict_p_value_monotonic_with_n` — increasing n narrows CI, lowers p-value (sanity only, not a formal statistical test).
- `test_experiment_run_links_trace_to_variant` — `ExperimentRun(trace_id=T)` is reachable from `get_trace` via a metadata path.
- `test_cross_backend_parity_experiments` — same as §6.7 parity, scoped to the experiment tables.

---

## Appendix A — environment variables

HFAO\_BASE\_URL          default: http://localhost:4318    (SDK target)

HFAO\_API\_KEY           required for non-local            (PAT)

HFAO\_PROJECT           default: default

HFAO\_ENVIRONMENT       default: production

HFAO\_BACKEND           one of: duckdb | clickhouse        (default: duckdb)

HFAO\_DUCKDB\_PATH       default: /data/hfao.duckdb

HFAO\_CLICKHOUSE\_DSN    e.g. clickhouse://user:pass@host:9000/hfao

HFAO\_CONTROL\_PLANE\_DSN default: sqlite:///data/control.db

HFAO\_REDIS\_URL         default: none (in-memory buffer); else redis://...

HFAO\_BODIES\_PATH       default: /data/bodies | s3://hfao-bodies

HFAO\_HF\_BUCKET         e.g. f8n-ai/hfao-warm

HFAO\_MCP\_READ\_ONLY     default: false

HFAO\_JUDGE\_PROVIDER    default: anthropic

HFAO\_JUDGE\_MODEL       default: claude-haiku-4-5

HFAO\_OIDC\_ISSUER\_URL   optional

HFAO\_OIDC\_CLIENT\_ID    optional

HFAO\_OIDC\_CLIENT\_SECRET optional

HFAO\_REDACTION\_PROFILE default: standard

OTEL\_SEMCONV\_STABILITY\_OPT\_IN  recommended: gen\_ai\_latest\_experimental

## Appendix B — Conventional Commit prefixes

`feat(scope): ...`, `fix(scope): ...`, `refactor(scope): ...`, `perf(scope): ...`, `test(scope): ...`, `docs(scope): ...`, `chore(scope): ...`, `ci(scope): ...`, `build(scope): ...`. Scopes: `schema`, `storage`, `ingest`, `sdk`, `mcp`, `cockpit`, `console`, `compute`, `integrations`, `auth`, `cli`, `docker`, `helm`, `examples`, `docs`, `spec`.

## Appendix C — non-negotiable principles

1. **Standards first.** OTel \+ OpenInference \+ MCP. No proprietary wire format. Ever.
2. **Hypotheses, not verdicts.** Causal attribution is decision support; the UI and MCP must reinforce this with copy and confidence scores.
3. **One file when it earns it.** `cockpit.py` stays single-file. `ingest/server.py` stays small. Everything else is conventional.
4. **Backend abstraction is sacred.** No SQL outside `storage/`. Backends are interchangeable.
5. **HF distribution is amplification, not the moat.** The moat is causal attribution \+ closed eval-trace loop \+ MCP-native. HF is how the world finds it.
6. **Type strictness.** Pyright strict. TypeScript strict. msgspec at every boundary.

— *End of SPEC.md, version 1.0.0.*
