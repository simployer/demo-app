"""Anthropic Claude client.

Implements the ``complete_json`` primitive; ``decide`` (coordinator) and
``assess`` (workers) are inherited from ``LLMClient``. Uses adaptive thinking +
structured outputs, degrading gracefully on SDKs that predate ``output_config``.
Coordinator runs on Claude Opus 4.8; worker agents on a cheaper/faster model.
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLMClient, LLMError


class AnthropicLLMClient(LLMClient):
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        effort: str = "low",
        timeout_s: float = 30.0,
    ):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMError("anthropic SDK not installed; `pip install anthropic`") from exc

        client_kwargs: dict[str, Any] = {"timeout": timeout_s}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        try:
            self._client = anthropic.Anthropic(**client_kwargs)
        except Exception as exc:  # noqa: BLE001 - e.g. missing API key
            raise LLMError(f"could not init anthropic client: {exc}") from exc
        self.model = model
        self._max_tokens = max_tokens
        self._effort = effort

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = self._create(request)
        except Exception as exc:  # noqa: BLE001 - surface as LLMError
            raise LLMError(f"anthropic request failed: {exc}") from exc

        text = _first_text_block(response)
        if not text:
            raise LLMError("anthropic returned no text content")
        return _parse_json(text)

    def _create(self, request: dict[str, Any]):
        """Call messages.create, degrading gracefully on older SDKs."""
        try:
            return self._client.messages.create(**request)
        except TypeError as exc:
            if "output_config" not in str(exc):
                raise
            request.pop("output_config", None)
            return self._client.messages.create(**request)


def _first_text_block(response: Any) -> str:
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _parse_json(text: str) -> dict[str, Any]:
    """Parse the model's JSON, tolerating prose around it."""
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
