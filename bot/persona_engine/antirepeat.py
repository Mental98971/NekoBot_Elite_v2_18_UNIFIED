"""Repetition control: recent-reply memory, similarity, catchphrase budgets (standard library only)."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .core import char_vec, cosine, emojis, norm, words


def similarity(a: str, b: str) -> float:
    """0..1: exact (normalised) match = 1; otherwise the larger of word-set Jaccard and character-3-gram cosine."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    wa, wb = set(words(a)), set(words(b))
    jac = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return max(jac, cosine(char_vec(a), char_vec(b)))


class RepetitionGuard:
    def __init__(self, window: int = 20, ttl_seconds: float = 6 * 3600, max_chats: int = 500,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.window, self.ttl, self.max_chats, self._clock = window, ttl_seconds, max_chats, clock
        self._replies: "OrderedDict[int, Deque[Tuple[float, str]]]" = OrderedDict()
        self._evidence: Dict[int, Deque[Tuple[float, FrozenSet[str]]]] = {}

    def _touch(self, chat: int) -> Deque[Tuple[float, str]]:
        dq = self._replies.get(chat)
        now = self._clock()
        if dq is None:
            dq = deque(maxlen=self.window)
            self._replies[chat] = dq
            while len(self._replies) > self.max_chats:
                old, _ = self._replies.popitem(last=False)
                self._evidence.pop(old, None)
        else:
            self._replies.move_to_end(chat)
            while dq and now - dq[0][0] > self.ttl:
                dq.popleft()
        return dq

    def remember(self, chat: int, text: str, evidence_ids: Iterable[str] = ()) -> None:
        self._touch(chat).append((self._clock(), text))
        ev = self._evidence.setdefault(chat, deque(maxlen=8))
        ev.append((self._clock(), frozenset(evidence_ids)))

    def recent(self, chat: int, n: Optional[int] = None) -> List[str]:
        r = [t for _ts, t in self._touch(chat)]
        return r[-n:] if n else r

    def used_evidence(self, chat: int) -> FrozenSet[str]:
        now = self._clock()
        out: set = set()
        for ts, ids in self._evidence.get(chat, ()):
            if now - ts <= self.ttl:
                out |= ids
        return frozenset(out)

    def penalty(self, chat: int, text: str, recurring: Sequence[Dict] = ()) -> float:
        """0 = fresh, 1 = a repeat.  The largest of: similarity to a recent reply (recent ones weigh more),
        a repeated opening word, a repeated emoji set, an over-used personal expression."""
        recent = self.recent(chat, 10)
        if not recent:
            return 0.0
        pen = 0.0
        for k, prev in enumerate(reversed(recent)):
            pen = max(pen, similarity(text, prev) * (0.92 ** k))
        ws = words(text)
        if ws:
            last5 = [words(r)[:1] for r in recent[-5:]]
            if sum(1 for f in last5 if f and f[0] == ws[0]) >= 3 and len(ws) > 1:
                pen = max(pen, 0.5)
        em = set(emojis(text))
        if em and em == set(emojis(recent[-1])):
            pen = max(pen, 0.4)
        W = min(len(recent), 10)
        window_text = " ".join(norm(r) for r in recent[-W:])
        for rec in recurring:
            g = rec["gram"]
            if f" {g} " in f" {norm(text)} ":
                expected = rec["rate"] * W
                actual = window_text.count(g)
                if actual >= max(2, math.ceil(2 * expected) + 1):
                    pen = max(pen, 0.6)
        return min(1.0, pen)

    def too_similar(self, chat: int, text: str, threshold: float = 0.85) -> bool:
        return any(similarity(text, p) >= threshold for p in self.recent(chat, 6))

    def diversify(self, chat: int, text: str) -> str:
        """Cheap, meaning-preserving edit: drop an emoji that the previous reply already ended with."""
        recent = self.recent(chat, 1)
        if not recent:
            return text
        last_em = emojis(recent[-1])
        em = emojis(text)
        if em and last_em and em[-1] == last_em[-1] and len(text.split()) > 1:
            trimmed = text.rstrip()
            while trimmed and trimmed[-1] in em[-1] + " ":
                trimmed = trimmed[:-1]
            return trimmed.rstrip() or text
        return text
