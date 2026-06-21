"""Status board + agents publishing their activity for the live dashboard."""

import pykka
import pytest

from obs_agents.agents import MetricsAgent
from obs_agents.config import Thresholds
from obs_agents.status import StatusBoard


def test_status_board_lifecycle():
    board = StatusBoard()
    board.register("a1", "metrics", "metrics")
    board.set_state("a1", "polling", "querying")
    board.bump("a1", polls=1)
    board.bump("a1", polls=2, reported=1)
    board.set_counter("a1", open_incidents=3)

    snap = board.snapshot()
    assert len(snap) == 1
    a = snap[0]
    assert a["kind"] == "metrics" and a["state"] == "polling"
    assert a["counters"] == {"polls": 3, "reported": 1, "open_incidents": 3}

    board.unregister("a1")
    assert board.snapshot() == []


def test_unknown_agent_updates_are_noops():
    board = StatusBoard()
    board.set_state("ghost", "polling")  # not registered
    board.bump("ghost", polls=1)
    assert board.snapshot() == []


class _Sink(pykka.ThreadingActor):
    def on_receive(self, message):
        return None


class _FakePrometheus:
    def scalar(self, promql, default=0.0):
        return 0.5 if "5.." in promql else 2000  # both breach


def test_agent_publishes_activity_to_board():
    board = StatusBoard()
    coordinator = _Sink.start()
    agent = MetricsAgent.start(
        coordinator, _FakePrometheus(), Thresholds(), 999, None, board
    )
    try:
        agent.ask({"poll": True}, timeout=2)
        snap = {a["label"]: a for a in board.snapshot()}
        assert "metrics" in snap
        assert snap["metrics"]["state"] == "reporting"
        assert snap["metrics"]["counters"].get("reported", 0) >= 1
    finally:
        pykka.ActorRegistry.stop_all()


def test_stopped_agent_leaves_the_board():
    board = StatusBoard()
    coordinator = _Sink.start()
    agent = MetricsAgent.start(
        coordinator, _FakePrometheus(), Thresholds(), 999, None, board
    )
    agent.ask({"poll": True}, timeout=2)
    assert board.snapshot()  # present while running
    agent.stop()
    assert board.snapshot() == []  # gone after stop
    pykka.ActorRegistry.stop_all()
