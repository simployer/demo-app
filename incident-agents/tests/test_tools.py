"""Agentic coordinator: investigation tools + the tool-use decision path."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.llm import InvestigationTools, LLMClient
from obs_agents.messages import AgentAssessment, Severity


class _FakeProm:
    def __init__(self, raise_=False):
        self._raise = raise_

    def instant_query(self, query):
        if self._raise:
            raise RuntimeError("prometheus down")
        return [{"metric": {"service": "checkout"}, "value": [0, "1"]}]


class _FakeLoki:
    def sample_lines(self, logql, lookback_s=300.0, max_lines=15):
        return ["ERROR boom at /checkout", "ERROR db timeout"][:max_lines]


class _FakeTempo:
    def search(self, traceql, limit=10):
        return [{"traceID": "abc123", "durationMs": 1200, "rootTraceName": "GET /checkout"}]


def _tools(raise_=False):
    return InvestigationTools(_FakeProm(raise_), _FakeLoki(), _FakeTempo())


def test_investigation_tools_execute():
    t = _tools()
    assert "boom" in t.execute("get_service_logs", {"service": "checkout"})
    assert "abc123" in t.execute("get_service_traces", {"service": "checkout"})
    assert "1" in t.execute("run_promql", {"query": "up"})
    assert t.execute("nope", {}).startswith("unknown tool")


def test_investigation_tool_errors_are_caught():
    # A backend failure becomes a tool error string, not a crashed triage.
    out = _tools(raise_=True).execute("run_promql", {"query": "x"})
    assert out.startswith("error running run_promql")


def test_base_tool_loop_falls_back_to_one_shot():
    class OneShot(LLMClient):
        def complete_json(self, system, user, schema):
            return {"ok": 1}

    c = OneShot()
    # No tool support → ignores tools, single completion.
    assert c.run_tool_loop("s", "u", {}, [{"name": "t"}], lambda n, i: "r") == {"ok": 1}


class _ToolUsingLLM(LLMClient):
    """Simulates a model that calls one tool, then answers."""

    name = "toolfake"

    def __init__(self, payload):
        self._payload = payload
        self.tools_called = []

    def run_tool_loop(self, system, user, schema, tools, execute, max_steps=4):
        out = execute(tools[0]["name"], {"service": "checkout"})
        self.tools_called.append((tools[0]["name"], out))
        return self._payload


class _Inline:
    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, wait=True):
        pass


def _assess(source):
    return AgentAssessment(
        source=source, anomalous=True, confidence=0.9, component="checkout",
        severity_hint=Severity.WARNING, summary=f"{source} anomaly", analysis="r",
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def test_coordinator_investigates_with_tools():
    payload = {
        "action": "escalate", "severity": "critical",
        "summary": "checkout failing", "analysis": "logs confirm db timeouts",
        "explanation": "Page on-call.",
    }
    llm = _ToolUsingLLM(payload)
    coordinator = CoordinatorAgent.start(
        llm_client=llm, triage_executor=_Inline(), tools=_tools()
    )
    coordinator.ask({"assessment": _assess("metrics")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["decision_source"] == "llm"
    assert incidents[0]["recommended_action"] == "escalate"
    # The coordinator actually used a tool to investigate before deciding.
    assert llm.tools_called and llm.tools_called[0][0] == "get_service_logs"
