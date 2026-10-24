"""OpenCode Console and OpenCode Go gateway clients.

OpenCode serves its catalog through one gateway but not through one wire
protocol: each model is exposed as Chat Completions, the OpenAI Responses API,
Anthropic Messages, or Google Gemini. This module owns the model -> protocol
route and builds the matching LangChain client, reusing the provider-native
classes the rest of the package already normalizes.

Routes are taken from the catalog snapshot at models.dev (providers
``opencode`` and ``opencode-go``, model ``provider.npm`` / ``provider.api``).
Refresh with::

    curl -s https://models.dev/api.json | jq '.opencode, .["opencode-go"]'

The prefix rules below cover the families that always use the same protocol;
only the Qwen/MiniMax exceptions are listed by ID because the gateway splits
them differently between Console and Go.
"""

from __future__ import annotations

import os
from typing import Any

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient
from .openai_client import (
    DeepSeekChatOpenAI,
    LocalCompatibleChatOpenAI,
    MinimaxChatOpenAI,
    NormalizedChatOpenAI,
    _supports_reasoning_effort,
)

# Gateway roots. Each protocol appends its own path below the root.
CONSOLE_ROOT = "https://opencode.ai/inference"
GO_ROOT = "https://opencode.ai/inference/go"

OPENCODE_PROVIDERS: dict[str, str] = {
    "opencode": CONSOLE_ROOT,
    "opencode-go": GO_ROOT,
}

# Models served over Anthropic Messages on each variant. Everything prefixed
# claude- routes there by rule; these are the Qwen/MiniMax exceptions.
_ANTHROPIC_EXACT: dict[str, frozenset[str]] = {
    "opencode": frozenset({
        "minimax-m2.1-free",
        "minimax-m2.5-free",
        "minimax-m3-free",
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.6-plus-free",
        "qwen3.8-flash",
    }),
    "opencode-go": frozenset({
        "minimax-m2.7",
        "minimax-m3",
        "qwen3.7-plus",
        "qwen3.8-flash",
        "qwen3.8-max",
    }),
}

_RESPONSES_PREFIXES = ("gpt-", "grok-", "muse-")
_ANTHROPIC_PREFIXES = ("claude-",)
_GOOGLE_PREFIXES = ("gemini-",)


def is_opencode_provider(provider: str) -> bool:
    """Whether ``provider`` is one of the OpenCode gateway providers."""
    return provider.lower() in OPENCODE_PROVIDERS


def pinned_root(provider: str) -> str | None:
    """The fixed gateway root for an OpenCode provider, else None."""
    return OPENCODE_PROVIDERS.get(provider.lower())


def opencode_protocol(provider: str, model: str) -> str:
    """Resolve a model to ``responses``, ``anthropic``, ``google`` or ``chat``."""
    provider_lower = provider.lower()
    model_lower = model.lower()

    if model_lower in _ANTHROPIC_EXACT.get(provider_lower, frozenset()):
        return "anthropic"
    if model_lower.startswith(_RESPONSES_PREFIXES):
        return "responses"
    if model_lower.startswith(_ANTHROPIC_PREFIXES):
        return "anthropic"
    if model_lower.startswith(_GOOGLE_PREFIXES):
        return "google"
    return "chat"


def _chat_class(model: str) -> type:
    """The OpenAI-compatible subclass that carries this model's quirks."""
    model_lower = model.lower()
    if model_lower.startswith("deepseek-"):
        return DeepSeekChatOpenAI
    if model_lower.startswith("minimax-"):
        return MinimaxChatOpenAI
    # Heterogeneous gateway models have unknown tool_choice support; bind the
    # schema without forcing the object form, like the generic local provider.
    return LocalCompatibleChatOpenAI


def _endpoint(root: str, protocol: str) -> str:
    """The base URL for a protocol below an OpenCode gateway root."""
    if protocol == "anthropic":
        # ChatAnthropic appends /v1/messages.
        return f"{root}/anthropic"
    if protocol == "google":
        # ChatGoogleGenerativeAI appends /v1beta/models/<id>:generateContent.
        return f"{root}/google"
    # ChatOpenAI appends /chat/completions or /responses.
    return f"{root}/openai/v1"


class OpenCodeClient(BaseLLMClient):
    """Client for the OpenCode Console and Go gateways.

    One provider key selects the gateway root; ``opencode_protocol`` selects
    the wire protocol for the model. The endpoint is pinned: a different
    ``backend_url`` is rejected rather than silently sending the key somewhere
    else.
    """

    def __init__(self, model: str, base_url: str | None = None,
                 provider: str = "opencode", **kwargs):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()
        if self.provider not in OPENCODE_PROVIDERS:
            raise ValueError(f"Unsupported OpenCode provider: {provider}")
        self._root = OPENCODE_PROVIDERS[self.provider]

    def get_llm(self) -> Any:
        """Build the LangChain client matching this model's gateway protocol."""
        self.warn_if_unknown_model()
        self._check_endpoint()

        api_key = self.kwargs.get("api_key") or os.environ.get(get_api_key_env(self.provider) or "")
        if not api_key:
            env_var = get_api_key_env(self.provider) or "OPENCODE_API_KEY"
            raise ValueError(
                f"API key for provider '{self.provider}' is not set. "
                f"Please set the {env_var} environment variable "
                f"(e.g. add {env_var}=your_key to your .env file)."
            )

        protocol = opencode_protocol(self.provider, self.model)
        if protocol == "responses":
            return self._responses_client(api_key)
        if protocol == "anthropic":
            return self._anthropic_client(api_key)
        if protocol == "google":
            return self._google_client(api_key)
        return self._chat_client(api_key)

    def _check_endpoint(self) -> None:
        if not self.base_url:
            return
        normalized = self.base_url.rstrip("/")
        if normalized != self._root:
            raise ValueError(
                f"Provider '{self.provider}' only connects to {self._root}; "
                "unset backend_url / TRADINGAGENTS_LLM_BACKEND_URL for it."
            )

    def _cross_kwargs(self, *, max_tokens_key: str = "max_tokens",
                      http_clients: bool = True) -> dict[str, Any]:
        """Forward the cross-provider knobs under the target SDK's names."""
        forwarded: dict[str, Any] = {}
        for key in ("timeout", "max_retries", "temperature", "callbacks"):
            if key in self.kwargs:
                forwarded[key] = self.kwargs[key]
        if http_clients:
            for key in ("http_client", "http_async_client"):
                if key in self.kwargs:
                    forwarded[key] = self.kwargs[key]
        if self.kwargs.get("max_tokens") is not None:
            forwarded[max_tokens_key] = self.kwargs["max_tokens"]
        return forwarded

    def _chat_client(self, api_key: str) -> Any:
        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "base_url": _endpoint(self._root, "chat"),
            "api_key": api_key,
            **self._cross_kwargs(),
        }
        reasoning_effort = self.kwargs.get("reasoning_effort")
        if reasoning_effort and _supports_reasoning_effort(self.model):
            # Chat Completions reasoning models that accept it; filtered for the
            # rest exactly like the native OpenAI provider.
            llm_kwargs["reasoning_effort"] = reasoning_effort
        return _chat_class(self.model)(**llm_kwargs)

    def _responses_client(self, api_key: str) -> Any:
        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "base_url": _endpoint(self._root, "responses"),
            "api_key": api_key,
            "use_responses_api": True,
            **self._cross_kwargs(),
        }
        reasoning_effort = self.kwargs.get("reasoning_effort")
        if reasoning_effort and _supports_reasoning_effort(self.model):
            llm_kwargs["reasoning_effort"] = reasoning_effort
        return NormalizedChatOpenAI(**llm_kwargs)

    def _anthropic_client(self, api_key: str) -> Any:
        from .anthropic_client import NormalizedChatAnthropic

        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "base_url": _endpoint(self._root, "anthropic"),
            "api_key": api_key,
            **self._cross_kwargs(),
        }
        effort = self.kwargs.get("effort")
        if effort:
            llm_kwargs["effort"] = effort
        return NormalizedChatAnthropic(**llm_kwargs)

    def _google_client(self, api_key: str) -> Any:
        from .google_client import NormalizedChatGoogleGenerativeAI

        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "base_url": _endpoint(self._root, "google"),
            "google_api_key": api_key,
            **self._cross_kwargs(max_tokens_key="max_output_tokens",
                                 http_clients=False),
        }
        # Gemini 3.x takes the string thinking_level, same as the native client.
        thinking_level = self.kwargs.get("thinking_level")
        if thinking_level:
            llm_kwargs["thinking_level"] = thinking_level
        return NormalizedChatGoogleGenerativeAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """The gateway serves a large, fast-changing catalog: accept any ID."""
        return True
