"""CLI for the dunc-connector — wraps a child command or local HTTP target.

Usage:
  dunc-connector command --command "python agent.py" \\
      --base-url ... --connection-id ... --connection-token ...

  dunc-connector http --target-url http://localhost:9000/run \\
      --base-url ... --connection-id ... --connection-token ...

Common flags:
  --poll-interval (default 2.0)
  --batch-limit   (default 10)
  --command-timeout / --http-timeout (default 60s)
  --once          (run a single poll cycle and exit; useful for tests)
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import shlex
import subprocess
import sys
from typing import Any, Callable

import httpx

from dunc_connector.service import DuncService

_LOG = logging.getLogger("dunc_connector.cli")

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def build_command_handler(command: str, *, timeout: float = 60.0) -> Handler:
    """Return a handler that pipes input_json into a child process and reads JSON from stdout."""
    args = shlex.split(command)

    def handler(input_json: dict[str, Any]) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                args,
                input=_json.dumps(input_json),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"child process timed out after {timeout}s") from e
        if proc.returncode != 0:
            raise RuntimeError(
                f"child exited with code {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        try:
            data = _json.loads(proc.stdout)
        except _json.JSONDecodeError as e:
            raise ValueError(f"child stdout was not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"child returned {type(data).__name__}, expected JSON object")
        return data

    return handler


def build_http_handler(
    target_url: str,
    *,
    http_client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> Handler:
    """Return a handler that POSTs input_json to a local HTTP endpoint and parses JSON response."""
    client = http_client or httpx.Client(timeout=timeout)

    def handler(input_json: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = client.post(target_url, json=input_json)
        except httpx.HTTPError as e:
            raise RuntimeError(f"http target unreachable: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"http target returned {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except _json.JSONDecodeError as e:
            raise ValueError(f"http target response was not JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"http target returned {type(data).__name__}, expected JSON object")
        return data

    return handler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dunc-connector",
        description="Run a seller agent against the Dunc platform.",
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--connection-token", required=True)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--batch-limit", type=int, default=10)
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")

    subs = parser.add_subparsers(dest="mode", required=True)

    cmd = subs.add_parser(
        "command",
        help="Run a child command per run; pipe input_json -> stdin, parse stdout JSON",
    )
    cmd.add_argument("--command", required=True, help='Shell command to run, e.g. "python agent.py"')
    cmd.add_argument("--command-timeout", type=float, default=60.0)

    http = subs.add_parser(
        "http",
        help="POST input_json to a local HTTP endpoint, expect JSON response",
    )
    http.add_argument("--target-url", required=True)
    http.add_argument("--http-timeout", type=float, default=60.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[connector] %(message)s")
    args = _build_parser().parse_args(argv)
    if args.mode == "command":
        handler = build_command_handler(args.command, timeout=args.command_timeout)
    elif args.mode == "http":
        handler = build_http_handler(args.target_url, timeout=args.http_timeout)
    else:  # pragma: no cover — argparse guards this
        print(f"unknown mode: {args.mode}", file=sys.stderr)
        return 2

    svc = DuncService(
        base_url=args.base_url,
        connection_id=args.connection_id,
        connection_token=args.connection_token,
        poll_interval=args.poll_interval,
        batch_limit=args.batch_limit,
    )
    svc.run(handler)

    if args.once:
        svc.process_once()
        return 0
    svc.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
