"""High-level service wrapper: register a handler, .start() polls forever."""

from __future__ import annotations

import json as _json
import logging
import time
from typing import Any, Callable

from dunc_connector.client import DuncClient
from dunc_connector.errors import DuncTransportError, DuncValidationError

_LOG = logging.getLogger("dunc_connector")

Handler = Callable[[dict[str, Any]], dict[str, Any]]

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_BATCH_LIMIT = 10


class DuncService:
    """Polls Vicilus on a single AgentConnection and dispatches runs to a handler.

    Usage:
        svc = DuncService(base_url, connection_id, connection_token)

        @svc.run
        def handle(input_json):
            return {"answer": 42}

        svc.start()  # blocks; ^C to stop
    """

    def __init__(
        self,
        base_url: str,
        connection_id: str,
        connection_token: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        client: DuncClient | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._max_output_bytes = max_output_bytes
        self._handler: Handler | None = None
        if client is None:
            client = DuncClient(
                base_url=base_url,
                connection_id=connection_id,
                connection_token=connection_token,
            )
        self._client = client

    def __repr__(self) -> str:
        return f"DuncService(connection_id={self._connection_id!r}, poll_interval={self._poll_interval}, token=***)"

    def run(self, fn: Handler) -> Handler:
        """Decorator: registers the seller's handler. Returns fn unchanged."""
        self._handler = fn
        return fn

    def process_once(self) -> int:
        """Run a single poll cycle. Returns the number of runs processed."""
        if self._handler is None:
            raise RuntimeError("No handler registered. Use @service.run before calling process_once/start.")
        self._client.heartbeat()
        runs = self._client.fetch_runs(limit=self._batch_limit)
        for run in runs:
            self._dispatch(run)
        return len(runs)

    def start(self) -> None:
        """Poll forever until KeyboardInterrupt."""
        if self._handler is None:
            raise RuntimeError("No handler registered. Use @service.run before calling start.")
        _LOG.info(
            "dunc connector starting: connection_id=%s poll_interval=%ss",
            self._connection_id,
            self._poll_interval,
        )
        try:
            while True:
                try:
                    self.process_once()
                except DuncTransportError as e:
                    _LOG.warning("transport error during poll: %s", e)
                time.sleep(self._poll_interval)
        except KeyboardInterrupt:
            _LOG.info("dunc connector stopped by KeyboardInterrupt")
        finally:
            self._client.close()

    def _dispatch(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        input_json = run.get("input_json", {})
        assert self._handler is not None
        try:
            output = self._handler(input_json)
        except Exception as exc:  # noqa: BLE001 — deliberately catch broad and forward as fail
            msg = self._sanitize_error(exc)
            _LOG.warning("handler raised for run %s: %s", run_id, msg)
            self._safe_fail(run_id, msg)
            return
        try:
            self._validate_output(output)
        except DuncValidationError as e:
            self._safe_fail(run_id, str(e))
            return
        try:
            self._client.complete_run(run_id, output)
        except DuncTransportError as e:
            _LOG.warning("complete_run failed for %s: %s", run_id, e)

    def _validate_output(self, output: Any) -> None:
        if not isinstance(output, dict):
            raise DuncValidationError(
                f"Handler must return a dict, got {type(output).__name__}."
            )
        try:
            payload = _json.dumps(output)
        except (TypeError, ValueError) as e:
            raise DuncValidationError(f"Handler output is not JSON-serializable: {e}") from e
        size = len(payload.encode("utf-8"))
        if size > self._max_output_bytes:
            raise DuncValidationError(
                f"Handler output size {size} bytes exceeds max {self._max_output_bytes} bytes."
            )

    @staticmethod
    def _sanitize_error(exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"[:1000]

    def _safe_fail(self, run_id: str, message: str) -> None:
        try:
            self._client.fail_run(run_id, message)
        except DuncTransportError as e:
            _LOG.warning("fail_run failed for %s: %s", run_id, e)
