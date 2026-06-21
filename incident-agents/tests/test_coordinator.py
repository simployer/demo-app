"""Coordinator correlation behaviour."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.messages import LogsAlert, MetricsAlert, TracesAlert


@pytest.fixture()
def coordinator():
    ref = CoordinatorAgent.start(correlation_window_s=120.0)
    yield ref
    pykka.ActorRegistry.stop_all()


def _open_count(ref) -> int:
    return ref.ask({"query": "open_incident_count"}, timeout=2)


def test_single_signal_does_not_open_incident(coordinator):
    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    assert _open_count(coordinator) == 0


def test_two_signals_open_warning_incident(coordinator):
    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="boom", match_count=99)}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "warning"
    assert "http" in incidents[0]["components"]


def test_three_signals_escalate_to_critical(coordinator):
    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="boom", match_count=99)}, timeout=2)
    coordinator.ask({"alert": TracesAlert(service="api", error_trace_count=10)}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "critical"


def test_fan_out_to_responder():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = CoordinatorAgent.start(responders=[responder])
    try:
        coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
        coordinator.ask({"alert": LogsAlert(error_pattern="boom")}, timeout=2)
        # give the tell() time to land
        incidents = coordinator.ask({"query": "incidents"}, timeout=2)
        assert len(incidents) == 1
        assert any("incident" in m for m in received)
    finally:
        pykka.ActorRegistry.stop_all()
