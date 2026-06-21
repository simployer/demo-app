"""Traces Agent — monitors Tempo for slow/errored traces, emits TracesAlerts."""

from __future__ import annotations

import pykka

from ..clients import TempoClient
from ..config import Thresholds
from ..messages import Alert, TracesAlert
from .monitor import MonitorAgent

# TraceQL: traces with errors, and slow root spans.
ERROR_TRACES_QUERY = '{ status = error }'
SLOW_TRACES_QUERY = "{ duration > %dms }"


class TracesAgent(MonitorAgent):
    """Watches Tempo for error traces and latency spikes."""

    name = "traces"

    def __init__(
        self,
        coordinator: pykka.ActorRef,
        client: TempoClient,
        thresholds: Thresholds,
        poll_interval_s: float,
        llm_client=None,
    ):
        super().__init__(coordinator, poll_interval_s, llm_client)
        self._client = client
        self._thresholds = thresholds

    def poll(self) -> list[Alert]:
        alerts: list[Alert] = []

        error_ids = self._client.trace_ids(ERROR_TRACES_QUERY, limit=20)
        if len(error_ids) > self._thresholds.max_error_traces:
            alerts.append(
                TracesAlert(
                    service="unknown",
                    error_trace_count=len(error_ids),
                    sample_trace_ids=tuple(error_ids[:5]),
                )
            )

        slow_query = SLOW_TRACES_QUERY % int(self._thresholds.max_trace_p99_ms)
        slow_ids = self._client.trace_ids(slow_query, limit=20)
        if slow_ids:
            alerts.append(
                TracesAlert(
                    service="unknown",
                    p99_latency_ms=self._thresholds.max_trace_p99_ms,
                    sample_trace_ids=tuple(slow_ids[:5]),
                )
            )

        return alerts
