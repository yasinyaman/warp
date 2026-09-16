"""Internationalization and localization utilities.

Manages multi-language text generation and translation
for database catalog descriptions.
"""

import logging
from typing import Any

from warp.domain.catalog import LocalizedText

logger = logging.getLogger(__name__)

# Default language-specific system prompts for LLM
DEFAULT_LANGUAGE_PROMPTS: dict[str, str] = {
    "en": "Respond in English.",
    "tr": ("Yanıtlarını Türkçe ver. Teknik terimleri parantez içinde İngilizce olarak da belirt."),
    "de": "Antworte auf Deutsch. Technische Begriffe in Klammern auf Englisch angeben.",
    "fr": "Répondez en français. Indiquez les termes techniques entre parenthèses en anglais.",
    "es": "Responde en español. Indica los términos técnicos entre paréntesis en inglés.",
    "ja": "日本語で回答してください。技術用語は括弧内に英語で記載してください。",
    "zh": "请用中文回答。技术术语请在括号内标注英文。",
    "ko": "한국어로 응답해주세요. 기술 용어는 괄호 안에 영어로 표기해주세요.",
    "pt": "Responda em português. Indique os termos técnicos entre parênteses em inglês.",
    "ru": "Отвечайте на русском. Технические термины указывайте в скобках на английском.",
}

# Language display names
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "tr": "Türkçe",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "ja": "日本語",
    "zh": "中文",
    "ko": "한국어",
    "pt": "Português",
    "ru": "Русский",
}


class LocalizationManager:
    """Manages multi-language description generation.

    Handles language configuration, system prompt selection,
    and LocalizedText creation/merging.
    """

    def __init__(
        self,
        default_language: str = "en",
        languages: list[str] | None = None,
        fallback_language: str = "en",
        auto_translate: bool = True,
        translation_strategy: str = "single",
        language_prompts: dict[str, str] | None = None,
    ):
        """Store the default/fallback languages and translation settings."""
        self.default_language = default_language
        self.languages = languages or [default_language]
        self.fallback_language = fallback_language
        self.auto_translate = auto_translate
        self.translation_strategy = translation_strategy

        self.language_prompts = {**DEFAULT_LANGUAGE_PROMPTS}
        if language_prompts:
            self.language_prompts.update(language_prompts)

    @classmethod
    def from_config(cls, config: Any) -> "LocalizationManager":
        """Create from Settings instance.

        Args:
            config: Settings instance (accesses config.settings.i18n and config.settings.llm)

        Returns:
            Configured LocalizationManager
        """
        return cls(
            default_language=config.settings.i18n.default_language,
            languages=config.settings.i18n.languages,
            fallback_language=config.settings.i18n.fallback_language,
            auto_translate=config.settings.i18n.auto_translate,
            translation_strategy=config.settings.i18n.translation_strategy,
            language_prompts=config.settings.llm.language_prompts,
        )

    def get_system_prompt(self, lang: str | None = None) -> str:
        """Get the system prompt for a specific language."""
        lang = lang or self.default_language
        return self.language_prompts.get(
            lang,
            self.language_prompts.get(self.fallback_language, "Respond in English."),
        )

    def get_language_instruction(self) -> str:
        """Get instruction for multi-language generation in a single LLM call."""
        if len(self.languages) <= 1:
            lang = self.languages[0] if self.languages else self.default_language
            name = LANGUAGE_NAMES.get(lang, lang)
            return f"Respond in {name}."

        lang_names = []
        for lang in self.languages:
            name = LANGUAGE_NAMES.get(lang, lang)
            lang_names.append(f"{name} ({lang})")

        example_parts = [f'"{lang}": "..."' for lang in self.languages]
        example_json = "{" + ", ".join(example_parts) + "}"

        return (
            f"Provide descriptions in the following languages: {', '.join(lang_names)}. "
            f"Use JSON format with language codes as keys, e.g., {example_json}"
        )

    def create_text(self, texts: dict[str, str] | str) -> LocalizedText:
        """Create a LocalizedText from various input formats."""
        if isinstance(texts, str):
            return LocalizedText(texts={self.default_language: texts})
        return LocalizedText(texts=texts)

    def merge_texts(
        self,
        *sources: LocalizedText | None,
    ) -> LocalizedText:
        """Merge multiple LocalizedText sources.

        Later sources override earlier ones for the same language.
        """
        merged: dict[str, str] = {}
        for source in sources:
            if source and not source.is_empty:
                for lang, text in source.texts.items():
                    if text.strip():
                        merged[lang] = text
        return LocalizedText(texts=merged)

    def get_translation_prompt(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Build a translation prompt for LLM."""
        source_name = LANGUAGE_NAMES.get(source_lang, source_lang)
        target_name = LANGUAGE_NAMES.get(target_lang, target_lang)

        return (
            f"Translate the following database description from {source_name} "
            f"to {target_name}. Keep technical terms accurate. "
            f"Only output the translation, nothing else.\n\n"
            f"Text: {text}"
        )

    def get_missing_languages(self, text: LocalizedText) -> list[str]:
        """Get languages that are configured but missing from a text."""
        return [lang for lang in self.languages if not text.texts.get(lang, "").strip()]

    def is_complete(self, text: LocalizedText) -> bool:
        """Check if a LocalizedText has all configured languages."""
        return len(self.get_missing_languages(text)) == 0

    @property
    def is_multilingual(self) -> bool:
        """Check if multiple languages are configured."""
        return len(self.languages) > 1
