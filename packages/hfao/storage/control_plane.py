"""Control plane: workspaces, projects, API keys, prompts, datasets, settings.

SPEC §6.1 (SQLite for single-binary, Postgres for Docker/K8s), §13 (auth),
§4.1 (prompt/dataset/annotation Structs), Appendix A (HFAO_CONTROL_PLANE_DSN).

SQLite is the default per §6.1. Postgres is referenced via DSN but the v1
single-binary shape ships with SQLite. When the DSN begins with
``postgresql://`` the caller must install the optional psycopg dependency;
this module raises a clear error in that case until Docker/K8s shape lands.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id           TEXT PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (workspace_id, slug)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    key_hash      TEXT NOT NULL UNIQUE,
    prefix        TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    type           TEXT NOT NULL CHECK (type IN ('text','chat')),
    content        TEXT NOT NULL,
    config         TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    commit_message TEXT,
    PRIMARY KEY (project_id, name, version)
);

CREATE TABLE IF NOT EXISTS prompt_labels (
    project_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    label      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name, label),
    FOREIGN KEY (project_id, name, version)
        REFERENCES prompt_versions(project_id, name, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS datasets (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS dataset_items (
    project_id            TEXT NOT NULL,
    dataset_id            TEXT NOT NULL,
    id                    TEXT NOT NULL,
    input                 TEXT NOT NULL,
    expected_output       TEXT,
    metadata              TEXT NOT NULL DEFAULT '{}',
    source_trace_id       TEXT,
    source_observation_id TEXT,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (project_id, dataset_id, id),
    FOREIGN KEY (project_id, dataset_id) REFERENCES datasets(project_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}',
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotation_queues (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    filter_query TEXT NOT NULL,
    score_schema TEXT NOT NULL DEFAULT '[]',   -- JSON array of score names
    created_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS annotation_items (
    queue_id       TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    observation_id TEXT NOT NULL DEFAULT '',   -- '' sentinel for trace-level items (§4.5)
    assigned_to    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','in_progress','completed','skipped')),
    completed_at   TEXT,
    PRIMARY KEY (queue_id, trace_id, observation_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class ControlPlane:
    """SQLite-backed control plane.

    Thread-safe via a single RLock around the connection; SQLite connections
    are not safe to share across threads without this.
    """

    def __init__(self, dsn: str = "sqlite:///:memory:") -> None:
        if not dsn.startswith("sqlite"):
            raise NotImplementedError(
                "Only sqlite:// DSNs are supported in v1 single-binary shape; "
                "Postgres support ships with the Docker/K8s shape."
            )
        path = dsn.removeprefix("sqlite:///")
        if path == ":memory:":
            self._path: str = ":memory:"
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(p)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        self._con.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def init_schema(self) -> None:
        with self._lock:
            self._con.executescript(_SCHEMA)

    # ---- workspaces ----

    def create_workspace(self, *, slug: str, name: str) -> dict[str, Any]:
        wid = _new_id("ws")
        with self._lock:
            self._con.execute(
                "INSERT INTO workspaces (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                (wid, slug, name, _now()),
            )
        return self.get_workspace(wid)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return dict(row)

    def get_workspace_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM workspaces WHERE slug = ?", (slug,)
            ).fetchone()
        return dict(row) if row else None

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM workspaces ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- projects ----

    def create_project(
        self, *, workspace_id: str, slug: str, name: str
    ) -> dict[str, Any]:
        pid = _new_id("prj")
        with self._lock:
            self._con.execute(
                "INSERT INTO projects (id, workspace_id, slug, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, workspace_id, slug, name, _now()),
            )
        return self.get_project(pid)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM projects WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- api keys ----

    def issue_api_key(
        self, *, workspace_id: str, role: str, name: str
    ) -> tuple[str, dict[str, Any]]:
        """Returns (raw_token, metadata). The raw token is only shown once."""
        raw = f"hfao_pat_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[: len("hfao_pat_") + 8]
        kid = _new_id("key")
        with self._lock:
            self._con.execute(
                "INSERT INTO api_keys (id, workspace_id, key_hash, prefix, role, name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kid, workspace_id, key_hash, prefix, role, name, _now()),
            )
            row = self._con.execute(
                "SELECT * FROM api_keys WHERE id = ?", (kid,)
            ).fetchone()
        assert row is not None
        return raw, dict(row)

    def verify_api_key(self, raw: str) -> dict[str, Any] | None:
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            self._con.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        return dict(row)

    def revoke_api_key(self, key_id: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ?",
                (_now(), key_id),
            )

    def list_api_keys(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, workspace_id, prefix, role, name, created_at, "
                "last_used_at, revoked_at FROM api_keys WHERE workspace_id = ? "
                "ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- prompts ----

    def create_prompt_version(
        self,
        *,
        project_id: str,
        name: str,
        type: str,  # noqa: A002 — matches SPEC §4.1 PromptVersion.type
        content: str,
        config: str = "{}",
        created_by: str,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_versions "
                "WHERE project_id = ? AND name = ?",
                (project_id, name),
            ).fetchone()
            assert row is not None
            version = int(row["v"]) + 1
            self._con.execute(
                "INSERT INTO prompt_versions (project_id, name, version, type, content, "
                "config, created_at, created_by, commit_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    name,
                    version,
                    type,
                    content,
                    config,
                    _now(),
                    created_by,
                    commit_message,
                ),
            )
            return self._get_prompt_version(project_id, name, version)

    def _get_prompt_version(
        self, project_id: str, name: str, version: int
    ) -> dict[str, Any]:
        row = self._con.execute(
            "SELECT * FROM prompt_versions WHERE project_id = ? AND name = ? AND version = ?",
            (project_id, name, version),
        ).fetchone()
        if row is None:
            raise KeyError((project_id, name, version))
        return dict(row)

    def set_prompt_label(
        self, *, project_id: str, name: str, label: str, version: int
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO prompt_labels (project_id, name, label, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id, name, label) DO UPDATE SET "
                "version = excluded.version, updated_at = excluded.updated_at",
                (project_id, name, label, version, _now()),
            )

    def get_prompt(
        self, *, project_id: str, name: str, label: str = "production"
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT pv.* FROM prompt_versions pv "
                "JOIN prompt_labels pl ON pl.project_id = pv.project_id "
                "  AND pl.name = pv.name AND pl.version = pv.version "
                "WHERE pv.project_id = ? AND pv.name = ? AND pl.label = ?",
                (project_id, name, label),
            ).fetchone()
        return dict(row) if row else None

    def list_prompt_versions(
        self, *, project_id: str, name: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM prompt_versions WHERE project_id = ? AND name = ? "
                "ORDER BY version DESC",
                (project_id, name),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_prompts(self, *, project_id: str) -> list[dict[str, Any]]:
        """Latest version of every prompt in a project (one row per name).

        Backs the §9.2 ``list_prompts`` MCP tool.
        """
        with self._lock:
            rows = self._con.execute(
                "SELECT pv.* FROM prompt_versions pv "
                "JOIN (SELECT name, MAX(version) AS v FROM prompt_versions "
                "      WHERE project_id = ? GROUP BY name) latest "
                "  ON latest.name = pv.name AND latest.v = pv.version "
                "WHERE pv.project_id = ? ORDER BY pv.name",
                (project_id, project_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- datasets (§4.1 Dataset / DatasetItem) ----

    def create_dataset(
        self, *, project_id: str, name: str, description: str | None = None
    ) -> dict[str, Any]:
        did = _new_id("ds")
        with self._lock:
            self._con.execute(
                "INSERT INTO datasets (project_id, id, name, description, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, did, name, description, _now()),
            )
        return self.get_dataset(project_id=project_id, dataset_id=did)

    def get_dataset(self, *, project_id: str, dataset_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM datasets WHERE project_id = ? AND id = ?",
                (project_id, dataset_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, dataset_id))
        return dict(row)

    def list_datasets(self, *, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM datasets WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_dataset_item(
        self,
        *,
        project_id: str,
        dataset_id: str,
        input: str,  # noqa: A002 — matches SPEC §4.1 DatasetItem.input
        expected_output: str | None = None,
        metadata: dict[str, str] | None = None,
        source_trace_id: str | None = None,
        source_observation_id: str | None = None,
    ) -> dict[str, Any]:
        # Ensure the dataset exists (and is in this project) before adding items.
        self.get_dataset(project_id=project_id, dataset_id=dataset_id)
        item_id = _new_id("dsi")
        with self._lock:
            self._con.execute(
                "INSERT INTO dataset_items (project_id, dataset_id, id, input, "
                "expected_output, metadata, source_trace_id, source_observation_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    dataset_id,
                    item_id,
                    input,
                    expected_output,
                    json.dumps(metadata or {}),
                    source_trace_id,
                    source_observation_id,
                    _now(),
                ),
            )
            row = self._con.execute(
                "SELECT * FROM dataset_items WHERE project_id = ? AND dataset_id = ? "
                "AND id = ?",
                (project_id, dataset_id, item_id),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_dataset_items(
        self, *, project_id: str, dataset_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM dataset_items WHERE project_id = ? AND dataset_id = ? "
                "ORDER BY created_at",
                (project_id, dataset_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- annotation queues (§4.1 AnnotationQueue / AnnotationItem) ----

    def create_annotation_queue(
        self,
        *,
        project_id: str,
        name: str,
        filter_query: str = "1=1",
        score_schema: list[str] | None = None,
    ) -> dict[str, Any]:
        qid = _new_id("aq")
        with self._lock:
            self._con.execute(
                "INSERT INTO annotation_queues (project_id, id, name, filter_query, "
                "score_schema, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    qid,
                    name,
                    filter_query,
                    json.dumps(score_schema or []),
                    _now(),
                ),
            )
        return self.get_annotation_queue(project_id=project_id, queue_id=qid)

    def get_annotation_queue(
        self, *, project_id: str, queue_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM annotation_queues WHERE project_id = ? AND id = ?",
                (project_id, queue_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, queue_id))
        return dict(row)

    def list_annotation_queues(self, *, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM annotation_queues WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def enqueue_annotation_item(
        self,
        *,
        queue_id: str,
        trace_id: str,
        observation_id: str | None = None,
        assigned_to: str | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        obs = observation_id or ""
        with self._lock:
            self._con.execute(
                "INSERT INTO annotation_items (queue_id, trace_id, observation_id, "
                "assigned_to, status) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (queue_id, trace_id, observation_id) DO UPDATE SET "
                "assigned_to = excluded.assigned_to, status = excluded.status",
                (queue_id, trace_id, obs, assigned_to, status),
            )
            row = self._con.execute(
                "SELECT * FROM annotation_items WHERE queue_id = ? AND trace_id = ? "
                "AND observation_id = ?",
                (queue_id, trace_id, obs),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_annotation_items(
        self, *, queue_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM annotation_items WHERE queue_id = ?"
        params: list[str] = [queue_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_annotation_item_status(
        self,
        *,
        queue_id: str,
        trace_id: str,
        observation_id: str | None,
        status: str,
        completed_at: str | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE annotation_items SET status = ?, completed_at = ? "
                "WHERE queue_id = ? AND trace_id = ? AND observation_id = ?",
                (status, completed_at, queue_id, trace_id, observation_id or ""),
            )

    # ---- audit log ----

    def record_audit(
        self,
        *,
        workspace_id: str,
        actor: str,
        action: str,
        target: str,
        details: str = "{}",
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO audit_log (id, workspace_id, actor, action, target, details, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_new_id("aud"), workspace_id, actor, action, target, details, _now()),
            )

    def list_audit(self, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM audit_log WHERE workspace_id = ? "
                "ORDER BY at DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


__all__ = ["ControlPlane"]
