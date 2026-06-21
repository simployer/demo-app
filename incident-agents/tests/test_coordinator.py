"""Coordinator correlation over agent assessments (heuristic, no coordinator LLM)."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.messages import AgentAssessment, Severity


def _assess(source: str, component: str = "checkout") -> AgentAssessment:
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
    coordinator.ask({"assessment": _assess("metrics")}, timeout=2)
    assert _open_count(coordinator) == 0


def test_two_sources_same_entity_open_warning_incident(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs", "checkout")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "warning"
    assert incidents[0]["decision_source"] == "heuristic"
    assert incidents[0]["recommended_action"] == "investigate"
    assert tuple(incidents[0]["components"]) == ("checkout",)


def test_two_sources_different_entities_do_not_correlate(coordinator):
    # Topological separation: same window, different services → NOT one incident.
    coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs", "search")}, timeout=2)
    assert _open_count(coordinator) == 0


def test_concurrent_incidents_per_entity(coordinator):
    # Two distinct services each correlated by 2 sources → two incidents.
    coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("metrics", "search")}, timeout=2)
    coordinator.ask({"assessment": _assess("traces", "search")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    entities = sorted(i["incident_id"] for i in incidents)
    assert entities == ["checkout", "search"]


def test_three_sources_same_entity_escalate_to_critical(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs", "checkout")}, timeout=2)
    coordinator.ask({"assessment": _assess("traces", "checkout")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "critical"
    assert incidents[0]["recommended_action"] == "escalate"


def test_non_anomalous_assessment_is_ignored(coordinator):
    coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
    suppressed = AgentAssessment(
        source="logs", anomalous=False, confidence=0.2, component="checkout",
        severity_hint=Severity.INFO, summary="benign", analysis="transient",
    )
    coordinator.ask({"assessment": suppressed}, timeout=2)
    # Only one anomalous source → no incident.
    assert _open_count(coordinator) == 0


def test_trace_id_links_unscoped_assessment(coordinator):
    # A logs assessment with no component but a shared trace id inherits the
    # entity of the traces assessment that carries that trace id.
    trace = "abc1234567890def"
    coordinator.ask(
        {"assessment": AgentAssessment(
            source="traces", anomalous=True, confidence=0.9, component="payments",
            severity_hint=Severity.WARNING, summary="slow", analysis="r",
            evidence=(trace,))},
        timeout=2,
    )
    coordinator.ask(
        {"assessment": AgentAssessment(
            source="logs", anomalous=True, confidence=0.9, component="",
            severity_hint=Severity.WARNING, summary="errors", analysis="r",
            evidence=(trace,))},
        timeout=2,
    )
    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["incident_id"] == "payments"


def test_escalation_fans_out_to_responder():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = CoordinatorAgent.start(responders=[responder])
    try:
        coordinator.ask({"assessment": _assess("metrics", "checkout")}, timeout=2)
        coordinator.ask({"assessment": _assess("logs", "checkout")}, timeout=2)
        coordinator.ask({"assessment": _assess("traces", "checkout")}, timeout=2)
        incidents = coordinator.ask({"query": "incidents"}, timeout=2)
        assert len(incidents) == 1
        assert any("incident" in m for m in received)
    finally:
        pykka.ActorRegistry.stop_all()
