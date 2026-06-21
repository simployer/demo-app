"""OpenAI-compatible client (OpenAI / Azure OpenAI / local LM Studio).

Implements the ``complete_json`` primitive over the OpenAI chat-completions wire
format with plain ``requests`` (no extra SDK). ``decide`` and ``assess`` are
inherited from ``LLMClient``.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .base import LLMError, LLMClient


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
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._session = session or requests.Session()

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "obs_agents", "schema": schema},
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
        return _parse_json(content)


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(f"could not parse JSON: {exc}") from exc
        raise LLMError("response was not JSON")
