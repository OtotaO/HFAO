"""Webhook outcome reporting through HTTPX, without any live network access.

These tests cover delivery classification and bounded diagnostics. They do not
establish destination safety: monitor records still include configured URLs.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NoReturn
from unittest.mock import Mock

import httpx
import httpx._api
import pytest
from hfao.compute.monitor import MonitorEngine, create_monitor, http_webhook
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

_URL = "https://alerts.invalid/hook"
_MALFORMED_URL = "http://[::1"
_OVERLONG_HOST_URL = "http://" + "a" * 64 + ".example/hook"
_REMOTE_SECRET = "remote-response-secret-that-must-not-be-recorded"
_EXCEPTION_SECRET = "exception-secret-that-must-not-be-recorded"


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Mock:
    """Keep the real post helper/client while replacing only their transport."""
    # An explicit transport prevents environment proxy autodiscovery in this
    # test client. The production helper's trust_env argument is captured
    # unchanged, and the transport never opens a socket.
    factory = Mock(
        side_effect=partial(httpx.Client, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(httpx._api, "Client", factory)
    return factory


def _install_idna_failure(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Reach the real HTTPX/httpcore socket path with DNS and sockets blocked."""
    def resolve(host: str, *args: object, **kwargs: object) -> NoReturn:
        # Python's socket resolver uses this codec before resolving a hostname.
        # Reproduce its failure without ever invoking the system DNS resolver.
        host.encode("idna")
        raise AssertionError("Expected an invalid IDNA hostname")

    resolver = Mock(side_effect=resolve)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("Network forbidden")),
    )
    # Explicit real transport bypasses environment proxy discovery in the test
    # client, so the malformed destination reaches httpcore's socket backend.
    monkeypatch.setattr(
        httpx._api, "Client",
        Mock(side_effect=partial(httpx.Client, transport=httpx.HTTPTransport())),
    )
    return resolver


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_webhook_accepts_only_success_class(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text=_REMOTE_SECRET)

    factory = _install_transport(monkeypatch, respond)
    payload = {"monitor_id": "monitor-1", "message": "threshold exceeded"}

    assert http_webhook(_URL, payload) == (True, None)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == _URL
    assert json.loads(requests[0].content) == payload
    assert requests[0].headers["content-type"] == "application/json"
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0,
    }
    factory.assert_called_once()
    assert factory.call_args.kwargs["trust_env"] is True
    assert factory.call_args.kwargs["proxy"] is None


@pytest.mark.parametrize("status", [100, 199, 300, 304, 399, 400, 401, 429, 500, 599])
def test_webhook_failure_contains_status_without_response_body(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(status, text=_REMOTE_SECRET),
    )

    result = http_webhook(_URL, {"event": "alert"})

    assert result == (False, f"HTTP {status}")
    assert _REMOTE_SECRET not in str(result)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_webhook_redirect_is_failed_without_second_request(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                status,
                headers={"Location": "https://redirect-target.invalid/private"},
                text=_REMOTE_SECRET,
            )
        # A client following the redirect would obtain apparent success.
        return httpx.Response(200)

    _install_transport(monkeypatch, respond)

    assert http_webhook(_URL, {"event": "alert"}) == (False, f"HTTP {status}")
    assert [str(request.url) for request in requests] == [_URL]


@pytest.mark.parametrize(
    "exception_type",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError],
)
def test_webhook_transport_error_does_not_copy_exception_text(
    monkeypatch: pytest.MonkeyPatch, exception_type: type[httpx.RequestError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exception_type(_EXCEPTION_SECRET, request=request)

    _install_transport(monkeypatch, fail)

    result = http_webhook(_URL, {"event": "alert"})

    assert result == (False, "HTTP request failed")
    assert _EXCEPTION_SECRET not in str(result)


def test_webhook_malformed_url_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    _install_transport(monkeypatch, respond)

    assert http_webhook(_MALFORMED_URL, {"event": "alert"}) == (
        False, "HTTP request failed",
    )
    assert requests == []


@pytest.mark.parametrize("url", [_OVERLONG_HOST_URL, "http://a..example/hook"])
def test_webhook_hostname_encoding_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, url: str,
) -> None:
    resolver = _install_idna_failure(monkeypatch)

    assert http_webhook(url, {"event": "alert"}) == (
        False, "HTTP request failed",
    )
    resolver.assert_called_once()
    assert resolver.call_args.args[0] == httpx.URL(url).host


@pytest.mark.parametrize(
    "outcome", ["response", "transport", "redirect", "invalid_url", "idna", "success"],
)
def test_monitor_persists_webhook_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str,
) -> None:
    requests: list[httpx.Request] = []
    url = {"invalid_url": _MALFORMED_URL, "idna": _OVERLONG_HOST_URL}.get(outcome, _URL)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if outcome == "transport":
            raise httpx.ConnectError(_EXCEPTION_SECRET, request=request)
        if outcome == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://redirect-target.invalid/private"},
                text=_REMOTE_SECRET,
            )
        if outcome == "success":
            return httpx.Response(204)
        return httpx.Response(503, text=_REMOTE_SECRET)

    resolver = None
    if outcome == "idna":
        resolver = _install_idna_failure(monkeypatch)
    else:
        _install_transport(monkeypatch, respond)
    backend = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    control = ControlPlane(f"sqlite:///{tmp_path / 'control.db'}")
    try:
        backend.init_schema()
        control.init_schema()
        workspace = control.create_workspace(slug="webhook-test", name="Webhook test")
        control.create_project_with_id(
            project_id="p1", workspace_id=workspace["id"], slug="p1", name="p1",
        )
        monitor = create_monitor(
            control, project_id="p1", name="delivery test", nl_description="total cost",
            threshold=-1.0, operator="gt", window="1h", channels=[url],
        )

        result = MonitorEngine(backend, control).evaluate(monitor)

        assert result.threshold_breached is True
        assert result.alert is not None
        rows = control.list_alerts(project_id="p1")
        assert len(rows) == 1
        assert json.loads(rows[0]["channels_notified"]) == (
            [url] if outcome == "success" else []
        )
        error_message = {
            "response": "HTTP 503",
            "transport": "HTTP request failed",
            "redirect": "HTTP 302",
            "invalid_url": "HTTP request failed",
            "idna": "HTTP request failed",
        }.get(outcome)
        expected_errors = [f"{url}: {error_message}"] if error_message is not None else []
        # Existing behavior retains the configured destination in stored errors;
        # only untrusted response/exception text is excluded by this repair.
        assert json.loads(rows[0]["delivery_errors"]) == expected_errors
        assert _REMOTE_SECRET not in rows[0]["delivery_errors"]
        assert _EXCEPTION_SECRET not in rows[0]["delivery_errors"]
        assert len(requests) == (0 if outcome in {"invalid_url", "idna"} else 1)
        if resolver is not None:
            resolver.assert_called_once()
    finally:
        control.close()
        backend.close()
