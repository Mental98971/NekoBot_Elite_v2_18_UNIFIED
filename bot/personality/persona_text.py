"""Text helpers shared by the person-persona builder and the runtime.

Stdlib only, no bot imports: ``scripts/build_person_personas.py`` loads this file by path, so the
offline build and the live bot classify and scrub text with exactly the same code.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

MEDIA_MARKER = "[media/non-text]"  # compared case-insensitively; stored in this canonical form
RUN_CAP = 12                       # "noooooo…" is capped at this many repeated characters
OWN_MAX_CHARS = 400
CTX_MAX_CHARS = 200

# ── scrubbing ────────────────────────────────────────────────────────────────
_RE_TG_LINK = re.compile(r"\[([^\]]*)\]\(tg://[^)]*\)")
_RE_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:https?://|t\.me/)[^)]*\)")
_RE_TGURI = re.compile(r"\(?tg://\S*?\)?(?=\s|$)|tg://[^\s)]*\)?", re.IGNORECASE)
_RE_URL = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_RE_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
_RE_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_RE_LONGNUM = re.compile(r"\d{6,}")
_RE_HANDLE = re.compile(r"@[A-Za-z0-9_]{3,}")
_RE_BOTCMD = re.compile(r"(/[A-Za-z0-9_]+)@[A-Za-z0-9_]+")
_RE_RUN = re.compile(r"(.)\1{%d,}" % RUN_CAP, re.DOTALL)
_RE_WS = re.compile(r"[ \t]+")

_RE_ALNUM = re.compile(r"\w", re.UNICODE)
_RE_TOKEN = re.compile(r"[a-z']{2,}")


def scrub(text: Any, limit: int) -> str:
    """Remove contact data / identifiers from ``text`` and bound its length."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    s = _RE_TG_LINK.sub("<user>", s)
    s = _RE_MD_LINK.sub(r"\1 <url>", s)
    s = _RE_TGURI.sub("<user>", s)
    s = _RE_URL.sub("<url>", s)
    s = _RE_EMAIL.sub("<email>", s)
    s = _RE_PHONE.sub("<num>", s)
    s = _RE_LONGNUM.sub("<num>", s)
    s = _RE_BOTCMD.sub(r"\1", s)
    s = _RE_HANDLE.sub("@user", s)
    s = _RE_RUN.sub(lambda m: m.group(1) * RUN_CAP, s)
    s = "\n".join(_RE_WS.sub(" ", ln).strip() for ln in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:
        cut = s[:limit]
        if not s[limit].isspace() and " " in cut:  # never end mid-word (would split a placeholder)
            cut = cut.rsplit(" ", 1)[0]
        s = cut.rstrip() + "…"
    return s


# ── classification ───────────────────────────────────────────────────────────
_CLASS_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("greeting", re.compile(r"^\s*(hi+e*|hello+|hey+|yo+|sup|hola|hiya|gm|good\s*morning|morning|namaste)\b", re.I)),
    ("goodbye", re.compile(r"^\s*(bye+|goodbye|gn|good\s*night|cya|see\s*ya|tc|take\s*care|night)\b", re.I)),
    ("thanks", re.compile(r"\b(thanks?|thank\s*you|thankyou|tysm|ty|thnx|shukriya)\b", re.I)),
    ("laughter", re.compile(r"^\s*(?:(?:a?ha)+h?|(?:he)+h?e?|lo+l+|lma+o+|rofl|kek|xd+|😂|🤣)+\W*$", re.I)),
    ("agree", re.compile(r"^\s*(yes+h?|yep|yup|yea+h?|yeah|ya+|ok+|okie|oki|okay|k|sure|fr|mhm+|hnn+|haan?|han|hmm+m*|correct|true|same)\W*$", re.I)),
    ("disagree", re.compile(r"^\s*(no+|nope|nah+|na|nai|nahi|nhi|ni|never)\W*$", re.I)),
]
_RE_QUESTION_START = re.compile(r"^\s*(why|what|how|kya|kon|kaun|kyu|kyun|kab|kaise|who|when|where)\b", re.I)
# romanised-Hindi interrogatives anywhere in the message, or a trailing "kya"/"na"
_RE_QUESTION_ANY = re.compile(r"\b(kya|kon|kaun|kyu|kyun|kab|kaise|kahan|kaha|kidhar|kitna|kitne|konsa|kaunsa)\b|\b(kya|na)\s*$", re.I)
_RE_LAUGH_ANY = re.compile(r"\b(lol|lmao|lmfao|rofl|haha+|hehe+|kek)\b", re.I)


def classify_utterance(text: str) -> str:
    """greeting / goodbye / thanks / laughter / agree / disagree / question / other."""
    t = (text or "").strip()
    if not t:
        return "other"
    for name, pat in _CLASS_PATTERNS:
        if pat.search(t):
            return name
    if t.endswith("?") or _RE_QUESTION_START.match(t) or _RE_QUESTION_ANY.search(t):
        return "question"
    return "other"


def classify_context(text: str) -> str:
    """Class of the message a person replied to (also used for the live user message)."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if t.startswith("/") or "<user>" in t or t.count("\n") >= 3:
        return "structured"
    if t.lower() == MEDIA_MARKER:
        return "media"
    c = classify_utterance(t)
    if c in ("greeting", "goodbye", "thanks", "laughter", "question"):
        return c
    if len(t.split()) <= 4 and _RE_LAUGH_ANY.search(t):
        return "laughter"
    return "statement"


def message_kind(raw_text: str) -> str:
    """media / command / emoji_only / text."""
    t = (raw_text or "").strip()
    if not t or t.lower() == MEDIA_MARKER:
        return "media"
    if t.startswith("/"):
        return "command"
    if not _RE_ALNUM.search(t):
        return "emoji_only"
    return "text"


def norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def tokens(text: str) -> List[str]:
    return _RE_TOKEN.findall((text or "").lower())
