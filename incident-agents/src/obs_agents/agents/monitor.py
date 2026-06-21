"""Base class for the monitoring AI agents.

Each agent owns its inbox (Pykka gives every actor a private mailbox) and polls
its backend on an interval. The poll is a **cheap pre-filter**: a static
threshold flags *candidate* anomalies. Only then does the agent "reason" — it
asks its (cheap/fast) LLM to judge whether the candidate is a genuine problem
and produce a structured ``AgentAssessment``, which it reports up to the
Coordinator. ``anomalous=False`` lets the agent suppress its own false positive.

Gating the LLM behind the threshold keeps cost bounded: agents only think when
there is something potentially worth thinking about. A blocking LLM call here
only delays *this* agent's next poll — each monitoring agent is an independent
actor — so triage stays simple at the worker tier.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pykka
import requests

from ..llm import LLMClient, LLMError
from ..messages import AgentAssessment, Alert, Severity


class MonitorAgent(pykka.ThreadingActor):
    """A Pykka actor that detects candidates, reasons, and reports assessments."""

    #: human-readable name / signal source, set by subclasses
    name: str = "monitor"

    def __init__(
        self,
        coordinator: pykka.ActorRef,
        poll_interval_s: float,
        llm_client: LLMClient | None = None,
    ):
        super().__init__()
        self._coordinator = coordinator
        self._poll_interval_s = poll_interval_s
        self._llm = llm_client
        self._timer: threading.Timer | None = None
        self._log = logging.getLogger(f"obs_agents.{self.name}")
        #: set True only after at least one successful poll — feeds readiness
        self.healthy = False

    # -- Pykka lifecycle -------------------------------------------------
    def on_start(self) -> None:
        self._log.info(
            "%s agent starting (interval=%ss, reasoning=%s)",
            self.name, self._poll_interval_s, "llm" if self._llm else "heuristic",
        )
        self.actor_ref.tell({"poll": True})

    def on_stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def on_receive(self, message: dict[str, Any]) -> None:
        if message.get("poll"):
            self._safe_poll()
            self._schedule_next()

    # -- polling ---------------------------------------------------------
    def _schedule_next(self) -> None:
        self._timer = threading.Timer(
            self._poll_interval_s,
            lambda: self._tell_if_alive({"poll": True}),
        )
        self._timer.daemon = True
        self._timer.start()

    def _tell_if_alive(self, message: dict[str, Any]) -> None:
        try:
            self.actor_ref.tell(message)
        except pykka.ActorDeadError:
            pass

    def _safe_poll(self) -> None:
        try:
            candidates = self.poll()  # cheap threshold pre-filter
            self.healthy = True
        except requests.RequestException as exc:
            self.healthy = False
            self._log.warning("%s poll failed: %s", self.name, exc)
            return
        except Exception:  # noqa: BLE001 - POC: never let a poll kill the actor
            self.healthy = False
            self._log.exception("%s poll failed unexpectedly", self.name)
            return

        if not candidates:
            return
        assessment = self._assess(candidates)
        if assessment.anomalous:
            self._coordinator.tell({"assessment": assessment})
        else:
            self._log.info(
                "%s suppressed candidate (not anomalous): %s",
                self.name, assessment.summary,
            )

    # -- reasoning (gated) -----------------------------------------------
    def _assess(self, candidates: list[Alert]) -> AgentAssessment:
        component = _extract_component(candidates)
        evidence = _extract_evidence(candidates)
        if self._llm is not None:
            context = {
                "source": self.name,
                "candidate_signals": [a.to_dict() for a in candidates],
            }
            try:
                return self._llm.assess(
                    self.name, context, component=component, evidence=evidence
                )
            except LLMError as exc:
                self._log.warning(
                    "%s LLM assessment failed (%s); using heuristic", self.name, exc
                )
        return self._heuristic_assessment(candidates, component, evidence)

    def _heuristic_assessment(
        self, candidates: list[Alert], component: str, evidence: tuple[str, ...]
    ) -> AgentAssessment:
        """Fallback when no LLM is available: trust the threshold breach."""
        summary = f"{len(candidates)} threshold breach(es) on {self.name}"
        return AgentAssessment(
            source=self.name,
            anomalous=True,
            confidence=0.5,
            severity_hint=Severity.WARNING,
            summary=summary,
            analysis="Static threshold breached; no LLM available to qualify it.",
            component=component,
            evidence=evidence,
            assessed_by="heuristic",
        )

    # -- to be implemented by subclasses ---------------------------------
    def poll(self) -> list[Alert]:
        """Cheap pre-filter: query the backend, return candidate alerts. Override."""
        raise NotImplementedError


def _extract_component(candidates: list[Alert]) -> str:
    for alert in candidates:
        comp = getattr(alert, "component", "") or getattr(alert, "service", "")
        if comp:
            return comp
    return ""


def _extract_evidence(candidates: list[Alert], limit: int = 5) -> tuple[str, ...]:
    refs: list[str] = []
    for alert in candidates:
        for attr in ("sample_entries", "sample_trace_ids"):
            refs.extend(str(x) for x in getattr(alert, attr, ()) or ())
        name = getattr(alert, "threshold_name", "") or getattr(alert, "error_pattern", "")
        if name:
            refs.append(str(name))
    return tuple(refs[:limit])
