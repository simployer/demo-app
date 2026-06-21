# AI-Powered Observability Incident-Response Agents (POC)

> SIP-1765 — AI-powered actor-model agent orchestration for Observability incident response (Pykka + LLM)

An exploratory POC of a distributed, actor-model agent system in Python using
[Pykka](https://pykka.readthedocs.io/). Monitoring agents watch each
Observability signal in parallel and detect anomalies; the **Coordinator calls
an LLM** with the correlated signal context and acts on its structured decision
— **agents don't just alert, they reason and act**. No shared mutable state, no
locks.

## Architecture

```
 Prometheus ──▶ MetricsAgent ─┐
                              │  MetricsAlert        ┌──── LLM (Claude / OpenAI / LM Studio)
 Loki ─────────▶ LogsAgent ───┼──────────────▶ CoordinatorAgent ──▶ (response agents)
                              │  LogsAlert      │   correlate → ask LLM →
 Tempo ───────▶ TracesAgent ──┘  TracesAlert    │   decide (escalate / auto_remediate /
                                                │   wait / investigate) → IncidentReport
                                                └──▶ escalation / notify / remediate
```

Each agent is a Pykka `ThreadingActor` with its own inbox. The three monitoring
agents poll their backend on an interval (default 30s, via `POLL_INTERVAL_S`),
emit lightweight alert messages, and `tell()` them to the coordinator through
its `ActorRef`. The coordinator is the only actor holding incident state;
because Pykka serialises message handling per actor, that state needs no locks.

### AI-driven triage (the core differentiator)

When signals from ≥2 sources correlate inside the window, the Coordinator builds
a JSON context of the recent alerts and hands it to an LLM, which returns a
**structured decision**: an `action` (`escalate` / `auto_remediate` / `wait` /
`investigate`), a `severity`, and a human-readable `analysis` + `explanation`.
The decision is stored on the `IncidentReport` as an audit trail
(`decision_source: "llm"`), and the Coordinator fans out to response agents when
the action is `escalate` or `auto_remediate`.

The LLM provider is pluggable (`LLM_PROVIDER`):

| Provider | `LLM_PROVIDER` | Notes |
|----------|----------------|-------|
| Anthropic Claude (default) | `anthropic` | Official SDK, Claude Opus 4.8, adaptive thinking + structured outputs |
| OpenAI | `openai` | Set `LLM_BASE_URL=https://api.openai.com/v1` + `LLM_API_KEY` |
| Azure OpenAI | `azure-openai` | Set `LLM_BASE_URL` to your deployment endpoint |
| Local LM Studio | `lmstudio` | Set `LLM_BASE_URL=http://localhost:1234/v1` |
| Disabled | `none` | Falls back to the count-based heuristic |

If no LLM is configured, or a call fails/times out, the Coordinator falls back
to a simple count-based heuristic (2 signals → warning/investigate, 3 →
critical/escalate) so the system always produces a decision. The LLM call is
synchronous inside the actor loop and is only re-run when a *new* signal type
joins an incident — a response cache (Redis) is the documented next step if call
volume grows.

### Agents

| Agent | Backend | Emits |
|-------|---------|-------|
| `MetricsAgent` | Prometheus HTTP API | `MetricsAlert` (error rate, p99 latency) |
| `LogsAgent` | Loki LogQL | `LogsAlert` (error-level log spikes) |
| `TracesAgent` | Tempo TraceQL | `TracesAlert` (error / slow traces) |
| `CoordinatorAgent` | LLM | `IncidentReport` (LLM analysis, recommended action, severity, explanation) |

### Messages

All messages are frozen dataclasses, JSON-serializable, and reference
observability data by id/timestamp/query — never full payloads. `IncidentReport`
carries the LLM's `analysis`, `recommended_action`, `severity`, and plain-language
`explanation`. See [`messages.py`](src/obs_agents/messages.py) and the pluggable
LLM clients in [`llm/`](src/obs_agents/llm/).

Detection thresholds, correlation rules, queries, and the LLM prompt are
deliberately left open for the agents to evolve — this is a spike.

## Running locally

```bash
cd incident-agents
pip install -e ".[dev]"

# Point at your stack (defaults assume localhost)
export PROMETHEUS_URL=http://localhost:9090
export LOKI_URL=http://localhost:3100
export TEMPO_URL=http://localhost:3200

obs-agents
```

Health endpoints come up on `:8080`:

- `GET /healthz` — liveness (process + coordinator alive)
- `GET /readyz` — readiness (every monitor has polled successfully)
- `GET /incidents` — current open incidents

## Configuration

Everything is env-var driven so one image is swappable per environment.

| Var | Default | Purpose |
|-----|---------|---------|
| `PROMETHEUS_URL` / `LOKI_URL` / `TEMPO_URL` | `localhost` defaults | backend base URLs |
| `*_TOKEN` | — | bearer token (service account / AKS managed identity) |
| `GRAFANA_URL` / `GRAFANA_TOKEN` | — | optional, for dashboard/alert updates |
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `azure-openai` / `lmstudio` / `none` |
| `LLM_MODEL` | `claude-opus-4-8` | model id (provider-specific) |
| `LLM_API_KEY` | — | provider key (Anthropic also reads `ANTHROPIC_API_KEY`) |
| `LLM_BASE_URL` | — | required for OpenAI-compatible providers |
| `LLM_EFFORT` | `low` | Anthropic effort: `low`/`medium`/`high`/`xhigh`/`max` |
| `LLM_MAX_TOKENS` | `4096` | max output tokens for the triage call |
| `LLM_TIMEOUT_S` | `30` | per-call timeout |
| `POLL_INTERVAL_S` | `30` | monitoring poll cadence |
| `THRESHOLD_ERROR_RATE` | `0.05` | metrics: max error fraction |
| `THRESHOLD_P99_LATENCY_MS` | `750` | metrics: max p99 latency |
| `THRESHOLD_ERROR_LOG_RATE` | `10` | logs: max error lines / window |
| `THRESHOLD_TRACE_P99_MS` | `1000` | traces: slow-trace threshold |
| `THRESHOLD_ERROR_TRACES` | `5` | traces: max error traces |
| `HEALTH_PORT` | `8080` | probe server port |
| `LOG_LEVEL` | `INFO` | log level |

## Tests

```bash
cd incident-agents
pip install -e ".[dev]"
pytest
```

## Container & deployment

```bash
docker build -t demo-app/obs-agents incident-agents/
```

Deploy as a single Python service (Deployment; switch to StatefulSet if incident
state needs to survive restarts). Wire the Kubernetes probes to `/healthz` and
`/readyz` — see [`deploy/deployment.yaml`](deploy/deployment.yaml). Endpoints are
supplied via env vars per environment.

## Scaling path

Pykka handles in-process actor inboxes for single-machine concurrency. To
distribute agents across nodes, layer a broker (Redis/RabbitMQ) for cross-pod
messaging, or migrate to [Ray](https://www.ray.io/) for distributed actors.

## Future agents

The coordinator fans out to response agents via `responders` when the LLM
decides `escalate` or `auto_remediate`. Planned: escalation (ticket creation),
notification (Slack), remediation (automated actions) — each consuming the
`IncidentReport`'s `recommended_action` and `explanation`.
