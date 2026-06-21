"""Coordinator AI agent — topological correlation of agent assessments.

The monitoring agents each reason over their own signal and report an
``AgentAssessment`` (verdict + reasoning + the entity it implicates). The
coordinator groups recent anomalous assessments **by entity** — the affected
service/component, with trace-id linking — rather than by mere co-occurrence in
a time window. An incident opens per entity once ≥2 distinct sources implicate
the *same* entity, so unrelated anomalies (latency on `checkout`, errors on
`search`) no longer merge into one incident, and same-service signals across
metrics/logs/traces do.

State is lock-free: Pykka serialises message handling per actor. The coordinator
LLM call is non-blocking (provisional heuristic now, LLM verdict folded back via
``triage_result``). Triage re-runs only when a new source joins an entity.
"""

from __future__ import annotations

import logging
import string
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Protocol

import pykka

from ..llm import Decision, DecisionAction, LLMClient, LLMError
from ..messages import AgentAssessment, IncidentReport, Severity
from ..status import StatusBoard

_FANOUT_ACTIONS = {DecisionAction.ESCALATE, DecisionAction.AUTO_REMEDIATE}
_UNSCOPED = "unknown"  # entity bucket for assessments with no identifiable component


class _Executor(Protocol):
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any: ...
    def shutdown(self, wait: bool = ...) -> None: ...


class CoordinatorAgent(pykka.ThreadingActor):
    """Correlates assessments by entity and runs async AI triage per incident."""

    name = "coordinator"

    def __init__(
        self,
        correlation_window_s: float = 120.0,
        responders: list[pykka.ActorRef] | None = None,
        llm_client: LLMClient | None = None,
        triage_executor: _Executor | None = None,
        status_board: StatusBoard | None = None,
        tools: Any = None,
    ):
        super().__init__()
        self._window_s = correlation_window_s
        self._responders = responders or []
        self._llm = llm_client
        self._tools = tools
        self._board = status_board
        self._recent: list[AgentAssessment] = []
        #: incidents keyed by entity (affected service/component)
        self._incidents: dict[str, IncidentReport] = {}
        self._incident_signals: dict[str, frozenset] = {}
        self._pending_triage: set[str] = set()
        self._fanned_out: dict[str, DecisionAction] = {}
        self._log = logging.getLogger("obs_agents.coordinator")

        if triage_executor is not None:
            self._executor: _Executor | None = triage_executor
        elif llm_client is not None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="triage")
        else:
            self._executor = None

    def on_start(self) -> None:
        if self._board is not None:
            self._board.register(self.actor_urn, "coordinator", "coordinator")
            self._board.set_state(self.actor_urn, "idle", "awaiting assessments")

    def on_stop(self) -> None:
        if isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=False)
        if self._board is not None:
            self._board.unregister(self.actor_urn)

    def _publish(self, state: str, detail: str = "") -> None:
        if self._board is not None:
            self._board.set_state(self.actor_urn, state, detail)
            self._board.set_counter(self.actor_urn, open_incidents=len(self._incidents))

    # -- message handling ------------------------------------------------
    def on_receive(self, message: dict[str, Any]) -> Any:
        if "assessment" in message:
            self._handle_assessment(message["assessment"])
            return None
        if "triage_result" in message:
            self._handle_triage_result(message["triage_result"])
            return None
        if message.get("query") == "incidents":
            return [inc.to_dict() for inc in self._incidents.values()]
        if message.get("query") == "open_incident_count":
            return len(self._incidents)
        return None

    def _handle_assessment(self, assessment: AgentAssessment) -> None:
        self._log.info(
            "coordinator received %s assessment (entity=%s, anomalous=%s)",
            assessment.source, assessment.component or _UNSCOPED, assessment.anomalous,
        )
        self._prune()
        self._recent.append(assessment)
        self._publish(
            "correlating", f"{assessment.source} → {assessment.component or _UNSCOPED}"
        )
        self._correlate()

    # -- topological grouping --------------------------------------------
    def _prune(self) -> None:
        cutoff = time.time() - self._window_s
        self._recent = [a for a in self._recent if a.timestamp >= cutoff]

    def _groups(self) -> dict[str, list[AgentAssessment]]:
        """Group recent anomalous assessments by the entity they implicate."""
        anomalous = [a for a in self._recent if a.anomalous]
        # Map trace ids → entity from assessments that carry both, so an
        # unscoped assessment sharing a trace can inherit the entity.
        trace_entity: dict[str, str] = {}
        for a in anomalous:
            if a.component:
                for tok in a.evidence:
                    if _is_trace_id(tok):
                        trace_entity.setdefault(tok, a.component)

        groups: dict[str, list[AgentAssessment]] = defaultdict(list)
        for a in anomalous:
            entity = a.component
            if not entity:
                entity = next(
                    (trace_entity[tok] for tok in a.evidence if tok in trace_entity),
                    _UNSCOPED,
                )
            groups[entity].append(a)
        return groups

    def _correlate(self) -> None:
        for entity, group in self._groups().items():
            sources = {a.source for a in group}
            if len(sources) < 2:
                continue  # not enough distinct sources implicating this entity
            contributing = tuple(sorted(sources))
            components = (entity,)

            if entity in self._incidents:
                self._incidents[entity] = replace(
                    self._incidents[entity],
                    components=components,
                    contributing_alerts=contributing,
                    updated_at=time.time(),
                )
                if frozenset(sources) != self._incident_signals.get(entity):
                    self._retriage(entity, sources)
            else:
                self._open_incident(entity, sources, components, contributing)

    def _open_incident(
        self, entity: str, sources: set[str],
        components: tuple[str, ...], contributing: tuple[str, ...],
    ) -> None:
        provisional = self._heuristic_decision(entity, sources)
        report = _build_report(entity, provisional, "heuristic", components, contributing)
        self._incidents[entity] = report
        self._incident_signals[entity] = frozenset(sources)

        self._publish("incident", f"opened {entity} ({report.severity.value})")
        if self._llm is not None:
            self._log.warning(
                "opened incident on %s (provisional %s); awaiting LLM triage",
                entity, report.severity.value,
            )
            self._submit_triage(entity)
        else:
            self._log.warning(
                "opened incident on %s (heuristic/%s) action=%s",
                entity, report.severity.value, report.recommended_action,
            )
            self._maybe_fan_out(report, provisional.action)

    def _retriage(self, entity: str, sources: set[str]) -> None:
        self._incident_signals[entity] = frozenset(sources)
        if self._llm is not None:
            self._submit_triage(entity)
            return
        decision = self._heuristic_decision(entity, sources)
        existing = self._incidents[entity]
        report = _build_report(
            entity, decision, "heuristic",
            existing.components, existing.contributing_alerts, opened_at=existing.opened_at,
        )
        self._incidents[entity] = report
        self._maybe_fan_out(report, decision.action)

    # -- async triage ----------------------------------------------------
    def _submit_triage(self, entity: str) -> None:
        if self._llm is None or self._executor is None:
            return
        if entity in self._pending_triage:
            return
        self._pending_triage.add(entity)
        context = self._context_for(entity)
        self._executor.submit(self._run_triage, entity, context)

    def _run_triage(self, entity: str, context: dict[str, Any]) -> None:
        try:
            if self._tools is not None:
                decision = self._llm.decide_with_tools(context, self._tools)  # type: ignore[union-attr]
            else:
                decision = self._llm.decide(context)  # type: ignore[union-attr]
            result: dict[str, Any] = {"entity": entity, "decision": decision}
        except LLMError as exc:
            result = {"entity": entity, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            result = {"entity": entity, "error": repr(exc)}
        try:
            self.actor_ref.tell({"triage_result": result})
        except pykka.ActorDeadError:
            pass

    def _handle_triage_result(self, result: dict[str, Any]) -> None:
        entity = result["entity"]
        self._pending_triage.discard(entity)
        existing = self._incidents.get(entity)
        if existing is None:
            return
        if "error" in result:
            self._log.warning(
                "async LLM triage failed for %s (%s); keeping heuristic",
                entity, result["error"],
            )
            self._maybe_fan_out(existing, DecisionAction(existing.recommended_action))
            return
        decision: Decision = result["decision"]
        report = _build_report(
            entity, decision, "llm",
            existing.components, existing.contributing_alerts, opened_at=existing.opened_at,
        )
        self._incidents[entity] = report
        self._log.warning(
            "incident on %s triaged by LLM: action=%s severity=%s",
            entity, decision.action.value, decision.severity.value,
        )
        self._publish("decided", f"{entity}: {decision.action.value} ({decision.severity.value})")
        self._maybe_fan_out(report, decision.action)

    # -- heuristic fallback ---------------------------------------------
    def _heuristic_decision(self, entity: str, sources: set[str]) -> Decision:
        names = ", ".join(sorted(sources))
        if len(sources) >= 3:
            severity, action = Severity.CRITICAL, DecisionAction.ESCALATE
        elif len(sources) == 2:
            severity, action = Severity.WARNING, DecisionAction.INVESTIGATE
        else:
            severity, action = Severity.INFO, DecisionAction.WAIT
        return Decision(
            action=action,
            severity=severity,
            summary=f"correlated anomalies on {entity} across {names}",
            analysis=f"{len(sources)} agents implicate {entity} in-window.",
            explanation=(
                f"Heuristic triage: {len(sources)} agents agree on {entity} → "
                f"{severity.value}. Recommended action: {action.value}."
            ),
        )

    def _context_for(self, entity: str) -> dict[str, Any]:
        group = self._groups().get(entity, [])
        return {
            "entity": entity,
            "assessments": [a.to_dict() for a in group],
            "signal_sources": sorted({a.source for a in group}),
            "correlation_window_seconds": self._window_s,
        }

    # -- fan-out ---------------------------------------------------------
    def _maybe_fan_out(self, report: IncidentReport, action: DecisionAction) -> None:
        if action not in _FANOUT_ACTIONS:
            return
        if self._fanned_out.get(report.incident_id) == action:
            return
        self._fanned_out[report.incident_id] = action
        for responder in self._responders:
            try:
                responder.tell({"incident": report})
            except pykka.ActorDeadError:
                self._log.warning("responder %s is dead", responder)


def _is_trace_id(token: str) -> bool:
    """Heuristic: a hex string ≥16 chars looks like a trace id."""
    return len(token) >= 16 and all(c in string.hexdigits for c in token)


def _build_report(
    entity: str,
    decision: Decision,
    source: str,
    components: tuple[str, ...],
    contributing: tuple[str, ...],
    *,
    opened_at: float | None = None,
) -> IncidentReport:
    now = time.time()
    return IncidentReport(
        incident_id=entity,
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
