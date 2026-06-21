"""Coordinator Agent — correlates signals and manages incident state.

The coordinator is the only actor that holds incident state, and because Pykka
serialises message handling per actor, that state needs no locks. It keeps a
short rolling window of recent alerts; when signals from multiple sources line
up inside the window it opens (or updates) an incident and can fan out to
response agents.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import pykka

from ..messages import (
    Alert,
    IncidentReport,
    LogsAlert,
    MetricsAlert,
    Severity,
    TracesAlert,
)


class CoordinatorAgent(pykka.ThreadingActor):
    """Receives alerts from the monitoring agents and correlates them."""

    name = "coordinator"

    def __init__(
        self,
        correlation_window_s: float = 120.0,
        responders: list[pykka.ActorRef] | None = None,
    ):
        super().__init__()
        self._window_s = correlation_window_s
        self._responders = responders or []
        self._recent: list[Alert] = []
        self._incidents: dict[str, IncidentReport] = {}
        self._log = logging.getLogger("obs_agents.coordinator")

    # -- message handling ------------------------------------------------
    def on_receive(self, message: dict[str, Any]) -> Any:
        if "alert" in message:
            self._handle_alert(message["alert"])
            return None
        # Introspection helpers (used by tests / health endpoint).
        if message.get("query") == "incidents":
            return [inc.to_dict() for inc in self._incidents.values()]
        if message.get("query") == "open_incident_count":
            return len(self._incidents)
        return None

    def _handle_alert(self, alert: Alert) -> None:
        self._log.info("coordinator received %s", type(alert).__name__)
        self._prune()
        self._recent.append(alert)
        self._correlate()

    # -- correlation -----------------------------------------------------
    def _prune(self) -> None:
        cutoff = time.time() - self._window_s
        self._recent = [a for a in self._recent if a.timestamp >= cutoff]

    def _correlate(self) -> None:
        sources = {type(a) for a in self._recent}
        distinct = {MetricsAlert, LogsAlert, TracesAlert} & sources

        if not distinct:
            return

        # Severity scales with how many independent signals agree.
        if len(distinct) >= 3:
            severity = Severity.CRITICAL
        elif len(distinct) == 2:
            severity = Severity.WARNING
        else:
            severity = Severity.INFO

        components = self._affected_components()
        summary = self._summarize(distinct)
        contributing = tuple(type(a).__name__ for a in self._recent)

        # One open incident per active correlation window; update in place.
        if self._incidents:
            incident_id = next(iter(self._incidents))
            existing = self._incidents[incident_id]
            self._incidents[incident_id] = IncidentReport(
                incident_id=incident_id,
                severity=severity,
                summary=summary,
                components=components,
                contributing_alerts=contributing,
                opened_at=existing.opened_at,
                updated_at=time.time(),
            )
            return

        # Only open an incident once at least two signals correlate.
        if len(distinct) < 2:
            return

        incident_id = uuid.uuid4().hex[:12]
        report = IncidentReport(
            incident_id=incident_id,
            severity=severity,
            summary=summary,
            components=components,
            contributing_alerts=contributing,
        )
        self._incidents[incident_id] = report
        self._log.warning("opened incident %s (%s): %s", incident_id, severity.value, summary)
        self._fan_out(report)

    def _affected_components(self) -> tuple[str, ...]:
        components: set[str] = set()
        for alert in self._recent:
            if isinstance(alert, MetricsAlert) and alert.component:
                components.add(alert.component)
            elif isinstance(alert, TracesAlert) and alert.service:
                components.add(alert.service)
        return tuple(sorted(components))

    def _summarize(self, distinct: set[type]) -> str:
        names = sorted(t.__name__.replace("Alert", "").lower() for t in distinct)
        return f"correlated anomalies across {', '.join(names)}"

    def _fan_out(self, report: IncidentReport) -> None:
        """Forward a new incident to any response agents (escalation, etc.)."""
        for responder in self._responders:
            try:
                responder.tell({"incident": report})
            except pykka.ActorDeadError:
                self._log.warning("responder %s is dead", responder)
