"""High-level service wrapper: register a handler, .start() polls forever.

Reliability notes (v0.1.1):

* `start()` installs SIGTERM and SIGINT handlers that flip a shutdown flag.
  The poll loop checks the flag between fetches, so an in-flight `_dispatch`
  finishes (or times out via `handler_timeout`) before the process exits.
  This avoids leaving runs stuck in `processing` across deploys.

* Default `batch_limit=1` so the connector heartbeats *between* every dispatch
  rather than at the start of a multi-run batch. Long handlers no longer make
  the connector look offline mid-batch. Callers who run very fast handlers
  can opt in to higher concurrency by setting `batch_limit` explicitly — at
  their operational risk.

* `handler_timeout` (default 90s) bounds the seller's handler under the
  platform's `RUN_TIMEOUT_SECONDS` (default 120s). If the handler exceeds it,
  the SDK fails the run cleanly with a sanitized message *before* the
  platform sweeps and refunds the buyer. Sellers who want to run longer must
  raise both their `handler_timeout` and the platform timeout.

* If `complete_run` / `fail_run` returns 422 invalid_state_transition (the
  run was already finalized by a platform sweep or a buyer cancel), the SDK
  logs a clear "your work was not accepted" message instead of warning about
  a generic transport error. The seller's local handler still ran; nothing
  is rolled back on the connector side.

* Transport errors trigger exponential backoff (start at `poll_interval`,
  cap at 60s, reset on the next successful heartbeat/fetch). Forever-retry
  is unchanged; we just stop hammering the platform during outages.
"""

from __future__ import annotations

import json as _json
import logging
import signal
import threading
import time
from typing import Any, Callable

from dunc_connector.client import DuncClient
from dunc_connector.errors import (
    DuncRunFinalizedError,
    DuncTransportError,
    DuncValidationError,
)

_LOG = logging.getLogger("dunc_connector")

Handler = Callable[[dict[str, Any]], dict[str, Any]]

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB
DEFAULT_POLL_INTERVAL = 2.0
# Default to single-flight dispatch so the connector heartbeats between runs.
# A long-running handler at batch_limit=10 would skip 9 heartbeats and look
# offline to buyers mid-batch. Sellers who explicitly want higher concurrency
# can still raise this — they accept the tradeoff.
DEFAULT_BATCH_LIMIT = 1
# Bound the handler under the platform's run-timeout sweep (default 120s).
# 90s leaves headroom for the connector's complete/fail call to land before
# the sweep would mark the run timed_out and refund the buyer.
DEFAULT_HANDLER_TIMEOUT = 90.0
DEFAULT_BACKOFF_CAP = 60.0


class HandlerTimeout(Exception):
    """Raised by the dispatch path when the handler exceeds handler_timeout.

    Internal — never leaks past `_dispatch`. The fail message sent to the
    platform is fixed and contains no exception text from the handler.
    """


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run `fn` in a daemon thread, raise HandlerTimeout if it doesn't return.

    Pure-Python timeout that works for any callable on any platform (unlike
    signal.alarm, which is Unix-only and main-thread only). The handler's
    thread keeps running after timeout — Python can't safely kill a thread.
    The connector continues; the orphaned handler will be cleaned up when
    the process exits.
    """
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_runner, daemon=True, name="dunc-handler")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise HandlerTimeout(f"handler did not return within {timeout}s")
    if "exc" in box:
        raise box["exc"]  # type: ignore[misc]
    return box.get("result")


class DuncService:
    """Polls Vicilus on a single AgentConnection and dispatches runs to a handler.

    Usage:
        svc = DuncService(base_url, connection_id, connection_token)

        @svc.run
        def handle(input_json):
            return {"answer": 42}

        svc.start()  # blocks; SIGTERM / Ctrl-C to stop cleanly
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
        handler_timeout: float | None = DEFAULT_HANDLER_TIMEOUT,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        client: DuncClient | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._max_output_bytes = max_output_bytes
        self._handler_timeout = handler_timeout
        self._backoff_cap = backoff_cap
        self._handler: Handler | None = None
        # Set when SIGTERM/SIGINT arrives. The poll loop checks this between
        # fetches and exits cleanly after the current dispatch returns.
        self._shutdown = threading.Event()
        if client is None:
            client = DuncClient(
                base_url=base_url,
                connection_id=connection_id,
                connection_token=connection_token,
            )
        self._client = client

    def __repr__(self) -> str:
        return (
            f"DuncService(connection_id={self._connection_id!r}, "
            f"poll_interval={self._poll_interval}, "
            f"batch_limit={self._batch_limit}, "
            f"handler_timeout={self._handler_timeout}, token=***)"
        )

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown.is_set()

    def request_shutdown(self) -> None:
        """Signal the loop to stop. Safe to call from any thread or signal handler."""
        self._shutdown.set()

    def run(self, fn: Handler) -> Handler:
        """Decorator: registers the seller's handler. Returns fn unchanged."""
        self._handler = fn
        return fn

    def process_once(self) -> int:
        """Run a single poll cycle. Returns the number of runs processed.

        Heartbeats once before fetching, and again after each dispatch so
        long handlers don't make the connector look offline mid-batch.
        """
        if self._handler is None:
            raise RuntimeError(
                "No handler registered. Use @service.run before calling process_once/start."
            )
        self._client.heartbeat()
        runs = self._client.fetch_runs(limit=self._batch_limit)
        for run in runs:
            self._dispatch(run)
            # Heartbeat between dispatches — important when batch_limit > 1
            # and a handler took most of a minute. Cheap; ignores transport
            # blips so a flaky network doesn't abort the rest of the batch.
            try:
                self._client.heartbeat()
            except DuncTransportError as e:
                _LOG.debug("intra-batch heartbeat failed (will retry next cycle): %s", e)
            if self._shutdown.is_set():
                _LOG.info(
                    "shutdown requested mid-batch — finished current run, exiting loop"
                )
                break
        return len(runs)

    def start(self) -> None:
        """Poll forever until SIGTERM, SIGINT, or KeyboardInterrupt.

        Installs signal handlers only when run on the main thread (signals
        are main-thread-only in Python). Tests / embedded use that drive
        process_once() directly skip signal install entirely.
        """
        if self._handler is None:
            raise RuntimeError(
                "No handler registered. Use @service.run before calling start."
            )
        self._install_signal_handlers()
        _LOG.info(
            "dunc connector starting: connection_id=%s poll_interval=%ss batch_limit=%d handler_timeout=%ss",
            self._connection_id,
            self._poll_interval,
            self._batch_limit,
            self._handler_timeout,
        )

        backoff = self._poll_interval
        try:
            while not self._shutdown.is_set():
                try:
                    self.process_once()
                    backoff = self._poll_interval  # success — reset
                except DuncTransportError as e:
                    _LOG.warning(
                        "transport error: %s — backing off %.1fs", e, backoff
                    )
                    self._sleep_interruptible(backoff)
                    backoff = min(backoff * 2, self._backoff_cap)
                    continue
                self._sleep_interruptible(self._poll_interval)
        except KeyboardInterrupt:
            # Belt-and-suspenders: signal handler should already have flipped
            # the shutdown flag, but if signals weren't installed (e.g. when
            # called from a non-main thread), fall through here.
            _LOG.info("dunc connector stopped by KeyboardInterrupt")
        finally:
            _LOG.info("dunc connector shutting down cleanly")
            self._client.close()

    # ---- internals -----------------------------------------------------

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except (ValueError, OSError):
            # Not on the main thread (or signal not supported on this
            # platform — Windows lacks SIGTERM in some cases). Fall back to
            # KeyboardInterrupt-only behavior.
            _LOG.debug(
                "signal handlers not installed (likely non-main thread); "
                "shutdown will rely on KeyboardInterrupt or request_shutdown()"
            )

    def _on_signal(self, signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name if signum in signal.Signals.__members__.values() else str(signum)
        _LOG.info("received %s — finishing current run and exiting", name)
        self._shutdown.set()

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep, but wake immediately if shutdown was requested."""
        self._shutdown.wait(timeout=seconds)

    def _dispatch(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        input_json = run.get("input_json", {})
        assert self._handler is not None
        handler = self._handler

        # Guard the seller's handler with a local timeout so we can fail-fast
        # with a clean message before the platform's run-timeout sweep refunds
        # the buyer. None disables the guard (callers that explicitly want
        # unbounded handlers).
        try:
            if self._handler_timeout is None:
                output = handler(input_json)
            else:
                output = _run_with_timeout(
                    lambda: handler(input_json), self._handler_timeout
                )
        except HandlerTimeout:
            _LOG.warning(
                "handler timed out for run %s after %.0fs",
                run_id,
                self._handler_timeout,
            )
            self._safe_fail(
                run_id,
                "handler timed out before platform run timeout",
            )
            return
        except Exception as exc:  # noqa: BLE001 — broad on purpose, sanitize + forward
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
        except DuncRunFinalizedError:
            _LOG.warning(
                "run %s was already finalized by platform; result was not accepted",
                run_id,
            )
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
            raise DuncValidationError(
                f"Handler output is not JSON-serializable: {e}"
            ) from e
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
        except DuncRunFinalizedError:
            _LOG.warning(
                "run %s was already finalized by platform; fail message was not recorded",
                run_id,
            )
        except DuncTransportError as e:
            _LOG.warning("fail_run failed for %s: %s", run_id, e)
