#!/usr/bin/env bash
#
# HFAO end-to-end demo — no API keys required.
#
# Walks the closed eval-trace loop in one terminal session:
#
#   1. `hfao migrate`        — initialise the hot-tier + control-plane schemas
#   2. `hfao up`             — boot the Cockpit (Gradio :PORT) in the background
#   3. sample agent          — emit synthetic OTel GenAI spans into HFAO via the
#                              in-process ingest path (`hfao ingest send`); this
#                              stands in for "point your agent at HFAO"
#   4. `hfao query`          — print the resulting trace
#   5. seed + `hfao eval run`— score a golden dataset with a CI gate and print
#                              the eval. The "LM" is the built-in `echo` runtime
#                              (a stub that returns the input unchanged), so the
#                              gate passes deterministically with NO keys.
#
# Everything is written under a throwaway $HFAO_DEMO_HOME (default: a fresh
# mktemp dir) so the demo never touches your real data and is re-runnable.
#
# This is asciinema-ready: `asciinema rec --command scripts/demo.sh`.
#
# Usage:
#   scripts/demo.sh                 # full run, boots the cockpit
#   HFAO_DEMO_NO_UP=1 scripts/demo.sh   # skip the long-running `hfao up` boot
#
set -euo pipefail

# --- locate the repo root (this script lives in scripts/) -------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# `hfao up` imports apps.cockpit.cockpit, which lives at the repo root and is
# not part of the wheel. Put the repo root on PYTHONPATH so the demo works from
# a source checkout. (`hfao ingest send` / `query` / `eval run` need only the
# installed `hfao` package and work from a bare `pip install hfao` too.)
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# --- isolated, throwaway data home ------------------------------------------
HFAO_DEMO_HOME="${HFAO_DEMO_HOME:-$(mktemp -d -t hfao-demo.XXXXXX)}"
mkdir -p "${HFAO_DEMO_HOME}"
export HFAO_PROJECT="${HFAO_PROJECT:-demo}"
export HFAO_DUCKDB_PATH="${HFAO_DEMO_HOME}/hfao.duckdb"
export HFAO_CONTROL_PLANE_DSN="sqlite:///${HFAO_DEMO_HOME}/control.db"
export HFAO_BODIES_PATH="${HFAO_DEMO_HOME}/bodies"

PORT="${HFAO_DEMO_PORT:-7860}"
HOST="${HFAO_DEMO_HOST:-127.0.0.1}"

# Prefer `uv run` when available (it resolves the project venv); fall back to
# the `hfao` console script / `python -m`.
if command -v uv >/dev/null 2>&1; then
  HFAO=(uv run hfao)
  PY=(uv run python)
elif command -v hfao >/dev/null 2>&1; then
  HFAO=(hfao)
  PY=(python)
else
  HFAO=(python -m hfao.cli)
  PY=(python)
fi

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

UP_PID=""
cleanup() {
  if [[ -n "${UP_PID}" ]] && kill -0 "${UP_PID}" 2>/dev/null; then
    say "tearing down the cockpit (pid ${UP_PID})"
    kill "${UP_PID}" 2>/dev/null || true
    wait "${UP_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

say "data home: ${HFAO_DEMO_HOME}   project: ${HFAO_PROJECT}"
"${HFAO[@]}" --version

# --- 1. migrate -------------------------------------------------------------
say "hfao migrate — initialise schemas"
"${HFAO[@]}" migrate

# --- 2. boot the cockpit ----------------------------------------------------
if [[ "${HFAO_DEMO_NO_UP:-0}" != "1" ]]; then
  say "hfao up — booting the Cockpit on http://${HOST}:${PORT}"
  "${HFAO[@]}" up --host "${HOST}" --port "${PORT}" --no-mcp-server \
      >"${HFAO_DEMO_HOME}/cockpit.log" 2>&1 &
  UP_PID=$!
  for _ in $(seq 1 60); do
    if curl -sf "http://${HOST}:${PORT}/" -o /dev/null 2>/dev/null; then
      printf '   cockpit is live (HTTP 200) at http://%s:%s/\n' "${HOST}" "${PORT}"
      break
    fi
    if ! kill -0 "${UP_PID}" 2>/dev/null; then
      echo "   cockpit failed to boot; log:" >&2
      cat "${HFAO_DEMO_HOME}/cockpit.log" >&2
      exit 1
    fi
    sleep 1
  done
else
  say "hfao up — skipped (HFAO_DEMO_NO_UP=1)"
fi

# --- 3. point a sample agent at HFAO ----------------------------------------
# A real agent calls `hfao.init(project=...)` and its OTel/OpenInference spans
# flow to the OTLP endpoint. Here we drive the same in-process ingest pipeline
# (normalize -> redact -> body-offload -> store) with three synthetic spans, so
# the demo needs no external agent process or model keys.
say "sample agent emits 3 OTel GenAI spans into HFAO"
"${HFAO[@]}" ingest send --count 3 --vary --seed 7

# --- 4. print the trace -----------------------------------------------------
say "hfao query — the trace HFAO captured"
"${HFAO[@]}" query 3

# --- 5. closed eval-trace loop: seed goldens + gated eval (no keys) ---------
say "seed a deterministic golden dataset (expected_output == input)"
"${PY[@]}" scripts/demo_seed_eval.py

say "hfao eval run — score the goldens with a CI gate (echo runtime, no keys)"
"${HFAO[@]}" eval run goldens \
    --evaluators exact_match,levenshtein_ratio \
    --gate "exact_match>=0.9"

say "done — trace ingested, eval scored, gate passed. No API keys were used."
