# HFAO Makefile — dev-flow convenience targets.
#
# All real build metadata lives in pyproject.toml + uv.lock. This file only
# wraps frequent workflows behind short names.

.PHONY: help sync test lint typecheck ac cli-demo cli-screenshot clean

help:
	@echo "HFAO — convenience targets:"
	@echo "  make sync           Install/refresh deps via uv"
	@echo "  make test           Run the full acceptance suite"
	@echo "  make lint           ruff check"
	@echo "  make typecheck      pyright --strict on packages/hfao"
	@echo "  make ac             lint + typecheck + test (CI-equivalent)"
	@echo "  make cli-demo       hfao ingest send --count 5 && hfao query 5 && hfao dashboard"
	@echo "  make cli-screenshot Regenerate docs/hfao-cli.png"
	@echo "  make clean          Remove demo DuckDB / bodies / pycache"

sync:
	uv sync --extra dev

test:
	uv run pytest tests/acceptance -v

lint:
	uv run ruff check .

typecheck:
	uv run pyright packages/hfao

ac: lint typecheck test

cli-demo:
	@mkdir -p .hfao-demo
	@HFAO_DUCKDB_PATH=.hfao-demo/hfao.duckdb \
	 HFAO_BODIES_PATH=.hfao-demo/bodies \
	 HFAO_PROJECT=demo \
	 uv run hfao ingest send --count 5 --vary --seed 7
	@HFAO_DUCKDB_PATH=.hfao-demo/hfao.duckdb \
	 HFAO_BODIES_PATH=.hfao-demo/bodies \
	 HFAO_PROJECT=demo \
	 uv run hfao query 5
	@HFAO_DUCKDB_PATH=.hfao-demo/hfao.duckdb \
	 HFAO_BODIES_PATH=.hfao-demo/bodies \
	 HFAO_PROJECT=demo \
	 uv run hfao dashboard

cli-screenshot:
	uv run python scripts/generate_cli_screenshot.py

clean:
	rm -rf .hfao-demo
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
