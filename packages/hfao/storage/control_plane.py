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

CREATE TABLE IF NOT EXISTS monitors (
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id                  TEXT NOT NULL,
    name                TEXT NOT NULL,
    nl_description      TEXT NOT NULL,
    sql_query           TEXT NOT NULL,
    threshold           REAL NOT NULL,
    operator            TEXT NOT NULL CHECK (operator IN ('gt','lt','gte','lte','eq')),
    window              TEXT NOT NULL,
    channels            TEXT NOT NULL DEFAULT '[]',   -- JSON list[str]
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    last_evaluated_at   TEXT,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    monitor_id          TEXT NOT NULL,
    fired_at            TEXT NOT NULL,
    actual_value        REAL NOT NULL,
    threshold           REAL NOT NULL,
    operator            TEXT NOT NULL,
    message             TEXT NOT NULL,
    channels_notified   TEXT NOT NULL DEFAULT '[]',
    delivery_errors     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS retention_policies (
    project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    hot_days      INTEGER NOT NULL DEFAULT 30,
    warm_days     INTEGER NOT NULL DEFAULT 365,
    bodies_days   INTEGER NOT NULL DEFAULT 90,
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

-- §16 Q-10a experiment family. Definition is immutable; Experiment is mutable
-- runtime state with an FK to the definition (mirrors §4.1 PromptVersion /
-- PromptLabel).
CREATE TABLE IF NOT EXISTS experiment_definitions (
    project_id                TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id                        TEXT NOT NULL,
    name                      TEXT NOT NULL,
    description               TEXT,
    dataset_id                TEXT NOT NULL,
    evaluator_ids             TEXT NOT NULL,             -- JSON list[str]
    variants                  TEXT NOT NULL,             -- JSON list[Variant]
    held_constant             TEXT NOT NULL DEFAULT '{}',-- JSON dict[str,str]
    planned_runs_per_variant  INTEGER NOT NULL,
    created_by                TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS experiments (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id              TEXT NOT NULL,
    definition_id   TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending','running','complete','aborted')),
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    PRIMARY KEY (project_id, id),
    FOREIGN KEY (project_id, definition_id)
        REFERENCES experiment_definitions(project_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS experiment_pairings (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL,
    dataset_item_id     TEXT NOT NULL,
    seed                INTEGER NOT NULL,
    run_ids_by_variant  TEXT NOT NULL DEFAULT '{}'    -- JSON dict[str,str]
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    project_id     TEXT NOT NULL,
    experiment_id  TEXT NOT NULL,
    variant_id     TEXT NOT NULL,
    pairing_id     TEXT,
    trace_id       TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    PRIMARY KEY (project_id, experiment_id, trace_id)
);

CREATE TABLE IF NOT EXISTS experiment_verdicts (
    id                    TEXT PRIMARY KEY,
    experiment_id         TEXT NOT NULL,
    evaluator             TEXT NOT NULL,
    ranking               TEXT NOT NULL,                -- JSON list[str]
    mean_by_variant       TEXT NOT NULL,                -- JSON dict[str,float]
    ci_low_by_variant     TEXT NOT NULL,                -- JSON dict[str,float]
    ci_high_by_variant    TEXT NOT NULL,                -- JSON dict[str,float]
    n_pairings            INTEGER NOT NULL,
    paired_test           TEXT NOT NULL,
    p_value               REAL,
    computed_at           TEXT NOT NULL
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

    def create_project_with_id(
        self, *, project_id: str, workspace_id: str, slug: str, name: str
    ) -> dict[str, Any]:
        """Insert a project with a caller-supplied id.

        Used by the cockpit's single-binary auto-bootstrap (``_ensure_project``)
        when an event already references a project_id that the control plane
        has never seen — the literal id must be preserved so the events table
        and the ``projects`` row stay joined.
        """
        with self._lock:
            self._con.execute(
                "INSERT INTO projects (id, workspace_id, slug, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, workspace_id, slug, name, _now()),
            )
        return self.get_project(project_id)

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

    # ---- monitors (§8.4 Monitor / Alert) ----

    def create_monitor(
        self,
        *,
        project_id: str,
        name: str,
        nl_description: str,
        sql_query: str,
        threshold: float,
        operator: str,
        window: str,
        channels: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if operator not in ("gt", "lt", "gte", "lte", "eq"):
            raise ValueError(f"invalid operator: {operator!r}")
        mid = _new_id("mon")
        with self._lock:
            self._con.execute(
                "INSERT INTO monitors (project_id, id, name, nl_description, "
                "sql_query, threshold, operator, window, channels, enabled, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    mid,
                    name,
                    nl_description,
                    sql_query,
                    float(threshold),
                    operator,
                    window,
                    json.dumps(channels or []),
                    1 if enabled else 0,
                    _now(),
                ),
            )
        return self.get_monitor(project_id=project_id, monitor_id=mid)

    def get_monitor(
        self, *, project_id: str, monitor_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM monitors WHERE project_id = ? AND id = ?",
                (project_id, monitor_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, monitor_id))
        return dict(row)

    def list_monitors(
        self, *, project_id: str, only_enabled: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM monitors WHERE project_id = ?"
        params: list[Any] = [project_id]
        if only_enabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_monitor_enabled(
        self, *, project_id: str, monitor_id: str, enabled: bool
    ) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE monitors SET enabled = ? WHERE project_id = ? AND id = ?",
                (1 if enabled else 0, project_id, monitor_id),
            )

    def mark_monitor_evaluated(
        self, *, project_id: str, monitor_id: str, at: str | None = None
    ) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE monitors SET last_evaluated_at = ? "
                "WHERE project_id = ? AND id = ?",
                (at or _now(), project_id, monitor_id),
            )

    def record_alert(
        self,
        *,
        project_id: str,
        monitor_id: str,
        fired_at: str,
        actual_value: float,
        threshold: float,
        operator: str,
        message: str,
        channels_notified: list[str] | None = None,
        delivery_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        aid = _new_id("alrt")
        with self._lock:
            self._con.execute(
                "INSERT INTO monitor_alerts (id, project_id, monitor_id, fired_at, "
                "actual_value, threshold, operator, message, channels_notified, "
                "delivery_errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    aid,
                    project_id,
                    monitor_id,
                    fired_at,
                    float(actual_value),
                    float(threshold),
                    operator,
                    message,
                    json.dumps(channels_notified or []),
                    json.dumps(delivery_errors or []),
                ),
            )
            row = self._con.execute(
                "SELECT * FROM monitor_alerts WHERE id = ?", (aid,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_alerts(
        self, *, project_id: str, monitor_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM monitor_alerts WHERE project_id = ?"
        params: list[Any] = [project_id]
        if monitor_id is not None:
            sql += " AND monitor_id = ?"
            params.append(monitor_id)
        sql += " ORDER BY fired_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---- retention policies (§6.4) ----

    def upsert_retention_policy(
        self,
        *,
        project_id: str,
        hot_days: int = 30,
        warm_days: int = 365,
        bodies_days: int = 90,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create or update the retention policy for a project."""
        if min(hot_days, warm_days, bodies_days) < 0:
            raise ValueError("retention day-counts must be ≥ 0 (0 disables that tier)")
        with self._lock:
            self._con.execute(
                "INSERT INTO retention_policies (project_id, hot_days, warm_days, "
                "bodies_days, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET "
                "hot_days = excluded.hot_days, "
                "warm_days = excluded.warm_days, "
                "bodies_days = excluded.bodies_days, "
                "enabled = excluded.enabled, "
                "updated_at = excluded.updated_at",
                (
                    project_id,
                    int(hot_days),
                    int(warm_days),
                    int(bodies_days),
                    1 if enabled else 0,
                    _now(),
                ),
            )
        return self.get_retention_policy(project_id=project_id)

    def get_retention_policy(self, *, project_id: str) -> dict[str, Any]:
        """Get the retention policy for a project; create default on miss."""
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM retention_policies WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return self.upsert_retention_policy(project_id=project_id)
        return dict(row)

    def list_retention_policies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM retention_policies ORDER BY project_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- experiments (§4.1 + §16 Q-10a) ----

    def create_experiment_definition(
        self,
        *,
        project_id: str,
        name: str,
        dataset_id: str,
        evaluator_ids: list[str],
        variants: list[dict[str, Any]],
        held_constant: dict[str, str] | None = None,
        planned_runs_per_variant: int,
        created_by: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Insert an immutable experiment definition. Returns the row."""
        if planned_runs_per_variant <= 0:
            raise ValueError("planned_runs_per_variant must be > 0")
        if not variants:
            raise ValueError("at least one variant required")
        def_id = _new_id("expdef")
        with self._lock:
            self._con.execute(
                "INSERT INTO experiment_definitions "
                "(project_id, id, name, description, dataset_id, evaluator_ids, "
                "variants, held_constant, planned_runs_per_variant, created_by, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    def_id,
                    name,
                    description,
                    dataset_id,
                    json.dumps(evaluator_ids),
                    json.dumps(variants),
                    json.dumps(held_constant or {}),
                    int(planned_runs_per_variant),
                    created_by,
                    _now(),
                ),
            )
        return self.get_experiment_definition(project_id=project_id, def_id=def_id)

    def get_experiment_definition(
        self, *, project_id: str, def_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM experiment_definitions "
                "WHERE project_id = ? AND id = ?",
                (project_id, def_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, def_id))
        return dict(row)

    def list_experiment_definitions(
        self, *, project_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM experiment_definitions "
                "WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_experiment(
        self,
        *,
        project_id: str,
        definition_id: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        """Materialise a runtime experiment row pointing at ``definition_id``."""
        if status not in ("pending", "running", "complete", "aborted"):
            raise ValueError(f"invalid status: {status!r}")
        # FK enforcement: definition must exist in this project.
        self.get_experiment_definition(project_id=project_id, def_id=definition_id)
        exp_id = _new_id("exp")
        with self._lock:
            self._con.execute(
                "INSERT INTO experiments (project_id, id, definition_id, status, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, exp_id, definition_id, status, _now()),
            )
        return self.get_experiment(project_id=project_id, experiment_id=exp_id)

    def get_experiment(
        self, *, project_id: str, experiment_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM experiments WHERE project_id = ? AND id = ?",
                (project_id, experiment_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, experiment_id))
        return dict(row)

    def list_experiments(self, *, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM experiments WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_experiment_status(
        self,
        *,
        project_id: str,
        experiment_id: str,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        if status not in ("pending", "running", "complete", "aborted"):
            raise ValueError(f"invalid status: {status!r}")
        with self._lock:
            self._con.execute(
                "UPDATE experiments SET status = ?, "
                "started_at = COALESCE(?, started_at), "
                "finished_at = COALESCE(?, finished_at) "
                "WHERE project_id = ? AND id = ?",
                (status, started_at, finished_at, project_id, experiment_id),
            )

    def repoint_experiment_definition(
        self, *, project_id: str, experiment_id: str, new_definition_id: str
    ) -> dict[str, Any]:
        """Update an experiment's active definition_id without mutating the
        old definition. The Q-10a.3 immutable-definition contract: changing
        intent on a launched experiment produces a new definition row, then
        repoints the experiment to it."""
        self.get_experiment_definition(project_id=project_id, def_id=new_definition_id)
        with self._lock:
            self._con.execute(
                "UPDATE experiments SET definition_id = ? "
                "WHERE project_id = ? AND id = ?",
                (new_definition_id, project_id, experiment_id),
            )
        return self.get_experiment(project_id=project_id, experiment_id=experiment_id)

    def record_pairing(
        self,
        *,
        experiment_id: str,
        dataset_item_id: str,
        seed: int,
        run_ids_by_variant: dict[str, str],
    ) -> dict[str, Any]:
        pair_id = _new_id("pair")
        with self._lock:
            self._con.execute(
                "INSERT INTO experiment_pairings (id, experiment_id, "
                "dataset_item_id, seed, run_ids_by_variant) VALUES (?, ?, ?, ?, ?)",
                (
                    pair_id,
                    experiment_id,
                    dataset_item_id,
                    int(seed),
                    json.dumps(run_ids_by_variant),
                ),
            )
            row = self._con.execute(
                "SELECT * FROM experiment_pairings WHERE id = ?", (pair_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_pairings(self, *, experiment_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM experiment_pairings WHERE experiment_id = ? "
                "ORDER BY dataset_item_id, seed",
                (experiment_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_experiment_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        variant_id: str,
        trace_id: str,
        seed: int,
        started_at: str,
        pairing_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO experiment_runs (project_id, experiment_id, "
                "variant_id, pairing_id, trace_id, seed, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    experiment_id,
                    variant_id,
                    pairing_id,
                    trace_id,
                    int(seed),
                    started_at,
                ),
            )
            row = self._con.execute(
                "SELECT * FROM experiment_runs WHERE project_id = ? "
                "AND experiment_id = ? AND trace_id = ?",
                (project_id, experiment_id, trace_id),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_experiment_runs(
        self, *, project_id: str, experiment_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM experiment_runs WHERE project_id = ? AND experiment_id = ? "
                "ORDER BY started_at",
                (project_id, experiment_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_verdict(
        self,
        *,
        experiment_id: str,
        evaluator: str,
        ranking: list[str],
        mean_by_variant: dict[str, float],
        ci_low_by_variant: dict[str, float],
        ci_high_by_variant: dict[str, float],
        n_pairings: int,
        paired_test: str,
        p_value: float | None = None,
    ) -> dict[str, Any]:
        vid = _new_id("vrd")
        with self._lock:
            self._con.execute(
                "INSERT INTO experiment_verdicts (id, experiment_id, evaluator, "
                "ranking, mean_by_variant, ci_low_by_variant, ci_high_by_variant, "
                "n_pairings, paired_test, p_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vid,
                    experiment_id,
                    evaluator,
                    json.dumps(ranking),
                    json.dumps(mean_by_variant),
                    json.dumps(ci_low_by_variant),
                    json.dumps(ci_high_by_variant),
                    int(n_pairings),
                    paired_test,
                    p_value,
                    _now(),
                ),
            )
            row = self._con.execute(
                "SELECT * FROM experiment_verdicts WHERE id = ?", (vid,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_verdicts(
        self, *, experiment_id: str, evaluator: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM experiment_verdicts WHERE experiment_id = ?"
        params: list[Any] = [experiment_id]
        if evaluator is not None:
            sql += " AND evaluator = ?"
            params.append(evaluator)
        sql += " ORDER BY computed_at DESC"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

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
