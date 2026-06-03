# Changelog

All notable changes to HFAO are documented in this file. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); HFAO uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) with the milestone
discipline laid out in [SPEC §15.1](SPEC.md).

## [Unreleased]

Nothing yet. Open §16 questions Q-18 (proactive anomaly surfacing), Q-19
(insight routing intelligence), Q-20 (Stage 2 counterfactual replay reorder)
are filed with proposed defaults, gated on human greenlight.

## [0.5.0] — 2026-06-02 — M2: Phase 1 feature parity + Experiment primitive

The closed eval-trace loop is operational end-to-end. Every §15.2 Week-1
through Week-8 line is on `main` except the Q-10a-gated experiment runner,
which landed in PR #16 alongside its schema. M2 definition-of-done per §15.1
is satisfied.

**Test gate at v0.5.0:** 196 passed / 6 skipped (the skips are ClickHouse
testcontainer + optional Presidio paths).

### Added — Phase 1 insight engines

- **Causal attribution** (PR #12). `compute/causal/static.py` (Stage 1
  lexical/structural extraction: parent/child, HANDOFF, TOOL_DEPENDENCY,
  PROMPT_CONDITIONING, retrieval→generation) + `compute/causal/judge.py`
  (Stage 3 LLM judge with pluggable backends: Anthropic, OpenAI, HF
  Inference, Deterministic). Pipeline orchestrator + per-framework
  `replay_supported` registry. AC §8 causal subset, 18 tests.
- **Eval engine** (PR #13). 8 built-in evaluators (`exact_match`,
  `regex_match`, `json_schema_match`, `levenshtein_ratio`, `llm_judge`,
  `latency_p95`, `cost_per_call`, `tool_use_correct`). Offline runner,
  HTTP / echo runtimes, deterministic online sampler. CI gate parser
  (`hfao eval run --gate "exact_match>=0.9"` exits 1 on fail). Judge ↔
  human calibration with `Score.calibration_bias`. MCP `run_eval` tool
  now executes (no more `NotImplementedError`).
- **Cost rollup** (PR #14). DuckDB `refresh_cost_rollup()` + 60s
  background worker; ClickHouse SummingMergeTree MV. Pivot by any subset
  of (date, user_id, agent_id, model, prompt_name).
- **Monitor engine** (PR #14). NL→SQL with keyword-template generator
  (deterministic) and LLM-driven generator (fallback-safe). SQL is
  **frozen on create** per §8.4 — re-evaluation never regenerates.
  Threshold breach → audit-logged Alert + outbound webhook with delivery
  tracking. `cockpit_create_monitor` exposes the flow.
- **Retention worker** (PR #15). Per-project `RetentionPolicy` with
  per-tier `*_days` (`0` disables that tier). DuckDB count-then-DELETE,
  ClickHouse `ALTER TABLE DELETE`. Daemon thread + `hfao retention
  {set,run,show}` CLI.
- **Experiment primitive** (PR #16). Six msgspec Structs:
  `ExperimentDefinition` (immutable, Q-10a.3 Option B), `Experiment`
  (mutable runtime state), `Variant` (axis-tagged, `config_hash =
  sha256:HEXDIGEST` of canonical JSON per Q-10a.1 Option A), `Pairing`
  (matched runs), `Verdict` (one per evaluator, append-only per Q-10a.2
  Option A), `ExperimentRun`. Multi-variant tournament runner with
  bootstrap CIs + paired statistics (Wilcoxon signed-rank default,
  paired-t, sign-test). `verdict_matrix()` aggregation helper.

### Added — Surfaces

- **MCP server** (PR #9). FastMCP Streamable HTTP at `:4319/mcp` with the
  full §9.2 read tool surface + gated `score_observation` write +
  `explain_failure` prompt + `hfao://traces/{project}/{trace_id}`
  resource. Per-request Bearer / HTTP Basic auth with workspace
  isolation. The MCP-native queryability pillar.
- **Cockpit round 2** (PR #10). The remaining 8 §10.2 tabs: Datasets,
  Prompts, Evals, Annotations, Monitors, Costs, Settings, Ask HFAO. The
  Ask HFAO copilot routes natural-language questions through the MCP
  surface.
- **Auth and multi-tenancy §13** (PR #11). `Permission` enum (15 perms),
  cumulative role grants, generic OIDC client with RS256 + JWKS
  verification, HF Hub token verifier. Settings changes route through
  RBAC + audit log.
- **Tier-2 instrumentation harness** (PR #15). One shared catalog covering
  CrewAI, AutoGen, DSPy, LlamaIndex, Haystack, Pydantic AI, Google ADK,
  AWS Strands, LiteLLM, MCP. Community PRs add a framework with a single
  catalog entry; no per-framework AC test file.
- **`hfao parquet export`** (PR #15). Materialises hourly Parquet shards
  per §4.4 partition convention; optional `--hf-bucket` upload via
  `huggingface_hub`. The v1 manual warm-tier path per §16 Q-13.

### Added — Docs

- **README rewrite** (PR #15). Leads with the Q-9 three-pillar framing
  (standards-nativeness, MCP-native queryability, closed eval-trace
  loop). Insight-surface table, deployment-shape matrix, framework
  tiers, quickstart with CI snippet.

### Resolved §16 questions

The audit trail of every design decision since launch lives in §16.1:

- **Q-9** (2026-04-19) — three-pillar reframe.
- **Q-10 / Q-10a** (2026-04-19 / 2026-05-31) — Experiment primitive
  greenlight (A/A/B).
- **Q-11** (2026-04-19) — SvelteKit console deferred to v2.0.
- **Q-12** (2026-04-19) — Tier 1 / Tier 2 framework split.
- **Q-13** (2026-04-19) — DuckLake auto-sync deferred to v1.1, CLI
  parquet export in v1.
- **Q-14** (2026-04-19) — AgentXAgent stays in a separate repo.
- **Q-17** (2026-05-29) — Week 6 ordering.

### Filed (pending greenlight)

- **Q-18** — proactive anomaly surfacing (`Insight` schema + `AnomalyEngine`).
- **Q-19** — insight routing intelligence (`Subscription` model).
- **Q-20** — pull Stage 2 counterfactual replay forward from Week 11.

## [0.1.0] — M1: Walking skeleton

Initial release. OTLP ingest, DuckDB hot tier, cockpit renders a trace,
MCP returns `list_traces` / `get_trace`, single-binary deploy via
`hfao up`. Weeks 1–5 of §15.2.
