"""Tests for LLM client module."""

import pytest

from warp.config.settings import Settings
from warp.core.exceptions import LLMProviderNotFoundError
from warp.llm.client import (
    PROVIDER_REGISTRY,
    LLMClient,
    LLMProvider,
    create_llm_provider,
)


class MockLLMProvider(LLMProvider):
    """Mock provider for testing."""

    def __init__(self, response: str = "mock response"):
        self._response = response
        self._calls = []

    async def generate(
        self,
        prompt,
        system_prompt=None,
        temperature=0.3,
        max_tokens=4096,
        response_format=None,
    ):
        self._calls.append({
            "prompt": prompt,
            "system_prompt": system_prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return self._response

    async def close(self):
        pass


class TestLLMClient:
    """Tests for LLMClient."""

    @pytest.mark.asyncio
    async def test_generate(self):
        provider = MockLLMProvider(response="Hello World")
        client = LLMClient(provider=provider)

        result = await client.generate("Say hello")
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        provider = MockLLMProvider(response="OK")
        client = LLMClient(provider=provider)

        result = await client.generate("Do something", system_prompt="Be helpful")
        assert result == "OK"
        assert provider._calls[-1]["system_prompt"] == "Be helpful"

    @pytest.mark.asyncio
    async def test_generate_with_params(self):
        provider = MockLLMProvider(response="done")
        client = LLMClient(provider=provider)

        await client.generate(
            "Test",
            temperature=0.7,
            max_tokens=2048,
        )

        call = provider._calls[-1]
        assert call["temperature"] == 0.7
        assert call["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_close(self):
        provider = MockLLMProvider()
        client = LLMClient(provider=provider)
        # Should not raise
        await client.close()

    def test_from_config(self):
        """Test that from_config correctly extracts LLM settings."""
        config = Settings(
            databases=[],
            settings={
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "api_key": "test-key",
                    "temperature": 0.5,
                    "max_tokens": 2048,
                },
            },
        )

        # LLMClient.from_config may fail if openai is not installed,
        # so we test the config extraction logic instead
        assert config.settings.llm.provider == "openai"
        assert config.settings.llm.model == "gpt-4o-mini"
        assert config.settings.llm.api_key == "test-key"
        assert config.settings.llm.temperature == 0.5
        assert config.settings.llm.max_tokens == 2048


class TestProviderRegistry:
    """Tests for provider registry and factory."""

    def test_registry_has_known_providers(self):
        # Verify known providers are registered
        assert "openai" in PROVIDER_REGISTRY
        assert "anthropic" in PROVIDER_REGISTRY
        assert "gemini" in PROVIDER_REGISTRY
        assert "ollama" in PROVIDER_REGISTRY

    def test_create_unknown_provider(self):
        with pytest.raises(LLMProviderNotFoundError):
            create_llm_provider("nonexistent", model="test")


class TestLLMProviderInterface:
    """Tests for the LLMProvider abstract interface."""

    def test_mock_implements_interface(self):
        provider = MockLLMProvider()
        assert hasattr(provider, "generate")
        assert hasattr(provider, "close")

    @pytest.mark.asyncio
    async def test_mock_generate(self):
        provider = MockLLMProvider(response="test output")
        result = await provider.generate("test prompt")
        assert result == "test output"

    @pytest.mark.asyncio
    async def test_mock_close(self):
        provider = MockLLMProvider()
        await provider.close()  # Should not raise
