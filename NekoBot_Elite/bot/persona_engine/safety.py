"""Output cleaning and hard safety gates for generated replies (standard library only)."""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .identity import claims_to_be, mentions_ai_internals

RE_CONTACT = re.compile(
    r"(?:https?://|www\.|t\.me/)\S+"
    r"|[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+"
    r"|(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"
    r"|\d{6,}"
    r"|@[A-Za-z0-9_]{3,}"
)
RE_PLACEHOLDER = re.compile(r"<(?:url|user|num|email)>|@user\b")
_RE_MD = re.compile(r"(\*\*|__|`+)")
_RE_TOKEN = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", re.UNICODE)
_RE_SPECIAL = re.compile(r"<\|[^|>]{1,40}\|>|</?s>|\[/?INST\]")
_RE_LABEL = re.compile(r"^\s*(?:me|persona|reply|answer|message|response|bot)\s*[:\uff1a-]\s*", re.IGNORECASE)
_RE_LIST = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+")


def _strip_labels(t: str, names: Iterable[str]) -> str:
    """Remove a leading ``Name:`` / ``Me:`` / ``Reply:`` style label."""
    for nm in names:
        if nm:
            t = re.sub(rf"^\s*{re.escape(nm)}\s*[:\uff1a-]\s*", "", t, flags=re.IGNORECASE)
    return _RE_LABEL.sub("", t)


def clean_text(raw: Optional[str], max_chars: int, names: Iterable[str] = ()) -> str:
    """One chat message: first paragraph, no label/quotes/markdown, bounded length.  May return ''."""
    t = _RE_SPECIAL.sub("", raw or "").strip()
    if not t:
        return ""
    t = t.split("\n\n")[0].strip()
    t = _strip_labels(t, names)
    t = _RE_MD.sub("", t).strip()
    if len(t) >= 2 and t[0] in "\"'\u201c\u2018" and t[-1] in "\"'\u201d\u2019":
        t = _strip_labels(t[1:-1].strip(), names)  # a label can also sit inside the quotation marks
    if len(t) > max_chars:
        cut = t[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: stop + 1] if stop >= max_chars // 3 else (cut.rsplit(" ", 1)[0] if " " in cut else cut)
    return t.strip()


def has_contact_data(text: str) -> bool:
    return bool(RE_CONTACT.search(text or "")) or bool(RE_PLACEHOLDER.search(text or ""))


def has_blocked_token(text: str, blocked: Iterable[str]) -> bool:
    b = {x.lower() for x in blocked}
    return bool(b) and any(t.lower() in b for t in _RE_TOKEN.findall(text or ""))


def looks_like_list_or_markup(text: str) -> bool:
    return bool(_RE_LIST.search(text or "")) or "```" in (text or "") or bool(re.search(r"\[[^\]]+\]\([^)]+\)", text or ""))


def hard_gates(text: str, *, blocked: Iterable[str], names: Iterable[str], max_chars: int) -> List[str]:
    """Reasons a reply must never be sent, whatever its score."""
    out: List[str] = []
    if not (text or "").strip():
        return ["empty"]
    if has_contact_data(text):
        out.append("contact_data")
    if has_blocked_token(text, blocked):
        out.append("blocked_token")
    if claims_to_be(text, names):
        out.append("claims_real_person")
    if mentions_ai_internals(text):
        out.append("ai_talk")
    if len(text) > max_chars:
        out.append("too_long")
    return out
