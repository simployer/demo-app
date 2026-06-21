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
class AgentAssessment:
    """A monitoring AI agent's reasoned verdict on its own signal.

    Each worker agent (metrics/logs/traces) cheaply pre-filters for a candidate
    anomaly, then asks its LLM to judge whether it's a *genuine* problem and
    reports this assessment up to the Coordinator — which correlates the
    assessments rather than raw threshold alerts. ``anomalous=False`` lets an
    agent suppress its own false positive.
    """

    source: str
    anomalous: bool
    confidence: float
    severity_hint: Severity
    summary: str
    analysis: str
    component: str = ""
    evidence: tuple[str, ...] = ()
    assessed_by: str = "heuristic"  # "llm:<model>" | "heuristic"
    timestamp: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity_hint"] = self.severity_hint.value
        return data


@dataclass(frozen=True)
class IncidentReport:
    """Correlated incident produced by the Coordinator.

    Carries the LLM-generated analysis and recommended action when AI triage is
    enabled (SIP-1765); ``decision_source`` records whether the verdict came
    from the model or the fallback heuristic, for the audit trail.
    """

    incident_id: str
    severity: Severity
    summary: str
    components: tuple[str, ...]
    contributing_alerts: tuple[str, ...]
    recommended_action: str = "investigate"
    analysis: str = ""
    explanation: str = ""
    decision_source: str = "heuristic"  # "llm" | "heuristic"
    opened_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data
