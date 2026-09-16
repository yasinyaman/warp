"""Tests for concrete LLM providers and create_llm_provider.

All third-party SDK clients are replaced with AsyncMocks so no network calls
happen. Covers generate(), close(), error mapping, json mode, and the factory.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warp.adapters.outbound.llm.providers import (
    AnthropicProvider,
    LLMClient,
    OllamaProvider,
    OpenAIProvider,
    _raise_quota_or_rate_limit,
    create_llm_provider,
    is_quota_or_rate_limit_error,
)
from warp.application.config import Settings
from warp.domain.errors import LLMGenerationError, LLMProviderNotFoundError

# --- _raise_quota_or_rate_limit ---


def test_raise_quota_message() -> None:
    with pytest.raises(LLMGenerationError, match="quota or rate limit"):
        _raise_quota_or_rate_limit("openai", Exception("Error 429 insufficient_quota"))


def test_raise_generic_message() -> None:
    with pytest.raises(LLMGenerationError, match="generation failed"):
        _raise_quota_or_rate_limit("openai", Exception("boom"))


@pytest.mark.parametrize(
    "message",
    [
        "Error 429 insufficient_quota",
        "Rate limit exceeded, retry later",
        "rate_limit_exceeded",
        "You exceeded your current quota",
        "Too Many Requests",
    ],
)
def test_quota_messages_detected(message: str) -> None:
    assert is_quota_or_rate_limit_error(Exception(message))


@pytest.mark.parametrize(
    "message",
    [
        "generation failed: model not found",
        "models/gemma is not supported for generateContent",
        "moderate content flagged",
        "iteration error",
    ],
)
def test_unrelated_messages_not_misclassified(message: str) -> None:
    assert not is_quota_or_rate_limit_error(Exception(message))
    with pytest.raises(LLMGenerationError, match="generation failed"):
        _raise_quota_or_rate_limit("gemini", Exception(message))


def test_status_code_attribute_wins() -> None:
    err = Exception("opaque")
    err.status_code = 429  # type: ignore[attr-defined]
    assert is_quota_or_rate_limit_error(err)


# --- OpenAIProvider ---


@pytest.fixture
def openai_provider() -> OpenAIProvider:
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = OpenAIProvider(api_key="k", model="gpt-4o-mini")
    return provider


@pytest.mark.asyncio
async def test_openai_generate(openai_provider: OpenAIProvider) -> None:
    msg = MagicMock()
    msg.message.content = "hello"
    completion = MagicMock()
    completion.choices = [msg]
    openai_provider.client.chat.completions.create = AsyncMock(return_value=completion)

    result = await openai_provider.generate("hi", system_prompt="sys", response_format="json")
    assert result == "hello"
    kwargs = openai_provider.client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_openai_generate_quota_error(openai_provider: OpenAIProvider) -> None:
    openai_provider.client.chat.completions.create = AsyncMock(
        side_effect=Exception("429 rate limit")
    )
    with pytest.raises(LLMGenerationError):
        await openai_provider.generate("hi")


@pytest.mark.asyncio
async def test_openai_close(openai_provider: OpenAIProvider) -> None:
    openai_provider.client.close = AsyncMock()
    await openai_provider.close()
    openai_provider.client.close.assert_awaited_once()


def test_openai_with_base_url() -> None:
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        OpenAIProvider(api_key="k", base_url="http://x")
        assert mock_cls.call_args.kwargs["base_url"] == "http://x"


# --- AnthropicProvider ---


@pytest.fixture
def anthropic_provider() -> AnthropicProvider:
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = AnthropicProvider(api_key="k")
    return provider


@pytest.mark.asyncio
async def test_anthropic_generate(anthropic_provider: AnthropicProvider) -> None:
    block = MagicMock()
    block.text = " response "
    response = MagicMock()
    response.content = [block]
    anthropic_provider.client.messages.create = AsyncMock(return_value=response)

    result = await anthropic_provider.generate("hi", system_prompt="sys")
    assert result == "response"
    assert anthropic_provider.client.messages.create.call_args.kwargs["system"] == "sys"


@pytest.mark.asyncio
async def test_anthropic_empty_response_raises(anthropic_provider: AnthropicProvider) -> None:
    block = MagicMock()
    block.text = ""
    response = MagicMock()
    response.content = [block]
    anthropic_provider.client.messages.create = AsyncMock(return_value=response)
    with pytest.raises(LLMGenerationError):
        await anthropic_provider.generate("hi")


@pytest.mark.asyncio
async def test_anthropic_error_mapped(anthropic_provider: AnthropicProvider) -> None:
    anthropic_provider.client.messages.create = AsyncMock(side_effect=Exception("boom"))
    with pytest.raises(LLMGenerationError):
        await anthropic_provider.generate("hi")


# --- OllamaProvider ---


@pytest.fixture
def ollama_provider() -> OllamaProvider:
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = OllamaProvider(model="llama3.1", base_url="http://localhost:11434")
    return provider


@pytest.mark.asyncio
async def test_ollama_generate(ollama_provider: OllamaProvider) -> None:
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"response": " hi there "})
    ollama_provider.client.post = AsyncMock(return_value=response)

    result = await ollama_provider.generate("q", system_prompt="s", response_format="json")
    assert result == "hi there"
    payload = ollama_provider.client.post.call_args.kwargs["json"]
    assert payload["format"] == "json"
    assert payload["system"] == "s"


@pytest.mark.asyncio
async def test_ollama_model_not_found(ollama_provider: OllamaProvider) -> None:
    response = MagicMock()
    response.status_code = 404
    response.json = MagicMock(return_value={"error": "not found"})
    ollama_provider.client.post = AsyncMock(return_value=response)
    with pytest.raises(LLMGenerationError, match="not found"):
        await ollama_provider.generate("q")


@pytest.mark.asyncio
async def test_ollama_empty_response(ollama_provider: OllamaProvider) -> None:
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"response": ""})
    ollama_provider.client.post = AsyncMock(return_value=response)
    with pytest.raises(LLMGenerationError, match="Empty response"):
        await ollama_provider.generate("q")


@pytest.mark.asyncio
async def test_ollama_connection_error(ollama_provider: OllamaProvider) -> None:
    ollama_provider.client.post = AsyncMock(side_effect=Exception("connection refused"))
    with pytest.raises(LLMGenerationError, match="connection failed"):
        await ollama_provider.generate("q")


@pytest.mark.asyncio
async def test_ollama_generic_error(ollama_provider: OllamaProvider) -> None:
    ollama_provider.client.post = AsyncMock(side_effect=Exception("weird"))
    with pytest.raises(LLMGenerationError, match="generation failed"):
        await ollama_provider.generate("q")


@pytest.mark.asyncio
async def test_ollama_close(ollama_provider: OllamaProvider) -> None:
    ollama_provider.client.aclose = AsyncMock()
    await ollama_provider.close()
    ollama_provider.client.aclose.assert_awaited_once()


def test_ollama_resolve_base_url_no_docker() -> None:
    with patch("os.path.exists", return_value=False):
        assert (
            OllamaProvider._resolve_base_url("http://localhost:11434/") == "http://localhost:11434"
        )


def test_ollama_resolve_base_url_docker() -> None:
    with patch("os.path.exists", return_value=True):
        resolved = OllamaProvider._resolve_base_url("http://localhost:11434")
        assert "host.docker.internal" in resolved


# --- create_llm_provider factory ---


def test_create_provider_unknown() -> None:
    with pytest.raises(LLMProviderNotFoundError):
        create_llm_provider("bogus")


def test_create_provider_requires_api_key() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(LLMProviderNotFoundError, match="requires an API key"),
    ):
        create_llm_provider("openai", api_key="")


def test_create_provider_uses_env_key() -> None:
    with (
        patch.dict("os.environ", {"OPENAI_API_KEY": "envkey"}),
        patch("openai.AsyncOpenAI") as mock_cls,
    ):
        mock_cls.return_value = MagicMock()
        provider = create_llm_provider("openai", api_key="")
        assert isinstance(provider, OpenAIProvider)


def test_create_provider_ollama_no_key_required() -> None:
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = create_llm_provider("ollama", base_url="http://localhost:11434")
        assert isinstance(provider, OllamaProvider)


def test_create_provider_explicit_key() -> None:
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        provider = create_llm_provider("anthropic", api_key="k", model="claude")
        assert isinstance(provider, AnthropicProvider)


# --- LLMClient.from_config and generate_json ---


def test_from_config_builds_client() -> None:
    config = Settings(
        settings={"llm": {"provider": "anthropic", "api_key": "k", "model": "claude"}}
    )
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient.from_config(config)
        assert isinstance(client, LLMClient)
        assert client.temperature == config.settings.llm.temperature


@pytest.mark.asyncio
async def test_generate_json_sets_format() -> None:
    provider = MagicMock()
    provider.generate = AsyncMock(return_value='{"a": 1}')
    client = LLMClient(provider=provider)
    result = await client.generate_json("prompt", system_prompt="sys")
    assert result == '{"a": 1}'
    assert provider.generate.call_args.kwargs["response_format"] == "json"
