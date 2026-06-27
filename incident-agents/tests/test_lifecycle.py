"""Incident lifecycle: auto-resolve when signals clear, history, fan-out."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.messages import AgentAssessment, Severity


def _assess(source, component="checkout"):
    return AgentAssessment(
        source=source, anomalous=True, confidence=0.9, component=component,
        severity_hint=Severity.WARNING, summary=f"{source} anomaly", analysis="r",
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def _open_incident(coordinator, component="checkout"):
    coordinator.ask({"assessment": _assess("metrics", component)}, timeout=2)
    coordinator.ask({"assessment": _assess("logs", component)}, timeout=2)


def test_incident_auto_resolves_when_signals_clear():
    # sweep_interval large so it only fires when we trigger it; resolve immediately.
    coordinator = CoordinatorAgent.start(resolve_after_s=0.0, sweep_interval_s=999)
    _open_incident(coordinator)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 1

    # No new signals arrive; a sweep finds the entity stale → resolves it.
    coordinator.ask({"sweep": True}, timeout=2)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 0

    history = coordinator.ask({"query": "incident_history"}, timeout=2)
    assert len(history) == 1
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"] is not None
    assert history[0]["incident_id"] == "checkout"


def test_fresh_signals_keep_incident_open():
    coordinator = CoordinatorAgent.start(resolve_after_s=120.0, sweep_interval_s=999)
    _open_incident(coordinator)
    # Signals are recent (just sent) → sweep must NOT resolve.
    coordinator.ask({"sweep": True}, timeout=2)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 1


def test_resolution_notifies_responders():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = CoordinatorAgent.start(
        responders=[responder], resolve_after_s=0.0, sweep_interval_s=999
    )
    _open_incident(coordinator)
    coordinator.ask({"sweep": True}, timeout=2)
    assert any("incident_resolved" in m for m in received)


def test_recurrence_opens_a_fresh_incident_after_resolution():
    coordinator = CoordinatorAgent.start(resolve_after_s=0.0, sweep_interval_s=999)
    _open_incident(coordinator)
    coordinator.ask({"sweep": True}, timeout=2)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 0

    # Same entity flares again → a new open incident.
    _open_incident(coordinator)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 1


def test_distinct_entities_resolve_independently():
    coordinator = CoordinatorAgent.start(resolve_after_s=120.0, sweep_interval_s=999)
    _open_incident(coordinator, "checkout")
    _open_incident(coordinator, "search")
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 2
    # Both fresh → none resolve.
    coordinator.ask({"sweep": True}, timeout=2)
    assert coordinator.ask({"query": "open_incident_count"}, timeout=2) == 2
