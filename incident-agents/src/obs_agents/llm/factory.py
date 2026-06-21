"""Build the configured LLM clients, or ``None`` when disabled.

Two tiers are built from one config: a smart **coordinator** client
(``config.model``) and a cheaper/faster **worker** client
(``config.worker_model``) shared by the monitoring agents.
"""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMClient, LLMError


def _disabled(config: LLMConfig | None) -> bool:
    return config is None or config.provider in ("", "none", "disabled")


def build_llm_client(
    config: LLMConfig | None, *, model: str | None = None, max_tokens: int | None = None
) -> LLMClient | None:
    """Construct an ``LLMClient`` for ``config``, optionally overriding the model.

    Returns ``None`` when no provider is configured.
    """
    if _disabled(config):
        return None
    assert config is not None
    provider = config.provider.lower()
    chosen_model = model or config.model

    if provider == "anthropic":
        from .anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(
            model=chosen_model,
            api_key=config.api_key,
            base_url=config.base_url,
            max_tokens=max_tokens or config.max_tokens,
            effort=config.effort,
            timeout_s=config.timeout_s,
        )

    if provider in ("openai", "azure-openai", "lmstudio", "openai-compatible"):
        from .openai_compatible import OpenAICompatibleLLMClient

        if not config.base_url:
            raise LLMError(f"LLM_BASE_URL is required for provider '{provider}'")
        return OpenAICompatibleLLMClient(
            model=chosen_model,
            base_url=config.base_url,
            api_key=config.api_key,
            max_tokens=max_tokens or config.max_tokens,
            timeout_s=config.timeout_s,
        )

    raise LLMError(f"unknown LLM provider: {config.provider}")


def build_worker_llm_client(config: LLMConfig | None) -> LLMClient | None:
    """Build the shared worker-tier client (cheaper model, smaller token cap)."""
    if _disabled(config):
        return None
    assert config is not None
    return build_llm_client(config, model=config.worker_model, max_tokens=1024)
