"""Low-level HTTP client for the Vicilus platform."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from dunc_connector.errors import (
    DuncAuthError,
    DuncRunFinalizedError,
    DuncTransportError,
)


def _user_agent() -> str:
    """Return the SDK's User-Agent string. Imported lazily to avoid a
    circular import (errors.py is leaf-level)."""
    from dunc_connector import __version__

    return f"dunc-connector/{__version__}"


class DuncClient:
    """Thin HTTP wrapper around the four connector endpoints.

    Token is never written to logs, repr, or str. Auth failures (401/403)
    raise DuncAuthError; 422 from complete/fail (run already finalized by
    the platform's timeout sweep or a buyer cancel) raises
    DuncRunFinalizedError; all other HTTP/network errors raise
    DuncTransportError.

    Every request carries a `User-Agent: dunc-connector/<version>` header
    so platform-side debugging can correlate connector behavior to a
    specific SDK release.
    """

    def __init__(
        self,
        base_url: str,
        connection_id: str,
        connection_token: str,
        *,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._connection_id = connection_id
        self._token = connection_token
        ua = _user_agent()
        if http_client is None:
            http_client = httpx.Client(
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {connection_token}",
                    "User-Agent": ua,
                },
            )
        else:
            http_client.headers["Authorization"] = f"Bearer {connection_token}"
            http_client.headers["User-Agent"] = ua
        self._http = http_client

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def base_url(self) -> str:
        return self._base

    def __repr__(self) -> str:
        return f"DuncClient(base_url={self._base!r}, connection_id={self._connection_id!r}, token=***)"

    def __str__(self) -> str:
        return self.__repr__()

    def __enter__(self) -> DuncClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def heartbeat(self) -> None:
        self._post(f"/agent-connections/{self._connection_id}/heartbeat")

    def fetch_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        url = f"{self._base}/agent-connections/{self._connection_id}/runs"
        try:
            resp = self._http.get(url, params={"limit": limit})
        except httpx.HTTPError as e:
            raise DuncTransportError(f"fetch_runs network error: {e}") from e
        self._raise_for_status(resp, "fetch_runs")
        return resp.json()

    def complete_run(self, run_id: str, output_json: dict[str, Any]) -> None:
        self._post(
            f"/agent-runs/{run_id}/complete",
            json={"output_json": output_json},
            run_terminal=True,
        )

    def fail_run(self, run_id: str, error_message: str) -> None:
        self._post(
            f"/agent-runs/{run_id}/fail",
            json={"error_message": error_message},
            run_terminal=True,
        )

    def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        run_terminal: bool = False,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            resp = self._http.post(url, json=json)
        except httpx.HTTPError as e:
            raise DuncTransportError(f"POST {path} network error: {e}") from e
        self._raise_for_status(resp, path, run_terminal=run_terminal)
        return resp

    @staticmethod
    def _raise_for_status(
        resp: httpx.Response, where: str, *, run_terminal: bool = False
    ) -> None:
        if resp.status_code in (401, 403):
            raise DuncAuthError(
                f"{where}: platform rejected connection token (status {resp.status_code})"
            )
        # complete/fail return 422 invalid_state_transition when the run is
        # no longer in `processing` — sweeps to `timed_out` or buyer cancels
        # are the realistic causes. Surface this distinctly so the service
        # layer can log a clear "your work was wasted" message and continue.
        if run_terminal and resp.status_code == 422:
            try:
                body = resp.json()
                code = (body.get("error") or {}).get("code") or body.get("detail")
            except Exception:  # noqa: BLE001
                code = None
            if code == "invalid_state_transition":
                raise DuncRunFinalizedError(
                    f"{where}: run was already finalized by platform; result was not accepted"
                )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001
                detail = resp.text
            raise DuncTransportError(f"{where}: HTTP {resp.status_code}: {detail}")
