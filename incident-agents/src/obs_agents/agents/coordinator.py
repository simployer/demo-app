"""Coordinator Agent — AI-driven correlation and incident triage.

The coordinator is the only actor that holds incident state, and because Pykka
serialises message handling per actor, that state needs no locks. It keeps a
short rolling window of recent alerts; when signals from multiple sources line
up inside the window it opens an incident and asks an LLM how to respond
(SIP-1765 — the AI decision-making is the core differentiator).

**The LLM call is non-blocking.** Triage runs on a worker thread; the incident
is opened immediately with a provisional count-based decision, and the model's
verdict is folded back in via a ``triage_result`` message. A slow LLM call
therefore delays the *upgrade* of a decision, never the processing of new
alerts. Triage is re-run only when a new signal type joins an incident, which
bounds LLM cost; a response cache (Redis) is the documented next step.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Protocol

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


class _Executor(Protocol):
    """Minimal executor surface the coordinator needs (lets tests inject one)."""

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any: ...
    def shutdown(self, wait: bool = ...) -> None: ...


class CoordinatorAgent(pykka.ThreadingActor):
    """Receives alerts, correlates them, and runs async AI-driven triage."""

    name = "coordinator"

    def __init__(
        self,
        correlation_window_s: float = 120.0,
        responders: list[pykka.ActorRef] | None = None,
        llm_client: LLMClient | None = None,
        triage_executor: _Executor | None = None,
    ):
        super().__init__()
        self._window_s = correlation_window_s
        self._responders = responders or []
        self._llm = llm_client
        self._recent: list[Alert] = []
        self._incidents: dict[str, IncidentReport] = {}
        #: signal-type set the current decision is based on, per incident
        self._incident_signals: dict[str, frozenset] = {}
        #: incidents with an in-flight async triage call
        self._pending_triage: set[str] = set()
        #: last fan-out action per incident — dedups repeated escalations
        self._fanned_out: dict[str, DecisionAction] = {}
        self._log = logging.getLogger("obs_agents.coordinator")

        if triage_executor is not None:
            self._executor: _Executor | None = triage_executor
        elif llm_client is not None:
            self._executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="triage"
            )
        else:
            self._executor = None

    def on_stop(self) -> None:
        if isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=False)

    # -- message handling ------------------------------------------------
    def on_receive(self, message: dict[str, Any]) -> Any:
        if "alert" in message:
            self._handle_alert(message["alert"])
            return None
        if "triage_result" in message:
            self._handle_triage_result(message["triage_result"])
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
            self._incidents[incident_id] = replace(
                self._incidents[incident_id],
                components=components,
                contributing_alerts=contributing,
                updated_at=time.time(),
            )
            # Re-triage only when a new signal type joins the correlation.
            if frozenset(distinct) != self._incident_signals.get(incident_id):
                self._retriage(incident_id, distinct)
            return

        # Open a new incident only once at least two signals correlate.
        if len(distinct) < 2:
            return
        self._open_incident(distinct, components, contributing)

    def _open_incident(
        self,
        distinct: set[type],
        components: tuple[str, ...],
        contributing: tuple[str, ...],
    ) -> None:
        incident_id = uuid.uuid4().hex[:12]
        provisional = self._heuristic_decision(distinct)
        report = _build_report(
            incident_id, provisional, "heuristic", components, contributing
        )
        self._incidents[incident_id] = report
        self._incident_signals[incident_id] = frozenset(distinct)

        if self._llm is not None:
            # Open provisionally now; the LLM upgrades the decision async.
            self._log.warning(
                "opened incident %s (provisional %s); awaiting LLM triage",
                incident_id, report.severity.value,
            )
            self._submit_triage(incident_id)
        else:
            self._log.warning(
                "opened incident %s (heuristic/%s) action=%s: %s",
                incident_id, report.severity.value,
                report.recommended_action, report.summary,
            )
            self._maybe_fan_out(report, provisional.action)

    def _retriage(self, incident_id: str, distinct: set[type]) -> None:
        self._incident_signals[incident_id] = frozenset(distinct)
        if self._llm is not None:
            self._submit_triage(incident_id)
            return
        # No LLM — recompute the heuristic synchronously.
        decision = self._heuristic_decision(distinct)
        existing = self._incidents[incident_id]
        report = _build_report(
            incident_id, decision, "heuristic",
            existing.components, existing.contributing_alerts,
            opened_at=existing.opened_at,
        )
        self._incidents[incident_id] = report
        self._maybe_fan_out(report, decision.action)

    # -- async triage ----------------------------------------------------
    def _submit_triage(self, incident_id: str) -> None:
        """Dispatch the LLM call to a worker thread (non-blocking)."""
        if self._llm is None or self._executor is None:
            return
        if incident_id in self._pending_triage:
            return
        self._pending_triage.add(incident_id)
        # Snapshot the context on the actor thread; the worker only reads it.
        context = self._build_context()
        self._executor.submit(self._run_triage, incident_id, context)

    def _run_triage(self, incident_id: str, context: dict[str, Any]) -> None:
        """Runs on a worker thread. Folds the result back as a message."""
        try:
            decision = self._llm.decide(context)  # type: ignore[union-attr]
            result: dict[str, Any] = {"incident_id": incident_id, "decision": decision}
        except LLMError as exc:
            result = {"incident_id": incident_id, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never lose the worker silently
            result = {"incident_id": incident_id, "error": repr(exc)}
        try:
            self.actor_ref.tell({"triage_result": result})
        except pykka.ActorDeadError:
            pass

    def _handle_triage_result(self, result: dict[str, Any]) -> None:
        incident_id = result["incident_id"]
        self._pending_triage.discard(incident_id)
        existing = self._incidents.get(incident_id)
        if existing is None:
            return  # incident gone (e.g. resolved) before triage returned

        if "error" in result:
            self._log.warning(
                "async LLM triage failed for %s (%s); keeping heuristic",
                incident_id, result["error"],
            )
            self._maybe_fan_out(existing, DecisionAction(existing.recommended_action))
            return

        decision: Decision = result["decision"]
        report = _build_report(
            incident_id, decision, "llm",
            existing.components, existing.contributing_alerts,
            opened_at=existing.opened_at,
        )
        self._incidents[incident_id] = report
        self._log.warning(
            "incident %s triaged by LLM: action=%s severity=%s",
            incident_id, decision.action.value, decision.severity.value,
        )
        self._maybe_fan_out(report, decision.action)

    # -- heuristic fallback ---------------------------------------------
    def _heuristic_decision(self, distinct: set[type]) -> Decision:
        """Count-based decision: provisional, and the fallback when no LLM."""
        names = ", ".join(sorted(t.__name__.replace("Alert", "").lower() for t in distinct))
        if len(distinct) >= 3:
            severity, action = Severity.CRITICAL, DecisionAction.ESCALATE
        elif len(distinct) == 2:
            severity, action = Severity.WARNING, DecisionAction.INVESTIGATE
        else:
            severity, action = Severity.INFO, DecisionAction.WAIT
        return Decision(
            action=action,
            severity=severity,
            summary=f"correlated anomalies across {names}",
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
        """Forward to response agents, deduping repeated identical actions."""
        if action not in _FANOUT_ACTIONS:
            return
        if self._fanned_out.get(report.incident_id) == action:
            return  # already escalated/remediated this incident with this action
        self._fanned_out[report.incident_id] = action
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
