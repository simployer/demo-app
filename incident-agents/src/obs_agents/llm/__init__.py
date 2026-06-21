"""Pluggable LLM clients for AI-driven incident triage.

The Coordinator hands correlated signal context to an ``LLMClient`` and acts on
the structured ``Decision`` it returns. Anthropic Claude is the default (see
``anthropic_client``); an OpenAI-compatible adapter (``openai_compatible``)
covers local LM Studio, OpenAI, and Azure OpenAI per SIP-1765.
"""

from .base import Decision, DecisionAction, LLMClient, LLMError
from .factory import build_llm_client, build_worker_llm_client

__all__ = [
    "Decision",
    "DecisionAction",
    "LLMClient",
    "LLMError",
    "build_llm_client",
    "build_worker_llm_client",
]
