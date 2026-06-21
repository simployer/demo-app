"""Metrics Agent — polls Prometheus, emits MetricsAlerts."""

from __future__ import annotations

import pykka

from ..clients import PrometheusClient
from ..config import Thresholds
from ..messages import Alert, MetricsAlert
from .monitor import MonitorAgent

# Default queries; overridable later as detection logic evolves.
ERROR_RATE_QUERY = (
    'sum(rate(http_requests_total{status=~"5.."}[5m])) '
    "/ sum(rate(http_requests_total[5m]))"
)
P99_LATENCY_QUERY = (
    "histogram_quantile(0.99, sum(rate("
    "http_request_duration_seconds_bucket[5m])) by (le)) * 1000"
)


class MetricsAgent(MonitorAgent):
    """Watches request latency and error rates in Prometheus."""

    name = "metrics"

    def __init__(
        self,
        coordinator: pykka.ActorRef,
        client: PrometheusClient,
        thresholds: Thresholds,
        poll_interval_s: float,
        llm_client=None,
        status_board=None,
    ):
        super().__init__(coordinator, poll_interval_s, llm_client, status_board)
        self._client = client
        self._thresholds = thresholds

    def poll(self) -> list[Alert]:
        alerts: list[Alert] = []

        error_rate = self._client.scalar(ERROR_RATE_QUERY)
        if error_rate > self._thresholds.max_error_rate:
            alerts.append(
                MetricsAlert(
                    threshold_name="error_rate",
                    current_value=error_rate,
                    threshold_value=self._thresholds.max_error_rate,
                    component="http",
                )
            )

        p99_ms = self._client.scalar(P99_LATENCY_QUERY)
        if p99_ms > self._thresholds.max_p99_latency_ms:
            alerts.append(
                MetricsAlert(
                    threshold_name="p99_latency_ms",
                    current_value=p99_ms,
                    threshold_value=self._thresholds.max_p99_latency_ms,
                    component="http",
                )
            )

        return alerts
