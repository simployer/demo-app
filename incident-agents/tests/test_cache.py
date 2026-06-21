"""Decision cache: key stability, Redis round-trip, and coordinator reuse."""

import pykka
import pytest

from obs_agents.agents import CoordinatorAgent
from obs_agents.cache import (
    NoopCache,
    RedisDecisionCache,
    cache_key,
)
from obs_agents.llm import Decision, DecisionAction, LLMClient
from obs_agents.messages import AgentAssessment, Severity


def _ctx(entity="checkout", sources=("metrics", "logs"), sevs=("warning", "warning")):
    return {
        "entity": entity,
        "signal_sources": list(sources),
        "assessments": [{"severity_hint": s} for s in sevs],
    }


def _decision():
    return Decision(
        action=DecisionAction.ESCALATE, severity=Severity.CRITICAL,
        summary="s", analysis="a", explanation="e",
    )


def test_cache_key_is_stable_for_same_shape():
    # Same entity/sources/severities → same key, regardless of order or prose.
    a = cache_key(_ctx(sources=("metrics", "logs")))
    b = cache_key(_ctx(sources=("logs", "metrics")))
    assert a == b


def test_cache_key_differs_by_shape():
    assert cache_key(_ctx(entity="checkout")) != cache_key(_ctx(entity="search"))
    assert cache_key(_ctx(sources=("metrics", "logs"))) != cache_key(
        _ctx(sources=("metrics", "traces"))
    )


def test_noop_cache_always_misses():
    c = NoopCache()
    c.set(_ctx(), _decision())
    assert c.get(_ctx()) is None


class _FakeRedis:
    """Minimal get/setex backing store."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


def test_redis_cache_roundtrip():
    cache = RedisDecisionCache(_FakeRedis(), ttl_s=60)
    assert cache.get(_ctx()) is None
    cache.set(_ctx(), _decision())
    got = cache.get(_ctx())
    assert got is not None
    assert got.action is DecisionAction.ESCALATE
    assert got.severity is Severity.CRITICAL


class _FlakyRedis:
    def get(self, key):
        raise ConnectionError("redis down")

    def setex(self, key, ttl, value):
        raise ConnectionError("redis down")


def test_redis_cache_survives_backend_failure():
    cache = RedisDecisionCache(_FlakyRedis())
    # A down cache is a miss and a silent set — never an exception.
    assert cache.get(_ctx()) is None
    cache.set(_ctx(), _decision())  # must not raise


# -- coordinator integration --------------------------------------------------
class _RecordingLLM(LLMClient):
    name = "rec"

    def __init__(self, decision):
        self._decision = decision
        self.decide_calls = 0

    def decide(self, context):
        self.decide_calls += 1
        return self._decision


class _Inline:
    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, wait=True):
        pass


class _HitCache:
    def __init__(self, decision):
        self._d = decision
        self.sets = []

    def get(self, context):
        return self._d

    def set(self, context, decision):
        self.sets.append(decision)


class _MissCache:
    def __init__(self):
        self.sets = []

    def get(self, context):
        return None

    def set(self, context, decision):
        self.sets.append(decision)


def _assess(source):
    return AgentAssessment(
        source=source, anomalous=True, confidence=0.9, component="checkout",
        severity_hint=Severity.WARNING, summary=f"{source} anomaly", analysis="r",
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    pykka.ActorRegistry.stop_all()


def test_coordinator_reuses_cached_decision_without_calling_llm():
    llm = _RecordingLLM(_decision())
    coordinator = CoordinatorAgent.start(
        llm_client=llm, triage_executor=_Inline(), decision_cache=_HitCache(_decision())
    )
    coordinator.ask({"assessment": _assess("metrics")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["decision_source"] == "cache"
    assert llm.decide_calls == 0  # cache hit — LLM never invoked


def test_coordinator_stores_fresh_decision_on_miss():
    llm = _RecordingLLM(_decision())
    cache = _MissCache()
    coordinator = CoordinatorAgent.start(
        llm_client=llm, triage_executor=_Inline(), decision_cache=cache
    )
    coordinator.ask({"assessment": _assess("metrics")}, timeout=2)
    coordinator.ask({"assessment": _assess("logs")}, timeout=2)

    incidents = coordinator.ask({"query": "incidents"}, timeout=2)
    assert incidents[0]["decision_source"] == "llm"
    assert llm.decide_calls == 1
    assert len(cache.sets) == 1  # fresh decision written to cache
