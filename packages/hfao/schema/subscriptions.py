"""Insight routing subscriptions (SPEC §16 Q-19).

A :class:`Subscription` says "send any insight/alert that matches this
filter to these channels for this subscriber." Three subscriber kinds:

  * ``role`` — every member of an HFAO role in this workspace (e.g.
    ``subscriber_id="role:owner"``).
  * ``user`` — a specific human (e.g. ``subscriber_id="user:alice@example.com"``).
  * ``agent`` — a non-human consumer (e.g. ``subscriber_id="agent:on-call-bot"``).
    These get dispatched via :class:`hfao.compute.routing.AgentDispatcher`
    instead of HTTP webhooks.

The Q-19 contract is **rule-based, no learning**. The "learning *who*
should see *what*" version is deferred to Q-19-next.

Wildcard matching:

  * ``match_kind = "*"`` matches every :class:`InsightKind`.
  * ``match_signal_name = "*"`` matches every signal name.
  * Glob is supported via trailing ``*`` (e.g. ``"error_rate_*"``).
  * ``match_min_severity`` is the severity ladder rank — only insights
    at or above that rank deliver.

Auto-resolved ownership: ``subscriber_id="auto:prompt_owner:<name>"``
resolves at route time against the audit log (the latest
``create_prompt_version`` actor for that prompt is the owner). No new
ownership table per the §16.2 resolution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from msgspec import Struct, field

SubscriberKind = Literal["role", "user", "agent"]


class Subscription(Struct, kw_only=True):
    project_id: str
    id: str
    subscriber_kind: SubscriberKind
    # "role:owner" / "user:alice@…" / "agent:bot" / "auto:prompt_owner:<name>"
    subscriber_id: str
    match_kind: str = "*"            # "*" or an InsightKind literal
    match_signal_name: str = "*"     # "*" or glob with trailing "*"
    match_min_severity: Literal["info", "notice", "warning", "critical"] = "info"
    channels: list[str] = field(default_factory=lambda: [])
    enabled: bool = True
    created_at: datetime
    created_by: str | None = None


__all__ = ["Subscription", "SubscriberKind"]
