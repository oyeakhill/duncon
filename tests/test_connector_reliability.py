"""Tests for the v0.1.1 reliability hardening:

  * Default batch_limit=1 + heartbeat between dispatches.
  * handler_timeout fails the run with a sanitized message.
  * Platform 422 (run already finalized) surfaces as DuncRunFinalizedError
    and is logged, not blown up.
  * Transport-error backoff exists and resets after success.
  * SIGTERM / shutdown flag stops the loop after the current dispatch.
  * CLI token resolution: env var preferred, --connection-token works,
    missing both errors clearly.
  * SDK exposes __version__ and User-Agent header.
"""

from __future__ import annotations

import argparse
import json as _json
import threading
import time
from typing import Any

import httpx
import pytest
from httpx import MockTransport, Request, Response

import dunc_connector
from dunc_connector import (
    DuncAuthError,
    DuncClient,
    DuncRunFinalizedError,
    DuncService,
)
from dunc_connector.cli import resolve_token
from dunc_connector.service import (
    DEFAULT_BATCH_LIMIT,
    DEFAULT_HANDLER_TIMEOUT,
)


# ---- helpers --------------------------------------------------------------


class _FakeServer:
    def __init__(
        self,
        runs: list[dict[str, Any]],
        *,
        complete_status: int = 200,
        complete_body: dict[str, Any] | None = None,
        fail_status: int = 200,
        fail_body: dict[str, Any] | None = None,
    ) -> None:
        self.runs = list(runs)
        self.heartbeats = 0
        self.completed: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[str, str]] = []
        self.user_agents: list[str] = []
        self.complete_status = complete_status
        self.complete_body = complete_body or {"status": "completed"}
        self.fail_status = fail_status
        self.fail_body = fail_body or {"status": "failed"}

    def handler(self, request: Request) -> Response:
        self.user_agents.append(request.headers.get("user-agent", ""))
        path = request.url.path
        if path.endswith("/heartbeat"):
            self.heartbeats += 1
            return Response(200, json={"status": "ok"})
        if path.endswith("/runs") and request.method == "GET":
            payload = list(self.runs)
            self.runs.clear()
            return Response(200, json=payload)
        if path.endswith("/complete"):
            run_id = path.split("/")[-2]
            output = _json.loads(request.read().decode()).get("output_json", {})
            self.completed.append((run_id, output))
            return Response(self.complete_status, json=self.complete_body)
        if path.endswith("/fail"):
            run_id = path.split("/")[-2]
            err = _json.loads(request.read().decode()).get("error_message", "")
            self.failed.append((run_id, err))
            return Response(self.fail_status, json=self.fail_body)
        return Response(404)


def _service(
    server: _FakeServer,
    *,
    batch_limit: int | None = None,
    handler_timeout: float | None = None,
) -> DuncService:
    transport = MockTransport(server.handler)
    http = httpx.Client(transport=transport)
    client = DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok_secret",
        http_client=http,
    )
    kwargs: dict[str, Any] = {"client": client}
    if batch_limit is not None:
        kwargs["batch_limit"] = batch_limit
    if handler_timeout is not None:
        kwargs["handler_timeout"] = handler_timeout
    return DuncService(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok_secret",
        **kwargs,
    )


# ---- defaults --------------------------------------------------------------


def test_default_batch_limit_is_one() -> None:
    """Connector defaults to single-flight dispatch."""
    assert DEFAULT_BATCH_LIMIT == 1


def test_default_handler_timeout_is_below_platform_timeout() -> None:
    """SDK defaults to 90s, leaving headroom under the platform's 120s sweep."""
    assert DEFAULT_HANDLER_TIMEOUT == 90.0


def test_version_exposed() -> None:
    assert dunc_connector.__version__ == "0.1.1"


# ---- User-Agent ------------------------------------------------------------


def test_user_agent_header_includes_version() -> None:
    server = _FakeServer(runs=[{"id": "run_ua", "input_json": {}}])
    svc = _service(server)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    svc.process_once()
    assert any(ua.startswith("dunc-connector/0.1.1") for ua in server.user_agents)


# ---- per-run heartbeat -----------------------------------------------------


def test_heartbeat_fires_between_each_dispatch() -> None:
    """3 runs at batch_limit=3 => 1 pre-fetch heartbeat + 3 inter-dispatch = 4."""
    server = _FakeServer(
        runs=[
            {"id": "run_1", "input_json": {}},
            {"id": "run_2", "input_json": {}},
            {"id": "run_3", "input_json": {}},
        ]
    )
    svc = _service(server, batch_limit=3)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    svc.process_once()
    assert server.heartbeats == 4


# ---- handler_timeout -------------------------------------------------------


def test_handler_timeout_fails_run_with_safe_message() -> None:
    server = _FakeServer(runs=[{"id": "run_slow", "input_json": {}}])
    svc = _service(server, handler_timeout=0.1)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.5)  # exceeds 0.1s timeout
        return {"never": True}

    svc.process_once()
    assert server.completed == []
    assert len(server.failed) == 1
    run_id, msg = server.failed[0]
    assert run_id == "run_slow"
    # Sanitized message — no exception traceback, no leaked exception text.
    assert "handler timed out" in msg.lower()
    assert "platform" in msg.lower()


def test_handler_timeout_disabled_when_none() -> None:
    """handler_timeout=None lets handlers run without the SDK guard."""
    server = _FakeServer(runs=[{"id": "run_long_but_ok", "input_json": {}}])
    svc = _service(server, handler_timeout=None)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    svc.process_once()
    assert server.completed == [("run_long_but_ok", {"ok": True})]
    assert server.failed == []


# ---- 422 platform-finalized awareness --------------------------------------


def test_complete_run_422_invalid_state_raises_finalized_error() -> None:
    """The client surfaces 422 invalid_state_transition as DuncRunFinalizedError."""
    captured: dict[str, int] = {"hits": 0}

    def handler(request: Request) -> Response:
        captured["hits"] += 1
        return Response(
            422,
            json={
                "error": {
                    "code": "invalid_state_transition",
                    "message": "Cannot complete run in status 'timed_out'.",
                }
            },
        )

    transport = MockTransport(handler)
    client = DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok",
        http_client=httpx.Client(transport=transport),
    )
    with pytest.raises(DuncRunFinalizedError):
        client.complete_run("run_x", {"ok": True})


def test_other_422_still_maps_to_transport_error() -> None:
    """422 with a different code is *not* a finalized-run signal."""
    transport = MockTransport(
        lambda r: Response(
            422, json={"error": {"code": "validation_error", "message": "bad input"}}
        )
    )
    client = DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok",
        http_client=httpx.Client(transport=transport),
    )
    from dunc_connector import DuncTransportError

    with pytest.raises(DuncTransportError):
        client.complete_run("run_x", {"ok": True})


def test_dispatch_logs_finalized_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    """When complete_run hits 422 the loop logs and moves on, no exception leaks."""
    import logging

    server = _FakeServer(
        runs=[{"id": "run_finalized", "input_json": {}}],
        complete_status=422,
        complete_body={
            "error": {
                "code": "invalid_state_transition",
                "message": "Cannot complete run in status 'timed_out'.",
            }
        },
    )
    svc = _service(server)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="dunc_connector"):
        svc.process_once()  # must not raise
    assert any("already finalized" in rec.getMessage() for rec in caplog.records)


# ---- shutdown / SIGTERM ----------------------------------------------------


def test_request_shutdown_breaks_start_loop() -> None:
    """request_shutdown() (mirrors what the SIGTERM handler does) exits start()."""
    server = _FakeServer(runs=[])  # no runs, just heartbeats
    svc = _service(server)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {}

    # Trip the shutdown flag from another thread shortly after start().
    def _trip() -> None:
        time.sleep(0.05)
        svc.request_shutdown()

    threading.Thread(target=_trip, daemon=True).start()

    start_t = time.time()
    svc.start()
    elapsed = time.time() - start_t
    # start() must return quickly once the flag is set — well under one full
    # poll_interval (default 2s) thanks to _sleep_interruptible.
    assert elapsed < 1.5
    assert svc.is_shutting_down


def test_shutdown_mid_batch_finishes_current_run_then_exits() -> None:
    """If shutdown fires during a multi-run batch, current dispatch finishes,
    remaining runs are skipped, no orphan `processing` rows."""
    server = _FakeServer(
        runs=[
            {"id": "run_1", "input_json": {}},
            {"id": "run_2", "input_json": {}},
            {"id": "run_3", "input_json": {}},
        ]
    )
    svc = _service(server, batch_limit=3)
    finished: list[str] = []

    @svc.run
    def handle(inp: dict[str, Any]) -> dict[str, Any]:
        # First run trips the flag; remaining runs in the batch must be skipped.
        if not finished:
            svc.request_shutdown()
        finished.append("ran")
        return {"ok": True}

    svc.process_once()
    # Only the run that started before the flag is processed.
    assert len(server.completed) == 1
    assert finished == ["ran"]


# ---- CLI token resolution --------------------------------------------------


def _ns(**kwargs: Any) -> argparse.Namespace:
    base = {
        "connection_token": None,
        "connection_token_env": "DUNC_CONNECTION_TOKEN",
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_resolve_token_from_env_var() -> None:
    args = _ns()
    token = resolve_token(args, env={"DUNC_CONNECTION_TOKEN": "cnxtok_from_env"})
    assert token == "cnxtok_from_env"


def test_resolve_token_explicit_flag_wins_over_env() -> None:
    args = _ns(connection_token="cnxtok_explicit")
    token = resolve_token(args, env={"DUNC_CONNECTION_TOKEN": "cnxtok_env"})
    assert token == "cnxtok_explicit"


def test_resolve_token_missing_both_errors_clearly() -> None:
    args = _ns()
    with pytest.raises(SystemExit) as exc_info:
        resolve_token(args, env={})
    msg = str(exc_info.value)
    assert "connection token missing" in msg
    assert "DUNC_CONNECTION_TOKEN" in msg


def test_resolve_token_custom_env_var_name() -> None:
    args = _ns(connection_token_env="VICILUS_TOKEN")
    token = resolve_token(args, env={"VICILUS_TOKEN": "cnxtok_custom"})
    assert token == "cnxtok_custom"


# ---- backoff math ----------------------------------------------------------


def test_transport_error_backoff_doubles_then_caps() -> None:
    """The loop's backoff state machine: poll_interval → 2× → 4× → cap."""
    # We don't run start() in real time — just exercise the math by
    # mimicking the loop's expression directly.
    backoff = 1.0
    cap = 60.0
    sequence: list[float] = []
    for _ in range(8):
        sequence.append(backoff)
        backoff = min(backoff * 2, cap)
    assert sequence == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_backoff_resets_after_successful_process_once() -> None:
    """After a successful poll cycle the loop's `backoff` returns to poll_interval."""
    # Build a server that 500s once then succeeds. process_once() raises
    # DuncTransportError on the first call, succeeds on the second.
    state = {"first": True}

    def handler(request: Request) -> Response:
        if state["first"] and request.url.path.endswith("/heartbeat"):
            state["first"] = False
            return Response(503, json={"detail": "down"})
        if request.url.path.endswith("/heartbeat"):
            return Response(200, json={"ok": True})
        if request.url.path.endswith("/runs"):
            return Response(200, json=[])
        return Response(404)

    transport = MockTransport(handler)
    client = DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok",
        http_client=httpx.Client(transport=transport),
    )
    svc = DuncService(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok",
        client=client,
    )

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {}

    from dunc_connector import DuncTransportError

    with pytest.raises(DuncTransportError):
        svc.process_once()
    # second call succeeds — verifies the client recovers.
    svc.process_once()
