"""Candidate scoring: context, persona, linguistic, diversity, naturalness, evidence (standard library only)."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .core import (
    Situation, content_tokens, length_bucket, norm, reply_act, surface, words,
)
from .profile import BehaviorProfile, ResponsePlan
from .safety import hard_gates, looks_like_list_or_markup

_ASSISTANT_ISMS = re.compile(
    r"\b(?:sure[,!]|certainly|absolutely[,!]|of course[,!]|great question|i'?d be happy|happy to help|here(?:'s| is| are)|"
    r"let me know if|feel free|i hope this helps|as for your|in summary|to summarize)\b", re.IGNORECASE)


class PersonaLM:
    """Character n-gram log-likelihood ratio: how much more likely is this text from the person than from the others?"""

    def __init__(self, own: Sequence[str], others: Sequence[str], orders: Sequence[int] = (1, 2, 3)) -> None:
        self.orders = tuple(orders)
        self.own, self.oth = Counter(), Counter()
        for texts, tgt in ((own, self.own), (others, self.oth)):
            for t in texts:
                tgt.update(self._grams(t))
        self.n_own, self.n_oth = sum(self.own.values()) or 1, sum(self.oth.values()) or 1
        self.vocab = len(set(self.own) | set(self.oth)) + 1
        sample = lambda xs: list(xs)[:: max(1, len(xs) // 800)][:800]  # noqa: E731
        so, sb = [self.llr(t) for t in sample(own) if t.strip()], [self.llr(t) for t in sample(others) if t.strip()]
        self.m1 = sum(so) / len(so) if so else 0.1
        self.m0 = sum(sb) / len(sb) if sb else -0.1
        if self.m1 - self.m0 < 1e-6:
            self.m1 = self.m0 + 1e-6

    def _grams(self, text: str) -> List[str]:
        s = f"^{text}$"
        return [s[i:i + n] for n in self.orders for i in range(max(0, len(s) - n + 1))]

    def llr(self, text: str) -> float:
        g = self._grams(text)
        if not g:
            return 0.0
        v = self.vocab
        return sum(math.log((self.own.get(x, 0) + 1) / (self.n_own + v)) - math.log((self.oth.get(x, 0) + 1) / (self.n_oth + v)) for x in g) / len(g)

    def score(self, text: str) -> float:
        """0 = reads like the people around them, 0.8 = as typical as their own messages, 1 = more so."""
        x = (self.llr(text) - self.m0) / (self.m1 - self.m0)
        return max(0.0, min(1.25, x)) / 1.25


@dataclass(frozen=True)
class ScoreWeights:
    context: float = 0.30
    persona: float = 0.25
    linguistic: float = 0.20
    diversity: float = 0.10
    naturalness: float = 0.10
    evidence: float = 0.05

    def total(self) -> float:
        return self.context + self.persona + self.linguistic + self.diversity + self.naturalness + self.evidence


@dataclass
class Scores:
    context: float = 0.0
    persona: float = 0.0
    linguistic: float = 0.0
    diversity: float = 0.0
    naturalness: float = 0.0
    evidence: float = 0.0
    composite: float = 0.0
    gates: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, float]:
        return {k: round(getattr(self, k), 3) for k in ("context", "persona", "linguistic", "diversity", "naturalness", "evidence", "composite")}


class CandidateScorer:
    def __init__(
        self, profile: BehaviorProfile, lm: PersonaLM, own_vocab: FrozenSet[str], other_vocab: FrozenSet[str], *,
        blocked: Iterable[str] = (), names: Iterable[str] = (), max_chars: int = 160, weights: Optional[ScoreWeights] = None,
        assoc: Optional["ReplyAssociation"] = None, assoc_weight: float = 0.35,
    ) -> None:
        self.assoc, self.assoc_weight = assoc, assoc_weight
        self.profile, self.lm = profile, lm
        self.own_vocab, self.other_vocab = own_vocab, other_vocab
        self.blocked, self.names, self.max_chars = tuple(blocked), tuple(names), max_chars
        self.weights = weights or ScoreWeights()

    def gates(self, text: str, evidence_replies: Sequence[str] = ()) -> List[str]:
        out = hard_gates(text, blocked=self.blocked, names=self.names, max_chars=self.max_chars)
        n = norm(text)
        tw = set(words(text))
        if len(tw) >= 4:
            for r in evidence_replies:
                rw = set(words(r))
                if norm(r) == n or (rw and len(tw & rw) / len(tw | rw) >= 0.85):
                    out.append("copies_evidence")
                    break
        return out

    # components ----------------------------------------------------------------
    def context(self, text: str, sit: Situation, trigger_tokens: FrozenSet[str], acct: Optional[str]) -> float:
        dist = self.profile.reply_act_dist(sit, acct)
        ra = reply_act(text)
        a = dist.get(ra, 0.0) / (max(dist.values()) or 1.0)
        toks = content_tokens(text)
        overlap = bool(toks & trigger_tokens)
        short_ack = len(text.split()) <= 3
        b = 1.0 if (sit.family in ("social", "reaction", "ack") or overlap or short_ack or not trigger_tokens) else 0.7
        tw, trw = set(words(text)), set(trigger_tokens)
        parrot = bool(trw) and len(tw) >= 3 and len(tw & trw) / len(tw | trw) >= 0.8
        base = 0.7 * a + 0.2 * b + 0.1 * (0.3 if parrot else 1.0)
        if self.assoc is not None:
            return (1 - self.assoc_weight) * base + self.assoc_weight * self.assoc.score(trigger_tokens, text)
        return base

    def linguistic(self, text: str, plan: ResponsePlan) -> float:
        sf = surface(text)
        db = abs(length_bucket(len(text.split())) - plan.length_bucket) if plan.kind == "text" else (0 if not any(c.isalnum() for c in text) else 3)
        length = (1.0, 0.65, 0.3, 0.0)[min(db, 3)]
        emoji = 1.0 if sf["emoji"] == plan.emoji else (0.5 if plan.emoji else 0.3)
        lower = 1.0 if sf["all_lower"] == plan.lower else 0.6
        endp = 1.0 if (not sf["no_end_punct"]) == plan.end_punct else 0.6
        clean = 0.0 if looks_like_list_or_markup(text) else 1.0
        return 0.35 * length + 0.20 * emoji + 0.15 * lower + 0.15 * endp + 0.15 * clean

    def naturalness(self, text: str) -> float:
        s = 1.0
        if len(text) > self.max_chars:
            s -= 0.5
        if _ASSISTANT_ISMS.search(text):
            s -= 0.4
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
        if len(sentences) > 2:
            s -= 0.3
        if re.search(r"(.)\1{12,}", text):
            s -= 0.3
        return max(0.0, s)

    def evidence(self, text: str, trigger_tokens: FrozenSet[str]) -> float:
        ws = [w for w in words(text) if not w.isdigit()]
        if not ws:
            return 1.0
        ok = sum(1 for w in ws if w in self.own_vocab or w in self.other_vocab or w in trigger_tokens)
        return ok / len(ws)

    def score(
        self, text: str, sit: Situation, plan: ResponsePlan, trigger_tokens: FrozenSet[str], repetition_penalty: float,
        acct: Optional[str] = None, evidence_replies: Sequence[str] = (),
    ) -> Scores:
        w = self.weights
        sc = Scores(
            context=self.context(text, sit, trigger_tokens, acct), persona=self.lm.score(text),
            linguistic=self.linguistic(text, plan), diversity=1.0 - repetition_penalty,
            naturalness=self.naturalness(text), evidence=self.evidence(text, trigger_tokens),
            gates=self.gates(text, evidence_replies),
        )
        sc.composite = (w.context * sc.context + w.persona * sc.persona + w.linguistic * sc.linguistic
                        + w.diversity * sc.diversity + w.naturalness * sc.naturalness + w.evidence * sc.evidence) / (w.total() or 1.0)
        return sc


class ReplyAssociation:
    """How strongly a reply's words go with the words of the message it answers, learned from the person's own pairs.

    Score = mean over reply words of the best positive pointwise mutual information with any trigger word, squashed to 0..1.
    It captures regularities such as "what are you doing" -> "nothing much" without any language model.  Sparse by
    nature (short messages), so it is only one signal among several and is shrunk toward neutral when evidence is thin.
    """

    def __init__(self, pairs: Sequence[Tuple[str, str]], alpha: float = 0.5, min_pair: int = 2) -> None:
        from .core import content_tokens, words as _w
        self._ct, self._w = content_tokens, _w
        self.alpha, self.min_pair = alpha, min_pair
        self.pair: Counter = Counter()
        self.t_cnt: Counter = Counter()
        self.r_cnt: Counter = Counter()
        self.n = 0
        for trig, rep in pairs:
            tt, rr = self._ct(trig), set(self._w(rep))
            if not tt or not rr:
                continue
            self.n += 1
            for t in tt:
                self.t_cnt[t] += 1
            for r in rr:
                self.r_cnt[r] += 1
            for t in tt:
                for r in rr:
                    self.pair[(t, r)] += 1

    def pmi(self, t: str, r: str) -> float:
        c = self.pair.get((t, r), 0)
        if c < self.min_pair:
            return 0.0
        p_r = (self.r_cnt[r] + self.alpha) / (self.n + self.alpha * 2)
        p_r_t = (c + self.alpha * p_r) / (self.t_cnt[t] + self.alpha)
        return max(0.0, math.log(p_r_t / p_r))

    def score(self, trigger_tokens: Iterable[str], reply: str) -> float:
        rr = [r for r in set(self._w(reply))]
        tt = list(trigger_tokens)
        if not rr or not tt or self.n == 0:
            return 0.5
        best = [max((self.pmi(t, r) for t in tt), default=0.0) for r in rr]
        raw = sum(best) / len(best)
        return 0.5 + 0.5 * math.tanh(raw / 1.5)        # 0.5 = no association evidence, ->1 = strongly associated
