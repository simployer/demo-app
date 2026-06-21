"""Monitoring agents forward alerts to the coordinator when thresholds breach."""

import pykka
import pytest

from obs_agents.agents import MetricsAgent
from obs_agents.config import Thresholds


class _RecordingCoordinator(pykka.ThreadingActor):
    def __init__(self):
        super().__init__()
        self.alerts = []

    def on_receive(self, message):
        if "alert" in message:
            self.alerts.append(message["alert"])
        if message.get("query") == "alerts":
            return list(self.alerts)
        return None


class _FakePrometheus:
    """Stands in for PrometheusClient.scalar()."""

    def __init__(self, error_rate, p99_ms):
        self._values = {"error_rate": error_rate, "p99": p99_ms}

    def scalar(self, promql, default=0.0):
        if "5.." in promql:
            return self._values["error_rate"]
        return self._values["p99"]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def test_metrics_agent_emits_on_breach():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.5, p99_ms=2000)
    agent = MetricsAgent.start(coordinator, client, Thresholds(), poll_interval_s=999)

    # poll fires on start; force one synchronously and wait for it to drain
    agent.ask({"poll": True}, timeout=2)
    alerts = coordinator.ask({"query": "alerts"}, timeout=2)

    names = {a.threshold_name for a in alerts}
    assert "error_rate" in names
    assert "p99_latency_ms" in names


def test_metrics_agent_silent_when_healthy():
    coordinator = _RecordingCoordinator.start()
    client = _FakePrometheus(error_rate=0.001, p99_ms=50)
    agent = MetricsAgent.start(coordinator, client, Thresholds(), poll_interval_s=999)

    agent.ask({"poll": True}, timeout=2)
    alerts = coordinator.ask({"query": "alerts"}, timeout=2)
    assert alerts == []
