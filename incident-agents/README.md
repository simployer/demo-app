# AI-Powered Observability Incident-Response Agents (POC)

> SIP-1765 — AI-powered actor-model agent orchestration for Observability incident response (Pykka + LLM)

An exploratory POC of a distributed, actor-model agent system in Python using
[Pykka](https://pykka.readthedocs.io/). **All four actors are AI agents.** Each
monitoring agent (metrics/logs/traces) reasons with an LLM over its own signal
and reports a structured assessment up to the **Coordinator AI agent**, which
correlates those assessments and decides the response — **agents don't just
alert, they reason and act**. No shared mutable state, no locks.

## Architecture

```
 Prometheus ─▶ Metrics AI agent ─┐  AgentAssessment   ┌─ Haiku (worker LLM)
 Loki ───────▶ Logs AI agent ────┼──────────────▶ Coordinator AI agent ─▶ responders
 Tempo ──────▶ Traces AI agent ──┘  (anomalous?       │   correlate assessments →
   each: cheap threshold gate →     severity, etc.)    │   Opus decides (escalate /
   LLM reasons → assessment                            │   auto_remediate / wait /
                                                       │   investigate) → IncidentReport
                                                       └─▶ escalation / notify / remediate
```

Each agent is a Pykka `ThreadingActor` with its own inbox. The coordinator is
the only actor holding incident state; because Pykka serialises message handling
per actor, that state needs no locks.

### The agents reason, not just threshold (the core differentiator)

**Monitoring agents (gated reasoning).** Each polls its backend on an interval
(default 30s). The poll is a *cheap pre-filter* — a static threshold flags a
candidate anomaly. Only then does the agent invoke its (fast/cheap) LLM to judge
whether the candidate is a *genuine* problem and produce a structured
`AgentAssessment` (`anomalous?`, confidence, severity hint, component, summary,
reasoning). `anomalous=False` lets an agent **suppress its own false positive**.
Gating the LLM behind the threshold keeps cost bounded — agents only think when
there's something worth thinking about.

**Coordinator AI agent — topological correlation.** Recent anomalous
assessments are grouped **by the entity they implicate** (affected
service/component, with trace-id linking), not by mere co-occurrence in a time
window. An incident opens **per entity** once ≥2 distinct sources implicate the
*same* one — so unrelated anomalies (latency on `checkout`, errors on `search`)
no longer merge into a single incident, and same-service signals across
metrics/logs/traces do. Each incident is keyed by its entity (multiple
concurrent incidents are supported). For each, the coordinator asks the (smart)
LLM for a **structured decision**: an `action` (`escalate` / `auto_remediate` /
`wait` / `investigate`), a `severity`, and a human-readable `analysis` +
`explanation`, stored on the `IncidentReport` (`decision_source: "llm"`). It fans
out to response agents on `escalate` / `auto_remediate`.

> Topological correlation is only as good as the entity labels the agents emit:
> two agents must name the same service consistently for their signals to
> correlate. Production-izing this means normalizing service names from backend
> labels; trace-id linking (already wired) is the stronger join when traces and
> logs share trace context.

**Agentic investigation.** The coordinator isn't a one-shot classifier — it's
given read-only tools (`get_service_logs`, `get_service_traces`, `run_promql`)
and runs a short tool-use loop: it pulls *more* evidence on demand for the
affected service before deciding, instead of guessing from the assessment
summaries. Tool results are size-capped, and the loop is bounded by
`LLM_MAX_INVESTIGATION_STEPS` (default 4). This runs on the non-blocking triage
worker thread, so the investigation never stalls the coordinator's inbox.

**Model + effort tiering.** Worker agents run on a fast/cheap model
(`claude-haiku-4-5`, effort `low`); the coordinator runs on `claude-opus-4-8` at
effort `high` for the harder, tool-using reasoning. Both tiers fall back to
heuristics if no LLM is configured or a call fails.

The LLM provider is pluggable (`LLM_PROVIDER`):

| Provider | `LLM_PROVIDER` | Notes |
|----------|----------------|-------|
| Anthropic Claude (default) | `anthropic` | Official SDK, Claude Opus 4.8, adaptive thinking + structured outputs |
| OpenAI | `openai` | Set `LLM_BASE_URL=https://api.openai.com/v1` + `LLM_API_KEY` |
| Azure OpenAI | `azure-openai` | Set `LLM_BASE_URL` to your deployment endpoint |
| Local LM Studio | `lmstudio` | Set `LLM_BASE_URL=http://localhost:1234/v1` |
| Disabled | `none` | Falls back to the count-based heuristic |

**The LLM call is non-blocking.** The incident opens immediately with a
provisional count-based decision; the triage call runs on a worker thread and
its verdict is folded back in via a `triage_result` message, so a slow model
call delays only the *upgrade* of a decision, never the processing of new
alerts. If no LLM is configured, or a call fails/times out, the provisional
heuristic (2 signals → warning/investigate, 3 → critical/escalate) stands, so
the system always produces a decision. Triage is re-run only when a *new* signal
type joins an incident — a response cache (Redis) is the documented next step if
call volume grows.

### Agents

| Agent | Backend | Reasons about | Emits |
|-------|---------|---------------|-------|
| `MetricsAgent` | Prometheus | is the error-rate/latency breach a real anomaly? | `AgentAssessment` |
| `LogsAgent` | Loki LogQL | do these error patterns indicate a real problem? | `AgentAssessment` |
| `TracesAgent` | Tempo TraceQL | is this latency/error pattern a real problem? | `AgentAssessment` |
| `CoordinatorAgent` | the 3 assessments | one incident? severity + response? | `IncidentReport` |

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

Then open the **live dashboard** at `http://localhost:8080/` — an
auto-refreshing view of every agent with a symbol for what it's doing
(🔍 polling, 🧠 reasoning, 🚨 reporting, 🤫 suppressed, 💤 idle; coordinator
🧩 correlating / 🔥 incident / ✅ decided) plus open incidents. It refreshes
every second and discovers agents dynamically, so newly spawned agents appear on
their own. Run with a short `POLL_INTERVAL_S` (e.g. `2`) to watch the activity
flow.

HTTP endpoints on `:8080`:

- `GET /` — live HTML dashboard
- `GET /status` — JSON snapshot of every agent + open incidents (drives the dashboard)
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
| `LLM_MODEL` | `claude-opus-4-8` | coordinator (smart) model |
| `LLM_WORKER_MODEL` | `claude-haiku-4-5` | monitoring-agent (fast/cheap) model |
| `LLM_API_KEY` | — | provider key (Anthropic also reads `ANTHROPIC_API_KEY`) |
| `LLM_BASE_URL` | — | required for OpenAI-compatible providers |
| `LLM_EFFORT` | `high` | coordinator effort: `low`/`medium`/`high`/`xhigh`/`max` |
| `LLM_WORKER_EFFORT` | `low` | monitoring-agent effort |
| `LLM_MAX_INVESTIGATION_STEPS` | `4` | coordinator tool-use iterations per incident |
| `LLM_MAX_TOKENS` | `8192` | coordinator max output tokens (tools + thinking) |
| `LLM_TIMEOUT_S` | `45` | per-call timeout (agentic loop may take several) |
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
