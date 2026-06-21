# Observability Incident-Response Agents (POC)

> SIP-1765 — Actor-model agent orchestration for Observability incident response (Pykka)

An exploratory POC of a distributed, actor-model agent system in Python using
[Pykka](https://pykka.readthedocs.io/). Agents monitor each Observability
signal in parallel, detect anomalies, and coordinate incident response via
message passing — **no shared mutable state, no locks**.

## Architecture

```
 Prometheus ──▶ MetricsAgent ─┐
                              │  MetricsAlert
 Loki ─────────▶ LogsAgent ───┼─────────────▶ CoordinatorAgent ──▶ (response agents)
                              │  LogsAlert         │  correlates signals,
 Tempo ───────▶ TracesAgent ──┘  TracesAlert       │  opens IncidentReports
                                                   └──▶ escalation / notify / remediate
```

Each agent is a Pykka `ThreadingActor` with its own inbox. The three monitoring
agents poll their backend on an interval (default 30s, via `POLL_INTERVAL_S`),
emit lightweight alert messages, and `tell()` them to the coordinator through
its `ActorRef`. The coordinator is the only actor holding incident state;
because Pykka serialises message handling per actor, that state needs no locks.

### Agents

| Agent | Backend | Emits |
|-------|---------|-------|
| `MetricsAgent` | Prometheus HTTP API | `MetricsAlert` (error rate, p99 latency) |
| `LogsAgent` | Loki LogQL | `LogsAlert` (error-level log spikes) |
| `TracesAgent` | Tempo TraceQL | `TracesAlert` (error / slow traces) |
| `CoordinatorAgent` | — | `IncidentReport` (correlated findings) |

### Messages

All messages are frozen dataclasses, JSON-serializable, and reference
observability data by id/timestamp/query — never full payloads. See
[`messages.py`](src/obs_agents/messages.py).

### Correlation (intentionally simple)

The coordinator keeps a rolling window (default 120s) of recent alerts.
Severity scales with how many independent signals agree:

- 1 signal → no incident (noise)
- 2 signals → `warning`
- 3 signals → `critical`

Detection thresholds, correlation rules, and queries are deliberately left open
for the agents to evolve — this is a spike.

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

The coordinator can fan out to response agents via `responders`. Planned:
escalation (ticket creation), notification (Slack), remediation (automated
actions).
