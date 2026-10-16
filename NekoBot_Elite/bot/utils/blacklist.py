"""Word-blacklist matching (v2.12; ported in spirit from FallenRobot's blacklist module).

Matching is on *whole words / phrases*, never substrings (blacklisting "ass" must not hit "class"),
case-insensitively, after Unicode NFKC normalisation and removal of zero-width characters so that
"ｂａｄ" (full-width) or "b\u200bad" cannot slip past. ``*`` inside an entry matches any run of
word characters ("spam*" -> spam, spammer, spamming). Entries are always ``re.escape``d, so an
admin cannot create a pathological regular expression.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from typing import Iterable, List, Optional, Pattern, Sequence, Tuple

MAX_ENTRIES = 200
MAX_ENTRY_LEN = 64
MAX_WILDCARDS = 2

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"))


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).casefold()


def clean_entry(raw: str) -> Optional[str]:
    """Normalised entry, or ``None`` when it is empty, too long or wildcard-only."""
    entry = " ".join(normalize(raw).split())
    if not entry or len(entry) > MAX_ENTRY_LEN:
        return None
    if entry.count("*") > MAX_WILDCARDS or not entry.replace("*", "").strip():
        return None
    return entry


def split_entries(blob: str) -> List[str]:
    """One entry per line; a single line is one phrase (spaces allowed). De-duplicated, order kept."""
    seen, out = set(), []
    for line in (blob or "").splitlines():
        entry = clean_entry(line)
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def _entry_pattern(entry: str) -> str:
    tokens = entry.split(" ")
    return r"\s+".join(r"\w*".join(re.escape(part) for part in token.split("*")) for token in tokens)


@functools.lru_cache(maxsize=512)
def _entry_regex(entry: str) -> Pattern[str]:
    return re.compile(r"(?<!\w)" + _entry_pattern(entry) + r"(?!\w)", re.UNICODE)


@functools.lru_cache(maxsize=256)
def _combined_regex(entries: Tuple[str, ...]) -> Optional[Pattern[str]]:
    if not entries:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(_entry_pattern(e) for e in entries) + r")(?!\w)", re.UNICODE)


def find_match(text: str, entries: Iterable[str]) -> Optional[str]:
    """The blacklist entry that ``text`` violates, or ``None``. One combined regex answers the common
    (clean) case; only a hit pays for the per-entry search that names the entry."""
    cleaned: Sequence[str] = tuple(e for e in entries if isinstance(e, str) and e)
    pattern = _combined_regex(cleaned)
    if pattern is None:
        return None
    haystack = normalize(text)
    if not pattern.search(haystack):
        return None
    for entry in cleaned:
        if _entry_regex(entry).search(haystack):
            return entry
    return None
