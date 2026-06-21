"""OpenAI-compatible triage client.

Covers the non-Anthropic options listed in SIP-1765 — a local **LM Studio**
instance, **OpenAI**, or **Azure OpenAI** — all of which speak the OpenAI
chat-completions wire format. Implemented with plain ``requests`` to keep the
POC dependency-light (no extra SDK).
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .base import (
    DECISION_SCHEMA,
    SYSTEM_PROMPT,
    Decision,
    LLMClient,
    LLMError,
    build_decision,
    build_user_prompt,
)


class OpenAICompatibleLLMClient(LLMClient):
    name = "openai_compatible"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        max_tokens: int = 1024,
        timeout_s: float = 30.0,
        session: requests.Session | None = None,
    ):
        # LM Studio defaults to http://localhost:1234/v1 and ignores the key;
        # OpenAI/Azure require a real key.
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._session = session or requests.Session()

    def decide(self, incident_context: dict[str, Any]) -> Decision:
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(incident_context)},
            ],
            # Ask for a JSON object constrained to our schema where supported;
            # servers that ignore the schema still honor json_object mode.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "incident_decision", "schema": DECISION_SCHEMA},
            },
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = self._session.post(
                self._url, json=body, headers=headers, timeout=self._timeout_s
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise LLMError(f"openai-compatible request failed: {exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {exc}") from exc

        return build_decision(_parse_json(content), raw=content)


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(f"could not parse decision JSON: {exc}") from exc
        raise LLMError("response was not JSON")
