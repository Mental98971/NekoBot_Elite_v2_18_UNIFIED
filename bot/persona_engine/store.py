"""Evidence store and selective retrieval (standard library only).

Retrieval answers "in situations like this one, how did the person actually behave?".  It matches on the
message the person REPLIED TO (the trigger), not on their reply, scores several independent signals, then
selects a small diverse set.  The chat an example came from is only a soft signal whose weight is set by
what the behaviour profile found to be genuinely context-dependent; nothing is filtered out by chat.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from ._compat import persona_text as pt
from .core import (
    Situation, analyze, char_vec, content_tokens, cosine, norm, reply_act, sit_similarity,
)


@dataclass
class Example:
    id: str
    person: str
    acct: Optional[str]
    chat: str
    conv: str
    ts: Optional[str]
    reply: str
    kind: str                       # text | emoji_only | media | command
    trigger: str                    # the message the person responded to ('' for ambient messages)
    ctx: Tuple[Tuple[str, str], ...] = ()
    lat: Optional[float] = None     # seconds since the previous message in the conversation
    safe: bool = True               # may be shown to a model as evidence
    # derived (filled by finalize)
    trigger_sit: Optional[Situation] = None
    reply_act: str = "other"
    n_words: int = 0
    trig_tokens: FrozenSet[str] = frozenset()
    trig_vec: Dict[int, float] = field(default_factory=dict)
    reply_vec: Dict[int, float] = field(default_factory=dict)

    def finalize(self) -> "Example":
        self.n_words = len(self.reply.split())
        self.reply_act = reply_act(self.reply)
        if self.trigger:
            self.trigger_sit = analyze(self.trigger, has_reply_target=True)
            self.trig_tokens = content_tokens(self.trigger)
            self.trig_vec = char_vec(self.trigger)
        self.reply_vec = char_vec(self.reply)
        return self

    @property
    def usable_evidence(self) -> bool:
        return self.safe and bool(self.trigger) and self.kind in ("text", "emoji_only") and bool(self.reply.strip())


# ── loaders ──────────────────────────────────────────────────────────────────
def _blocked_in(text: str, blocked: FrozenSet[str]) -> bool:
    return bool(blocked) and any(t in blocked for t in pt.tokens(text))


def from_corpus(path: Path, person: str, acct_tags: Optional[Dict[str, str]] = None, blocked: Iterable[str] = ()) -> List[Example]:
    """The shipped, scrubbed per-person corpus (data/personality/persons/<id>/corpus.jsonl)."""
    bl = frozenset(blocked)
    tags = acct_tags or {}
    out: List[Example] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ctx_ok = r.get("ctx") and r.get("ctx_kind") not in (None, "structured", "empty")
            trig = r["ctx"] if ctx_ok else ""
            acct = str(r.get("acct")) if r.get("acct") is not None else None
            out.append(Example(
                id=str(r["id"]), person=person, acct=tags.get(acct, acct), chat=str(r.get("chat") or ""), conv="",
                ts=r.get("ts"), reply=str(r.get("text") or ""), kind=str(r.get("kind") or "text"), trigger=trig,
                safe=bool(r.get("rp")) and not _blocked_in(str(r.get("text") or ""), bl),
            ).finalize())
    return out


def from_prepared(directory: Path, splits: Sequence[str] = ("train",), blocked: Iterable[str] = ()) -> List[Example]:
    """Prepared SFT examples (training/prepare_sft.py).  Pass only the splits the evaluation is allowed to see."""
    bl = frozenset(blocked)
    out: List[Example] = []
    d = Path(directory)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    person = manifest["person"]
    for split in splits:
        with (d / f"{split}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                ctx = tuple((str(w), str(t)) for w, t in r.get("ctx") or ())
                rt = r.get("reply_to")
                if rt and rt[1]:
                    trig = str(rt[1])
                else:
                    trig = next((t for w, t in reversed(ctx) if w != "me" and t), "")
                out.append(Example(
                    id=str(r["id"]), person=person, acct=r.get("acct"), chat=str(r.get("chat") or ""), conv=str(r.get("conv") or ""),
                    ts=r.get("ts"), reply=str(r["target"]), kind=str(r.get("tk") or "text"), trigger=trig, ctx=ctx,
                    lat=r.get("lat"), safe=not _blocked_in(str(r["target"]), bl),
                ).finalize())
    return out


# ── store ────────────────────────────────────────────────────────────────────
class ExampleStore:
    def __init__(self, examples: Iterable[Example]) -> None:
        self.examples: List[Example] = list(examples)
        self._inv: Dict[str, List[int]] = defaultdict(list)
        df: Dict[str, int] = defaultdict(int)
        n_trig = 0
        self.by_act: Dict[str, List[int]] = defaultdict(list)
        self.by_family: Dict[str, List[int]] = defaultdict(list)
        for i, e in enumerate(self.examples):
            if not e.usable_evidence:
                continue
            n_trig += 1
            for t in e.trig_tokens:
                self._inv[t].append(i)
                df[t] += 1
            self.by_act[e.trigger_sit.act].append(i)       # type: ignore[union-attr]
            self.by_family[e.trigger_sit.family].append(i)  # type: ignore[union-attr]
        self.n_trigger_examples = n_trig
        self.idf = {t: math.log(1 + max(1, n_trig) / c) for t, c in df.items()}
        self._default_idf = math.log(1 + max(1, n_trig))
        self._tnorm: Dict[int, float] = {}
        for i, e in enumerate(self.examples):
            if e.usable_evidence:
                self._tnorm[i] = math.sqrt(sum(self.idf.get(t, self._default_idf) ** 2 for t in e.trig_tokens)) or 1.0
        # deterministic pseudo-random order so "same-act" pools are not always the same rows
        for pool in (self.by_act, self.by_family):
            for k in pool:
                pool[k].sort(key=lambda i: hashlib.sha1(self.examples[i].id.encode()).hexdigest())

    def __len__(self) -> int:
        return len(self.examples)

    def lexical(self, q_tokens: FrozenSet[str], cap: int = 120) -> Dict[int, float]:
        """tf-idf cosine between the query tokens and each example trigger that shares at least one token."""
        if not q_tokens:
            return {}
        qn = math.sqrt(sum(self.idf.get(t, self._default_idf) ** 2 for t in q_tokens)) or 1.0
        acc: Dict[int, float] = defaultdict(float)
        for t in q_tokens:
            w = self.idf.get(t)
            if w is None:
                continue
            for i in self._inv.get(t, ())[:2000]:
                acc[i] += w * w
        scored = {i: s / (qn * self._tnorm[i]) for i, s in acc.items()}
        top = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]
        return dict(top)


# ── retrieval ────────────────────────────────────────────────────────────────
@dataclass
class RetrievalConfig:
    k: int = 4
    pool_same_act: int = 120
    pool_same_family: int = 40
    lam: float = 0.55                 # MMR trade-off: relevance vs. diversity of the replies selected
    w_lex: float = 0.35
    w_chr: float = 0.20
    w_sit: float = 0.30
    w_ctx: float = 0.05
    w_plan: float = 0.10
    stale_penalty: float = 0.25       # example was shown to a model in this chat very recently
    acct_bonus: float = 0.0           # set from the profile (only if accounts measurably differ)
    chat_bonus: float = 0.0           # set from the profile (only if a chat effect was demonstrated)
    recency_weight: float = 0.03
    recency_halflife_days: float = 540.0
    min_relevance: float = 0.12       # below this an example says nothing about the current situation


@dataclass
class Query:
    text: str
    sit: Situation
    ctx_text: str = ""
    acct: Optional[str] = None
    chat: Optional[str] = None
    plan: Any = None                  # ResponsePlan (profile.py) - used only as a soft preference
    used_ids: FrozenSet[str] = frozenset()
    now_ts: Optional[float] = None


@dataclass
class Evidence:
    example: Example
    score: float
    parts: Dict[str, float]


def _age_days(ts: Optional[str], now: float) -> Optional[float]:
    if not ts:
        return None
    try:
        import calendar
        return max(0.0, (now - calendar.timegm(time.strptime(ts[:19] + "Z", "%Y-%m-%dT%H:%M:%SZ"))) / 86400.0)
    except ValueError:
        return None


class SelectiveRetriever:
    def __init__(self, store: ExampleStore, cfg: Optional[RetrievalConfig] = None) -> None:
        self.store, self.cfg = store, cfg or RetrievalConfig()

    def _candidates(self, q: Query, q_tokens: FrozenSet[str]) -> Dict[int, float]:
        lex = self.store.lexical(q_tokens)
        cand = dict(lex)
        h = int(hashlib.sha1(norm(q.text).encode()).hexdigest()[:8], 16)
        for pool, n in ((self.store.by_act.get(q.sit.act, ()), self.cfg.pool_same_act),
                        (self.store.by_family.get(q.sit.family, ()), self.cfg.pool_same_family)):
            if pool:
                start = h % len(pool)
                for j in range(min(n, len(pool))):
                    cand.setdefault(pool[(start + j) % len(pool)], 0.0)
        return cand

    def retrieve(self, q: Query) -> List[Evidence]:
        c = self.cfg
        now = q.now_ts if q.now_ts is not None else time.time()
        q_tokens = content_tokens(q.text)
        q_vec = char_vec(q.text)
        ctx_tokens = content_tokens(q.ctx_text) if q.ctx_text else frozenset()
        scored: List[Evidence] = []
        for i, lex in self._candidates(q, q_tokens).items():
            e = self.store.examples[i]
            if not e.usable_evidence:
                continue
            sit = sit_similarity(q.sit, e.trigger_sit)  # type: ignore[arg-type]
            chr_ = cosine(q_vec, e.trig_vec)
            ctxo = (len(ctx_tokens & e.trig_tokens) / max(1, len(ctx_tokens | e.trig_tokens))) if ctx_tokens else 0.0
            rel = c.w_lex * lex + c.w_chr * chr_ + c.w_sit * sit + c.w_ctx * ctxo
            if rel < c.min_relevance:
                continue
            parts = {"lex": lex, "chr": chr_, "sit": sit, "ctx": ctxo}
            plan_fit = 0.0
            if q.plan is not None:
                db = abs(_bucket(e.n_words) - q.plan.length_bucket)
                plan_fit = (0.6 if db == 0 else 0.3 if db == 1 else 0.0) + (0.4 if e.reply_act == q.plan.reply_act else 0.0)
                rel += c.w_plan * plan_fit
            parts["plan"] = plan_fit
            bonus = 0.0
            if c.acct_bonus and q.acct and e.acct == q.acct:
                bonus += c.acct_bonus
            if c.chat_bonus and q.chat and e.chat == q.chat:
                bonus += c.chat_bonus
            age = _age_days(e.ts, now)
            if c.recency_weight and age is not None:
                bonus += c.recency_weight * math.exp(-age * math.log(2) / c.recency_halflife_days)
            pen = c.stale_penalty if e.id in q.used_ids else 0.0
            parts.update(bonus=bonus, stale=pen)
            scored.append(Evidence(e, rel + bonus - pen, parts))
        scored.sort(key=lambda ev: (-ev.score, ev.example.id))
        return self._select(scored[:80])

    def _select(self, ranked: List[Evidence]) -> List[Evidence]:
        """Greedy MMR on reply similarity; identical replies are never selected twice."""
        chosen: List[Evidence] = []
        seen_norm: set = set()
        pool = list(ranked)
        while pool and len(chosen) < self.cfg.k:
            best, best_val = None, -1e9
            for ev in pool:
                n = norm(ev.example.reply)
                if n in seen_norm:
                    continue
                sim = 0.0
                for s in chosen:
                    v = cosine(ev.example.reply_vec, s.example.reply_vec)
                    if ev.example.reply_act == s.example.reply_act and _bucket(ev.example.n_words) == _bucket(s.example.n_words):
                        v = max(v, 0.3)
                    sim = max(sim, v)
                val = ev.score - self.cfg.lam * sim
                if val > best_val:
                    best, best_val = ev, val
            if best is None:
                break
            chosen.append(best)
            seen_norm.add(norm(best.example.reply))
            pool.remove(best)
        return chosen


def _bucket(n_words: int) -> int:
    from .core import length_bucket
    return length_bucket(n_words)
