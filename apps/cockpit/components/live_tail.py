"""HFAO Cockpit — live-tail component.

SPEC §10.3. Renders the most recent N traces as a scrolling ``gr.HTML``
panel with status pills. Designed to be paired with ``gr.Timer(1.0)``
in the cockpit's Live tail tab; the render function is pure so tests
can drive it with a fixed list of trace dicts.
"""

from __future__ import annotations

import html
from typing import Any

_PILL_BY_STATUS: dict[str, str] = {
    "ok": "background:#0f5132;color:#d1e7dd",
    "error": "background:#842029;color:#f8d7da",
    "unset": "background:#41464b;color:#cfd1d4",
}


def render(traces: list[dict[str, Any]], *, max_rows: int = 20) -> str:
    """Render the latest ``max_rows`` traces as an HTML strip.

    ``traces`` is the list ``StorageBackend.list_traces`` returns —
    each row is a dict with ``trace_id``, ``span_count``,
    ``has_error``, ``first_start`` / ``last_end`` (DuckDB column
    names), optionally ``session_id`` and ``total_cost_usd``.
    """
    if not traces:
        return _empty()
    rows: list[str] = []
    for trace in traces[:max_rows]:
        rows.append(_row(trace))
    return f'<div class="hfao-live-tail">{_styles()}{"".join(rows)}</div>'


def _row(trace: dict[str, Any]) -> str:
    status = "error" if trace.get("has_error") else "ok"
    pill = _PILL_BY_STATUS.get(status, _PILL_BY_STATUS["unset"])
    trace_id = html.escape(str(trace.get("trace_id", "")))
    short_tid = trace_id[:16] + ("…" if len(trace_id) > 16 else "")
    spans = int(trace.get("span_count") or 0)
    cost = trace.get("total_cost_usd")
    cost_str = f"${float(cost):.4f}" if cost else "—"
    session = html.escape(str(trace.get("session_id") or "—"))[:24]
    last = html.escape(
        str(trace.get("last_end") or trace.get("first_start") or "")
    )[:19]
    return (
        '<div class="hfao-live-row">'
        f'<span class="hfao-pill" style="{pill}">{status}</span>'
        f'<span class="hfao-tid">{short_tid}</span>'
        f'<span class="hfao-spans">{spans} spans</span>'
        f'<span class="hfao-cost">{cost_str}</span>'
        f'<span class="hfao-session">{session}</span>'
        f'<span class="hfao-time">{last}</span>'
        "</div>"
    )


def _empty() -> str:
    return (
        f'<div class="hfao-live-tail">{_styles()}'
        '<div class="hfao-live-empty">no traces in the last poll window</div>'
        "</div>"
    )


_STYLE_BLOCK = """
<style>
.hfao-live-tail {
  font-family: ui-monospace, Menlo, monospace; font-size: 12px;
  color:#e5e7eb; background:#0b1220; padding:6px; border-radius:6px;
  max-height:520px; overflow-y:auto;
}
.hfao-live-row {
  display:flex; gap:12px; align-items:center; padding:4px 6px;
  border-bottom:1px solid #111827;
}
.hfao-pill {
  padding:1px 6px; border-radius:3px; font-size:10px;
  text-transform:uppercase;
}
.hfao-tid { color:#67e8f9; min-width:160px; }
.hfao-spans { color:#a3a3a3; min-width:70px; }
.hfao-cost { color:#34d399; min-width:80px; font-variant-numeric: tabular-nums; }
.hfao-session { color:#a78bfa; min-width:120px; }
.hfao-time { color:#9ca3af; margin-left:auto; }
.hfao-live-empty { color:#6b7280; padding:20px; text-align:center; }
</style>
""".strip()


def _styles() -> str:
    return _STYLE_BLOCK


__all__ = ["render"]
