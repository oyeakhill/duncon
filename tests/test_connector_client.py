"""Unit tests for DuncClient — uses httpx.MockTransport so no real server needed."""

from typing import Any

import httpx
import pytest
from httpx import MockTransport, Request, Response

from dunc_connector import DuncAuthError, DuncClient, DuncTransportError


def _client_with_handler(handler: Any) -> DuncClient:
    transport = MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok_secret",
        http_client=http_client,
    )


def test_heartbeat_calls_correct_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return Response(200, json={"status": "ok"})

    client = _client_with_handler(handler)
    client.heartbeat()
    assert seen["url"].endswith("/agent-connections/cnx_test/heartbeat")
    assert seen["auth"] == "Bearer cnxtok_secret"


def test_fetch_runs_returns_list() -> None:
    payload = [{"id": "run_1", "input_json": {"k": "v"}}]
    client = _client_with_handler(lambda r: Response(200, json=payload))
    runs = client.fetch_runs(limit=3)
    assert runs == payload


def test_complete_run_posts_output() -> None:
    captured: dict[str, Any] = {}

    def handler(request: Request) -> Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return Response(200, json={"id": "run_1", "status": "completed"})

    client = _client_with_handler(handler)
    client.complete_run("run_1", {"answer": 42})
    assert "/agent-runs/run_1/complete" in captured["url"]
    assert '"output_json"' in captured["body"]
    assert '"answer":42' in captured["body"] or '"answer": 42' in captured["body"]


def test_fail_run_posts_error_message() -> None:
    captured: dict[str, Any] = {}

    def handler(request: Request) -> Response:
        captured["body"] = request.read().decode()
        return Response(200, json={"id": "run_1", "status": "failed"})

    client = _client_with_handler(handler)
    client.fail_run("run_1", "boom")
    assert '"error_message"' in captured["body"]
    assert "boom" in captured["body"]


def test_auth_error_maps_to_dunc_auth_error() -> None:
    client = _client_with_handler(lambda r: Response(401, json={"detail": "nope"}))
    with pytest.raises(DuncAuthError):
        client.heartbeat()


def test_other_http_errors_map_to_transport_error() -> None:
    client = _client_with_handler(lambda r: Response(500, json={"detail": "err"}))
    with pytest.raises(DuncTransportError):
        client.heartbeat()


def test_repr_masks_token() -> None:
    client = _client_with_handler(lambda r: Response(200))
    text = repr(client)
    assert "cnxtok_secret" not in text
    assert "cnx_test" in text


def test_str_masks_token() -> None:
    client = _client_with_handler(lambda r: Response(200))
    assert "cnxtok_secret" not in str(client)


def test_context_manager_closes_underlying_client() -> None:
    transport = MockTransport(lambda r: Response(200))
    http_client = httpx.Client(transport=transport)
    with DuncClient(
        base_url="http://test",
        connection_id="cnx_test",
        connection_token="cnxtok_secret",
        http_client=http_client,
    ) as client:
        client.heartbeat()
    assert http_client.is_closed
