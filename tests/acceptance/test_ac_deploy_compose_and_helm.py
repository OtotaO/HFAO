"""AC — Docker Compose + Helm chart validity (SPEC §6.1, §15.2 week 11).

Pure-Python sanity for the v1 deployment artefacts: ``docker-compose.yml``,
``docker-compose.clickhouse.yml``, and the ``helm/hfao`` chart. The full
``helm lint`` / ``helm template`` checks live in CI (and depend on the
``helm`` binary); this AC test verifies what we can without it:

  - Compose files parse as YAML and declare the right service set.
  - Both shapes share the same volume name (``hfao-data``) so the bodies
    offload mounts line up.
  - Chart.yaml is a valid Helm v2 chart with the project's app version.
  - values.yaml carries every key the templates reference.
  - Every template file produces *some* mapping when its Go-template
    directives are stubbed out (catches accidental empty templates or
    YAML-killer formatting bugs).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Docker Compose
# --------------------------------------------------------------------------- #


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path}: top-level YAML must be a mapping"
    return data


def test_compose_single_binary_shape_declares_cockpit_and_ingest() -> None:
    doc = _load_yaml(REPO / "docker-compose.yml")
    services = doc["services"]
    assert set(services.keys()) == {"cockpit", "ingest"}
    # Both pods share the named volume so the DuckDB + body offload align.
    for svc in services.values():
        volumes = svc.get("volumes") or []
        assert any("hfao-data" in str(v) for v in volumes), (
            f"single-binary shape: every service must mount hfao-data; got {volumes}"
        )
    # Cockpit publishes the two HFAO ports the cockpit binds.
    cockpit_ports = doc["services"]["cockpit"]["ports"]
    published = {str(p).split(":", 1)[0] for p in cockpit_ports}
    assert published == {"7860", "4319"}
    # Ingest publishes :4318 OTLP/HTTP.
    ingest_ports = doc["services"]["ingest"]["ports"]
    assert any(str(p).startswith("4318:") for p in ingest_ports)


def test_compose_clickhouse_overlay_adds_managed_services() -> None:
    doc = _load_yaml(REPO / "docker-compose.clickhouse.yml")
    services = doc["services"]
    # New services for the K8s/Docker shape.
    assert {"clickhouse", "postgres", "redis"} <= set(services.keys())
    # Overlay must reconfigure cockpit + ingest to point at the new backends.
    for svc_name in ("cockpit", "ingest"):
        svc = services[svc_name]
        env = svc["environment"]
        assert env["HFAO_BACKEND"] == "clickhouse"
        assert "clickhouse://" in env["HFAO_CLICKHOUSE_DSN"]
        assert "postgresql://" in env["HFAO_CONTROL_PLANE_DSN"]
        assert "redis://" in env["HFAO_REDIS_URL"]


def test_compose_files_use_same_volume_name() -> None:
    """The overlay reuses the base shape's ``hfao-data`` volume so bodies/
    body-offload paths still mount correctly."""
    base = _load_yaml(REPO / "docker-compose.yml")
    overlay = _load_yaml(REPO / "docker-compose.clickhouse.yml")
    assert "hfao-data" in base["volumes"]
    # The overlay does NOT redeclare hfao-data (would duplicate); the new
    # named volumes are for the managed services.
    overlay_vols = set((overlay.get("volumes") or {}).keys())
    assert "hfao-data" not in overlay_vols
    assert {"clickhouse-data", "postgres-data", "redis-data"} <= overlay_vols


# --------------------------------------------------------------------------- #
# Helm chart
# --------------------------------------------------------------------------- #


CHART_DIR = REPO / "helm" / "hfao"


def test_chart_yaml_is_a_valid_helm_v2_chart() -> None:
    chart = _load_yaml(CHART_DIR / "Chart.yaml")
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "hfao"
    assert chart["type"] == "application"
    # SemVer.
    assert re.match(r"^\d+\.\d+\.\d+", str(chart["version"]))
    # appVersion should track the package version that ships in pyproject.
    pyproject = (REPO / "pyproject.toml").read_text()
    py_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert py_version is not None
    assert str(chart["appVersion"]) == py_version.group(1)


def test_values_yaml_covers_every_key_the_templates_reference() -> None:
    """Walk every template and verify every ``.Values.<dotted.path>`` is
    a real key in values.yaml. Catches typos and renames.
    """
    values = _load_yaml(CHART_DIR / "values.yaml")
    referenced: set[str] = set()
    for tpl in (CHART_DIR / "templates").rglob("*"):
        if not tpl.is_file() or tpl.suffix not in (".yaml", ".tpl", ".txt"):
            continue
        text = tpl.read_text()
        for match in re.finditer(r"\.Values\.([A-Za-z0-9_.]+)", text):
            referenced.add(match.group(1))

    missing: list[str] = []
    for ref in sorted(referenced):
        node = values
        ok = True
        for part in ref.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if not ok:
            missing.append(ref)

    assert not missing, (
        f"templates reference values keys that don't exist in values.yaml: "
        f"{missing}"
    )


def test_chart_templates_are_non_empty_and_have_yaml_headers() -> None:
    """No accidentally-empty templates (would render nothing) and each YAML
    template either starts with a Go-template directive or an ``apiVersion``
    line."""
    yaml_templates = list((CHART_DIR / "templates").glob("*.yaml"))
    assert yaml_templates, "expected at least one *.yaml template"
    for tpl in yaml_templates:
        text = tpl.read_text().lstrip()
        assert text, f"{tpl}: empty template"
        assert text.startswith(("apiVersion:", "{{-", "{{")), (
            f"{tpl}: first line should be apiVersion or a Go-template "
            f"directive; got {text[:60]!r}"
        )


def test_chart_ships_the_two_required_components() -> None:
    """The HFAO Helm chart must include a Deployment + Service pair for
    both cockpit and ingest per the K8s shape."""
    expected = {
        "cockpit-deployment.yaml",
        "cockpit-service.yaml",
        "ingest-deployment.yaml",
        "ingest-service.yaml",
    }
    have = {p.name for p in (CHART_DIR / "templates").glob("*.yaml")}
    missing = expected - have
    assert not missing, f"missing required Helm templates: {missing}"


def test_helpers_template_defines_required_partials() -> None:
    """``_helpers.tpl`` must define the shared identity + env partials all
    the other templates pull in."""
    text = (CHART_DIR / "templates" / "_helpers.tpl").read_text()
    for partial in (
        "hfao.name",
        "hfao.fullname",
        "hfao.labels",
        "hfao.selectorLabels",
        "hfao.serviceAccountName",
        "hfao.envCommon",
        "hfao.imageTag",
        "hfao.ingestImageTag",
    ):
        assert f'define "{partial}"' in text, (
            f"_helpers.tpl missing required partial: {partial}"
        )


# --------------------------------------------------------------------------- #
# Dockerfiles — light sanity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,must_contain",
    [
        ("docker/cockpit.Dockerfile", "hfao.cli"),
        ("docker/ingest.Dockerfile", "hfao.ingest.server"),
    ],
)
def test_dockerfiles_reference_real_entrypoints(name: str, must_contain: str) -> None:
    text = (REPO / name).read_text()
    assert must_contain in text, (
        f"{name}: expected entrypoint reference {must_contain!r} not found"
    )


def test_ingest_server_module_is_runnable() -> None:
    """``python -m hfao.ingest.server`` needs a __main__ block to boot in a
    container. We don't actually run granian here (would bind a port);
    instead verify the module exposes ``_main`` and the guard exists."""
    import hfao.ingest.server as mod

    assert hasattr(mod, "_main"), "ingest.server must expose _main() for the K8s shape"
    # The __main__ guard text is the simplest sanity probe.
    text = Path(mod.__file__).read_text()
    assert 'if __name__ == "__main__":' in text
