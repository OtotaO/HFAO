"""Insight / alert routing engine (SPEC §16 Q-19).

Rule-based, no learning. Given an :class:`Insight` (or an
:class:`hfao.schema.monitors.Alert`), match it against the project's
subscriptions and return a list of :class:`Delivery` records the
caller dispatches.

Three subscriber kinds (per :class:`Subscription`):

  * ``role`` — every member of an HFAO role in the workspace. v1 routes
    by webhook URLs the user listed on the subscription; expanding to
    each individual role-holder is the Q-19-next learning surface.
  * ``user`` — a specific human, identified by ``"user:<email>"``.
  * ``agent`` — a non-human consumer (e.g. an on-call bot), identified
    by ``"agent:<name>"``. Agent deliveries flow through a pluggable
    :class:`AgentDispatcher` instead of HTTP webhooks.

Auto-resolved subscribers: ``subscriber_id="auto:prompt_owner:<name>"``
gets the last ``create_prompt_version`` actor from the audit log at
route time. No new ownership store; reuses existing data.

The router is pure with respect to its inputs — it never writes to the
control plane. Persistence (alert rows, audit log) stays the
responsibility of the engine that emitted the insight/alert.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from hfao.schema.insights import SEVERITY_RANK, InsightSeverity

if TYPE_CHECKING:
    from hfao.compute.monitor import WebhookFn
    from hfao.storage.control_plane import ControlPlane


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Dispatch result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Delivery:
    """One subscription's worth of routing output."""

    subscription_id: str
    subscriber_kind: str
    subscriber_id: str
    channels: list[str] = field(default_factory=lambda: [])
    agent_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class RouteResult:
    """Aggregated outcome of routing one insight/alert."""

    deliveries: list[Delivery] = field(default_factory=lambda: [])
    webhook_failures: list[str] = field(default_factory=lambda: [])
    agent_failures: list[str] = field(default_factory=lambda: [])


# --------------------------------------------------------------------------- #
# Agent dispatcher protocol
# --------------------------------------------------------------------------- #


class AgentDispatcher(Protocol):
    """Sends a routed insight payload to a non-human consumer.

    The default implementation in v1 is a no-op (records the delivery in
    the audit log). Production wires this to e.g. an MCP push, a queue
    write, or an internal RPC.
    """

    def dispatch(
        self, *, agent_id: str, payload: dict[str, Any]
    ) -> tuple[bool, str | None]: ...


class NoOpAgentDispatcher:
    """Default dispatcher: records but doesn't actually send.

    Useful for development and the AC test harness. Returns ``(True, None)``
    for every call so the routing engine treats the delivery as successful.
    """

    def dispatch(
        self, *, agent_id: str, payload: dict[str, Any]
    ) -> tuple[bool, str | None]:
        log.debug("NoOpAgentDispatcher: agent=%s payload=%r", agent_id, payload)
        return True, None


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def _signal_matches(pattern: str, actual: str) -> bool:
    """Wildcard match: ``"*"`` matches everything; glob otherwise."""
    if pattern == "*":
        return True
    return fnmatch.fnmatchcase(actual, pattern)


def _kind_matches(pattern: str, actual: str) -> bool:
    return pattern == "*" or pattern == actual


def _severity_matches(min_severity: InsightSeverity, actual: InsightSeverity) -> bool:
    return SEVERITY_RANK[actual] >= SEVERITY_RANK[min_severity]


def _parse_channels(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(c) for c in raw]  # type: ignore[arg-type]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(c) for c in parsed]  # type: ignore[arg-type]
        except json.JSONDecodeError:
            pass
    return []


# --------------------------------------------------------------------------- #
# Auto-resolved subscriber ids
# --------------------------------------------------------------------------- #


_AUTO_PREFIX = "auto:"


def _resolve_auto_subscriber(
    *,
    raw_subscriber_id: str,
    control: ControlPlane,
    workspace_id: str | None,
) -> str | None:
    """Translate ``auto:<scheme>:<arg>`` into a concrete identifier.

    Supported schemes:

      * ``auto:prompt_owner:<prompt_name>`` — latest
        ``create_prompt_version`` actor on the named prompt.

    Returns ``None`` when the auto-id can't be resolved (no audit history,
    unknown scheme, missing workspace) — the router skips that subscription
    rather than guessing.
    """
    if not raw_subscriber_id.startswith(_AUTO_PREFIX):
        return raw_subscriber_id
    spec = raw_subscriber_id[len(_AUTO_PREFIX) :]
    scheme, _, arg = spec.partition(":")
    if not scheme or not arg or not workspace_id:
        return None
    if scheme == "prompt_owner":
        actor = control.latest_audit_actor(
            workspace_id=workspace_id,
            action="create_prompt_version",
            target_contains=f"/{arg}@",
        )
        if actor:
            return f"user:{actor}"
        return None
    # Unknown scheme — refuse to invent.
    return None


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


@dataclass
class InsightRouter:
    """Match an insight/alert against subscriptions and dispatch.

    ``webhook`` is the existing :class:`hfao.compute.monitor.WebhookFn`
    pattern (already used for monitor alerts), and ``agent_dispatcher``
    handles the non-human path. Both default to in-test-friendly
    no-ops via :class:`NoOpAgentDispatcher` and the
    :mod:`hfao.compute.monitor.http_webhook` import-on-use pattern.
    """

    control: ControlPlane
    webhook: WebhookFn | None = None
    agent_dispatcher: AgentDispatcher = field(default_factory=NoOpAgentDispatcher)

    def route_insight(
        self,
        *,
        project_id: str,
        kind: str,
        signal_name: str,
        severity: InsightSeverity,
        payload: dict[str, Any],
        workspace_id: str | None = None,
    ) -> RouteResult:
        """Route a generic insight-shaped object.

        ``payload`` is the dict ultimately delivered (webhook body or agent
        message). Callers populate it with whatever the insight or alert
        carries — the router doesn't introspect.
        """
        candidates = self.control.list_subscriptions(
            project_id=project_id,
            only_enabled=True,
            match_kind=kind,
            match_signal_name=signal_name,
        )
        deliveries: list[Delivery] = []
        webhook_failures: list[str] = []
        agent_failures: list[str] = []

        for sub in candidates:
            if not _signal_matches(str(sub["match_signal_name"]), signal_name):
                continue
            if not _kind_matches(str(sub["match_kind"]), kind):
                continue
            min_sev: InsightSeverity = sub["match_min_severity"]  # type: ignore[assignment]
            if not _severity_matches(min_sev, severity):
                continue
            subscriber_id = _resolve_auto_subscriber(
                raw_subscriber_id=str(sub["subscriber_id"]),
                control=self.control,
                workspace_id=workspace_id,
            )
            if subscriber_id is None:
                continue
            channels = _parse_channels(sub["channels"])
            delivery = Delivery(
                subscription_id=str(sub["id"]),
                subscriber_kind=str(sub["subscriber_kind"]),
                subscriber_id=subscriber_id,
                channels=channels,
            )
            deliveries.append(delivery)
            # Webhook fan-out (role/user kinds).
            if delivery.subscriber_kind in ("role", "user") and channels:
                webhook_fn = self._resolve_webhook()
                for url in channels:
                    ok, err = webhook_fn(url, payload)
                    if not ok:
                        webhook_failures.append(
                            f"{delivery.subscription_id}:{url}: {err or 'delivery failed'}"
                        )
            # Agent dispatch.
            if delivery.subscriber_kind == "agent":
                ok, err = self.agent_dispatcher.dispatch(
                    agent_id=subscriber_id, payload=payload
                )
                if not ok:
                    agent_failures.append(
                        f"{delivery.subscription_id}:{subscriber_id}: "
                        f"{err or 'agent delivery failed'}"
                    )

        return RouteResult(
            deliveries=deliveries,
            webhook_failures=webhook_failures,
            agent_failures=agent_failures,
        )

    def _resolve_webhook(self) -> WebhookFn:
        if self.webhook is not None:
            return self.webhook
        from hfao.compute.monitor import http_webhook

        return http_webhook


# --------------------------------------------------------------------------- #
# Pre-flight dry-run helper
# --------------------------------------------------------------------------- #


def matching_subscriptions(
    control: ControlPlane,
    *,
    project_id: str,
    kind: str,
    signal_name: str,
    severity: InsightSeverity,
) -> list[dict[str, Any]]:
    """Return the subscription rows that **would** match a given event.

    Used by the cockpit Settings sub-section ("test routing for this signal")
    and by callers that want to preview before persisting. Does not
    dispatch.
    """
    rows: Iterable[dict[str, Any]] = control.list_subscriptions(
        project_id=project_id,
        only_enabled=True,
        match_kind=kind,
        match_signal_name=signal_name,
    )
    out: list[dict[str, Any]] = []
    for sub in rows:
        if not _signal_matches(str(sub["match_signal_name"]), signal_name):
            continue
        if not _kind_matches(str(sub["match_kind"]), kind):
            continue
        if not _severity_matches(sub["match_min_severity"], severity):  # type: ignore[arg-type]
            continue
        out.append(sub)
    return out


__all__ = [
    "AgentDispatcher",
    "Delivery",
    "InsightRouter",
    "NoOpAgentDispatcher",
    "RouteResult",
    "matching_subscriptions",
]
