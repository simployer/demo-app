"""Coordinator AI triage acts on the LLM's structured decision (async)."""

import time

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.llm import Decision, DecisionAction, LLMClient, LLMError
from obs_agents.llm.base import build_decision
from obs_agents.messages import AgentAssessment, Severity


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


class _FakeLLM(LLMClient):
    """Returns a canned decision; records the context passed to decide()."""

    name = "fake"

    def __init__(self, decision: Decision, delay_s: float = 0.0):
        self._decision = decision
        self._delay_s = delay_s
        self.calls = []

    def decide(self, incident_context):
        self.calls.append(incident_context)
        if self._delay_s:
            time.sleep(self._delay_s)
        return self._decision


class _BrokenLLM(LLMClient):
    name = "broken"

    def decide(self, incident_context):
        raise LLMError("boom")


def _coordinator(llm, **kwargs):
    kwargs.setdefault("triage_executor", _InlineExecutor())
    return CoordinatorAgent.start(llm_client=llm, **kwargs)


def _assess(source, component=""):
    return AgentAssessment(
        source=source, anomalous=True, confidence=0.9,
        severity_hint=Severity.WARNING, summary=f"{source} anomaly",
        analysis="reasoned", component=component,
    )


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
    coordinator = _coordinator(llm)

    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["decision_source"] == "llm"
    assert inc["severity"] == "critical"
    assert inc["recommended_action"] == "escalate"
    assert inc["explanation"].startswith("Page the on-call")
    # The coordinator received the agents' assessments as correlation context.
    assert llm.calls and "assessments" in llm.calls[0]


def test_llm_decision_fans_out_on_escalate():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    coordinator = _coordinator(_FakeLLM(_critical()), responders=[responder])

    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)
    coordinator.ask({"query": "incidents"}, timeout=2)
    assert any("incident" in m for m in received)


def test_wait_decision_does_not_fan_out():
    received = []

    class Responder(pykka.ThreadingActor):
        def on_receive(self, message):
            received.append(message)

    responder = Responder.start()
    wait_decision = Decision(
        action=DecisionAction.WAIT, severity=Severity.INFO,
        summary="likely transient", analysis="single brief blip",
        explanation="Keep watching; no action needed.",
    )
    coordinator = _coordinator(_FakeLLM(wait_decision), responders=[responder])

    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)
    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["recommended_action"] == "wait"
    assert received == []


def test_falls_back_to_heuristic_on_llm_error():
    coordinator = _coordinator(_BrokenLLM())
    coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)
    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["decision_source"] == "heuristic"


def test_triage_is_non_blocking():
    """A slow coordinator LLM call must not stall the inbox."""
    llm = _FakeLLM(_critical(), delay_s=0.4)
    coordinator = CoordinatorAgent.start(llm_client=llm)  # real ThreadPoolExecutor
    try:
        coordinator.ask({"assessment": _assess("metrics", "http")}, timeout=2)
        coordinator.ask({"assessment": _assess("logs")}, timeout=2)

        early = coordinator.ask({"query": "incidents"}, timeout=1)
        assert len(early) == 1
        assert early[0]["decision_source"] == "heuristic"  # provisional

        deadline = time.time() + 2.0
        source = early[0]["decision_source"]
        while source != "llm" and time.time() < deadline:
            time.sleep(0.05)
            source = coordinator.ask({"query": "incidents"}, timeout=1)[0]["decision_source"]
        assert source == "llm"
    finally:
        pykka.ActorRegistry.stop_all()


def test_build_decision_rejects_malformed_payload():
    with pytest.raises(LLMError):
        build_decision({"action": "not_a_real_action", "severity": "critical",
                        "summary": "x", "analysis": "y", "explanation": "z"})
