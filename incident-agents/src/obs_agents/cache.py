"""Decision cache — skip the expensive agentic triage on recurring patterns.

The coordinator's triage (Opus at high effort + several tool round-trips) is the
priciest call in the system. A flaky service that fires the *same* correlation
shape over and over shouldn't re-pay it each time. This caches the resulting
``Decision`` keyed by the incident's stable shape (entity + signal sources +
severity hints), so an identical recurrence — including across process restarts
or pods — reuses the prior verdict.

Redis-backed, but entirely optional: with no ``REDIS_URL`` (or the ``redis``
package missing, or Redis unreachable) it degrades to a no-op and the coordinator
simply always calls the LLM. Cache failures never break triage.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .llm.base import Decision, build_decision

_log = logging.getLogger("obs_agents.cache")


def cache_key(context: dict[str, Any]) -> str:
    """Stable key from the incident's *shape*, ignoring volatile prose.

    Two recurrences of "service X, these sources, these severities" map to the
    same key even though the per-run summaries/analysis differ.
    """
    basis = {
        "entity": context.get("entity", ""),
        "sources": sorted(context.get("signal_sources", [])),
        "severities": sorted(
            a.get("severity_hint", "") for a in context.get("assessments", [])
        ),
    }
    raw = json.dumps(basis, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


class DecisionCache:
    """Interface: look up / store a coordinator decision by incident shape."""

    def get(self, context: dict[str, Any]) -> Decision | None:
        raise NotImplementedError

    def set(self, context: dict[str, Any], decision: Decision) -> None:
        raise NotImplementedError


class NoopCache(DecisionCache):
    """Caching disabled — always a miss."""

    def get(self, context: dict[str, Any]) -> Decision | None:
        return None

    def set(self, context: dict[str, Any], decision: Decision) -> None:
        return None


class RedisDecisionCache(DecisionCache):
    """Redis-backed cache. ``client`` is injected for testability."""

    def __init__(self, client: Any, ttl_s: float = 300.0, prefix: str = "obsagents:decision:"):
        self._r = client
        self._ttl = int(ttl_s)
        self._prefix = prefix

    def get(self, context: dict[str, Any]) -> Decision | None:
        key = self._prefix + cache_key(context)
        try:
            raw = self._r.get(key)
        except Exception as exc:  # noqa: BLE001 - cache down → treat as miss
            _log.warning("cache get failed (%s); skipping cache", exc)
            return None
        if not raw:
            return None
        try:
            return build_decision(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 - corrupt entry → miss
            _log.warning("cache decode failed (%s); ignoring entry", exc)
            return None

    def set(self, context: dict[str, Any], decision: Decision) -> None:
        key = self._prefix + cache_key(context)
        try:
            self._r.setex(key, self._ttl, json.dumps(decision.to_dict()))
        except Exception as exc:  # noqa: BLE001 - cache down → just skip
            _log.warning("cache set failed (%s); not cached", exc)


def build_decision_cache(config: Any) -> DecisionCache:
    """Build a Redis cache from config, or a no-op when unavailable."""
    url = getattr(config, "redis_url", None)
    if not url:
        return NoopCache()
    try:
        import redis  # noqa: PLC0415 - optional dependency
    except ImportError:
        _log.warning("REDIS_URL set but 'redis' not installed; decision cache disabled")
        return NoopCache()
    try:
        client = redis.Redis.from_url(url)
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not init redis (%s); decision cache disabled", exc)
        return NoopCache()
    _log.info("decision cache enabled (redis, ttl=%ss)", int(config.cache_ttl_s))
    return RedisDecisionCache(client, ttl_s=config.cache_ttl_s)
