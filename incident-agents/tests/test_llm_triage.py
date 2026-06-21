"""AI-driven triage: the Coordinator acts on the LLM's structured decision."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.llm import Decision, DecisionAction, LLMClient, LLMError
from obs_agents.llm.base import build_decision
from obs_agents.messages import LogsAlert, MetricsAlert, Severity


class _FakeLLM(LLMClient):
    """Records the context it was given and returns a canned decision."""

    name = "fake"

    def __init__(self, decision: Decision):
        self._decision = decision
        self.calls = []

    def decide(self, incident_context):
        self.calls.append(incident_context)
        return self._decision


class _BrokenLLM(LLMClient):
    name = "broken"

    def decide(self, incident_context):
        raise LLMError("boom")


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def _critical():
    return Decision(
        action=DecisionAction.ESCALATE,
        severity=Severity.CRITICAL,
        summary="db saturation cascading to API errors",
        analysis="metrics error spike correlates with error logs",
        explanation="Page the on-call: error rate and logs both spiking.",
    )


def test_decision_drives_incident_fields():
    llm = _FakeLLM(_critical())
    coordinator = CoordinatorAgent.start(llm_client=llm)

    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="db timeout", match_count=50)}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["decision_source"] == "llm"
    assert inc["severity"] == "critical"
    assert inc["recommended_action"] == "escalate"
    assert inc["explanation"].startswith("Page the on-call")
    # The model received the correlated alert context.
    assert llm.calls and "alerts" in llm.calls[0]


def test_llm_decision_fans_out_on_escalate():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = CoordinatorAgent.start(llm_client=_FakeLLM(_critical()), responders=[responder])

    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="db timeout")}, timeout=2)
    coordinator.ask({"query": "incidents"}, timeout=2)
    assert any("incident" in m for m in received)


def test_wait_decision_does_not_fan_out():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    wait_decision = Decision(
        action=DecisionAction.WAIT,
        severity=Severity.INFO,
        summary="likely transient",
        analysis="single brief blip",
        explanation="Keep watching; no action needed.",
    )
    coordinator = CoordinatorAgent.start(llm_client=_FakeLLM(wait_decision), responders=[responder])

    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="blip")}, timeout=2)
    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["recommended_action"] == "wait"
    assert received == []


def test_falls_back_to_heuristic_on_llm_error():
    coordinator = CoordinatorAgent.start(llm_client=_BrokenLLM())
    coordinator.ask({"alert": MetricsAlert(threshold_name="error_rate", component="http")}, timeout=2)
    coordinator.ask({"alert": LogsAlert(error_pattern="boom")}, timeout=2)
    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["decision_source"] == "heuristic"


def test_build_decision_rejects_malformed_payload():
    with pytest.raises(LLMError):
        build_decision({"action": "not_a_real_action", "severity": "critical",
                        "summary": "x", "analysis": "y", "explanation": "z"})
