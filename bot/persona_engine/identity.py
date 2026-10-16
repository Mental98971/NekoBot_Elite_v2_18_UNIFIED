"""Identity honesty (standard library only): the persona is a bot, never the real person.

Single source of truth for the identity-question detector, the fixed disclosure line and the
"claims to be the real person" check.  ``bot/personality/persona_model.py`` re-exports the first two.
"""
from __future__ import annotations

import re
from typing import Iterable

# English + romanised Hindi.  Deliberately broad: a false positive costs one canned honest line,
# a false negative lets a model answer "yes it's me".
_PROBE_PATTERNS = [
    r"\bare\s+(?:you|u|ya)\s+(?:a\s+|an\s+|the\s+)?(?:real|human|person|bot|robot|ai|fake|actual|really|him|her|them)\b",
    r"\b(?:r|are)\s*(?:u|you)\s+(?:really\s+)?(?:\w+\s+)?(?:bot|ai|real|human|fake)\b",
    r"\bwho\s+is\s+this\s*[?!.]*\s*$",
    r"\bwho\s+am\s+i\s+(?:talking|speaking)\s+(?:to|with)\b",
    r"\bwho\s+(?:are|r)\s+(?:you|u)\b",
    r"\b(?:you|u)\s+(?:a\s+)?(?:bot|robot|ai|chatgpt|gpt|llm)\b",
    r"\b(?:bot|ai|robot|fake|real|asli|nakli)\s+(?:hai|ho|hain|h)\b",
    r"\b(?:tum|tu|aap|apna|aap\s+log)\s+(?:kaun|kon)\b",
    r"\b(?:kaun|kon)\s+(?:ho|hai|h)\s+(?:tum|tu|aap|ye|yeh)\b",
    r"\bkya\s+(?:tum|tu|aap)\s+(?:sach\s*mein\s+|sach\s*me\s+)?(?:real|asli|insaan|human|bot)\b",
    r"\b(?:real|asli)\s+(?:wali|wala)\b",
    r"\bis\s+(?:this|that)\s+(?:really\s+)?(?:you|u)\b",
    r"\bare\s+you\s+(?:really\s+)?(?:the\s+)?(?:actual|real)\b",
    r"\b(?:prove|proof)\s+(?:you|u)\s+(?:are|r)\b",
    r"\b(?:ai|bot|chatgpt|language\s+model)\s*\?",
]
_PROBE_RE = re.compile("|".join(f"(?:{p})" for p in _PROBE_PATTERNS), re.IGNORECASE)


def is_identity_probe(text: str, names: Iterable[str] = ()) -> bool:
    """True if the message sincerely or playfully asks what/who the bot is.

    ``names`` are the imitated person's names/aliases: "are you <name>", "is this <name>" and
    "<name> ho kya" also count.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _PROBE_RE.search(t):
        return True
    alts = [re.escape(n.strip()) for n in names if n and len(n.strip()) >= 3]
    if not alts:
        return False
    nm = "(?:" + "|".join(alts) + ")"
    return bool(re.search(
        rf"\b(?:are\s+(?:you|u|ya)|r\s+u|is\s+this|is\s+that|this\s+is|you\s+are|u\s+are)\s+(?:really\s+|actually\s+|the\s+real\s+)?{nm}\b"
        rf"|\b{nm}\s+(?:ho|hai|h)\s*(?:kya|\?)"
        rf"|\bkya\s+(?:tum|tu|aap)\s+{nm}\b",
        t, re.IGNORECASE,
    ))


def disclosure_reply(real_name: str) -> str:
    return (
        f"I'm a bot that imitates {real_name}'s chat style - I'm not {real_name} "
        f"and nothing I say is written by {real_name}."
    )


_AI_TALK = re.compile(
    r"\b(?:as\s+an?\s+(?:ai|language\s+model|assistant)|i\s*(?:am|'m)\s+(?:just\s+)?an?\s+(?:ai|bot|language\s+model|assistant)|"
    r"language\s+model|system\s+prompt|my\s+(?:instructions|training\s+data)|large\s+language|chatgpt|openai|gemini|llama)\b",
    re.IGNORECASE,
)


def mentions_ai_internals(text: str) -> bool:
    """Spontaneous talk about models/prompts inside an ordinary persona reply (sincere identity questions never reach a model)."""
    return bool(_AI_TALK.search(text or ""))


_CLAIM_NO_NAME = re.compile(
    r"\b(?:it'?s|its|this\s+is)\s+me\b|\byes\s+(?:i\s*am|it'?s\s+me)\b|\b(?:haan\s+)?(?:main|mai)\s+hi\s+(?:hu|hoon|hun)\b",
    re.IGNORECASE,
)


def claims_to_be(text: str, names: Iterable[str]) -> bool:
    """True if ``text`` asserts that the writer *is* the real person (any of ``names``) or says "it's me"."""
    t = text or ""
    if _CLAIM_NO_NAME.search(t):
        return True
    alts = [re.escape(n.strip()) for n in names if n and len(n.strip()) >= 3]
    if not alts:
        return False
    nm = "(?:" + "|".join(alts) + ")(?![\\w]|['\u2019]\\w)"
    return bool(re.search(
        rf"\b(?:i\s*am|i'm|im)\s+(?:the\s+|actual\s+|real\s+|asli\s+)*{nm}"
        rf"|\b(?:this\s+is|it'?s|its)\s+(?:the\s+|actual\s+|real\s+)*{nm}"
        rf"|\b(?:main|mai|mein)\s+(?:hi\s+)?{nm}\s+(?:hi\s+)?(?:hu|hoon|hun)\b"
        rf"|\b(?:real|actual|asli)\s+{nm}"
        rf"|\b{nm}\s+(?:here|hi\s+hu|hu\s+main|hoon)\b",
        t, re.IGNORECASE,
    ))
