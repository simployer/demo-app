"""Coordinator correlation over agent assessments (heuristic, no coordinator LLM)."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.messages import AgentAssessment, Severity


def _assess(source: str, component: str = "") -> AgentAssessment:
    return AgentAssessment(
        source=source,
        anomalous=True,
        confidence=0.9,
        severity_hint=Severity.WARNING,
        summary=f"{source} anomaly",
        analysis="reasoned",
        component=component,
    )


@pytest.fixture()
def coordinator():
    ref = CoordinatorAgent.start(correlation_window_s=120.0)
    yield ref
    pykka.ActorRegistry.stop_all()


def _open_count(ref) -> int:
    return ref.ask({"query": "open_incident_count"}, timeout=2)


def test_single_source_does_not_open_incident(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    assert _open_count(coordinator) == 0


def test_two_sources_open_warning_incident(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "warning"
    assert incidents[0]["decision_source"] == "heuristic"
    assert incidents[0]["recommended_action"] == "investigate"
    assert "http" in incidents[0]["components"]


def test_three_sources_escalate_to_critical(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)
    coordinator.ask({"assessment": _assess("traces", "api")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "critical"
    assert incidents[0]["recommended_action"] == "escalate"


def test_non_anomalous_assessment_is_ignored(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    suppressed = AgentAssessment(
        source="logs", anomalous=False, confidence=0.2,
        severity_hint=Severity.INFO, summary="benign", analysis="transient",
    )
    coordinator.ask({"assessment": suppressed}, timeout=2)
    # Only one anomalous source → no incident.
    assert _open_count(coordinator) == 0


def test_escalation_fans_out_to_responder():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = CoordinatorAgent.start(responders=[responder])
    try:
        coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
        coordinator.ask({"assessment": _assess("logs")}, timeout=2)
        coordinator.ask({"assessment": _assess("traces", "api")}, timeout=2)
        incidents = coordinator.ask({"query": "incidents"}, timeout=2)
        assert len(incidents) == 1
        assert any("incident" in m for m in received)
    finally:
        pykka.ActorRegistry.stop_all()
