"""Investigation tools the agentic coordinator can call mid-triage.

Instead of deciding from a fixed context, the coordinator LLM can pull more
evidence on demand — extra logs, slow traces, ad-hoc metrics — for the affected
service, then decide. Tools are **read-only** and results are size-capped so a
single investigation can't blow up the context window.
"""

from __future__ import annotations

from typing import Any

from ..clients import LokiClient, PrometheusClient, TempoClient

_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_service_logs",
        "description": "Fetch recent error-level log lines for a service from Loki.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Affected service/component."},
                "limit": {"type": "integer", "description": "Max lines (default 15)."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "get_service_traces",
        "description": "Fetch slow or errored trace summaries for a service from Tempo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "limit": {"type": "integer", "description": "Max traces (default 10)."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "run_promql",
        "description": "Run a read-only instant PromQL query against Prometheus for "
        "additional metric context (e.g. a per-service breakdown).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


class InvestigationTools:
    """Backs the coordinator's tool calls with the observability clients."""

    def __init__(
        self,
        prometheus: PrometheusClient,
        loki: LokiClient,
        tempo: TempoClient,
        max_chars: int = 2000,
    ):
        self._prom = prometheus
        self._loki = loki
        self._tempo = tempo
        self._max_chars = max_chars

    def schemas(self) -> list[dict[str, Any]]:
        return _TOOL_SCHEMAS

    def execute(self, name: str, tool_input: dict[str, Any]) -> str:
        try:
            result = self._dispatch(name, tool_input)
        except Exception as exc:  # noqa: BLE001 - return as tool error, don't crash triage
            return f"error running {name}: {exc}"
        return result[: self._max_chars] if result else "(no results)"

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "get_service_logs":
            service = str(args.get("service", ""))
            limit = int(args.get("limit", 15))
            logql = f'{{service="{service}"}} |~ "(?i)error|exception|critical"'
            lines = self._loki.sample_lines(logql, lookback_s=300.0, max_lines=limit)
            return "\n".join(lines) if lines else "(no matching log lines)"

        if name == "get_service_traces":
            service = str(args.get("service", ""))
            limit = int(args.get("limit", 10))
            traceql = f'{{ resource.service.name = "{service}" }}'
            traces = self._tempo.search(traceql, limit=limit)
            rows = [
                f"{t.get('traceID', '?')} dur={t.get('durationMs', '?')}ms "
                f"name={t.get('rootTraceName', '?')}"
                for t in traces
            ]
            return "\n".join(rows) if rows else "(no traces)"

        if name == "run_promql":
            query = str(args.get("query", ""))
            result = self._prom.instant_query(query)
            rows = [
                f"{r.get('metric', {})} = {r.get('value', ['', ''])[1]}"
                for r in result[:20]
            ]
            return "\n".join(rows) if rows else "(empty result)"

        return f"unknown tool: {name}"
