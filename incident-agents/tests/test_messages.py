"""Messages must stay lightweight and JSON-serializable."""

import json

from obs_agents.messages import (
    IncidentReport,
    LogsAlert,
    MetricsAlert,
    Severity,
    TracesAlert,
)


def test_metrics_alert_serializes():
    alert = MetricsAlert(
        threshold_name="error_rate",
        current_value=0.12,
        threshold_value=0.05,
        component="http",
    )
    payload = json.dumps(alert.to_dict())
    restored = json.loads(payload)
    assert restored["source"] == "metrics"
    assert restored["threshold_name"] == "error_rate"
    assert restored["timestamp"] > 0


def test_logs_and_traces_alerts_serialize():
    logs = LogsAlert(error_pattern="boom", match_count=42, sample_entries=("a", "b"))
    traces = TracesAlert(service="api", error_trace_count=7, sample_trace_ids=("t1",))
    assert json.loads(json.dumps(logs.to_dict()))["match_count"] == 42
    assert json.loads(json.dumps(traces.to_dict()))["service"] == "api"


def test_incident_report_serializes_with_enum():
    report = IncidentReport(
        incident_id="abc123",
        severity=Severity.CRITICAL,
        summary="things on fire",
        components=("http",),
        contributing_alerts=("MetricsAlert", "LogsAlert"),
    )
    restored = json.loads(json.dumps(report.to_dict()))
    assert restored["severity"] == "critical"
    assert restored["incident_id"] == "abc123"
