"""Text normalisation and near-duplicate detection (standard library only)."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, FrozenSet, Iterable, List, Set

_RE_WORD = re.compile(r"\w+", re.UNICODE)
_RE_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _RE_WS.sub(" ", (text or "").strip().casefold())


def words(text: str) -> List[str]:
    return _RE_WORD.findall((text or "").casefold())


def shingles(tokens: List[str], k: int = 3) -> FrozenSet[int]:
    if len(tokens) < k:
        return frozenset({hash(tuple(tokens))}) if tokens else frozenset()
    return frozenset(hash(tuple(tokens[i:i + k])) for i in range(len(tokens) - k + 1))


def jaccard(a: FrozenSet[int], b: FrozenSet[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class NearDupIndex:
    """Exact (normalised) and near-duplicate lookup over a reference set of texts.

    Texts with at least ``min_tokens`` words are compared by Jaccard similarity of word 3-gram
    shingles through an inverted index; shorter texts only by exact normalised match (a one-word
    reply being "similar" to another one-word reply carries no leakage information).
    """

    def __init__(self, texts: Iterable[str], threshold: float = 0.8, min_tokens: int = 8, k: int = 3) -> None:
        self.threshold, self.min_tokens, self.k = threshold, min_tokens, k
        self._exact: Set[str] = set()
        self._sets: List[FrozenSet[int]] = []
        self._inv: Dict[int, List[int]] = defaultdict(list)
        for t in texts:
            self.add(t)

    def add(self, text: str) -> None:
        n = normalize(text)
        if not n:
            return
        self._exact.add(n)
        toks = words(text)
        if len(toks) >= self.min_tokens:
            sh = shingles(toks, self.k)
            idx = len(self._sets)
            self._sets.append(sh)
            for s in sh:
                self._inv[s].append(idx)

    def exact(self, text: str) -> bool:
        return normalize(text) in self._exact

    def near(self, text: str) -> bool:
        """True if ``text`` is (normalised-)identical to, or >= threshold similar to, an indexed text."""
        if self.exact(text):
            return True
        toks = words(text)
        if len(toks) < self.min_tokens:
            return False
        sh = shingles(toks, self.k)
        hits: Dict[int, int] = defaultdict(int)
        for s in sh:
            for idx in self._inv.get(s, ()):
                hits[idx] += 1
        for idx, shared in hits.items():  # every candidate shares >= 1 shingle; verify by exact Jaccard
            other = self._sets[idx]
            if shared / (len(sh) + len(other) - shared) >= self.threshold:
                return True
        return False


def ngrams(tokens: List[str], n: int) -> Set[tuple]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)} if len(tokens) >= n else set()
