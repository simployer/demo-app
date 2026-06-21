"""Environment-driven configuration.

Every endpoint and threshold is overridable via env vars so the same image can
be swapped between environments (preview/staging/prod) without rebuild — as the
deployment notes in SIP-1765 require.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(frozen=True)
class EndpointConfig:
    """Base URL + optional bearer token for one observability backend."""

    base_url: str
    token: str | None = None
    timeout_s: float = 10.0


@dataclass(frozen=True)
class Thresholds:
    """Detection thresholds. Intentionally simple for the POC — the spike

    leaves real correlation/anomaly logic open for the agents to evolve.
    """

    # Metrics
    max_error_rate: float = 0.05  # 5% of requests
    max_p99_latency_ms: float = 750.0
    # Logs
    max_error_log_rate: float = 10.0  # matches per evaluation window
    # Traces
    max_trace_p99_ms: float = 1000.0
    max_error_traces: int = 5


@dataclass(frozen=True)
class LLMConfig:
    """AI-triage provider config.

    The Coordinator calls an LLM with correlated signals and acts on its
    decision (SIP-1765). Provider is pluggable: ``anthropic`` (default), or an
    OpenAI-compatible endpoint (``openai`` / ``azure-openai`` / ``lmstudio``).
    Set ``LLM_PROVIDER=none`` to disable and fall back to the heuristic.
    """

    provider: str = "anthropic"
    model: str = "claude-opus-4-8"  # coordinator (smart) model
    worker_model: str = "claude-haiku-4-5"  # monitoring agents (fast/cheap)
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 8192  # coordinator headroom for tool-use + thinking
    effort: str = "high"  # coordinator effort (anthropic): low|medium|high|xhigh|max
    worker_effort: str = "low"  # monitoring agents — fast/cheap
    timeout_s: float = 45.0  # agentic loop may run several round-trips
    max_investigation_steps: int = 4  # tool-use iterations the coordinator may take

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
        anthropic = provider == "anthropic"
        # Sensible default models per provider family and tier.
        default_model = "claude-opus-4-8" if anthropic else "gpt-4o"
        default_worker = "claude-haiku-4-5" if anthropic else "gpt-4o-mini"
        return cls(
            provider=provider,
            model=os.getenv("LLM_MODEL", default_model),
            worker_model=os.getenv("LLM_WORKER_MODEL", default_worker),
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            max_tokens=_env_int("LLM_MAX_TOKENS", 8192),
            effort=os.getenv("LLM_EFFORT", "high"),
            worker_effort=os.getenv("LLM_WORKER_EFFORT", "low"),
            timeout_s=_env_float("LLM_TIMEOUT_S", 45.0),
            max_investigation_steps=_env_int("LLM_MAX_INVESTIGATION_STEPS", 4),
        )


@dataclass(frozen=True)
class Config:
    prometheus: EndpointConfig
    loki: EndpointConfig
    tempo: EndpointConfig
    grafana: EndpointConfig | None

    poll_interval_s: float = 30.0
    thresholds: Thresholds = field(default_factory=Thresholds)
    llm: LLMConfig = field(default_factory=LLMConfig)

    health_host: str = "0.0.0.0"
    health_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        def endpoint(prefix: str, default_url: str) -> EndpointConfig:
            return EndpointConfig(
                base_url=os.getenv(f"{prefix}_URL", default_url).rstrip("/"),
                token=os.getenv(f"{prefix}_TOKEN"),
                timeout_s=_env_float(f"{prefix}_TIMEOUT_S", 10.0),
            )

        grafana_url = os.getenv("GRAFANA_URL")
        grafana = (
            EndpointConfig(
                base_url=grafana_url.rstrip("/"),
                token=os.getenv("GRAFANA_TOKEN"),
            )
            if grafana_url
            else None
        )

        return cls(
            prometheus=endpoint("PROMETHEUS", "http://localhost:9090"),
            loki=endpoint("LOKI", "http://localhost:3100"),
            tempo=endpoint("TEMPO", "http://localhost:3200"),
            grafana=grafana,
            poll_interval_s=_env_float("POLL_INTERVAL_S", 30.0),
            thresholds=Thresholds(
                max_error_rate=_env_float("THRESHOLD_ERROR_RATE", 0.05),
                max_p99_latency_ms=_env_float("THRESHOLD_P99_LATENCY_MS", 750.0),
                max_error_log_rate=_env_float("THRESHOLD_ERROR_LOG_RATE", 10.0),
                max_trace_p99_ms=_env_float("THRESHOLD_TRACE_P99_MS", 1000.0),
                max_error_traces=_env_int("THRESHOLD_ERROR_TRACES", 5),
            ),
            llm=LLMConfig.from_env(),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0"),
            health_port=_env_int("HEALTH_PORT", 8080),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
