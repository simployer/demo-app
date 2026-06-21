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
from .clients import LokiClient, PrometheusClient, TempoClient
from .config import Config
from .health import HealthState, start_health_server
from .llm import LLMError, build_llm_client

_log = logging.getLogger("obs_agents.main")


def build_system(config: Config):
    """Construct the actor system. Returns (coordinator_ref, monitor_refs)."""
    try:
        llm_client = build_llm_client(config.llm)
    except LLMError as exc:
        _log.warning("LLM client unavailable (%s); using heuristic triage", exc)
        llm_client = None
    if llm_client is not None:
        _log.info("AI triage enabled via %s (%s)", llm_client.name, config.llm.model)
    else:
        _log.info("AI triage disabled; using count-based heuristic")

    coordinator = CoordinatorAgent.start(llm_client=llm_client)

    metrics = MetricsAgent.start(
        coordinator,
        PrometheusClient(config.prometheus),
        config.thresholds,
        config.poll_interval_s,
    )
    logs = LogsAgent.start(
        coordinator,
        LokiClient(config.loki),
        config.thresholds,
        config.poll_interval_s,
    )
    traces = TracesAgent.start(
        coordinator,
        TempoClient(config.tempo),
        config.thresholds,
        config.poll_interval_s,
    )
    return coordinator, [metrics, logs, traces]


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("obs_agents.main")

    coordinator, monitors = build_system(config)
    shutdown_health = start_health_server(
        config.health_host,
        config.health_port,
        HealthState(monitors, coordinator),
    )

    log.info("incident-response agent system started")

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
