"""Shared types, deterministic text features and situation analysis (standard library only)."""
from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Sequence, Tuple

from ._compat import persona_text as pt

# Provenance of every piece of text the engine handles (never mix them - see docs/PERSONA_ENGINE.md)
HISTORICAL_EVIDENCE = "HISTORICAL_EVIDENCE"   # something the real person actually wrote
BOT_MEMORY = "BOT_MEMORY"                     # facts stored during the bot's own interactions
CURRENT_CONTEXT = "CURRENT_CONTEXT"           # the live conversation
GENERATED_CONTENT = "GENERATED_CONTENT"       # something the bot wrote

LENGTH_BINS = (0, 1, 2, 3, 4, 6, 9, 14, 24, 10**9)   # upper-exclusive word counts
N_LEN = len(LENGTH_BINS) - 1

_RE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2764\u2B06\u2B07\u2705\u274C\u2728\u2639\u263A]")
_RE_DEVANAGARI = re.compile("[\u0900-\u097F]")
_RE_ELONG = re.compile(r"([A-Za-z])\1{2,}")
_RE_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
_RE_CAPS_WORD = re.compile(r"\b[A-Z]{3,}\b")
_RE_REPEAT_PUNCT = re.compile(r"([!?.])\1{1,}")
HINGLISH_MARKERS = frozenset(
    "hai hain nahi nhi ni kya kyu kyun kaise kab kon kaun mai main mei me meko mujhe mera meri "
    "tera teri tu tum tha thi the ye yeh wo woh ka ki ke ko se bhi aur ya toh to hu hoon ho raha "
    "rahi rha rhi kr kar karo krna pta pata bs bas abhi kuch koi sab bhut bahut accha acha haan "
    "han ha nai nahin yaar bhai behen".split()
)
STOPWORDS = frozenset(
    "the a an and or but if is are was were be been am i you he she it we they me my your his her our their this that "
    "of to in on at for with from by as so do does did not no yes ok hai hain ho hu ka ki ke ko se me mai main tu tum ye yeh wo woh "
    "kya aur bhi toh to na".split()
)

_LEX_WH = re.compile(r"\b(why|what|how|who|when|where|which|kyu|kyun|kya|kaun|kon|kab|kaise|kahan|kaha|kidhar|kitna|kitne|konsa|kaunsa)\b", re.I)
_LEX_REQUEST = re.compile(r"\b(please|pls|plz|send|share|tell|show|give|bata|batao|bhej|bhejo|dikha|dikhao|de\s+do|kar\s+do|karo|help|chahiye|do\s+na)\b", re.I)
_LEX_YN_START = re.compile(
    r"^\s*(?:(?:are|is|am|was|were)\s+(?:you|u|ya|we|they|he|she|it|i|there|anyone|everyone|this|that)"
    r"|(?:do|does|did|can|could|will|would|should|have|has|shall|may)\s+(?:you|u|ya|we|they|he|she|i|there|anyone|everyone))\b", re.I)
_FIRST_AGREE = frozenset("haan han yes yeah yep yup ok okay sure hmm hmmm correct true same".split())
_FIRST_DISAGREE = frozenset("nahi nhi no nope nah na nai never".split())
_LEX_UNCERTAIN = re.compile(r"\b(maybe|idk|dunno|not\s+sure|shayad|pata\s+nahi|pta\s+nhi|kya\s+pata|probably|i\s+guess)\b", re.I)
_LEX_NEGATION = re.compile(r"\b(no|not|never|nahi|nhi|nope|nah|mat|na)\b", re.I)
_LEX_APOLOGY = re.compile(r"\b(sorry|sry|my\s+bad|maaf|maafi)\b", re.I)
_LEX_LAUGH = re.compile(r"\b(lol|lmao|lmfao|rofl|haha+|hehe+|kek|xd)\b|😂|🤣", re.I)

ACT_FAMILY = {
    "greeting": "social", "goodbye": "social", "thanks": "social", "apology": "social",
    "laughter": "reaction", "emoji_only": "reaction", "agree": "ack", "disagree": "ack",
    "question_yn": "question", "question_wh": "question", "request": "question",
    "statement": "statement", "media": "other", "structured": "other", "empty": "other",
}
REPLY_ACTS = ("agree", "disagree", "laughter", "question", "greeting", "goodbye", "thanks", "other")


# ── tokens / features ────────────────────────────────────────────────────────
def words(text: str) -> List[str]:
    return _RE_WORD.findall((text or "").casefold())


def content_tokens(text: str) -> FrozenSet[str]:
    return frozenset(w for w in words(text) if len(w) > 1 and w not in STOPWORDS and not w.isdigit())


def norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def length_bucket(n_words: int) -> int:
    for i in range(N_LEN):
        if LENGTH_BINS[i] <= n_words < LENGTH_BINS[i + 1]:
            return i
    return N_LEN - 1


def script_of(text: str) -> str:
    t = text or ""
    dev = len(_RE_DEVANAGARI.findall(t))
    lat = sum(1 for ch in t if ch.isalpha() and ch.isascii())
    if dev and lat:
        return "mixed"
    if dev:
        return "devanagari"
    if lat:
        return "latin"
    return "emoji" if _RE_EMOJI.search(t) else "none"


def hinglish_share(text: str) -> float:
    ws = words(text)
    if not ws:
        return 0.0
    return sum(1 for w in ws if w in HINGLISH_MARKERS) / len(ws)


def emojis(text: str) -> List[str]:
    return _RE_EMOJI.findall(text or "")


def surface(text: str) -> Dict[str, bool]:
    """Mechanical surface habits of one message (the same measurements the persona builder uses)."""
    s = (text or "").strip()
    first = next((ch for ch in s if ch.isalpha()), "")
    letters = [ch for ch in s if ch.isalpha()]
    return {
        "emoji": bool(_RE_EMOJI.search(s)),
        "question": "?" in s,
        "exclaim": "!" in s,
        "ellipsis": "..." in s or "\u2026" in s,
        "lower_start": bool(first) and first.islower(),
        "all_lower": bool(letters) and all(ch.islower() for ch in letters if ch.isascii()) and any(ch.isascii() for ch in letters),
        "elongated": bool(_RE_ELONG.search(s)),
        "no_end_punct": bool(s) and s[-1].isalnum(),
        "devanagari": bool(_RE_DEVANAGARI.search(s)),
        "multiline": "\n" in s,
        "caps_word": bool(_RE_CAPS_WORD.search(s)),
        "repeat_punct": bool(_RE_REPEAT_PUNCT.search(s)),
        "hinglish": hinglish_share(s) >= 0.2,
        "short": len(s.split()) <= 2,
    }


FEATURES: Tuple[str, ...] = tuple(surface("a").keys())


def reply_act(text: str) -> str:
    """Coarse class of a reply (agree / disagree / laughter / question / greeting / goodbye / thanks / other)."""
    t = (text or "").strip()
    if t and not any(ch.isalnum() for ch in t) and _RE_EMOJI.search(t):
        return "laughter" if _LEX_LAUGH.search(t) else "other"
    c = pt.classify_utterance(t)
    return c if c in REPLY_ACTS else "other"


# ── hashed character n-gram vectors (typo / transliteration tolerant similarity) ─
CHAR_DIM = 4096


def char_vec(text: str, n: int = 3) -> Dict[int, float]:
    s = f"^{norm(text)}$"
    counts: Dict[int, int] = {}
    for i in range(max(0, len(s) - n + 1)):
        h = zlib.crc32(s[i:i + n].encode("utf-8")) % CHAR_DIM
        counts[h] = counts.get(h, 0) + 1
    if not counts:
        return {}
    norm_ = math.sqrt(sum(v * v for v in counts.values()))
    return {k: v / norm_ for k, v in counts.items()}


def cosine(a: Dict[int, float], b: Dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


# ── situation ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Situation:
    """What the person is being asked to react to (observable features only)."""
    act: str
    family: str
    n_words: int
    length_bucket: int
    script: str
    hinglish: float
    has_reply_target: bool
    cues: Tuple[str, ...] = ()

    def key(self) -> str:
        return self.act


def analyze(text: str, *, has_reply_target: bool = False) -> Situation:
    """Dialogue act + mechanical cues of a message.  Deterministic; no model involved."""
    t = (text or "").strip()
    n = len(t.split())
    base = pt.classify_context(t)
    if base == "empty":
        act = "empty"
    elif base == "structured":
        act = "structured"
    elif base == "media":
        act = "media"
    elif not any(ch.isalnum() for ch in t) and _RE_EMOJI.search(t):
        act = "emoji_only"
    elif base in ("greeting", "goodbye", "thanks", "laughter"):
        act = base
    elif _LEX_APOLOGY.search(t) and n <= 8:
        act = "apology"
    else:
        u = pt.classify_utterance(t)
        first = (words(t) or [""])[0]
        if u in ("agree", "disagree"):
            act = u
        elif n <= 3 and first in _FIRST_DISAGREE and not t.endswith("?"):
            act = "disagree"
        elif n <= 3 and first in _FIRST_AGREE and not t.endswith("?"):
            act = "agree"
        elif base != "question" and n <= 14 and _LEX_YN_START.match(t):
            act = "question_yn"
        elif base == "question":
            act = "question_wh" if _LEX_WH.search(t) else "question_yn"
        elif _LEX_REQUEST.search(t):
            act = "request"
        else:
            act = "statement"
    cues: List[str] = []
    if "!" in t:
        cues.append("exclaim")
    if _RE_CAPS_WORD.search(t):
        cues.append("caps")
    if _LEX_LAUGH.search(t):
        cues.append("laugh")
    if _RE_EMOJI.search(t):
        cues.append("emoji")
    if _RE_ELONG.search(t):
        cues.append("elongation")
    if _LEX_UNCERTAIN.search(t):
        cues.append("uncertainty")
    if _LEX_NEGATION.search(t):
        cues.append("negation")
    if _LEX_APOLOGY.search(t):
        cues.append("apology")
    return Situation(act, ACT_FAMILY.get(act, "other"), n, length_bucket(n), script_of(t), round(hinglish_share(t), 3),
                     bool(has_reply_target), tuple(cues))


def sit_similarity(a: Situation, b: Situation) -> float:
    """0..1 closeness of two situations (act 0.5, reply structure 0.15, length 0.15, script 0.1, language mix 0.1)."""
    if a.act == b.act:
        s = 0.5
    elif a.family == b.family and a.family not in ("other",):
        s = 0.25
    else:
        s = 0.0
    s += 0.15 if a.has_reply_target == b.has_reply_target else 0.0
    s += 0.15 * max(0.0, 1.0 - abs(a.length_bucket - b.length_bucket) / 3.0)
    s += 0.10 if a.script == b.script else 0.0
    s += 0.10 * max(0.0, 1.0 - abs(a.hinglish - b.hinglish) / 0.4)
    return s


def smooth(counts: Sequence[float], prior: Sequence[float], strength: float) -> List[float]:
    """Dirichlet shrinkage of ``counts`` toward ``prior`` (a probability vector) with pseudo-count ``strength``."""
    total = float(sum(counts))
    out = [(c + strength * p) / (total + strength) for c, p in zip(counts, prior)]
    z = sum(out) or 1.0
    return [x / z for x in out]
