"""Coordinator Agent — AI-driven correlation and incident triage.

The coordinator is the only actor that holds incident state, and because Pykka
serialises message handling per actor, that state needs no locks. It keeps a
short rolling window of recent alerts; when signals from multiple sources line
up inside the window it builds a correlated context, asks an LLM how to respond
(SIP-1765 — the AI decision-making is the core differentiator), and records the
model's analysis, severity, and recommended action on the incident. When no LLM
is configured (or a call fails) it falls back to a simple count-based heuristic.

The LLM call is synchronous: it runs inside the actor loop, so a slow call
delays — but never drops — queued alerts. Triage is only re-run when a *new*
signal type joins an existing incident, which bounds LLM cost; a response cache
(Redis) is the documented next step if call volume grows.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import pykka

from ..llm import Decision, DecisionAction, LLMClient, LLMError
from ..messages import (
    Alert,
    IncidentReport,
    LogsAlert,
    MetricsAlert,
    Severity,
    TracesAlert,
)

#: actions that warrant fanning out to response agents
_FANOUT_ACTIONS = {DecisionAction.ESCALATE, DecisionAction.AUTO_REMEDIATE}


class CoordinatorAgent(pykka.ThreadingActor):
    """Receives alerts, correlates them, and runs AI-driven triage."""

    name = "coordinator"

    def __init__(
        self,
        correlation_window_s: float = 120.0,
        responders: list[pykka.ActorRef] | None = None,
        llm_client: LLMClient | None = None,
    ):
        super().__init__()
        self._window_s = correlation_window_s
        self._responders = responders or []
        self._llm = llm_client
        self._recent: list[Alert] = []
        self._incidents: dict[str, IncidentReport] = {}
        #: signal-type set last triaged per incident — re-run only when it grows
        self._incident_signals: dict[str, frozenset] = {}
        self._log = logging.getLogger("obs_agents.coordinator")

    # -- message handling ------------------------------------------------
    def on_receive(self, message: dict[str, Any]) -> Any:
        if "alert" in message:
            self._handle_alert(message["alert"])
            return None
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

    def _signal_types(self) -> set[type]:
        return {MetricsAlert, LogsAlert, TracesAlert} & {type(a) for a in self._recent}

    def _correlate(self) -> None:
        distinct = self._signal_types()
        if not distinct:
            return

        components = self._affected_components()
        contributing = tuple(type(a).__name__ for a in self._recent)

        # Update an already-open incident.
        if self._incidents:
            incident_id = next(iter(self._incidents))
            existing = self._incidents[incident_id]
            # No new signal type — just refresh membership, skip a fresh triage.
            if frozenset(distinct) == self._incident_signals.get(incident_id):
                self._incidents[incident_id] = _with_membership(
                    existing, components, contributing
                )
                return
            decision, source = self._triage(distinct)
            self._incident_signals[incident_id] = frozenset(distinct)
            report = _build_report(
                incident_id, decision, source, components, contributing,
                opened_at=existing.opened_at,
            )
            self._incidents[incident_id] = report
            self._log.warning(
                "incident %s re-triaged (%s/%s): %s",
                incident_id, source, report.severity.value, report.recommended_action,
            )
            self._maybe_fan_out(report, decision.action)
            return

        # Open a new incident only once at least two signals correlate.
        if len(distinct) < 2:
            return

        incident_id = uuid.uuid4().hex[:12]
        decision, source = self._triage(distinct)
        self._incident_signals[incident_id] = frozenset(distinct)
        report = _build_report(
            incident_id, decision, source, components, contributing,
        )
        self._incidents[incident_id] = report
        self._log.warning(
            "opened incident %s (%s/%s) action=%s: %s",
            incident_id, source, report.severity.value,
            report.recommended_action, report.summary,
        )
        self._maybe_fan_out(report, decision.action)

    # -- triage ----------------------------------------------------------
    def _triage(self, distinct: set[type]) -> tuple[Decision, str]:
        """Get a triage decision from the LLM, falling back to a heuristic."""
        if self._llm is not None:
            context = self._build_context()
            try:
                decision = self._llm.decide(context)
                self._log.info(
                    "LLM triage via %s: action=%s severity=%s",
                    self._llm.name, decision.action.value, decision.severity.value,
                )
                return decision, "llm"
            except LLMError as exc:
                self._log.warning("LLM triage failed (%s); using heuristic", exc)
        return self._heuristic_decision(distinct), "heuristic"

    def _heuristic_decision(self, distinct: set[type]) -> Decision:
        """Count-based fallback when no LLM is available."""
        names = ", ".join(sorted(t.__name__.replace("Alert", "").lower() for t in distinct))
        if len(distinct) >= 3:
            severity, action = Severity.CRITICAL, DecisionAction.ESCALATE
        elif len(distinct) == 2:
            severity, action = Severity.WARNING, DecisionAction.INVESTIGATE
        else:
            severity, action = Severity.INFO, DecisionAction.WAIT
        summary = f"correlated anomalies across {names}"
        return Decision(
            action=action,
            severity=severity,
            summary=summary,
            analysis=f"{len(distinct)} independent signal type(s) correlated in-window.",
            explanation=(
                f"Heuristic triage: {len(distinct)} signals agree → {severity.value}. "
                f"Recommended action: {action.value}."
            ),
        )

    def _build_context(self) -> dict[str, Any]:
        return {
            "alerts": [a.to_dict() for a in self._recent],
            "affected_components": list(self._affected_components()),
            "signal_types": sorted(t.__name__ for t in self._signal_types()),
            "correlation_window_seconds": self._window_s,
        }

    def _affected_components(self) -> tuple[str, ...]:
        components: set[str] = set()
        for alert in self._recent:
            if isinstance(alert, MetricsAlert) and alert.component:
                components.add(alert.component)
            elif isinstance(alert, TracesAlert) and alert.service:
                components.add(alert.service)
        return tuple(sorted(components))

    # -- fan-out ---------------------------------------------------------
    def _maybe_fan_out(self, report: IncidentReport, action: DecisionAction) -> None:
        """Forward to response agents when the decision warrants action."""
        if action not in _FANOUT_ACTIONS:
            return
        for responder in self._responders:
            try:
                responder.tell({"incident": report})
            except pykka.ActorDeadError:
                self._log.warning("responder %s is dead", responder)


def _build_report(
    incident_id: str,
    decision: Decision,
    source: str,
    components: tuple[str, ...],
    contributing: tuple[str, ...],
    *,
    opened_at: float | None = None,
) -> IncidentReport:
    now = time.time()
    return IncidentReport(
        incident_id=incident_id,
        severity=decision.severity,
        summary=decision.summary,
        components=components,
        contributing_alerts=contributing,
        recommended_action=decision.action.value,
        analysis=decision.analysis,
        explanation=decision.explanation,
        decision_source=source,
        opened_at=opened_at if opened_at is not None else now,
        updated_at=now,
    )


def _with_membership(
    existing: IncidentReport,
    components: tuple[str, ...],
    contributing: tuple[str, ...],
) -> IncidentReport:
    """Refresh affected components / contributing alerts without re-triaging."""
    from dataclasses import replace

    return replace(
        existing,
        components=components,
        contributing_alerts=contributing,
        updated_at=time.time(),
    )
