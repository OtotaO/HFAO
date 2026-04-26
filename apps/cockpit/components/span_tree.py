"""HFAO Cockpit — span tree component.

SPEC §10.3. Wraps ``gr.HTML`` with scoped CSS that renders a hierarchical
span tree from a JSON list of canonical :class:`Observation` rows. The
tree is collapsible, status-coloured, and lazy-rendered server-side
(client-side JS is intentionally minimal so the component still works
in the HF Space iframe sandbox).

Public surface:

- :func:`render` — turns a list of observation dicts into the HTML
  payload the ``Trace detail`` tab plugs into ``gr.HTML``.

The function is pure (no side effects, no IO) so it can be exercised
directly from acceptance tests without spinning up Gradio.
"""

from __future__ import annotations

import html
from typing import Any

# Status badge style keyed by canonical Observation.status (§4.1).
_STATUS_STYLE: dict[str, str] = {
    "ok": "background:#0f5132;color:#d1e7dd",
    "error": "background:#842029;color:#f8d7da",
    "unset": "background:#41464b;color:#cfd1d4",
}

# Observation.type → emoji + colour for quick visual scan.
_TYPE_BADGE: dict[str, tuple[str, str]] = {
    "AGENT": ("◆", "#a78bfa"),
    "GENERATION": ("✨", "#60a5fa"),
    "TOOL": ("⚒", "#f59e0b"),
    "RETRIEVAL": ("⌕", "#34d399"),
    "EMBEDDING": ("∴", "#22d3ee"),
    "EVAL": ("✓", "#facc15"),
    "GUARDRAIL": ("⛨", "#f87171"),
    "HANDOFF": ("→", "#fb7185"),
    "SPAN": ("·", "#94a3b8"),
    "EVENT": ("○", "#94a3b8"),
}


def render(observations: list[dict[str, Any]]) -> str:
    """Return an HTML fragment rendering the span tree.

    ``observations`` is the JSON-shape list of canonical Observation
    rows (the same shape ``StorageBackend.get_trace`` returns once
    msgspec-decoded). Empty input renders an empty-state message.
    """
    if not observations:
        return _empty_state()
    children_of: dict[str | None, list[dict[str, Any]]] = {}
    for obs in observations:
        parent = obs.get("parent_observation_id")
        children_of.setdefault(str(parent) if parent else None, []).append(obs)
    # Stable sort: oldest first within each parent.
    for siblings in children_of.values():
        siblings.sort(key=lambda o: str(o.get("start_time", "")))
    roots = children_of.get(None, [])
    if not roots:
        # Trace was ingested without a clear root; fall back to the oldest.
        roots = sorted(observations, key=lambda o: str(o.get("start_time", "")))[:1]
    body = "".join(_render_node(n, children_of, depth=0) for n in roots)
    return f"""<div class="hfao-span-tree">{_styles()}{body}</div>"""


def _render_node(
    obs: dict[str, Any],
    children_of: dict[str | None, list[dict[str, Any]]],
    *,
    depth: int,
) -> str:
    obs_id = str(obs["observation_id"])
    children = children_of.get(obs_id, [])
    name = html.escape(str(obs.get("name", "")))
    obs_type = str(obs.get("type", "SPAN"))
    badge_char, badge_color = _TYPE_BADGE.get(obs_type, _TYPE_BADGE["SPAN"])
    status = str(obs.get("status", "unset"))
    status_style = _STATUS_STYLE.get(status, _STATUS_STYLE["unset"])
    duration_ms = obs.get("duration_ms")
    duration_part = (
        f'<span class="hfao-dur">{int(duration_ms)} ms</span>' if duration_ms else ""
    )
    model_part = (
        f'<span class="hfao-model">{html.escape(str(obs["model"]))}</span>'
        if obs.get("model")
        else ""
    )
    has_kids = bool(children)
    chevron = "▾" if has_kids else "·"
    indent_px = depth * 18
    line = (
        f'<div class="hfao-row" style="padding-left:{indent_px}px">'
        f'<span class="hfao-chevron">{chevron}</span>'
        f'<span class="hfao-type" style="color:{badge_color}">{badge_char} {obs_type}</span>'
        f'<span class="hfao-name">{name}</span>'
        f'<span class="hfao-status" style="{status_style}">{html.escape(status)}</span>'
        f"{duration_part}{model_part}"
        f"</div>"
    )
    inner = "".join(_render_node(c, children_of, depth=depth + 1) for c in children)
    return line + inner


def _empty_state() -> str:
    return (
        f'<div class="hfao-span-tree">{_styles()}'
        '<div class="hfao-empty">No observations in this trace.</div>'
        "</div>"
    )


_STYLE_BLOCK = """
<style>
.hfao-span-tree {
  font-family: ui-monospace, Menlo, monospace; font-size: 13px;
  color: #e5e7eb; background:#0b1220; padding:8px; border-radius:6px;
}
.hfao-row {
  display:flex; gap:10px; align-items:center; padding:3px 0;
  border-bottom:1px solid #111827;
}
.hfao-chevron { width:14px; color:#475569; }
.hfao-type { width:110px; font-weight:600; }
.hfao-name { flex:1; color:#f3f4f6; }
.hfao-status {
  padding:1px 6px; border-radius:3px; font-size:11px;
  text-transform:uppercase;
}
.hfao-dur { color:#fbbf24; font-variant-numeric: tabular-nums; }
.hfao-model { color:#67e8f9; }
.hfao-empty { color:#6b7280; padding:24px; text-align:center; }
</style>
""".strip()


def _styles() -> str:
    return _STYLE_BLOCK


__all__ = ["render"]
