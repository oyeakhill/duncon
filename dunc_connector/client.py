"""Low-level HTTP client for the Dunc platform."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from dunc_connector.errors import DuncAuthError, DuncTransportError


class DuncClient:
    """Thin HTTP wrapper around the four connector endpoints.

    Token is never written to logs, repr, or str. Auth failures (401/403)
    raise DuncAuthError; all other HTTP/network errors raise DuncTransportError.
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
        if http_client is None:
            http_client = httpx.Client(
                timeout=timeout,
                headers={"Authorization": f"Bearer {connection_token}"},
            )
        else:
            http_client.headers["Authorization"] = f"Bearer {connection_token}"
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
        self._post(f"/agent-runs/{run_id}/complete", json={"output_json": output_json})

    def fail_run(self, run_id: str, error_message: str) -> None:
        self._post(f"/agent-runs/{run_id}/fail", json={"error_message": error_message})

    def _post(self, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            resp = self._http.post(url, json=json)
        except httpx.HTTPError as e:
            raise DuncTransportError(f"POST {path} network error: {e}") from e
        self._raise_for_status(resp, path)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response, where: str) -> None:
        if resp.status_code in (401, 403):
            raise DuncAuthError(f"{where}: platform rejected connection token (status {resp.status_code})")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001
                detail = resp.text
            raise DuncTransportError(f"{where}: HTTP {resp.status_code}: {detail}")
