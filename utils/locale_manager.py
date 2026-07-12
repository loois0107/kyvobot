import json
import os

class LocaleManager:
    def __init__(self) -> None:
        self.translations: dict = {}
        self._load_locales()

    def _load_locales(self) -> None:
        for lang in ["en", "ko"]:
            path = f"locales/{lang}.json"
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self.translations[lang] = json.load(f)

    def get(self, locale_str: str, key: str, **kwargs) -> str:
        lang = "ko" if "ko" in locale_str.lower() else "en"
        text = self.translations.get(lang, {}).get(key, self.translations.get("en", {}).get(key, key))
        if kwargs:
            for k, v in kwargs.items():
                text = text.replace(f"{{{k}}}", str(v))
        return text
