"""Unit tests for DuncService — verify the polling/dispatch loop and error paths."""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest
from httpx import MockTransport, Request, Response

from dunc_connector import DuncClient, DuncService


class _FakeServer:
    """Records platform-side state across a few connector cycles."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.heartbeats = 0
        self.completed: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[str, str]] = []

    def handler(self, request: Request) -> Response:
        path = request.url.path
        if path.endswith("/heartbeat"):
            self.heartbeats += 1
            return Response(200, json={"status": "ok"})
        if path.endswith("/runs") and request.method == "GET":
            payload = list(self.runs)
            self.runs.clear()  # one-shot: subsequent fetches return empty
            return Response(200, json=payload)
        if path.endswith("/complete"):
            run_id = path.split("/")[-2]
            body = request.read().decode()
            output = _json.loads(body).get("output_json", {})
            self.completed.append((run_id, output))
            return Response(200, json={"id": run_id, "status": "completed"})
        if path.endswith("/fail"):
            run_id = path.split("/")[-2]
            err = _json.loads(request.read().decode()).get("error_message", "")
            self.failed.append((run_id, err))
            return Response(200, json={"id": run_id, "status": "failed"})
        return Response(404)


def _service(
    server: _FakeServer,
    *,
    batch_limit: int = 1,
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
    return DuncService(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok_secret",
        batch_limit=batch_limit,
        handler_timeout=handler_timeout,
        client=client,
    )


def test_handler_invoked_for_each_run() -> None:
    # Two runs in one batch is unusual under the v0.1.1 default
    # (batch_limit=1) but explicit batch_limit override is allowed for
    # callers with very fast handlers.
    server = _FakeServer(runs=[
        {"id": "run_a", "input_json": {"x": 1}},
        {"id": "run_b", "input_json": {"x": 2}},
    ])
    svc = _service(server, batch_limit=2)
    seen: list[dict[str, Any]] = []

    @svc.run
    def handle(input_json: dict[str, Any]) -> dict[str, Any]:
        seen.append(input_json)
        return {"got": input_json}

    svc.process_once()
    assert seen == [{"x": 1}, {"x": 2}]
    assert server.completed == [
        ("run_a", {"got": {"x": 1}}),
        ("run_b", {"got": {"x": 2}}),
    ]
    # 1 heartbeat before fetch + 1 after each dispatch (2 runs) = 3.
    # The intra-batch heartbeats are why the connector won't look offline
    # mid-batch when handlers take a long time.
    assert server.heartbeats == 3


def test_handler_exception_calls_fail_run_with_sanitized_message() -> None:
    server = _FakeServer(runs=[{"id": "run_x", "input_json": {}}])
    svc = _service(server)

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    svc.process_once()
    assert server.completed == []
    assert len(server.failed) == 1
    run_id, msg = server.failed[0]
    assert run_id == "run_x"
    assert "RuntimeError" in msg
    assert "kaboom" in msg


def test_non_dict_output_fails_run() -> None:
    server = _FakeServer(runs=[{"id": "run_y", "input_json": {}}])
    svc = _service(server)

    @svc.run
    def handle(_inp: dict[str, Any]) -> Any:
        return "not a dict"  # type: ignore[return-value]

    svc.process_once()
    assert server.completed == []
    assert len(server.failed) == 1
    assert "dict" in server.failed[0][1].lower()


def test_oversized_output_fails_run() -> None:
    server = _FakeServer(runs=[{"id": "run_big", "input_json": {}}])
    svc = _service(server)
    svc._max_output_bytes = 100  # tighten for test

    @svc.run
    def handle(_inp: dict[str, Any]) -> dict[str, Any]:
        return {"data": "x" * 1_000}

    svc.process_once()
    assert server.completed == []
    assert "exceeds" in server.failed[0][1].lower() or "too large" in server.failed[0][1].lower()


def test_handler_required_before_process_once() -> None:
    server = _FakeServer(runs=[])
    svc = _service(server)
    with pytest.raises(RuntimeError):
        svc.process_once()


def test_handler_required_before_start() -> None:
    server = _FakeServer(runs=[])
    svc = _service(server)
    with pytest.raises(RuntimeError):
        svc.start()


def test_repr_masks_token() -> None:
    server = _FakeServer(runs=[])
    svc = _service(server)
    text = repr(svc)
    assert "cnxtok_secret" not in text
    assert "cnx_test" in text


def test_run_decorator_returns_function_unchanged() -> None:
    server = _FakeServer(runs=[])
    svc = _service(server)

    def raw(x: dict[str, Any]) -> dict[str, Any]:
        return {"y": 1}

    decorated = svc.run(raw)
    assert decorated is raw
