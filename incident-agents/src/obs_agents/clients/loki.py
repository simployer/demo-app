"""Loki HTTP API client (LogQL queries)."""

from __future__ import annotations

import time

from .base import BackendClient


class LokiClient(BackendClient):
    """Wraps the Loki ``/loki/api/v1/query_range`` endpoint."""

    def query_range(self, logql: str, lookback_s: float = 300.0, limit: int = 100) -> list[dict]:
        """Run a LogQL range query over the last ``lookback_s`` seconds.

        Returns the raw stream results; each stream has ``stream`` labels and
        a list of ``values`` (``[ts_ns, line]`` pairs).
        """
        now_ns = int(time.time() * 1e9)
        start_ns = int(now_ns - lookback_s * 1e9)
        body = self.get(
            "/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": start_ns,
                "end": now_ns,
                "limit": limit,
                "direction": "backward",
            },
        )
        if body.get("status") != "success":
            raise RuntimeError(f"Loki query failed: {body.get('error')}")
        return body.get("data", {}).get("result", [])

    def sample_lines(self, logql: str, lookback_s: float = 300.0, max_lines: int = 5) -> list[str]:
        """Flatten range-query streams into a few representative log lines."""
        streams = self.query_range(logql, lookback_s=lookback_s, limit=max_lines)
        lines: list[str] = []
        for stream in streams:
            for _ts, line in stream.get("values", []):
                lines.append(line)
                if len(lines) >= max_lines:
                    return lines
        return lines
