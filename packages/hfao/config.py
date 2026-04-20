"""HFAO runtime configuration.

SPEC Appendix A. Env-driven settings consumed by the ingest server, cockpit,
and MCP server. Values are resolved lazily at service startup via
``HFAOConfig.from_env()`` and frozen for the process lifetime.
"""

from __future__ import annotations

import os
from typing import Literal

from msgspec import Struct

Backend = Literal["duckdb", "clickhouse"]
JudgeProvider = Literal["anthropic", "openai", "hf"]


class HFAOConfig(Struct, frozen=True, kw_only=True):
    base_url: str = "http://localhost:4318"
    api_key: str | None = None
    project: str = "default"
    environment: str = "production"

    backend: Backend = "duckdb"
    duckdb_path: str = "/data/hfao.duckdb"
    clickhouse_dsn: str | None = None

    control_plane_dsn: str = "sqlite:///data/control.db"
    redis_url: str | None = None
    bodies_path: str = "/data/bodies"
    hf_bucket: str | None = None

    mcp_read_only: bool = False

    judge_provider: JudgeProvider = "anthropic"
    judge_model: str = "claude-haiku-4-5"

    redaction_profile: str = "standard"

    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None

    # Ingest server
    ingest_host: str = "0.0.0.0"
    ingest_port: int = 4318
    ingest_max_body_bytes: int = 4 * 1024 * 1024
    body_offload_threshold_bytes: int = 64 * 1024

    @classmethod
    def from_env(cls) -> HFAOConfig:
        def _bool(key: str, default: bool) -> bool:
            raw = os.environ.get(key)
            if raw is None:
                return default
            return raw.lower() in ("1", "true", "yes", "on")

        def _int(key: str, default: int) -> int:
            raw = os.environ.get(key)
            return int(raw) if raw else default

        backend_raw = os.environ.get("HFAO_BACKEND", "duckdb").lower()
        if backend_raw not in ("duckdb", "clickhouse"):
            raise ValueError(
                f"HFAO_BACKEND must be duckdb or clickhouse, got: {backend_raw!r}"
            )
        judge_provider_raw = os.environ.get("HFAO_JUDGE_PROVIDER", "anthropic").lower()
        if judge_provider_raw not in ("anthropic", "openai", "hf"):
            raise ValueError(
                f"HFAO_JUDGE_PROVIDER must be anthropic/openai/hf, "
                f"got: {judge_provider_raw!r}"
            )

        return cls(
            base_url=os.environ.get("HFAO_BASE_URL", "http://localhost:4318"),
            api_key=os.environ.get("HFAO_API_KEY"),
            project=os.environ.get("HFAO_PROJECT", "default"),
            environment=os.environ.get("HFAO_ENVIRONMENT", "production"),
            backend=backend_raw,
            duckdb_path=os.environ.get("HFAO_DUCKDB_PATH", "/data/hfao.duckdb"),
            clickhouse_dsn=os.environ.get("HFAO_CLICKHOUSE_DSN"),
            control_plane_dsn=os.environ.get(
                "HFAO_CONTROL_PLANE_DSN", "sqlite:///data/control.db"
            ),
            redis_url=os.environ.get("HFAO_REDIS_URL"),
            bodies_path=os.environ.get("HFAO_BODIES_PATH", "/data/bodies"),
            hf_bucket=os.environ.get("HFAO_HF_BUCKET"),
            mcp_read_only=_bool("HFAO_MCP_READ_ONLY", False),
            judge_provider=judge_provider_raw,
            judge_model=os.environ.get("HFAO_JUDGE_MODEL", "claude-haiku-4-5"),
            redaction_profile=os.environ.get("HFAO_REDACTION_PROFILE", "standard"),
            oidc_issuer_url=os.environ.get("HFAO_OIDC_ISSUER_URL"),
            oidc_client_id=os.environ.get("HFAO_OIDC_CLIENT_ID"),
            oidc_client_secret=os.environ.get("HFAO_OIDC_CLIENT_SECRET"),
            ingest_host=os.environ.get("HFAO_INGEST_HOST", "0.0.0.0"),
            ingest_port=_int("HFAO_INGEST_PORT", 4318),
            ingest_max_body_bytes=_int("HFAO_INGEST_MAX_BODY_BYTES", 4 * 1024 * 1024),
            body_offload_threshold_bytes=_int(
                "HFAO_BODY_OFFLOAD_THRESHOLD_BYTES", 64 * 1024
            ),
        )


__all__ = ["HFAOConfig", "Backend", "JudgeProvider"]
