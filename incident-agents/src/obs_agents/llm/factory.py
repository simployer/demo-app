"""Build the configured LLM client, or ``None`` when triage is disabled."""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMClient, LLMError


def build_llm_client(config: LLMConfig | None) -> LLMClient | None:
    """Construct an ``LLMClient`` from config.

    Returns ``None`` when no provider is configured (``LLM_PROVIDER=none`` or
    unset), in which case the Coordinator falls back to its heuristic.
    """
    if config is None or config.provider in ("", "none", "disabled"):
        return None

    provider = config.provider.lower()

    if provider == "anthropic":
        from .anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            effort=config.effort,
            timeout_s=config.timeout_s,
        )

    if provider in ("openai", "azure-openai", "lmstudio", "openai-compatible"):
        from .openai_compatible import OpenAICompatibleLLMClient

        if not config.base_url:
            raise LLMError(f"LLM_BASE_URL is required for provider '{provider}'")
        return OpenAICompatibleLLMClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
        )

    raise LLMError(f"unknown LLM provider: {config.provider}")
