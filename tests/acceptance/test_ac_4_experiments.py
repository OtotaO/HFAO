"""AC §4 experiments — schema + runner coverage (SPEC §4 + §16 Q-10a).

Covers the §16.2.4 AC sketch:

  - test_experiment_definition_immutable_after_launch
  - test_pairing_invariant_one_run_per_variant
  - test_verdict_paired_test_and_bootstrap_ci
  - test_verdict_p_value_monotonic_with_n
  - test_experiment_run_links_trace_to_variant
  - test_verdict_matrix_helper        (Q-10a.2 helper)
  - test_config_hash_canonical_deterministic  (Q-10a.1)
  - test_set_experiment_status_lifecycle

Cross-backend parity is exercised by the existing storage parity scaffold
(§6.7) once the ClickHouse backend gains an experiment DDL — Q-10a's
Postgres / SQLite control-plane CRUD is what lands in this PR.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.compute.eval import EvalContext, run_experiment, verdict_matrix
from hfao.compute.eval.experiments import (
    _bootstrap_ci,
    _wilcoxon_signed_rank,
    compute_verdicts,
)
from hfao.schema.experiments import (
    Experiment,
    ExperimentDefinition,
    ExperimentRun,
    Pairing,
    Variant,
    Verdict,
    canonical_config_hash,
)
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[DuckDBBackend]:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    yield b
    b.close()


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlPlane]:
    c = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    c.init_schema()
    yield c
    c.close()


@pytest.fixture
def project(control: ControlPlane) -> str:
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    return "p1"


@pytest.fixture
def dataset(control: ControlPlane, project: str) -> str:
    ds = control.create_dataset(project_id=project, name="bake-off")
    for q, a in (("2+2", "4"), ("3+3", "6"), ("5+5", "10")):
        control.add_dataset_item(
            project_id=project,
            dataset_id=ds["id"],
            input=q,
            expected_output=a,
        )
    return ds["id"]


# --------------------------------------------------------------------------- #
# Schema-shape tests
# --------------------------------------------------------------------------- #


def test_config_hash_canonical_deterministic() -> None:
    """Q-10a.1: SHA256 of canonical JSON; key order doesn't matter; prefix is fixed."""
    h1 = canonical_config_hash({"model": "haiku", "temperature": "0.2"})
    h2 = canonical_config_hash({"temperature": "0.2", "model": "haiku"})
    assert h1 == h2
    assert h1.startswith("sha256:")
    # Different config → different hash.
    assert canonical_config_hash({"model": "sonnet"}) != h1


def test_experiment_definition_round_trip(
    control: ControlPlane, project: str, dataset: str
) -> None:
    """Definition CRUD + variants JSON round-trip cleanly."""
    variants = [
        {
            "id": "a",
            "name": "baseline",
            "axis": "model",
            "config_hash": canonical_config_hash({"model": "haiku"}),
            "config": {"model": "haiku"},
        },
        {
            "id": "b",
            "name": "challenger",
            "axis": "model",
            "config_hash": canonical_config_hash({"model": "sonnet"}),
            "config": {"model": "sonnet"},
        },
    ]
    defn = control.create_experiment_definition(
        project_id=project,
        name="haiku vs sonnet",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=variants,
        planned_runs_per_variant=2,
        created_by="alice",
        held_constant={"system_prompt": "You are concise."},
    )
    assert defn["name"] == "haiku vs sonnet"
    parsed_variants = json.loads(defn["variants"])
    assert {v["id"] for v in parsed_variants} == {"a", "b"}
    # Validators reject zero / empty inputs.
    with pytest.raises(ValueError, match="planned_runs_per_variant"):
        control.create_experiment_definition(
            project_id=project,
            name="bad",
            dataset_id=dataset,
            evaluator_ids=["exact_match"],
            variants=variants,
            planned_runs_per_variant=0,
            created_by="alice",
        )
    with pytest.raises(ValueError, match="variant"):
        control.create_experiment_definition(
            project_id=project,
            name="bad",
            dataset_id=dataset,
            evaluator_ids=["exact_match"],
            variants=[],
            planned_runs_per_variant=1,
            created_by="alice",
        )


def test_experiment_definition_immutable_after_launch(
    control: ControlPlane, project: str, dataset: str
) -> None:
    """§16.2.4 line: edits produce a new definition; the old one survives."""
    variants_v1 = [
        {
            "id": "a",
            "name": "baseline",
            "axis": "prompt",
            "config_hash": canonical_config_hash({"prompt": "v1"}),
            "config": {"prompt": "v1"},
        }
    ]
    defn_v1 = control.create_experiment_definition(
        project_id=project,
        name="bake-off",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=variants_v1,
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(
        project_id=project, definition_id=defn_v1["id"]
    )
    assert exp["definition_id"] == defn_v1["id"]

    # Edit intent → new definition row (Q-10a.3 immutable contract).
    variants_v2 = [
        *variants_v1,
        {
            "id": "b",
            "name": "challenger",
            "axis": "prompt",
            "config_hash": canonical_config_hash({"prompt": "v2"}),
            "config": {"prompt": "v2"},
        },
    ]
    defn_v2 = control.create_experiment_definition(
        project_id=project,
        name="bake-off",
        description="now includes challenger",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=variants_v2,
        planned_runs_per_variant=1,
        created_by="alice",
    )
    control.repoint_experiment_definition(
        project_id=project,
        experiment_id=exp["id"],
        new_definition_id=defn_v2["id"],
    )
    # Old definition still fetchable + immutable.
    refetched_v1 = control.get_experiment_definition(
        project_id=project, def_id=defn_v1["id"]
    )
    assert json.loads(refetched_v1["variants"]) == variants_v1
    # Experiment now points at the new definition.
    refetched_exp = control.get_experiment(
        project_id=project, experiment_id=exp["id"]
    )
    assert refetched_exp["definition_id"] == defn_v2["id"]
    # list_experiment_definitions shows both.
    defs = control.list_experiment_definitions(project_id=project)
    assert {d["id"] for d in defs} == {defn_v1["id"], defn_v2["id"]}


def test_set_experiment_status_lifecycle(
    control: ControlPlane, project: str, dataset: str
) -> None:
    defn = control.create_experiment_definition(
        project_id=project,
        name="x",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=[
            {
                "id": "a", "name": "baseline", "axis": "prompt",
                "config_hash": canonical_config_hash({}), "config": {},
            }
        ],
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])
    control.set_experiment_status(
        project_id=project, experiment_id=exp["id"], status="running",
        started_at=_NOW.isoformat(),
    )
    running = control.get_experiment(project_id=project, experiment_id=exp["id"])
    assert running["status"] == "running"
    assert running["started_at"] == _NOW.isoformat()
    control.set_experiment_status(
        project_id=project, experiment_id=exp["id"], status="complete",
        finished_at=(_NOW + timedelta(minutes=5)).isoformat(),
    )
    done = control.get_experiment(project_id=project, experiment_id=exp["id"])
    assert done["status"] == "complete"
    assert done["finished_at"] is not None
    # Invalid status rejected.
    with pytest.raises(ValueError, match="invalid status"):
        control.set_experiment_status(
            project_id=project, experiment_id=exp["id"], status="exploded"
        )


# --------------------------------------------------------------------------- #
# Pairing invariant
# --------------------------------------------------------------------------- #


def test_pairing_invariant_one_run_per_variant(
    backend: DuckDBBackend, control: ControlPlane, project: str, dataset: str
) -> None:
    """§16.2.4 line: Pairing.run_ids_by_variant.keys() must equal variant ids.

    Enforced by the runner: a Pairing is recorded only when every variant
    produced a run for the same (item, seed). Below we run a two-variant
    experiment with 2 items × 1 run each, then assert pairings carry both
    variant ids as keys.
    """
    h_a = canonical_config_hash({"v": "a"})
    h_b = canonical_config_hash({"v": "b"})
    defn = control.create_experiment_definition(
        project_id=project,
        name="invariant",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=[
            {"id": "a", "name": "A", "axis": "prompt", "config_hash": h_a,
             "config": {"v": "a"}},
            {"id": "b", "name": "B", "axis": "prompt", "config_hash": h_b,
             "config": {"v": "b"}},
        ],
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])

    def runtime(variant: Variant, ctx: EvalContext) -> str:
        return str(ctx.expected_output) if variant.id == "a" else "wrong"

    run_experiment(
        backend=backend,
        control=control,
        project_id=project,
        experiment_id=exp["id"],
        runtime=runtime,
        bootstrap_iterations=100,
    )

    pairings = control.list_pairings(experiment_id=exp["id"])
    assert pairings  # at least one pairing exists
    variant_ids = {"a", "b"}
    for p in pairings:
        ids_in_pairing = set(json.loads(p["run_ids_by_variant"]).keys())
        assert ids_in_pairing == variant_ids


def test_experiment_run_links_trace_to_variant(
    backend: DuckDBBackend, control: ControlPlane, project: str, dataset: str
) -> None:
    """§16.2.4 line: every ExperimentRun row maps a trace_id to a variant_id."""
    defn = control.create_experiment_definition(
        project_id=project,
        name="link",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=[
            {"id": "only", "name": "Only", "axis": "prompt",
             "config_hash": canonical_config_hash({}), "config": {}}
        ],
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])
    run_experiment(
        backend=backend,
        control=control,
        project_id=project,
        experiment_id=exp["id"],
        runtime=lambda _v, ctx: ctx.expected_output,
        bootstrap_iterations=50,
    )
    runs = control.list_experiment_runs(
        project_id=project, experiment_id=exp["id"]
    )
    assert runs
    assert all(r["variant_id"] == "only" for r in runs)
    assert all(r["trace_id"].startswith(f"exp-{exp['id']}-") for r in runs)


# --------------------------------------------------------------------------- #
# Verdict statistics
# --------------------------------------------------------------------------- #


def test_verdict_paired_test_and_bootstrap_ci(
    backend: DuckDBBackend, control: ControlPlane, project: str, dataset: str
) -> None:
    """§16.2.4 line: variant A always wins → A ranked first, p-value computed."""
    defn = control.create_experiment_definition(
        project_id=project,
        name="winner",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=[
            {"id": "a", "name": "A", "axis": "prompt",
             "config_hash": canonical_config_hash({"v": "a"}), "config": {"v": "a"}},
            {"id": "b", "name": "B", "axis": "prompt",
             "config_hash": canonical_config_hash({"v": "b"}), "config": {"v": "b"}},
        ],
        planned_runs_per_variant=3,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])
    result = run_experiment(
        backend=backend,
        control=control,
        project_id=project,
        experiment_id=exp["id"],
        runtime=lambda v, ctx: ctx.expected_output if v.id == "a" else "wrong",
        bootstrap_iterations=500,
    )
    assert len(result.verdicts) == 1
    verdict = result.verdicts[0]
    assert verdict.ranking[0] == "a"
    assert verdict.mean_by_variant["a"] == pytest.approx(1.0)
    assert verdict.mean_by_variant["b"] == pytest.approx(0.0)
    # CI sanity: lower ≤ mean ≤ upper for both variants.
    for vid in ("a", "b"):
        assert verdict.ci_low_by_variant[vid] <= verdict.mean_by_variant[vid]
        assert verdict.ci_high_by_variant[vid] >= verdict.mean_by_variant[vid]
    # Paired test executed (≥ 2 variants, ≥ 2 pairings).
    assert verdict.paired_test == "wilcoxon_signed_rank"
    assert verdict.p_value is not None
    assert 0.0 <= verdict.p_value <= 1.0


def test_verdict_p_value_monotonic_with_n() -> None:
    """§16.2.4 line: more pairings narrows CI / lowers p (sanity, not formal)."""
    small_a = [1.0, 1.0]
    small_b = [0.0, 0.0]
    p_small = _wilcoxon_signed_rank(small_a, small_b)
    big_a = [1.0] * 10
    big_b = [0.0] * 10
    p_big = _wilcoxon_signed_rank(big_a, big_b)
    assert p_small is not None and p_big is not None
    assert p_big <= p_small  # more evidence → at-most-equal p-value
    # Bootstrap CI narrows with n.
    rng_small = __import__("random").Random(0)
    rng_big = __import__("random").Random(0)
    low_s, high_s = _bootstrap_ci([0.5, 1.0, 0.5, 1.0], 500, rng_small)
    low_b, high_b = _bootstrap_ci(
        [0.5, 1.0, 0.5, 1.0, 0.5, 1.0, 0.5, 1.0, 0.5, 1.0], 500, rng_big
    )
    assert (high_b - low_b) <= (high_s - low_s) + 1e-9


def test_compute_verdicts_one_per_evaluator() -> None:
    """Q-10a.2 contract: one Verdict per evaluator, append-only."""
    variants = [
        Variant(id="a", name="A", axis="prompt",
                config_hash="sha256:x", config={}),
        Variant(id="b", name="B", axis="prompt",
                config_hash="sha256:y", config={}),
    ]
    paired_samples: dict[str, dict[str, list[float]]] = {
        "exact_match": {"a": [1.0, 1.0, 1.0], "b": [0.0, 0.0, 0.0]},
        "levenshtein_ratio": {"a": [0.9, 0.8, 0.85], "b": [0.4, 0.5, 0.45]},
    }
    verdicts = compute_verdicts(
        experiment_id="e1",
        variants=variants,
        evaluator_ids=["exact_match", "levenshtein_ratio"],
        paired_samples=paired_samples,
        bootstrap_iterations=200,
    )
    assert len(verdicts) == 2
    assert {v.evaluator for v in verdicts} == {"exact_match", "levenshtein_ratio"}
    for v in verdicts:
        assert v.ranking[0] == "a"
        assert v.n_pairings == 3


def test_verdict_matrix_helper() -> None:
    """Q-10a.2 helper: {evaluator: {variant: mean}}."""
    verdicts = [
        Verdict(
            experiment_id="e1",
            evaluator="exact_match",
            ranking=["a", "b"],
            mean_by_variant={"a": 1.0, "b": 0.0},
            ci_low_by_variant={"a": 0.9, "b": 0.0},
            ci_high_by_variant={"a": 1.0, "b": 0.1},
            n_pairings=3,
            paired_test="wilcoxon_signed_rank",
            p_value=0.05,
            computed_at=_NOW,
        ),
        Verdict(
            experiment_id="e1",
            evaluator="levenshtein_ratio",
            ranking=["a", "b"],
            mean_by_variant={"a": 0.85, "b": 0.45},
            ci_low_by_variant={"a": 0.8, "b": 0.4},
            ci_high_by_variant={"a": 0.9, "b": 0.5},
            n_pairings=3,
            paired_test="wilcoxon_signed_rank",
            p_value=0.05,
            computed_at=_NOW,
        ),
    ]
    matrix = verdict_matrix(verdicts)
    assert matrix["exact_match"]["a"] == 1.0
    assert matrix["levenshtein_ratio"]["b"] == 0.45
    assert set(matrix.keys()) == {"exact_match", "levenshtein_ratio"}


def test_runner_marks_experiment_complete(
    backend: DuckDBBackend, control: ControlPlane, project: str, dataset: str
) -> None:
    """End-to-end: run_experiment transitions status pending → running → complete."""
    defn = control.create_experiment_definition(
        project_id=project,
        name="lifecycle",
        dataset_id=dataset,
        evaluator_ids=["exact_match"],
        variants=[
            {"id": "a", "name": "A", "axis": "prompt",
             "config_hash": canonical_config_hash({}), "config": {}}
        ],
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])
    assert exp["status"] == "pending"
    run_experiment(
        backend=backend,
        control=control,
        project_id=project,
        experiment_id=exp["id"],
        runtime=lambda _v, ctx: ctx.expected_output,
        bootstrap_iterations=50,
    )
    refetched = control.get_experiment(
        project_id=project, experiment_id=exp["id"]
    )
    assert refetched["status"] == "complete"
    assert refetched["started_at"] is not None
    assert refetched["finished_at"] is not None
    # Verdicts persisted to the control plane.
    verdicts = control.list_verdicts(experiment_id=exp["id"])
    assert any(v["evaluator"] == "exact_match" for v in verdicts)


def test_runner_rejects_empty_dataset(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """A definition pointing at an empty dataset is a hard error, not a no-op."""
    empty_ds = control.create_dataset(project_id=project, name="empty")
    defn = control.create_experiment_definition(
        project_id=project,
        name="empty",
        dataset_id=empty_ds["id"],
        evaluator_ids=["exact_match"],
        variants=[
            {"id": "a", "name": "A", "axis": "prompt",
             "config_hash": canonical_config_hash({}), "config": {}}
        ],
        planned_runs_per_variant=1,
        created_by="alice",
    )
    exp = control.create_experiment(project_id=project, definition_id=defn["id"])
    with pytest.raises(ValueError, match="no items"):
        run_experiment(
            backend=backend,
            control=control,
            project_id=project,
            experiment_id=exp["id"],
            runtime=lambda _v, ctx: ctx.expected_output,
        )


def test_msgspec_structs_round_trip() -> None:
    """The six experiment Structs encode/decode cleanly via msgspec."""
    import msgspec

    objs: list[object] = [
        Variant(id="a", name="A", axis="prompt",
                config_hash="sha256:x", config={"k": "v"}),
        ExperimentDefinition(
            project_id="p1", id="def1", name="x", dataset_id="ds",
            evaluator_ids=["exact_match"],
            variants=[Variant(id="a", name="A", axis="prompt",
                              config_hash="sha256:x", config={})],
            planned_runs_per_variant=1,
            created_by="alice", created_at=_NOW,
        ),
        Experiment(project_id="p1", id="e1", definition_id="def1",
                   status="pending", created_at=_NOW),
        Pairing(id="p1", experiment_id="e1", dataset_item_id="dsi",
                seed=1, run_ids_by_variant={"a": "t1"}),
        Verdict(experiment_id="e1", evaluator="exact_match",
                ranking=["a"], mean_by_variant={"a": 1.0},
                ci_low_by_variant={"a": 0.9}, ci_high_by_variant={"a": 1.0},
                n_pairings=1, paired_test="none", computed_at=_NOW),
        ExperimentRun(project_id="p1", experiment_id="e1", variant_id="a",
                      trace_id="t1", seed=1, started_at=_NOW),
    ]
    for obj in objs:
        encoded = msgspec.json.encode(obj)
        decoded = msgspec.json.decode(encoded, type=type(obj))
        assert decoded == obj
