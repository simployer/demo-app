"""Logs Agent — tails Loki for error patterns, emits LogsAlerts."""

from __future__ import annotations

import pykka

from ..clients import LokiClient
from ..config import Thresholds
from ..messages import Alert, LogsAlert
from .monitor import MonitorAgent

# Count error-level lines across all apps over the lookback window.
ERROR_COUNT_QUERY = 'count_over_time({level=~"error|critical"}[5m])'
# Pull a few representative lines for context.
ERROR_SAMPLE_QUERY = '{level=~"error|critical"}'


class LogsAgent(MonitorAgent):
    """Watches Loki for spikes in error-level log volume."""

    name = "logs"

    def __init__(
        self,
        coordinator: pykka.ActorRef,
        client: LokiClient,
        thresholds: Thresholds,
        poll_interval_s: float,
        llm_client=None,
        status_board=None,
    ):
        super().__init__(coordinator, poll_interval_s, llm_client, status_board)
        self._client = client
        self._thresholds = thresholds

    def poll(self) -> list[Alert]:
        # count_over_time returns a vector; sum the per-stream sample values.
        streams = self._client.query_range(ERROR_COUNT_QUERY, lookback_s=300.0)
        total = 0
        for stream in streams:
            for _ts, value in stream.get("values", []):
                try:
                    total += int(float(value))
                except (TypeError, ValueError):
                    continue

        if total <= self._thresholds.max_error_log_rate:
            return []

        samples = self._client.sample_lines(
            ERROR_SAMPLE_QUERY, lookback_s=300.0, max_lines=12
        )
        return [
            LogsAlert(
                error_pattern="level=~error|critical",
                match_count=total,
                sample_entries=tuple(samples),
                query=ERROR_SAMPLE_QUERY,
            )
        ]
