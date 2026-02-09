"""Tests for internationalization module."""

import pytest

from warp.config.settings import Settings
from warp.i18n.localization import LocalizationManager
from warp.catalog.models import LocalizedText


class TestLocalizationManager:
    """Tests for LocalizationManager."""

    def test_defaults(self):
        mgr = LocalizationManager()
        assert mgr.default_language == "en"
        assert mgr.languages == ["en"]
        assert not mgr.is_multilingual

    def test_multilingual(self):
        mgr = LocalizationManager(languages=["en", "tr"])
        assert mgr.is_multilingual

    def test_get_system_prompt(self):
        mgr = LocalizationManager(languages=["en", "tr"])
        prompt = mgr.get_system_prompt("tr")
        assert "Türkçe" in prompt

        prompt = mgr.get_system_prompt("en")
        assert "English" in prompt

    def test_get_system_prompt_unknown_lang(self):
        mgr = LocalizationManager(fallback_language="en")
        prompt = mgr.get_system_prompt("xx")
        assert "English" in prompt

    def test_create_text_from_string(self):
        mgr = LocalizationManager(default_language="tr")
        text = mgr.create_text("Kullanıcılar")
        assert text.get("tr") == "Kullanıcılar"

    def test_create_text_from_dict(self):
        mgr = LocalizationManager()
        text = mgr.create_text({"en": "Users", "tr": "Kullanıcılar"})
        assert text.get("en") == "Users"
        assert text.get("tr") == "Kullanıcılar"

    def test_merge_texts(self):
        mgr = LocalizationManager()
        t1 = LocalizedText(texts={"en": "Users"})
        t2 = LocalizedText(texts={"en": "User accounts", "tr": "Kullanıcı hesapları"})

        merged = mgr.merge_texts(t1, t2)
        # t2 overrides t1 for "en"
        assert merged.get("en") == "User accounts"
        assert merged.get("tr") == "Kullanıcı hesapları"

    def test_merge_texts_with_none(self):
        mgr = LocalizationManager()
        t1 = LocalizedText(texts={"en": "Hello"})
        merged = mgr.merge_texts(None, t1, None)
        assert merged.get("en") == "Hello"

    def test_get_missing_languages(self):
        mgr = LocalizationManager(languages=["en", "tr", "de"])
        text = LocalizedText(texts={"en": "Hello"})

        missing = mgr.get_missing_languages(text)
        assert "tr" in missing
        assert "de" in missing
        assert "en" not in missing

    def test_is_complete(self):
        mgr = LocalizationManager(languages=["en", "tr"])

        incomplete = LocalizedText(texts={"en": "Hello"})
        assert not mgr.is_complete(incomplete)

        complete = LocalizedText(texts={"en": "Hello", "tr": "Merhaba"})
        assert mgr.is_complete(complete)

    def test_get_language_instruction_single(self):
        mgr = LocalizationManager(languages=["tr"])
        instruction = mgr.get_language_instruction()
        assert "Türkçe" in instruction

    def test_get_language_instruction_multi(self):
        mgr = LocalizationManager(languages=["en", "tr"])
        instruction = mgr.get_language_instruction()
        assert "English" in instruction
        assert "Türkçe" in instruction

    def test_get_translation_prompt(self):
        mgr = LocalizationManager()
        prompt = mgr.get_translation_prompt(
            "User accounts", source_lang="en", target_lang="tr"
        )
        assert "User accounts" in prompt
        assert "English" in prompt
        assert "Türkçe" in prompt

    def test_from_config(self):
        config = Settings(
            databases=[],
            settings={
                "i18n": {
                    "default_language": "tr",
                    "languages": ["tr", "en"],
                    "fallback_language": "en",
                    "auto_translate": True,
                    "translation_strategy": "multi",
                },
                "llm": {
                    "language_prompts": {"tr": "Türkçe olarak yanıt ver."},
                },
            },
        )

        mgr = LocalizationManager.from_config(config)
        assert mgr.default_language == "tr"
        assert mgr.languages == ["tr", "en"]
        assert mgr.translation_strategy == "multi"
        assert mgr.is_multilingual
        # Custom prompt should override default
        assert mgr.get_system_prompt("tr") == "Türkçe olarak yanıt ver."
