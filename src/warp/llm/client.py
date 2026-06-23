"""Multi-provider LLM client with factory pattern.

Supports OpenAI, Anthropic, Gemini, and Ollama providers.
Each provider implements the same LLMProvider protocol.
"""

import contextlib
import os
import time
from abc import ABC, abstractmethod
from typing import Any

from warp.core.exceptions import LLMError, LLMGenerationError, LLMProviderNotFoundError
from warp.core.logging import get_logger

logger = get_logger(__name__)

# Provider name -> environment variable for API key
_PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}

# Providers that send prompt data off the local machine to a third-party API.
# (Ollama runs locally and is intentionally excluded.)
CLOUD_PROVIDERS: frozenset = frozenset(_PROVIDER_ENV_VARS)


def _raise_quota_or_rate_limit(provider: str, e: Exception) -> None:
    """Raise a clear error for 429 / quota issues and suggest alternatives."""
    msg = str(e).lower()
    if "429" in msg or "quota" in msg or "rate" in msg or "insufficient_quota" in msg:
        raise LLMGenerationError(
            f"{provider}: API quota or rate limit exceeded. "
            "Check your plan and billing at the provider dashboard, or switch provider: "
            "set settings.llm.provider to 'anthropic', 'gemini', or 'ollama' (local, no key)."
        ) from e
    raise LLMGenerationError(f"{provider} generation failed: {e}") from e


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: str | None = None,
    ) -> str:
        """Generate text from a prompt.

        Args:
            prompt: User prompt
            system_prompt: System prompt for context
            temperature: Sampling temperature
            max_tokens: Maximum response tokens
            response_format: Optional format hint (e.g., "json")

        Returns:
            Generated text response

        Raises:
            LLMGenerationError: If generation fails
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close any open connections."""
        ...


class OpenAIProvider(LLMProvider):
    """OpenAI API provider (GPT-4, GPT-4o, etc.)."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: str = ""):
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise LLMProviderNotFoundError(
                "openai - install with: pip install openai"
            ) from e

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncOpenAI(**kwargs)
        self.model = model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: str | None = None,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await self.client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if not content:
                raise LLMGenerationError("Empty response from OpenAI")
            return content.strip()
        except LLMError:
            raise
        except Exception as e:
            _raise_quota_or_rate_limit("OpenAI", e)

    async def close(self) -> None:
        await self.client.close()


class AnthropicProvider(LLMProvider):
    """Anthropic API provider (Claude models)."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5-20250929", base_url: str = ""):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise LLMProviderNotFoundError(
                "anthropic - install with: pip install anthropic"
            ) from e

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncAnthropic(**kwargs)
        self.model = model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if system_prompt:
            kwargs["system"] = system_prompt

        try:
            response = await self.client.messages.create(**kwargs)
            content = response.content[0].text
            if not content:
                raise LLMGenerationError("Empty response from Anthropic")
            return content.strip()
        except LLMError:
            raise
        except Exception as e:
            _raise_quota_or_rate_limit("Anthropic", e)

    async def close(self) -> None:
        await self.client.close()


class GeminiProvider(LLMProvider):
    """Google Gemini API provider (google-genai SDK).

    Uses the current `google-genai` package; the legacy `google-generativeai`
    SDK reached end of life on 2025-11-30.
    """

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash", base_url: str = ""):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise LLMProviderNotFoundError(
                "gemini - install with: pip install google-genai"
            ) from e

        self._types = types
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["http_options"] = types.HttpOptions(base_url=base_url)
        self.client = genai.Client(**client_kwargs)
        self.model_name = model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: str | None = None,
    ) -> str:
        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type=(
                "application/json" if response_format == "json" else None
            ),
        )

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            text = response.text
            if not text:
                raise LLMGenerationError("Empty response from Gemini")
            return text.strip()
        except LLMError:
            raise
        except Exception as e:
            _raise_quota_or_rate_limit("Gemini", e)

    async def close(self) -> None:
        pass


class OllamaProvider(LLMProvider):
    """Ollama local LLM provider."""

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
        api_key: str = "",
    ):
        try:
            import httpx
        except ImportError as e:
            raise LLMProviderNotFoundError(
                "ollama requires httpx - install with: pip install httpx"
            ) from e

        self.model = model
        self.base_url = self._resolve_base_url(base_url)
        self.client = httpx.AsyncClient(timeout=120.0)

    @staticmethod
    def _resolve_base_url(base_url: str) -> str:
        """Resolve base URL, auto-detecting Docker environment.

        When running inside Docker, localhost refers to the container itself.
        If Ollama runs on the host, we need host.docker.internal instead.
        """
        url = base_url.rstrip("/")

        # Auto-detect Docker: /.dockerenv exists inside containers
        if ("localhost" in url or "127.0.0.1" in url) and os.path.exists("/.dockerenv"):
            original = url
            url = url.replace("localhost", "host.docker.internal")
            url = url.replace("127.0.0.1", "host.docker.internal")
            logger.info(
                f"Docker detected: Ollama URL rewritten {original} -> {url}"
            )

        return url

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        if system_prompt:
            payload["system"] = system_prompt

        if response_format == "json":
            payload["format"] = "json"

        try:
            response = await self.client.post(
                f"{self.base_url}/api/generate",
                json=payload,
            )

            # Handle HTTP errors with Ollama-specific messages
            if response.status_code == 404:
                # Ollama returns 404 when the model is not found
                body = {}
                with contextlib.suppress(Exception):
                    body = response.json()
                error_msg = body.get("error", "")
                raise LLMGenerationError(
                    f"Ollama model '{self.model}' not found. "
                    f"Pull it first with: ollama pull {self.model}\n"
                    f"Available models: ollama list"
                    + (f" (detail: {error_msg})" if error_msg else "")
                )

            response.raise_for_status()
            data = response.json()
            text = data.get("response", "")
            if not text:
                raise LLMGenerationError("Empty response from Ollama")
            return text.strip()
        except LLMError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if "connect" in msg or "refused" in msg:
                hint = (
                    f"Ollama connection failed at {self.base_url}. "
                    "Make sure Ollama is running ('ollama serve'). "
                )
                if os.path.exists("/.dockerenv"):
                    hint += (
                        "You are inside Docker: set LLM_BASE_URL=http://host.docker.internal:11434 "
                        "or use --add-host=host.docker.internal:host-gateway in docker run."
                    )
                else:
                    hint += (
                        "Check that Ollama is listening on the configured host/port."
                    )
                raise LLMGenerationError(hint) from e
            raise LLMGenerationError(f"Ollama generation failed: {e}") from e

    async def close(self) -> None:
        await self.client.aclose()


# Provider factory registry
PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def create_llm_provider(
    provider: str,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> LLMProvider:
    """Factory function to create an LLM provider.

    Args:
        provider: Provider name (openai, anthropic, gemini, ollama)
        api_key: API key
        model: Model name
        base_url: Custom base URL

    Returns:
        Configured LLMProvider instance

    Raises:
        LLMProviderNotFoundError: If provider is unknown
    """
    provider_lower = provider.lower()

    if provider_lower not in PROVIDER_REGISTRY:
        available = ", ".join(PROVIDER_REGISTRY.keys())
        raise LLMProviderNotFoundError(
            f"{provider}. Available providers: {available}"
        )

    cls = PROVIDER_REGISTRY[provider_lower]

    # Resolve api_key: config first, then provider-specific env vars (ollama has none)
    resolved_api_key = api_key
    if not resolved_api_key and provider_lower in _PROVIDER_ENV_VARS:
        resolved_api_key = os.environ.get(_PROVIDER_ENV_VARS[provider_lower], "")

    kwargs: dict[str, Any] = {}
    if resolved_api_key:
        kwargs["api_key"] = resolved_api_key
    if model:
        kwargs["model"] = model
    if base_url:
        kwargs["base_url"] = base_url

    # openai, anthropic, gemini require api_key
    if provider_lower in _PROVIDER_ENV_VARS and not kwargs.get("api_key"):
        raise LLMProviderNotFoundError(
            f"LLM provider '{provider_lower}' requires an API key. "
            f"Set it in config (settings.llm.api_key) or set the "
            f"{_PROVIDER_ENV_VARS[provider_lower]} environment variable."
        )

    logger.info(
        f"Creating LLM provider: {provider_lower} (model={model or 'default'})"
    )

    return cls(**kwargs)


class LLMClient:
    """High-level LLM client that wraps a provider with config defaults.

    Usage:
        client = LLMClient.from_config(config)
        result = await client.generate("Describe this table...")
    """

    def __init__(
        self,
        provider: LLMProvider,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ):
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens

    @classmethod
    def from_config(cls, config: Any) -> "LLMClient":
        """Create LLMClient from Settings.

        Args:
            config: Settings instance (accesses config.settings.llm)

        Returns:
            Configured LLMClient
        """
        llm_config = config.settings.llm
        provider = create_llm_provider(
            provider=llm_config.provider,
            api_key=llm_config.api_key,
            model=llm_config.model,
            base_url=llm_config.base_url,
        )
        return cls(
            provider=provider,
            temperature=llm_config.temperature,
            max_tokens=llm_config.max_tokens,
        )

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: str | None = None,
    ) -> str:
        """Generate text using the configured provider."""
        t0 = time.monotonic()
        logger.debug(
            f"LLM request: format={response_format}, "
            f"prompt_len={len(prompt)}, temp={temperature or self.temperature}"
        )
        result = await self.provider.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature or self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format,
        )
        elapsed = time.monotonic() - t0
        logger.debug(
            f"LLM response: {len(result)} chars in {elapsed:.1f}s"
        )
        return result

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
    ) -> str:
        """Generate JSON response."""
        return await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            response_format="json",
        )

    async def close(self) -> None:
        """Close the provider connection."""
        await self.provider.close()
