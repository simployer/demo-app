"""Shared HTTP plumbing for the backend clients."""

from __future__ import annotations

from typing import Any

import requests

from ..config import EndpointConfig


class BackendClient:
    """Minimal authenticated JSON-over-HTTP client.

    Auth is a bearer token if configured (service account / AKS managed
    identity token can be injected via the ``*_TOKEN`` env vars). Kept
    deliberately small — the POC only needs read queries.
    """

    def __init__(self, config: EndpointConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        return headers

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._config.base_url}{path}"
        resp = self._session.get(
            url,
            params=params,
            headers=self._headers(),
            timeout=self._config.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()
