"""Base class for the periodic monitoring agents.

Each agent owns its inbox (Pykka gives every actor a private mailbox) and polls
its backend on an interval. Polls are driven by self-sent ``{"poll": True}``
messages so the work happens *inside* the actor loop — there is no shared state
and no locking. Detected anomalies are pushed to the coordinator via its
``ActorRef``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pykka
import requests

from ..messages import Alert


class MonitorAgent(pykka.ThreadingActor):
    """A Pykka actor that polls a backend and emits alerts to the coordinator."""

    #: human-readable name, set by subclasses
    name: str = "monitor"

    def __init__(self, coordinator: pykka.ActorRef, poll_interval_s: float):
        super().__init__()
        self._coordinator = coordinator
        self._poll_interval_s = poll_interval_s
        self._timer: threading.Timer | None = None
        self._log = logging.getLogger(f"obs_agents.{self.name}")
        #: set True only after at least one successful poll — feeds readiness
        self.healthy = False

    # -- Pykka lifecycle -------------------------------------------------
    def on_start(self) -> None:
        self._log.info("%s agent starting (interval=%ss)", self.name, self._poll_interval_s)
        # kick the first poll immediately; subsequent ones are self-scheduled
        self.actor_ref.tell({"poll": True})

    def on_stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def on_receive(self, message: dict[str, Any]) -> None:
        if message.get("poll"):
            self._safe_poll()
            self._schedule_next()

    # -- polling ---------------------------------------------------------
    def _schedule_next(self) -> None:
        self._timer = threading.Timer(
            self._poll_interval_s,
            lambda: self._tell_if_alive({"poll": True}),
        )
        self._timer.daemon = True
        self._timer.start()

    def _tell_if_alive(self, message: dict[str, Any]) -> None:
        # The timer may fire after the actor has been stopped.
        try:
            self.actor_ref.tell(message)
        except pykka.ActorDeadError:
            pass

    def _safe_poll(self) -> None:
        try:
            alerts = self.poll()
            self.healthy = True
            for alert in alerts:
                self._coordinator.tell({"alert": alert})
        except requests.RequestException as exc:
            # Backend unreachable / HTTP error — expected and transient; keep
            # it to a one-line warning rather than a full traceback per poll.
            self.healthy = False
            self._log.warning("%s poll failed: %s", self.name, exc)
        except Exception:  # noqa: BLE001 - POC: never let a poll kill the actor
            self.healthy = False
            self._log.exception("%s poll failed unexpectedly", self.name)

    # -- to be implemented by subclasses ---------------------------------
    def poll(self) -> list[Alert]:
        """Query the backend and return any alerts to forward. Override."""
        raise NotImplementedError
