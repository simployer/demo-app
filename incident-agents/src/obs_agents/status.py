"""A lock-free-ish status board for live visualization.

Each actor publishes its current activity here as it works; the dashboard reads
snapshots. Writes come from actor threads, reads from the health-server thread —
a single lock keeps it consistent without ever touching an actor's inbox, so a
busy agent (e.g. mid-LLM-call) never blocks the dashboard. Agents register on
start and unregister on stop, so newly spawned agents appear automatically and
stopped ones disappear.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any


class StatusBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, dict[str, Any]] = {}

    def register(self, agent_id: str, kind: str, label: str) -> None:
        now = time.time()
        with self._lock:
            self._agents[agent_id] = {
                "id": agent_id,
                "kind": kind,
                "label": label,
                "state": "starting",
                "detail": "",
                "counters": {},
                "since": now,
                "updated_at": now,
            }

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def set_state(self, agent_id: str, state: str, detail: str = "") -> None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return
            agent["state"] = state
            agent["detail"] = detail
            agent["updated_at"] = time.time()

    def set_counter(self, agent_id: str, **values: int) -> None:
        """Set absolute counter values (gauges), e.g. open_incidents."""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return
            agent["counters"].update(values)
            agent["updated_at"] = time.time()

    def bump(self, agent_id: str, **deltas: int) -> None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return
            counters = agent["counters"]
            for key, delta in deltas.items():
                counters[key] = counters.get(key, 0) + delta
            agent["updated_at"] = time.time()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            agents = [copy.deepcopy(a) for a in self._agents.values()]
        # Stable order: coordinator last, monitors alphabetical.
        agents.sort(key=lambda a: (a["kind"] == "coordinator", a["label"]))
        return agents
