"""Retention policy schema (SPEC §6.4).

Per-project. A daily worker (:mod:`hfao.compute.retention`) reads policies
from the control plane and prunes:

  * **hot tier** — DELETE from ``events`` / ``scores`` / ``causal_edges``
    older than ``hot_days``.
  * **warm tier** — DELETE Parquet shards from HF Buckets older than
    ``warm_days`` (deferred to v1.1 per §16 Q-13 — auto-sync worker not in v1).
  * **bodies** — purge offloaded body files older than ``bodies_days``.

Defaults below match §6.4. Disabling retention per project means setting the
corresponding ``*_days`` to ``0`` (no-op), not deleting the policy row.
"""

from __future__ import annotations

from msgspec import Struct


class RetentionPolicy(Struct, kw_only=True):
    project_id: str
    hot_days: int = 30
    warm_days: int = 365
    bodies_days: int = 90
    enabled: bool = True
