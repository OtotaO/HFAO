"""Experiment schema (SPEC §4.1 + §16 Q-10a resolution, 2026-05-31).

The unit of systematic comparison: CI gates, prompt A/Bs, model bake-offs,
tournament rounds. Six Structs cover the family:

  * :class:`ExperimentDefinition` — **immutable** contract. Carries the
    task definition (``dataset_id``), the evaluator list, the variant set,
    the held-constant axes, and the planned run count. Edits produce a new
    definition version, mirroring the §4.1 ``PromptVersion`` / ``PromptLabel``
    pattern (Q-10a.3 → Option B).
  * :class:`Experiment` — **mutable** runtime state. Holds the FK to the
    active definition, lifecycle status, and timestamps. Updating
    ``description`` or ``variants`` rebuilds a definition, not this row.
  * :class:`Variant` — one side of a comparison; first-class object so the
    runner can enforce "every run in this experiment resolves to exactly
    one variant" as a schema invariant rather than a tag-soup convention.
    ``config_hash`` is SHA256 of the canonical-JSON serialisation of
    ``config`` (Q-10a.1 → Option A); a forward-compatible ``sha256:`` URI
    prefix leaves room for a content-addressable store in v1.2.
  * :class:`Pairing` — matched-set: same dataset item, same seed, different
    variant. Makes mis-pairing a schema error instead of a silent
    statistical bug.
  * :class:`Verdict` — ranked outcome with bootstrap CIs and a paired-test
    p-value. One Verdict per evaluator (Q-10a.2 → Option A); append-only,
    preserves the audit trail when α or paired test changes.
  * :class:`ExperimentRun` — thin join: links one trace back to its
    experiment, variant, and pairing. Keeps the trace table clean (no
    nullable experiment/variant/pairing columns on the hot path).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal, cast

from msgspec import Struct, field

VariantAxis = Literal[
    "prompt",
    "model",
    "tools",
    "topology",
    "agent_config",
    "system_prompt",
    "other",
]

ExperimentStatus = Literal["pending", "running", "complete", "aborted"]

PairedTest = Literal[
    "wilcoxon_signed_rank",
    "paired_t",
    "sign_test",
    "none",
]


def canonical_config_hash(config: dict[str, str] | dict[str, Any]) -> str:
    """SHA256 of the canonical-JSON serialisation of ``config``.

    Canonical-JSON here means: ``sort_keys=True``, no insignificant
    whitespace, ``default=str`` for non-string atoms. Returned with the
    ``sha256:`` URI prefix so future content-addressable storage backends
    (e.g. ``content://...``) can land without a breaking change.
    """
    serialised = json.dumps(
        cast("dict[str, Any]", config),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialised.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# --------------------------------------------------------------------------- #


def _empty_str_dict() -> dict[str, str]:
    return cast("dict[str, str]", {})


def _empty_float_dict() -> dict[str, float]:
    return cast("dict[str, float]", {})


def _empty_str_list() -> list[str]:
    return cast("list[str]", [])


def _empty_run_map() -> dict[str, str]:
    return cast("dict[str, str]", {})


# --------------------------------------------------------------------------- #


class Variant(Struct, kw_only=True):
    """One side of a comparison. Multiple variants per experiment definition."""

    id: str
    name: str
    axis: VariantAxis
    config_hash: str
    config: dict[str, str] = field(default_factory=_empty_str_dict)


class ExperimentDefinition(Struct, kw_only=True):
    """Immutable contract per Q-10a.3 → Option B.

    A definition is created once and never edited. Changes to ``description``
    / ``variants`` / ``evaluator_ids`` produce a **new** definition; the
    :class:`Experiment` row's ``definition_id`` is updated to point at it.
    """

    project_id: str
    id: str
    name: str
    description: str | None = None
    dataset_id: str
    evaluator_ids: list[str]
    variants: list[Variant]
    held_constant: dict[str, str] = field(default_factory=_empty_str_dict)
    planned_runs_per_variant: int
    created_by: str
    created_at: datetime


class Experiment(Struct, kw_only=True):
    """Mutable runtime state. Holds the FK to its active definition."""

    project_id: str
    id: str
    definition_id: str
    status: ExperimentStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Pairing(Struct, kw_only=True):
    """Matched runs across variants: same task + seed, different variant.

    ``run_ids_by_variant`` is keyed by ``Variant.id`` and **must** cover
    every variant in the experiment's definition — the runner validates this
    on construction. Partial pairings (missing variants) are mis-pairings
    and would poison paired-statistics, so the schema rejects them up front.
    """

    id: str
    experiment_id: str
    dataset_item_id: str
    seed: int
    run_ids_by_variant: dict[str, str] = field(default_factory=_empty_run_map)


class Verdict(Struct, kw_only=True):
    """Ranked outcome for one evaluator, with bootstrap CIs."""

    experiment_id: str
    evaluator: str
    ranking: list[str] = field(default_factory=_empty_str_list)
    mean_by_variant: dict[str, float] = field(default_factory=_empty_float_dict)
    ci_low_by_variant: dict[str, float] = field(default_factory=_empty_float_dict)
    ci_high_by_variant: dict[str, float] = field(default_factory=_empty_float_dict)
    n_pairings: int
    paired_test: PairedTest
    p_value: float | None = None
    computed_at: datetime


class ExperimentRun(Struct, kw_only=True):
    """One trace's link back to its experiment / variant / pairing."""

    project_id: str
    experiment_id: str
    variant_id: str
    pairing_id: str | None = None
    trace_id: str
    seed: int
    started_at: datetime


__all__ = [
    "Experiment",
    "ExperimentDefinition",
    "ExperimentRun",
    "ExperimentStatus",
    "PairedTest",
    "Pairing",
    "Variant",
    "VariantAxis",
    "Verdict",
    "canonical_config_hash",
]
