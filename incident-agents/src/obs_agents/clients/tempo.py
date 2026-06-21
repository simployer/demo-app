"""Tempo HTTP API client (trace search + metrics)."""

from __future__ import annotations

from .base import BackendClient


class TempoClient(BackendClient):
    """Wraps Tempo's TraceQL search endpoint.

    Tempo exposes ``/api/search`` for TraceQL queries; we only pull back trace
    ids + summary fields and never the full span payloads.
    """

    def search(self, traceql: str, limit: int = 20) -> list[dict]:
        body = self.get("/api/search", params={"q": traceql, "limit": limit})
        return body.get("traces", [])

    def trace_ids(self, traceql: str, limit: int = 20) -> list[str]:
        return [
            t["traceID"]
            for t in self.search(traceql, limit=limit)
            if t.get("traceID")
        ]
