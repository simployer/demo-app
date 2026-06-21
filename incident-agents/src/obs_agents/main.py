"""Entrypoint — wires up clients, actors, and the health server.

Single Python service: spins up the coordinator, the three monitoring agents,
and an HTTP health server, then blocks until a signal arrives and shuts down
the actor system cleanly.
"""

from __future__ import annotations

import logging
import signal
import threading

import pykka

from .agents import CoordinatorAgent, LogsAgent, MetricsAgent, TracesAgent
from .cache import build_decision_cache
from .clients import LokiClient, PrometheusClient, TempoClient
from .config import Config
from .health import HealthState, start_health_server
from .llm import (
    InvestigationTools,
    LLMError,
    build_llm_client,
    build_worker_llm_client,
)
from .status import StatusBoard

_log = logging.getLogger("obs_agents.main")


def build_system(config: Config, board: StatusBoard | None = None):
    """Construct the actor system. Returns (coordinator_ref, monitor_refs)."""
    try:
        coord_llm = build_llm_client(config.llm)
        worker_llm = build_worker_llm_client(config.llm)
    except LLMError as exc:
        _log.warning("LLM clients unavailable (%s); agents use heuristics", exc)
        coord_llm = worker_llm = None

    # The agentic coordinator investigates with its own clients (separate
    # sessions from the monitoring agents — they run on different threads).
    tools = None
    if coord_llm is not None:
        tools = InvestigationTools(
            PrometheusClient(config.prometheus),
            LokiClient(config.loki),
            TempoClient(config.tempo),
        )
        _log.info(
            "AI agents enabled — workers: %s (effort=%s), coordinator: %s "
            "(effort=%s, agentic tools=%d)",
            config.llm.worker_model, config.llm.worker_effort,
            config.llm.model, config.llm.effort, len(tools.schemas()),
        )
    else:
        _log.info("AI disabled; agents use threshold + count-based heuristics")

    coordinator = CoordinatorAgent.start(
        llm_client=coord_llm, status_board=board, tools=tools,
        decision_cache=build_decision_cache(config),
    )

    metrics = MetricsAgent.start(
        coordinator, PrometheusClient(config.prometheus),
        config.thresholds, config.poll_interval_s, worker_llm, board,
    )
    logs = LogsAgent.start(
        coordinator, LokiClient(config.loki),
        config.thresholds, config.poll_interval_s, worker_llm, board,
    )
    traces = TracesAgent.start(
        coordinator, TempoClient(config.tempo),
        config.thresholds, config.poll_interval_s, worker_llm, board,
    )
    return coordinator, [metrics, logs, traces]


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("obs_agents.main")

    board = StatusBoard()
    coordinator, monitors = build_system(config, board)
    shutdown_health = start_health_server(
        config.health_host,
        config.health_port,
        HealthState(monitors, coordinator, board),
    )

    log.info(
        "incident-response agent system started — dashboard at http://%s:%s/",
        config.health_host, config.health_port,
    )

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        stop_event.wait()
    finally:
        shutdown_health()
        pykka.ActorRegistry.stop_all()
        log.info("shutdown complete")


if __name__ == "__main__":
    main()
