"""Monitoring AI agents: cheap pre-filter gates LLM reasoning, then report up."""

import pykka
import pytest

from obs_agents.agents import MetricsAgent
from obs_agents.config import Thresholds
from obs_agents.llm import LLMClient


class _RecordingCoordinator(pykka.ThreadingActor):
    def __init__(self):
        super().__init__()
        self.assessments = []

    def on_receive(self, message):
        if "assessment" in message:
            self.assessments.append(message["assessment"])
        if message.get("query") == "assessments":
            return list(self.assessments)
        return None


class _FakePrometheus:
    def __init__(self, error_rate, p99_ms):
        self._values = {"error_rate": error_rate, "p99": p99_ms}

    def scalar(self, promql, default=0.0):
        return self._values["error_rate"] if "5.." in promql else self._values["p99"]


class _FakeWorkerLLM(LLMClient):
    """Worker-tier fake: returns a canned assessment payload via complete_json."""

    name = "fakeworker"
    model = "fake"

    def __init__(self, anomalous: bool, severity: str = "warning"):
        self._anomalous = anomalous
        self._severity = severity

    def complete_json(self, system, user, schema):
        return {
            "anomalous": self._anomalous,
            "confidence": 0.8,
            "severity_hint": self._severity,
            "component": "http",
            "summary": "assessed",
            "analysis": "reasoned",
        }


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def _poll(agent):
    agent.ask({"poll": True}, timeout=2)


def test_breach_reports_heuristic_assessment_without_llm():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.5, p99_ms=2000)
    agent = MetricsAgent.start(coordinator, client, Thresholds(), 999, None)

    _poll(agent)
    assessments = coordinator.ask({"query": "assessments"}, timeout=2)
    assert assessments, "a breach should report an assessment"
    a = assessments[0]
    assert a.source == "metrics"
    assert a.anomalous is True
    assert a.assessed_by == "heuristic"


def test_no_breach_reports_nothing():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.001, p99_ms=50)
    agent = MetricsAgent.start(coordinator, client, Thresholds(), 999, None)

    _poll(agent)
    assert coordinator.ask({"query": "assessments"}, timeout=2) == []


def test_agent_reports_llm_assessment_on_breach():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.5, p99_ms=2000)
    llm = _FakeWorkerLLM(anomalous=True, severity="critical")
    agent = MetricsAgent.start(coordinator, client, Thresholds(), 999, llm)

    _poll(agent)
    assessments = coordinator.ask({"query": "assessments"}, timeout=2)
    assert assessments
    a = assessments[0]
    assert a.assessed_by.startswith("llm:")
    assert a.severity_hint.value == "critical"


def test_agent_suppresses_its_own_false_positive():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.5, p99_ms=2000)  # threshold breach...
    llm = _FakeWorkerLLM(anomalous=False)  # ...but the agent judges it benign
    agent = MetricsAgent.start(coordinator, client, Thresholds(), 999, llm)

    _poll(agent)
    assert coordinator.ask({"query": "assessments"}, timeout=2) == []
