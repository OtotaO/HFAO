"""Seed a deterministic golden dataset for the no-keys demo (scripts/demo.sh).

The demo's eval step runs the built-in ``echo`` runtime (which returns each
dataset item's ``input`` unchanged — i.e. a stub/mock "LM" that needs no API
keys) against the ``exact_match`` / ``levenshtein_ratio`` evaluators. For the
gate to pass deterministically we seed a ``goldens`` dataset whose
``expected_output`` equals its ``input`` on every item, so the echoed output
always matches the expectation.

Idempotent: re-running reuses the existing ``goldens`` dataset and project.

The project/workspace bootstrap mirrors ``apps.cockpit.cockpit._ensure_project``
but is inlined here so the script has no dependency on the (non-wheel-packaged)
``apps`` tree — it runs from a bare ``pip install hfao`` too.

Config is read from the standard ``HFAO_*`` environment variables via
``HFAOConfig.from_env()`` (see scripts/demo.sh for the values used).
"""

from __future__ import annotations

from hfao.config import HFAOConfig
from hfao.storage.control_plane import ControlPlane

_GOLDEN_ITEMS = ["2+2", "capital of France", "ping", "echo me", "42"]


def main() -> None:
    cfg = HFAOConfig.from_env()
    cp = ControlPlane(cfg.control_plane_dsn)
    cp.init_schema()
    try:
        _ensure_project(cp, cfg.project)

        existing = [
            d
            for d in cp.list_datasets(project_id=cfg.project)
            if d["name"] == "goldens"
        ]
        if existing:
            dataset = existing[0]
        else:
            dataset = cp.create_dataset(
                project_id=cfg.project,
                name="goldens",
                description="Demo golden set: expected_output == input so the "
                "echo runtime + exact_match gate passes with no API keys.",
            )
            for question in _GOLDEN_ITEMS:
                cp.add_dataset_item(
                    project_id=cfg.project,
                    dataset_id=dataset["id"],
                    input=question,
                    expected_output=question,
                )

        item_count = len(
            cp.list_dataset_items(project_id=cfg.project, dataset_id=dataset["id"])
        )
        print(
            f"dataset 'goldens' ready: id={dataset['id']} "
            f"project={cfg.project} items={item_count}"
        )
    finally:
        cp.close()


def _ensure_project(cp: ControlPlane, project: str) -> None:
    """Create the control-plane project row (and default workspace) if absent.

    Datasets are FK-bound to a ``projects`` row, so the row must exist before
    ``create_dataset``. The project id is the free-string label the rest of the
    demo uses (``HFAO_PROJECT``); we insert it literally so events and dataset
    rows stay joined under the same id.
    """
    try:
        cp.get_project(project)
        return
    except KeyError:
        pass
    workspace = cp.get_workspace_by_slug("default") or cp.create_workspace(
        slug="default", name="Default"
    )
    cp.create_project_with_id(
        project_id=project,
        workspace_id=workspace["id"],
        slug=project,
        name=project,
    )


if __name__ == "__main__":
    main()
