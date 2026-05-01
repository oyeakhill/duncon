"""Tests for the dunc-connector CLI handlers (command + http mode)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import MockTransport, Request, Response

from dunc_connector.cli import build_command_handler, build_http_handler


def test_command_handler_passes_input_json_to_stdin_and_reads_stdout(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(
        "import json,sys\n"
        "data=json.load(sys.stdin)\n"
        "json.dump({'echo':data,'tag':'cmd'}, sys.stdout)\n"
    )
    handler = build_command_handler(f"{sys.executable} {script}", timeout=10.0)
    out = handler({"hello": "world"})
    assert out == {"echo": {"hello": "world"}, "tag": "cmd"}


def test_command_handler_raises_on_non_json_stdout(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("import sys; sys.stdout.write('not json')\n")
    handler = build_command_handler(f"{sys.executable} {script}", timeout=5.0)
    with pytest.raises(ValueError):
        handler({})


def test_command_handler_raises_on_nonzero_exit(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("import sys; sys.exit(2)\n")
    handler = build_command_handler(f"{sys.executable} {script}", timeout=5.0)
    with pytest.raises(RuntimeError):
        handler({})


def test_command_handler_raises_when_stdout_is_not_object(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("import json,sys; json.dump([1,2,3], sys.stdout)\n")
    handler = build_command_handler(f"{sys.executable} {script}", timeout=5.0)
    with pytest.raises(ValueError):
        handler({})


def test_http_handler_posts_input_and_reads_json_response() -> None:
    seen: dict[str, Any] = {}

    def server(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return Response(200, json={"echo": {"hi": True}, "tag": "http"})

    transport = MockTransport(server)
    client = httpx.Client(transport=transport, timeout=5.0)
    handler = build_http_handler("http://target/run", http_client=client)
    out = handler({"hi": True})
    assert out == {"echo": {"hi": True}, "tag": "http"}
    assert "/run" in seen["url"]
    assert "true" in seen["body"]  # json-encoded "hi": true


def test_http_handler_raises_on_non_json() -> None:
    transport = MockTransport(
        lambda r: Response(200, text="not json", headers={"content-type": "text/plain"})
    )
    client = httpx.Client(transport=transport, timeout=5.0)
    handler = build_http_handler("http://target/run", http_client=client)
    with pytest.raises(ValueError):
        handler({})


def test_http_handler_raises_on_500() -> None:
    transport = MockTransport(lambda r: Response(500, json={"detail": "boom"}))
    client = httpx.Client(transport=transport, timeout=5.0)
    handler = build_http_handler("http://target/run", http_client=client)
    with pytest.raises(RuntimeError):
        handler({})


def test_http_handler_raises_on_non_object_response() -> None:
    transport = MockTransport(lambda r: Response(200, json=[1, 2, 3]))
    client = httpx.Client(transport=transport, timeout=5.0)
    handler = build_http_handler("http://target/run", http_client=client)
    with pytest.raises(ValueError):
        handler({})


def test_http_handler_raises_on_network_error() -> None:
    def server(_r: Request) -> Response:
        raise httpx.ConnectError("refused")

    transport = MockTransport(server)
    client = httpx.Client(transport=transport, timeout=5.0)
    handler = build_http_handler("http://target/run", http_client=client)
    with pytest.raises(RuntimeError):
        handler({})
