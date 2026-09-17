"""Text-generation port (an LLM behind a provider-agnostic interface)."""

from typing import Protocol


class TextGenerator(Protocol):
    """Generates text/JSON from prompts; closed when the use case is done."""

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: str | None = None,
    ) -> str:
        """Generate a completion."""
        ...

    async def generate_json(self, prompt: str, system_prompt: str | None = None) -> str:
        """Generate a completion expected to be a JSON document."""
        ...

    async def close(self) -> None:
        """Release provider resources."""
        ...
