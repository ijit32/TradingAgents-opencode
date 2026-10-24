"""OpenCode Console and OpenCode Go gateway provider.

The gateway serves one catalog over four wire protocols. These tests pin the
model -> protocol route, the endpoint each protocol uses, auth/key handling,
and the CLI wiring (provider table, key env, model catalog, prefs).
"""

from __future__ import annotations

import pytest

from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.openai_client import (
    DeepSeekChatOpenAI,
    LocalCompatibleChatOpenAI,
    MinimaxChatOpenAI,
    NormalizedChatOpenAI,
    check_endpoint,
)
from tradingagents.llm_clients.opencode_client import (
    CONSOLE_ROOT,
    GO_ROOT,
    OpenCodeClient,
    opencode_protocol,
)
from tradingagents.llm_clients.validators import validate_model


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-test")


# --- routing ---------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("provider,model,protocol", [
    # Console: prefix families plus the Qwen/MiniMax exceptions.
    ("opencode", "gpt-6-sol", "responses"),
    ("opencode", "grok-4.6", "responses"),
    ("opencode", "muse-wonder-1.0", "responses"),
    ("opencode", "claude-opus-5-5", "anthropic"),
    ("opencode", "minimax-m3-free", "anthropic"),
    ("opencode", "qwen3.8-flash", "anthropic"),
    ("opencode", "gemini-3.8-flash", "google"),
    ("opencode", "deepseek-v4.1-flash-free", "chat"),
    ("opencode", "minimax-m3", "chat"),
    ("opencode", "qwen3.8-max", "chat"),
    ("opencode", "mistral-large-4", "chat"),
    # Go: the same names split differently for Qwen/MiniMax.
    ("opencode-go", "gpt-6-luna", "responses"),
    ("opencode-go", "claude-haiku-5-5", "anthropic"),
    ("opencode-go", "minimax-m3", "anthropic"),
    ("opencode-go", "qwen3.8-max", "anthropic"),
    ("opencode-go", "gemini-4-argon-contributor", "google"),
    ("opencode-go", "deepseek-v4.1-flash", "chat"),
    ("opencode-go", "minimax-m3.1-flash", "chat"),
    ("opencode-go", "glm-5.3", "chat"),
])
def test_model_routes_to_the_gateway_protocol(provider, model, protocol):
    assert opencode_protocol(provider, model) == protocol


@pytest.mark.unit
def test_factory_routes_both_providers_to_the_opencode_client():
    assert isinstance(create_llm_client("opencode", "gpt-6-sol"), OpenCodeClient)
    assert isinstance(create_llm_client("opencode-go", "glm-5.3"), OpenCodeClient)
    assert create_llm_client("opencode", "gpt-6-sol").provider == "opencode"
    assert create_llm_client("opencode-go", "glm-5.3").provider == "opencode-go"


# --- built clients ---------------------------------------------------------

@pytest.mark.unit
def test_responses_route_uses_the_gateway_responses_api():
    llm = create_llm_client("opencode", "gpt-6-sol").get_llm()
    assert isinstance(llm, NormalizedChatOpenAI)
    assert llm.openai_api_base == "https://opencode.ai/inference/openai/v1"
    assert llm.use_responses_api is True


@pytest.mark.unit
def test_go_root_is_used_for_go_models():
    llm = create_llm_client("opencode-go", "glm-5.3").get_llm()
    assert isinstance(llm, LocalCompatibleChatOpenAI)
    assert llm.openai_api_base == "https://opencode.ai/inference/go/openai/v1"


@pytest.mark.unit
def test_anthropic_route_keeps_messages_path_and_normalizes():
    from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic

    llm = create_llm_client("opencode", "claude-opus-5-5").get_llm()
    assert isinstance(llm, NormalizedChatAnthropic)
    assert llm.anthropic_api_url == "https://opencode.ai/inference/anthropic"


@pytest.mark.unit
def test_google_route_uses_the_gemini_sdk_with_the_gateway_key():
    from tradingagents.llm_clients.google_client import NormalizedChatGoogleGenerativeAI

    llm = create_llm_client("opencode", "gemini-3.8-flash").get_llm()
    assert isinstance(llm, NormalizedChatGoogleGenerativeAI)
    assert llm.base_url == "https://opencode.ai/inference/google"
    assert llm.google_api_key.get_secret_value() == "sk-opencode-test"


@pytest.mark.unit
def test_chat_route_picks_the_provider_quirk_subclass():
    deepseek = create_llm_client("opencode", "deepseek-v4.1-flash-free").get_llm()
    minimax = create_llm_client("opencode", "minimax-m2.7").get_llm()
    generic = create_llm_client("opencode", "mistral-large-4").get_llm()
    assert isinstance(deepseek, DeepSeekChatOpenAI)
    assert isinstance(minimax, MinimaxChatOpenAI)
    assert isinstance(generic, LocalCompatibleChatOpenAI)


# --- auth / endpoint -------------------------------------------------------

@pytest.mark.unit
def test_both_providers_share_the_opencode_key(monkeypatch):
    assert get_api_key_env("opencode") == "OPENCODE_API_KEY"
    assert get_api_key_env("opencode-go") == "OPENCODE_API_KEY"


@pytest.mark.unit
def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENCODE_API_KEY"):
        create_llm_client("opencode", "gpt-6-sol").get_llm()


@pytest.mark.unit
@pytest.mark.parametrize("provider,root", [
    ("opencode", CONSOLE_ROOT),
    ("opencode-go", GO_ROOT),
])
def test_a_foreign_endpoint_is_refused(provider, root):
    with pytest.raises(ValueError, match="only connects to"):
        create_llm_client(provider, "gpt-6-sol",
                          base_url="https://relay.example/v1").get_llm()
    # The provider's own root (with or without trailing slash) is accepted.
    check_endpoint(provider, root + "/")


@pytest.mark.unit
def test_any_model_id_is_accepted_without_a_warning():
    assert validate_model("opencode", "not-a-known-model") is True
    assert validate_model("opencode-go", "not-a-known-model") is True


# --- catalog / CLI wiring ---------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["opencode", "opencode-go"])
@pytest.mark.parametrize("mode", ["quick", "deep"])
def test_catalog_offers_current_models_and_custom(provider, mode):
    from tradingagents.llm_clients.model_catalog import get_model_options

    values = [value for _, value in get_model_options(provider, mode)]
    assert "custom" in values
    for value in values:
        if value != "custom":
            assert validate_model(provider, value)


@pytest.mark.unit
def test_console_catalog_does_not_offer_api_key_blocked_free_models():
    """The Console free tier only serves OpenCode's own requests; a client with
    an API key gets 403 FreeTierError, so offering those IDs here would fail at
    the first call. They belong to the OpenCode plugin path."""
    from tradingagents.llm_clients.model_catalog import get_model_options

    values = [
        value
        for mode in ("quick", "deep")
        for _, value in get_model_options("opencode", mode)
    ]
    assert "deepseek-v4.1-flash-free" not in values
    assert "glm-5.3-flash-free" not in values


@pytest.mark.unit
def test_provider_table_has_both_entries_with_their_roots():
    from cli.prompts import provider_default_url

    assert provider_default_url("opencode") == CONSOLE_ROOT
    assert provider_default_url("opencode-go") == GO_ROOT


@pytest.mark.unit
def test_prefs_keep_the_opencode_provider():
    from cli.prefs import sanitize

    kept = sanitize({"llm_provider": "opencode", "quick_think_llm": "glm-5.3",
                     "deep_think_llm": "gpt-6-sol"}, "stock")
    assert kept["llm_provider"] == "opencode"


# --- kwargs ----------------------------------------------------------------

@pytest.mark.unit
def test_reasoning_effort_is_forwarded_only_where_the_gateway_takes_it():
    gpt = create_llm_client("opencode", "gpt-6-sol", reasoning_effort="high").get_llm()
    assert gpt.reasoning_effort == "high"
    chat = create_llm_client("opencode", "glm-5.3", reasoning_effort="high").get_llm()
    assert getattr(chat, "reasoning_effort", None) is None


@pytest.mark.unit
def test_output_token_cap_uses_each_sdk_name():
    anthropic_llm = create_llm_client(
        "opencode", "claude-opus-5-5", max_tokens=1234).get_llm()
    google_llm = create_llm_client(
        "opencode", "gemini-3.8-flash", max_tokens=1234).get_llm()
    assert anthropic_llm.max_tokens == 1234
    assert google_llm.max_output_tokens == 1234


@pytest.mark.unit
def test_deepseek_free_model_inherits_thinking_quirks():
    caps = get_capabilities("deepseek-v4.1-flash-free")
    assert caps.supports_tool_choice is False
    assert caps.requires_reasoning_content_roundtrip is True
