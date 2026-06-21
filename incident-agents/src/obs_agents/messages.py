"""Message types exchanged between agents.

Messages are deliberately lightweight and JSON-serializable. They reference
observability data by id/timestamp/query rather than carrying full payloads,
so the actor inboxes stay cheap and the messages can later cross a process or
pod boundary (e.g. via Redis) without change.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _now() -> float:
    """Unix timestamp (seconds) for when a message was created."""
    return time.time()


class Severity(str, Enum):
    """Incident severity, ordered low -> high."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """Common base for the signal alerts emitted by the monitoring agents."""

    source: str
    timestamp: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation."""
        return asdict(self)


@dataclass(frozen=True)
class MetricsAlert(Alert):
    """Anomaly detected in Prometheus metrics."""

    threshold_name: str = ""
    current_value: float = 0.0
    threshold_value: float = 0.0
    component: str = ""
    source: str = "metrics"


@dataclass(frozen=True)
class LogsAlert(Alert):
    """Error pattern detected in Loki logs.

    ``sample_entries`` carries only a few representative lines; the full set is
    referenced by the LogQL query + time window, never copied wholesale.
    """

    error_pattern: str = ""
    match_count: int = 0
    sample_entries: tuple[str, ...] = ()
    query: str = ""
    source: str = "logs"


@dataclass(frozen=True)
class TracesAlert(Alert):
    """Latency / error anomaly detected in Tempo traces."""

    service: str = ""
    p99_latency_ms: float = 0.0
    error_trace_count: int = 0
    sample_trace_ids: tuple[str, ...] = ()
    source: str = "traces"


@dataclass(frozen=True)
class IncidentReport:
    """Correlated incident produced by the Coordinator."""

    incident_id: str
    severity: Severity
    summary: str
    components: tuple[str, ...]
    contributing_alerts: tuple[str, ...]
    opened_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data
