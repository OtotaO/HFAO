"""A green check must mean the work actually happened.

Regression: `.github/workflows/docker-publish.yml` and `space-deploy.yml` were
echo-only stubs — `run: echo "Docker build and push steps"` — triggered on every
push to main. They stamped "Docker Publish: success" and "Space Deploy: success"
on commits where nothing was built and nothing was deployed. HFAO is a public
repo, so that fake green was visible to anyone evaluating the project.

Both files were deleted. This test stops an equivalent stub from reappearing: a
workflow that names itself a build/publish/deploy and runs automatically must do
something other than echo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# Words that make a check claim an artifact was produced or shipped.
_CLAIM_WORDS = ("publish", "deploy", "release", "push")


def _workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.y*ml"))


def _run_steps(doc: dict) -> list[str]:
    return [
        step["run"]
        for job in (doc.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and "run" in step
    ]


def _is_echo_only(runs: list[str]) -> bool:
    if not runs:
        return True
    return all(
        all(
            not line.strip() or line.strip().startswith(("echo ", "echo\t", "#", ":"))
            for line in run.splitlines()
        )
        for run in runs
    )


def _runs_automatically(doc: dict) -> bool:
    # PyYAML parses the bare key `on:` as the boolean True.
    triggers = doc.get("on", doc.get(True)) or {}
    if isinstance(triggers, str):
        triggers = {triggers: None}
    if isinstance(triggers, list):
        triggers = dict.fromkeys(triggers)
    return any(t != "workflow_dispatch" for t in triggers)


def test_there_are_workflows_to_check() -> None:
    assert _workflows(), "no workflows found — the guard would be vacuous"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_no_workflow_fakes_a_build_or_deploy(path: Path) -> None:
    doc = yaml.safe_load(path.read_text())
    name = str(doc.get("name", path.stem)).lower()
    if not any(word in name or word in path.stem.lower() for word in _CLAIM_WORDS):
        return
    if not _runs_automatically(doc):
        return  # manual-only placeholder cannot stamp a status on a commit
    assert not _is_echo_only(_run_steps(doc)), (
        f"{path.name} claims to build/publish/deploy, runs automatically, and only "
        "echoes. A green check that asserts nothing is worse than no check — either "
        "implement it or gate it to workflow_dispatch."
    )


def test_the_deleted_stubs_stay_deleted() -> None:
    for stub in ("docker-publish.yml", "space-deploy.yml"):
        assert not (WORKFLOW_DIR / stub).exists()
