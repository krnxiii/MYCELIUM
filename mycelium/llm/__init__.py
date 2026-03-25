"""LLM client: pluggable backend (CC CLI or direct API via httpx)."""

from mycelium.config import LLMSettings
from mycelium.llm.base import LLMBackend, LLMProgressFn
from mycelium.llm.client import LLMClient
from mycelium.llm.session import LLMSession


def make_llm_client(settings: LLMSettings | None = None) -> LLMBackend:
    """Factory: create LLM backend based on config provider."""
    s = settings or LLMSettings()

    if s.provider in ("api", "litellm"):
        from mycelium.llm.client_litellm import LiteLLMClient
        return LiteLLMClient(s)

    # Default: CC CLI subprocess
    return LLMClient(s)


__all__ = [
    "LLMBackend",
    "LLMClient",
    "LLMProgressFn",
    "LLMSession",
    "make_llm_client",
]
