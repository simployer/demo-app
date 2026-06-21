"""Prometheus HTTP API client (instant queries)."""

from __future__ import annotations

from .base import BackendClient


class PrometheusClient(BackendClient):
    """Wraps the Prometheus ``/api/v1/query`` instant-query endpoint."""

    def instant_query(self, promql: str) -> list[dict]:
        """Run an instant PromQL query, returning the raw result vector.

        Each element looks like ``{"metric": {...}, "value": [ts, "1.23"]}``.
        """
        body = self.get("/api/v1/query", params={"query": promql})
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {body.get('error')}")
        return body.get("data", {}).get("result", [])

    def scalar(self, promql: str, default: float = 0.0) -> float:
        """Run a query expected to return a single scalar/sample value."""
        result = self.instant_query(promql)
        if not result:
            return default
        value = result[0].get("value")
        if not value or len(value) < 2:
            return default
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return default
