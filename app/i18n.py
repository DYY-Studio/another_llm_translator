from __future__ import annotations

import locale
import os

SUPPORTED_LANGUAGES = ("zh-CN", "en")


def resolve_language(value: str | None = None) -> str:
    candidate = value or os.environ.get("MINIMAL_LLM_LANGUAGE")
    if candidate in SUPPORTED_LANGUAGES:
        return str(candidate)
    system = locale.getlocale()[0] or ""
    return "zh-CN" if system.casefold().startswith(("zh", "cmn")) else "en"


def cli_language(value: str | None) -> str:
    language = resolve_language(value)
    os.environ["MINIMAL_LLM_LANGUAGE"] = language
    return language
