"""Multi-language support: translation catalogs, language resolution and a reply-language directive for LLM prompts.

Standard library only (importable and testable without the Telegram stack); everything here is a pure function except the
lazily loaded, read-only catalog cache.

Catalogs live in ``data/locales/<code>.json`` as flat ``{"key": "text with {placeholders}"}`` objects.  English is the source
of truth: a key missing from another language falls back to English, then to the key itself, so a half-translated catalog
never breaks a message.  ``tests/test_i18n.py`` checks that every catalog has the same keys and placeholders as English and
that every literal key used in the code exists.

Language codes are lowercase ``xx`` (``hi``, ``es``, ``zh``) plus ``hl`` for Hinglish (Hindi in Latin script), which is how a
large part of the project's users actually write.

Adding a language: drop ``data/locales/<code>.json`` (same keys as ``en.json``), add its native and English names to
``LANGUAGE_NAMES`` / ``_ENGLISH_NAMES`` below and to ``SUPPORTED_LANGUAGES``'s default in ``bot/config.py``; ``tests/test_i18n.py``
tells you what is missing.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

LOCALES_DIR = Path(__file__).resolve().parents[2] / "data" / "locales"
DEFAULT_LANGUAGE = "en"

# Native names, shown by /lang.  The authoritative list of *available* languages is the set of catalog files.
LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English", "hi": "हिन्दी", "hl": "Hinglish", "es": "Español", "ru": "Русский", "id": "Bahasa Indonesia",
    "ar": "العربية", "zh": "中文", "fr": "Français", "pt": "Português",
}
# English names for LLM instructions ("Reply in Hindi").
_ENGLISH_NAMES: Dict[str, str] = {
    "en": "English", "hi": "Hindi (Devanagari script)", "hl": "Hinglish (Hindi written in the Latin alphabet)", "es": "Spanish",
    "ru": "Russian", "id": "Indonesian", "ar": "Arabic", "zh": "Chinese (Simplified)", "fr": "French", "pt": "Portuguese",
}
RTL_LANGUAGES: FrozenSet[str] = frozenset({"ar"})

_ALIASES = {"iw": "he", "in": "id", "zh-cn": "zh", "zh-tw": "zh", "zh-hans": "zh", "zh-hant": "zh", "hinglish": "hl", "hi-latn": "hl",
            "hi-en": "hl", "english": "en", "hindi": "hi", "spanish": "es", "russian": "ru", "indonesian": "id", "arabic": "ar",
            "chinese": "zh", "french": "fr", "portuguese": "pt"}
_CODE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z0-9]{2,8})*$")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_lock = threading.Lock()
_catalogs: Dict[str, Dict[str, str]] = {}
_loaded_dir: Optional[Path] = None


def normalize_language(code: Optional[str]) -> Optional[str]:
    """``pt-BR`` -> ``pt``, ``ZH_cn`` -> ``zh``, ``Hinglish`` -> ``hl``; None when it does not look like a language code."""
    if not code:
        return None
    c = str(code).strip().lower().replace("_", "-")
    if c in _ALIASES:
        return _ALIASES[c]
    if not _CODE.match(c):
        return None
    base = c.split("-")[0]
    return _ALIASES.get(base, base)


def _read(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and not str(k).startswith("_")}


def _load_all(directory: Optional[Path] = None) -> None:
    global _loaded_dir
    directory = Path(directory) if directory is not None else LOCALES_DIR
    with _lock:
        if _loaded_dir == directory and _catalogs:
            return
        _catalogs.clear()
        if directory.is_dir():
            for f in sorted(directory.glob("*.json")):
                code = normalize_language(f.stem)
                if code:
                    _catalogs[code] = _read(f)
        _loaded_dir = directory


def reload(directory: Optional[Path] = None) -> None:
    """Re-read the catalogs (tests, or after editing a file)."""
    global _loaded_dir
    with _lock:
        _loaded_dir = None
    _load_all(directory)


def available_languages() -> List[str]:
    """Codes that have a catalog, English first, the rest alphabetical."""
    _load_all()
    codes = sorted(c for c in _catalogs if c != DEFAULT_LANGUAGE)
    return ([DEFAULT_LANGUAGE] if DEFAULT_LANGUAGE in _catalogs else []) + codes


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def english_language_name(code: str) -> str:
    return _ENGLISH_NAMES.get(code, code)


def is_rtl(code: Optional[str]) -> bool:
    return (code or "") in RTL_LANGUAGES


def catalog(code: str) -> Dict[str, str]:
    _load_all()
    return dict(_catalogs.get(code, {}))


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # an unknown placeholder stays visible instead of raising
        return "{" + key + "}"


def t(key: str, lang: Optional[str] = None, **values: object) -> str:
    """Translate ``key`` into ``lang`` (fallback: English, then the key).  ``{placeholders}`` are filled from ``values``."""
    _load_all()
    code = normalize_language(lang) or DEFAULT_LANGUAGE
    text = _catalogs.get(code, {}).get(key)
    if text is None and code != DEFAULT_LANGUAGE:
        text = _catalogs.get(DEFAULT_LANGUAGE, {}).get(key)
    if text is None:
        text = key
    if not values:
        return text
    try:
        return text.format_map(_SafeDict(values))
    except (ValueError, IndexError, KeyError, AttributeError):
        return text  # a malformed catalog entry must never break a reply


def placeholders(text: str) -> FrozenSet[str]:
    return frozenset(_PLACEHOLDER.findall(text))


def resolve_language(chat_language: Optional[str], telegram_code: Optional[str] = None, default: str = DEFAULT_LANGUAGE,
                     allowed: Optional[Iterable[str]] = None) -> str:
    """Explicit chat setting, else the Telegram client language of the user, else ``default`` - limited to ``allowed`` and the catalogs."""
    catalogs = set(available_languages())
    have = catalogs if allowed is None else ({normalize_language(a) for a in allowed} - {None}) & catalogs
    for candidate in (chat_language, telegram_code):
        code = normalize_language(candidate)
        if code and code in have:
            return code
    fallback = normalize_language(default) or DEFAULT_LANGUAGE
    if fallback in have or not have:
        return fallback
    return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in have else sorted(have)[0]


_SCRIPTS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("hi", re.compile(r"[\u0900-\u097F]")),
    ("ar", re.compile(r"[\u0600-\u06FF\u0750-\u077F]")),
    ("zh", re.compile(r"[\u4E00-\u9FFF]")),
    ("ru", re.compile(r"[\u0400-\u04FF]")),
)
_JA_KANA = re.compile(r"[\u3040-\u30FF]")
_KO_HANGUL = re.compile(r"[\uAC00-\uD7AF]")


def detect_script_language(text: Optional[str]) -> Optional[str]:
    """Language implied by the *script* of ``text`` when the script identifies it (Devanagari, Arabic, Han, Cyrillic); else None.

    Latin text is deliberately not guessed (English / Spanish / Hinglish / Indonesian share it).  Japanese kana and Korean hangul
    return None: they are not supported UI languages, but a Han-only guess would mislabel Japanese as Chinese.
    """
    if not text:
        return None
    sample = text[:400]
    if _JA_KANA.search(sample) or _KO_HANGUL.search(sample):
        return None
    counts = [(len(rx.findall(sample)), code) for code, rx in _SCRIPTS]
    best, code = max(counts)
    letters = sum(1 for ch in sample if ch.isalpha()) or 1
    return code if best >= 2 and best / letters >= 0.3 else None


def language_directive(lang: Optional[str]) -> str:
    """One instruction line for an LLM system prompt; empty for English/unknown (the models already default to the user's language)."""
    code = normalize_language(lang)
    if not code or code == DEFAULT_LANGUAGE or code not in _ENGLISH_NAMES:  # unknown code: say nothing rather than "Reply in zz"
        return ""
    return (f"Reply in {english_language_name(code)} unless the user clearly writes in a different language, "
            f"in which case match theirs. Keep names, commands and code unchanged.")
