"""Monitor + Alert schema (SPEC §8.4).

A :class:`Monitor` is an NL description + frozen SQL query + threshold +
operator + window + outbound channels. Once created, the SQL is **not**
regenerated on every evaluation — that's the engine's contract: NL→SQL on
creation, then frozen. Re-running NL→SQL on every tick is both unnecessary
and a way to silently change semantics.

An :class:`Alert` is the persisted record of one threshold breach.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from msgspec import Struct, field

MonitorOperator = Literal["gt", "lt", "gte", "lte", "eq"]


class Monitor(Struct, kw_only=True):
    project_id: str
    id: str
    name: str
    nl_description: str
    sql_query: str
    threshold: float
    operator: MonitorOperator
    window: str  # "5m" / "1h" / "24h" — parsed by parse_window
    channels: list[str] = field(default_factory=lambda: [])
    enabled: bool = True
    created_at: datetime
    last_evaluated_at: datetime | None = None


class Alert(Struct, kw_only=True):
    id: str
    project_id: str
    monitor_id: str
    fired_at: datetime
    actual_value: float
    threshold: float
    operator: MonitorOperator
    message: str
    channels_notified: list[str] = field(default_factory=lambda: [])
    delivery_errors: list[str] = field(default_factory=lambda: [])
