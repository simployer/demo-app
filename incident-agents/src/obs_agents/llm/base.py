"""LLM client interface and the structured decision it returns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..messages import Severity


class DecisionAction(str, Enum):
    """What the model recommends doing about a correlated incident."""

    ESCALATE = "escalate"
    AUTO_REMEDIATE = "auto_remediate"
    WAIT = "wait"
    INVESTIGATE = "investigate"


@dataclass(frozen=True)
class Decision:
    """Structured triage decision produced by an LLM.

    This is the audit record stored on the incident: what the model decided,
    why, and a human-readable explanation. Kept JSON-serializable.
    """

    action: DecisionAction
    severity: Severity
    summary: str
    analysis: str
    explanation: str
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "analysis": self.analysis,
            "explanation": self.explanation,
        }


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns an unusable response."""


# The JSON shape every provider is asked to return. Shared so the Anthropic and
# OpenAI-compatible clients stay in lockstep.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [a.value for a in DecisionAction],
            "description": "Recommended response to the incident.",
        },
        "severity": {
            "type": "string",
            "enum": [s.value for s in Severity],
            "description": "Overall incident severity.",
        },
        "summary": {
            "type": "string",
            "description": "One-line incident summary.",
        },
        "analysis": {
            "type": "string",
            "description": "Concise correlation analysis / likely root cause.",
        },
        "explanation": {
            "type": "string",
            "description": "Plain-language explanation an on-call engineer can act on.",
        },
    },
    "required": ["action", "severity", "summary", "analysis", "explanation"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = (
    "You are an SRE incident-triage agent for an Observability stack "
    "(Prometheus metrics, Loki logs, Tempo traces). You receive correlated "
    "anomaly signals from monitoring agents and decide how to respond. "
    "Correlate the signals, judge severity, and recommend exactly one action: "
    "'escalate' (page a human), 'auto_remediate' (safe automated fix warranted), "
    "'wait' (likely transient — keep watching), or 'investigate' (needs a human "
    "to look but not page-worthy). Be concise and decisive."
)


def build_decision(payload: dict[str, Any], raw: str = "") -> Decision:
    """Validate a provider's JSON payload into a ``Decision``.

    Raises ``LLMError`` on missing/invalid fields so the coordinator can fall
    back to its heuristic.
    """
    try:
        return Decision(
            action=DecisionAction(payload["action"]),
            severity=Severity(payload["severity"]),
            summary=str(payload["summary"]),
            analysis=str(payload["analysis"]),
            explanation=str(payload["explanation"]),
            raw_response=raw,
        )
    except (KeyError, ValueError) as exc:
        raise LLMError(f"malformed decision payload: {exc}") from exc


def build_user_prompt(incident_context: dict[str, Any]) -> str:
    """Render the correlated signal context into the user message."""
    return (
        "Correlated observability signals for a potential incident:\n\n"
        f"{json.dumps(incident_context, indent=2, default=str)}\n\n"
        "Decide how to respond. Reply with the structured decision only."
    )


class LLMClient:
    """Interface every provider implements."""

    name: str = "llm"

    def decide(self, incident_context: dict[str, Any]) -> Decision:
        """Return a triage ``Decision`` for the correlated incident context.

        ``incident_context`` is a JSON-serializable dict of the recent alerts
        and affected components. Implementations should raise ``LLMError`` on
        failure rather than returning a degraded result.
        """
        raise NotImplementedError
