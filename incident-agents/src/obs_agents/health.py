"""Tiny HTTP health server for Kubernetes liveness/readiness probes.

- ``/healthz`` (liveness): the process and coordinator actor are alive.
- ``/readyz``  (readiness): every monitoring agent has completed a poll.
- ``/incidents``: current open incidents (handy for eyeballing the POC).

Implemented on the stdlib ``http.server`` to avoid pulling a web framework
into a background sidecar concern.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pykka

_log = logging.getLogger("obs_agents.health")


class HealthState:
    """Shared, read-only-ish view the probes report on."""

    def __init__(self, monitors: list[pykka.ActorRef], coordinator: pykka.ActorRef):
        self._monitors = monitors
        self._coordinator = coordinator

    def is_live(self) -> bool:
        return self._coordinator.is_alive()

    def is_ready(self) -> bool:
        if not self._coordinator.is_alive():
            return False
        # Every monitor must be alive and have polled at least once.
        for ref in self._monitors:
            if not ref.is_alive():
                return False
            proxy = ref.proxy()
            try:
                if not proxy.healthy.get(timeout=2):
                    return False
            except pykka.Timeout:
                return False
        return True

    def incidents(self) -> list[dict]:
        try:
            return self._coordinator.ask({"query": "incidents"}, timeout=2) or []
        except pykka.Timeout:
            return []


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.path == "/healthz":
                ok = state.is_live()
                self._send(200 if ok else 503, {"status": "ok" if ok else "down"})
            elif self.path == "/readyz":
                ok = state.is_ready()
                self._send(200 if ok else 503, {"ready": ok})
            elif self.path == "/incidents":
                self._send(200, {"incidents": state.incidents()})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *_args) -> None:  # silence default stderr logging
            pass

    return Handler


def start_health_server(
    host: str,
    port: int,
    state: HealthState,
) -> Callable[[], None]:
    """Start the health server in a daemon thread; return a shutdown callable."""
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    _log.info("health server listening on %s:%s", host, port)

    def shutdown() -> None:
        server.shutdown()
        server.server_close()

    return shutdown
