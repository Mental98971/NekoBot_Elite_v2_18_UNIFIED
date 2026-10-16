"""Measured behaviour profile of one person + response-plan sampling (standard library only).

Everything here is computed from the person's own messages; nothing is asserted by hand.  A trait is only
reported when it has enough support, and whether a habit is *context-dependent* (differs between chats or
between a person's accounts) is decided by a conversation-level permutation test instead of being assumed.
"""
from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import re

from .core import (
    N_LEN, REPLY_ACTS, STOPWORDS, Situation, content_tokens, emojis, length_bucket, smooth, surface, words,
)

_RE_RUN3 = re.compile(r"(.)\1{2,}")


def _plain_token(tok: str) -> bool:
    """Latin / Devanagari letters and digits only: decorative fonts (small caps, maths alphabets) are almost always
    other people's display names or bot output, never a speaking habit worth conditioning a model on."""
    for ch in tok:
        o = ord(ch)
        if ch.isdigit() or o < 0x250 or 0x900 <= o <= 0x97F or ch in "'\u2019":
            continue
        return False
    return True


def _lex(tokens: Iterable[str]) -> List[str]:
    return [_RE_RUN3.sub(r"\1\1", t) for t in tokens]
from .store import Example

PROFILE_SCHEMA = 1
MIN_CHAT_ROWS = 150             # a chat needs this many messages before its own habit rates are used
SMOOTH_CHAT = 80.0
MIN_ACT_SUPPORT = 12            # fewer examples than this for a situation: lean on the person-wide distribution
SMOOTH_ACT = 8.0
SMOOTH_LEN = 12.0
SMOOTH_SURF = 15.0
SMOOTH_ACCT = 40.0
STYLE_FEATURES = ("emoji", "question", "exclaim", "ellipsis", "lower_start", "all_lower", "elongated", "no_end_punct",
                  "hinglish", "short", "caps_word", "repeat_punct")
TESTED_FEATURES = ("emoji", "question", "lower_start", "elongated", "no_end_punct", "hinglish", "short", "exclaim", "devanagari")


@dataclass(frozen=True)
class ResponsePlan:
    """What kind of reply the person is statistically likely to give in this situation (sampled, not fixed)."""
    reply_act: str
    length_bucket: int
    kind: str                 # text | emoji_only
    emoji: bool
    emoji_pick: Optional[str]
    lower: bool
    end_punct: bool
    question: bool
    elongate: bool

    def length_words(self) -> str:
        from .core import LENGTH_BINS
        lo, hi = LENGTH_BINS[self.length_bucket], LENGTH_BINS[self.length_bucket + 1] - 1
        if self.length_bucket >= len(LENGTH_BINS) - 2:
            return f"{lo}+ words"
        return f"{lo} word" if lo == hi == 1 else (f"{lo}-{hi} words" if lo != hi else f"{lo} words")

    def lines(self) -> List[str]:
        out = [f"kind of reply: {self.reply_act.replace('other', 'statement / other')}",
               f"length: about {self.length_words()}" if self.kind == "text" else "length: emoji only"]
        out.append(("include an emoji" + (f" (for example {self.emoji_pick})" if self.emoji_pick else "")) if self.emoji else "no emoji")
        out.append("all lowercase" if self.lower else "normal capitalisation")
        out.append("end with punctuation" if self.end_punct else "no punctuation at the end")
        if self.question:
            out.append("it may be a short question back")
        if self.elongate:
            out.append("stretch one word (like 'soooo')")
        return out


# ── counting helpers ─────────────────────────────────────────────────────────
class _Tables:
    """Contingency tables for one scope (whole person, or one account)."""

    def __init__(self) -> None:
        self.n = 0
        self.act_given_trigger: Dict[str, Counter] = defaultdict(Counter)
        self.len_given_pair: Dict[str, Counter] = defaultdict(Counter)        # "trigger|reply_act"
        self.len_given_reply: Dict[str, Counter] = defaultdict(Counter)
        self.surf_given_reply: Dict[str, Counter] = defaultdict(Counter)      # reply_act -> feature -> count
        self.n_reply: Counter = Counter()
        self.kind_given_reply: Dict[str, Counter] = defaultdict(Counter)
        self.emoji: Counter = Counter()
        self.surf_all: Counter = Counter()
        self.len_all: Counter = Counter()
        self.act_all: Counter = Counter()

    def add(self, e: Example) -> None:
        ta = e.trigger_sit.act if e.trigger_sit else "ambient"
        ra, lb = e.reply_act, length_bucket(e.n_words)
        sf = surface(e.reply)
        self.n += 1
        self.act_given_trigger[ta][ra] += 1
        self.len_given_pair[f"{ta}|{ra}"][lb] += 1
        self.len_given_reply[ra][lb] += 1
        self.n_reply[ra] += 1
        self.kind_given_reply[ra][e.kind] += 1
        for f in STYLE_FEATURES:
            if sf[f]:
                self.surf_given_reply[ra][f] += 1
                self.surf_all[f] += 1
        self.len_all[lb] += 1
        self.act_all[ra] += 1
        self.emoji.update(emojis(e.reply))

    def to_json(self) -> Dict[str, Any]:
        cj = lambda d: {k: dict(v) for k, v in d.items()}  # noqa: E731
        return {"n": self.n, "act_given_trigger": cj(self.act_given_trigger), "len_given_pair": cj(self.len_given_pair),
                "len_given_reply": cj(self.len_given_reply), "surf_given_reply": cj(self.surf_given_reply),
                "n_reply": dict(self.n_reply), "kind_given_reply": cj(self.kind_given_reply),
                "emoji": dict(self.emoji.most_common(30)), "surf_all": dict(self.surf_all),
                "len_all": {str(k): v for k, v in self.len_all.items()}, "act_all": dict(self.act_all)}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "_Tables":
        t = cls()
        t.n = d["n"]
        for name in ("act_given_trigger", "len_given_pair", "len_given_reply", "surf_given_reply", "kind_given_reply"):
            tgt = getattr(t, name)
            for k, v in d[name].items():
                tgt[k] = Counter({(int(x) if name.startswith("len") else x): c for x, c in v.items()})
        t.n_reply = Counter(d["n_reply"])
        t.emoji = Counter(d["emoji"])
        t.surf_all = Counter(d["surf_all"])
        t.len_all = Counter({int(k): v for k, v in d["len_all"].items()})
        t.act_all = Counter(d["act_all"])
        return t


def _dist(counter: Counter, keys: Sequence[Any]) -> List[float]:
    tot = float(sum(counter.get(k, 0) for k in keys))
    return [counter.get(k, 0) / tot for k in keys] if tot else [1.0 / len(keys)] * len(keys)


def _sample(rng: random.Random, keys: Sequence[Any], probs: Sequence[float]) -> Any:
    x, acc = rng.random(), 0.0
    for k, p in zip(keys, probs):
        acc += p
        if x < acc:
            return k
    return keys[-1]


# ── permutation test (conversation-level) ────────────────────────────────────
def _unit_of(e: Example) -> str:
    return e.conv or f"{e.chat}:{(e.ts or '')[:10]}"


def _cv_gain(labels: Sequence[str], k_u: Sequence[int], n_u: Sequence[int], rng: random.Random, reps: int = 6) -> Tuple[float, float]:
    """Mean per-message log-loss gain of group-specific vs person-wide rates, fitted on one half of the units and scored on the other."""
    def clip(x: float) -> float:
        return max(0.01, min(0.99, x))
    idx = list(range(len(labels)))
    gains: List[float] = []
    for _ in range(reps):
        rng.shuffle(idx)
        half = len(idx) // 2
        fit, held = idx[:half], idx[half:]
        nf = sum(n_u[i] for i in fit)
        pg = clip((sum(k_u[i] for i in fit) + 0.5) / (nf + 1))
        gk: Dict[str, int] = defaultdict(int)
        gn: Dict[str, int] = defaultdict(int)
        for i in fit:
            gk[labels[i]] += k_u[i]
            gn[labels[i]] += n_u[i]
        lg = lc = 0.0
        for i in held:
            k, n = k_u[i], n_u[i]
            g = labels[i]
            pc = clip((gk[g] + SMOOTH_CHAT * pg) / (gn[g] + SMOOTH_CHAT))
            lg -= k * math.log(pg) + (n - k) * math.log(1 - pg)
            lc -= k * math.log(pc) + (n - k) * math.log(1 - pc)
        gains.append((lg - lc) / max(1, sum(n_u[i] for i in held)))
    return sum(gains) / len(gains), sum(1 for g in gains if g > 0) / len(gains)


def context_dependence_test(
    examples: Sequence[Example], group_of, features: Sequence[str] = TESTED_FEATURES, *,
    min_group: int = 60, n_perm: int = 300, seed: int = 0, alpha: float = 0.01,
) -> Dict[str, Any]:
    """Does feature f depend on the group (chat or account)?  Units are conversations, so within-conversation
    clustering cannot masquerade as a chat effect.  A feature is called context-dependent only if the
    permutation p-value is below ``alpha``, the largest group deviation is practically meaningful, AND group-specific rates
    predict held-out conversations better than the person-wide rate (cross-validated)."""
    units: Dict[str, Dict[str, Any]] = {}
    for e in examples:
        if e.kind not in ("text", "emoji_only"):
            continue
        g = group_of(e)
        if g is None:
            continue
        u = units.setdefault(_unit_of(e), {"g": Counter(), "n": 0, "k": Counter()})
        u["g"][g] += 1
        u["n"] += 1
        sf = surface(e.reply)
        for f in features:
            if sf[f]:
                u["k"][f] += 1
    size: Counter = Counter()
    for u in units.values():
        u["group"] = u["g"].most_common(1)[0][0]
        size[u["group"]] += u["n"]
    eligible = {g for g, n in size.items() if n >= min_group}
    out: Dict[str, Any] = {"groups_used": len(eligible), "min_group": min_group, "features": {}}
    ulist = [u for u in units.values() if u["group"] in eligible]
    if len(eligible) < 2 or len(ulist) < 4:
        out["note"] = "fewer than two groups with enough messages; nothing to compare"
        return out
    rng = random.Random(seed)
    labels = [u["group"] for u in ulist]
    n_u = [u["n"] for u in ulist]
    for f in features:
        k_u = [u["k"][f] for u in ulist]
        total_k, total_n = sum(k_u), sum(n_u)
        pbar = total_k / total_n
        if pbar <= 0 or pbar >= 1:
            out["features"][f] = {"rate": round(pbar, 4), "dependent": False, "p": 1.0, "effect": 0.0}
            continue

        def stat(lbls: Sequence[str]) -> Tuple[float, float]:
            gk: Dict[str, int] = defaultdict(int)
            gn: Dict[str, int] = defaultdict(int)
            for lab, k, n in zip(lbls, k_u, n_u):
                gk[lab] += k
                gn[lab] += n
            s = sum(gn[g] * (gk[g] / gn[g] - pbar) ** 2 for g in gn) / (pbar * (1 - pbar))
            eff = max(abs(gk[g] / gn[g] - pbar) for g in gn)
            return s, eff
        obs, effect = stat(labels)
        perm = list(labels)
        ge = 0
        for _ in range(n_perm):
            rng.shuffle(perm)
            if stat(perm)[0] >= obs:
                ge += 1
        p = (1 + ge) / (1 + n_perm)
        rec = {"rate": round(pbar, 4), "p": round(p, 4), "effect": round(effect, 4)}
        significant = p < alpha and effect >= max(0.03, 0.2 * pbar)
        if significant:
            # a real effect must also *predict*: group-specific rates fitted on half the conversations have to beat the
            # person-wide rate on the other half (6 random conversation-level splits, positive in >= 5)
            gain, pos = _cv_gain(labels, k_u, n_u, random.Random(seed * 7919 + 13))
            rec.update(cv_gain=round(gain, 5), cv_positive=round(pos, 2))
            significant = gain > 0 and pos >= 0.8
        rec["dependent"] = bool(significant)
        out["features"][f] = rec
    return out


# ── the profile ──────────────────────────────────────────────────────────────
class BehaviorProfile:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data
        self._all = _Tables.from_json(data["tables"]["all"])
        self._acct = {k: _Tables.from_json(v) for k, v in data["tables"].get("accounts", {}).items()}

    # construction ------------------------------------------------------------
    @classmethod
    def build(
        cls, examples: Sequence[Example], *, person: str, blocked: Iterable[str] = (), background: Sequence[str] = (),
        seed: int = 0, n_perm: int = 300, source: Optional[Dict[str, Any]] = None,
    ) -> "BehaviorProfile":
        blocked_set = frozenset(blocked)
        style_ex = [e for e in examples if e.kind in ("text", "emoji_only") and e.reply.strip()]
        allt = _Tables()
        per_acct: Dict[str, _Tables] = defaultdict(_Tables)
        for e in style_ex:
            allt.add(e)
            if e.acct:
                per_acct[e.acct].add(e)
        # unusual words: log-odds against everybody else's text in the same chats
        own = Counter()
        for e in style_ex:
            own.update(_lex(content_tokens(e.reply)))
        bg = Counter()
        bg_gram = Counter()
        n_bg_msgs = max(1, len(background))
        for t in background:
            bg.update(_lex(content_tokens(t)))
            ws_b = _lex(words(t))
            for n in (1, 2, 3):
                bg_gram.update({tuple(ws_b[i:i + n]) for i in range(len(ws_b) - n + 1)})
        n_own, n_bg = sum(own.values()) or 1, sum(bg.values()) or 1
        unusual = []
        for tok, c in own.items():
            if c < 8 or tok in blocked_set or len(tok) < 3 or any(ch.isdigit() for ch in tok) or not _plain_token(tok):
                continue
            lo = math.log((c + 0.5) / n_own) - math.log((bg.get(tok, 0) + 0.5) / n_bg)
            if lo > 1.0:
                unusual.append((tok, c, round(lo, 2)))
        unusual.sort(key=lambda x: (-x[2] * math.log(1 + x[1]), x[0]))
        # recurring expressions (1-3 grams) with breadth: used in many conversations, not one burst
        grams: Dict[Tuple[str, ...], set] = defaultdict(set)
        gram_n: Counter = Counter()
        for e in style_ex:
            ws = _lex(words(e.reply))
            seen = set()
            for n in (1, 2, 3):
                for i in range(len(ws) - n + 1):
                    g = tuple(ws[i:i + n])
                    if g in seen or all(w in STOPWORDS or len(w) < 2 for w in g):
                        continue
                    seen.add(g)
                    grams[g].add(_unit_of(e))
                    gram_n[g] += 1
        recurring = []
        n_own_msgs = max(1, len(style_ex))
        for g, c in gram_n.items():
            if c < 10 or len(grams[g]) < 5 or any(w in blocked_set for w in g) or len(" ".join(g)) < 2 or not all(_plain_token(w) for w in g):
                continue
            # an expression is *theirs* only if they use it clearly more often than the people around them do
            ratio = (c / n_own_msgs) / ((bg_gram.get(g, 0) + 0.5) / n_bg_msgs)
            if ratio >= 2.0:
                recurring.append({"gram": " ".join(g), "n": c, "units": len(grams[g]), "rate": round(c / n_own_msgs, 5), "vs_others": round(ratio, 1)})
        recurring.sort(key=lambda r: (-r["n"] * math.log(1 + r["vs_others"]), r["gram"]))
        latency = cls.latency_quantiles(e.lat for e in style_ex)
        chat_counts: Dict[str, Dict[str, Any]] = {}
        by_chat_ex: Dict[str, List[Example]] = defaultdict(list)
        for e in style_ex:
            if e.chat:
                by_chat_ex[e.chat].append(e)
        for ch, exs in by_chat_ex.items():
            if len(exs) >= MIN_CHAT_ROWS:
                k: Counter = Counter()
                for e in exs:
                    sf = surface(e.reply)
                    k.update(f for f in TESTED_FEATURES if sf[f])
                chat_counts[ch] = {"n": len(exs), "k": dict(k)}
        ctxdep = {
            "by_chat": context_dependence_test(style_ex, lambda e: e.chat or None, n_perm=n_perm, seed=seed),
            "by_account": context_dependence_test(style_ex, lambda e: e.acct, n_perm=n_perm, seed=seed + 1, min_group=100),
        }
        data = {
            "schema": PROFILE_SCHEMA, "person": person, "n_messages": len(style_ex), "source": dict(source or {}),
            "n_with_trigger": sum(1 for e in style_ex if e.trigger),
            "accounts": {a: t.n for a, t in per_acct.items()},
            "tables": {"all": allt.to_json(), "accounts": {a: t.to_json() for a, t in per_acct.items()}, "chats": chat_counts},
            "lexicon": {"unusual_words": unusual[:40], "recurring": recurring[:40]},
            "latency_seconds": latency,
            "context_dependence": ctxdep,
            # Held-out validation (docs/EVALUATION_REPORT.md) showed that preferring same-chat / same-account evidence does not make the
            # retrieved examples agree better with the person's real reply, while it multiplies same-chat selection.  Context
            # dependence is therefore applied where it was measured to matter (habit rates in the sampled plan), not as a retrieval filter.
            "retrieval_weights": {"chat_bonus": 0.0, "acct_bonus": 0.0},
            "context_dependent_features": {"chat": sorted(f for f, v in ctxdep["by_chat"].get("features", {}).items() if v["dependent"]),
                                           "account": sorted(f for f, v in ctxdep["by_account"].get("features", {}).items() if v["dependent"])},
            "evidence_floor": {"min_act_support": MIN_ACT_SUPPORT, "unusual_min_count": 8, "recurring_min_count": 10, "recurring_min_units": 5},
        }
        return cls(json.loads(json.dumps(data)))

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "BehaviorProfile":
        d = json.loads(text)
        if d.get("schema") != PROFILE_SCHEMA:
            raise ValueError(f"unsupported behaviour profile schema {d.get('schema')}")
        return cls(d)

    @staticmethod
    def latency_quantiles(values: Iterable[float], cap: float = 3600.0, min_n: int = 50) -> Dict[str, float]:
        """Quantiles (seconds) of the gap between a message and the one before it in the same conversation."""
        lat = sorted(v for v in values if v is not None and 0 <= v <= cap)
        if len(lat) < min_n:
            return {}
        q = lambda p: lat[min(len(lat) - 1, int(p * (len(lat) - 1)))]  # noqa: E731
        return {"n": len(lat), "p10": q(.1), "p25": q(.25), "p50": q(.5), "p75": q(.75), "p90": q(.9)}

    def with_latency(self, values: Iterable[float], provenance: Optional[Dict[str, Any]] = None) -> "BehaviorProfile":
        d = json.loads(json.dumps(self.data))
        d["latency_seconds"] = self.latency_quantiles(values)
        if provenance:
            d.setdefault("source", {})["latency"] = dict(provenance)
        return BehaviorProfile(d)

    # accessors ---------------------------------------------------------------
    @property
    def person(self) -> str:
        return self.data["person"]

    @property
    def n_messages(self) -> int:
        return int(self.data["n_messages"])

    @property
    def source(self) -> Dict[str, Any]:
        return dict(self.data.get("source") or {})

    def retrieval_weights(self) -> Dict[str, float]:
        return dict(self.data["retrieval_weights"])

    def recurring(self) -> List[Dict[str, Any]]:
        return list(self.data["lexicon"]["recurring"])

    def unusual_words(self, k: int = 12) -> List[str]:
        return [w for w, _c, _l in self.data["lexicon"]["unusual_words"][:k]]

    def latency(self) -> Dict[str, float]:
        return dict(self.data.get("latency_seconds") or {})

    def _scope(self, acct: Optional[str]) -> Tuple[_Tables, float]:
        t = self._acct.get(acct or "")
        return (t, SMOOTH_ACCT) if t is not None and t.n >= 100 else (self._all, 0.0)

    def chat_dependent(self) -> frozenset:
        return frozenset((self.data.get("context_dependent_features") or {}).get("chat", []))

    def chat_rate_ratio(self, chat: Optional[str], feature: str) -> float:
        """Shrunk (this chat's rate / the person-wide rate) for a feature measured to depend on the chat; 1.0 otherwise."""
        if not chat or feature not in self.chat_dependent():
            return 1.0
        rec = (self.data["tables"].get("chats") or {}).get(chat)
        if not rec or not self._all.n:
            return 1.0
        glob = self._all.surf_all.get(feature, 0) / self._all.n if feature in STYLE_FEATURES else None
        if glob is None:
            tot = sum(v["k"].get(feature, 0) for v in self.data["tables"]["chats"].values())
            glob = tot / max(1, sum(v["n"] for v in self.data["tables"]["chats"].values()))
        if glob <= 0:
            return 1.0
        rate = (rec["k"].get(feature, 0) + SMOOTH_CHAT * glob) / (rec["n"] + SMOOTH_CHAT)
        return max(0.25, min(4.0, rate / glob))

    def _surf_p(self, ra: str, feature: str, acct: Optional[str], chat: Optional[str] = None) -> float:
        p = self._surf_p_base(ra, feature, acct)
        return max(0.01, min(0.99, p * self.chat_rate_ratio(chat, feature)))

    def _surf_p_base(self, ra: str, feature: str, acct: Optional[str]) -> float:
        a, strength = self._scope(acct)

        def rate(t: _Tables) -> float:
            n = t.n_reply.get(ra, 0)
            glob = (t.surf_all.get(feature, 0) / t.n) if t.n else 0.0
            return ((t.surf_given_reply.get(ra, Counter()).get(feature, 0) + SMOOTH_SURF * glob) / (n + SMOOTH_SURF))
        p_all = rate(self._all)
        if a is self._all:
            return p_all
        n_a = a.n_reply.get(ra, 0)
        return (rate(a) * n_a + strength * p_all) / (n_a + strength)

    def reply_act_dist(self, sit: Situation, acct: Optional[str] = None) -> Dict[str, float]:
        a, strength = self._scope(acct)
        glob = _dist(self._all.act_all, REPLY_ACTS)
        prior = glob
        if a is not self._all:
            prior = smooth([a.act_all.get(k, 0) for k in REPLY_ACTS], glob, strength)
        ta = sit.act
        cnt = a.act_given_trigger.get(ta) or self._all.act_given_trigger.get(ta) or Counter()
        if sum(cnt.values()) < MIN_ACT_SUPPORT:
            # fall back to sibling acts of the same family, then to the person-wide distribution
            from .core import ACT_FAMILY
            sib = Counter()
            for k, v in self._all.act_given_trigger.items():
                if ACT_FAMILY.get(k) == sit.family:
                    sib.update(v)
            cnt = cnt + sib
        probs = smooth([cnt.get(k, 0) for k in REPLY_ACTS], prior, SMOOTH_ACT)
        return dict(zip(REPLY_ACTS, probs))

    def length_dist(self, trigger_act: str, ra: str, acct: Optional[str] = None) -> List[float]:
        a, strength = self._scope(acct)
        keys = list(range(N_LEN))
        glob = _dist(self._all.len_all, keys)
        by_reply = smooth([self._all.len_given_reply.get(ra, Counter()).get(k, 0) for k in keys], glob, SMOOTH_LEN)
        if a is not self._all:
            by_reply = smooth([a.len_given_reply.get(ra, Counter()).get(k, 0) for k in keys], by_reply, strength)
        pair = a.len_given_pair.get(f"{trigger_act}|{ra}") or self._all.len_given_pair.get(f"{trigger_act}|{ra}") or Counter()
        return smooth([pair.get(k, 0) for k in keys], by_reply, SMOOTH_LEN)

    def sample_plan(self, sit: Situation, rng: random.Random, acct: Optional[str] = None, chat: Optional[str] = None) -> ResponsePlan:
        acts = self.reply_act_dist(sit, acct)
        ra = _sample(rng, list(acts), list(acts.values()))
        lb = _sample(rng, list(range(N_LEN)), self.length_dist(sit.act, ra, acct))
        a, _ = self._scope(acct)
        kinds = (a if a.kind_given_reply.get(ra) else self._all).kind_given_reply.get(ra) or Counter({"text": 1})
        emoji_only = kinds.get("emoji_only", 0) / max(1, sum(kinds.values()))
        kind = "emoji_only" if rng.random() < emoji_only else "text"
        emoji = kind == "emoji_only" or rng.random() < self._surf_p(ra, "emoji", acct, chat)
        top = [(e, c) for e, c in (a.emoji or self._all.emoji).items()]
        pick = None
        if emoji and top:
            pick = _sample(rng, [e for e, _ in top], [c / sum(c2 for _, c2 in top) for _, c in top])
        return ResponsePlan(
            reply_act=ra, length_bucket=0 if kind == "emoji_only" else lb, kind=kind, emoji=emoji, emoji_pick=pick,
            lower=rng.random() < self._surf_p(ra, "all_lower", acct, chat), end_punct=rng.random() >= self._surf_p(ra, "no_end_punct", acct, chat),
            question=rng.random() < self._surf_p(ra, "question", acct, chat) if ra != "question" else True,
            elongate=rng.random() < self._surf_p(ra, "elongated", acct, chat),
        )

    def suggest_delay(self, rng: random.Random, cap: float = 6.0) -> float:
        lat = self.latency()
        if not lat:
            return 0.0
        pts = [(0.0, 0.0), (0.1, lat["p10"]), (0.25, lat["p25"]), (0.5, lat["p50"]), (0.75, lat["p75"]), (0.9, lat["p90"]), (1.0, lat["p90"] * 2)]
        x = rng.random()
        for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
            if x <= p1:
                v = v0 + (v1 - v0) * (x - p0) / max(1e-9, p1 - p0)
                return round(min(cap, math.sqrt(max(0.0, v))), 2)
        return cap

    # description for prompts / reports --------------------------------------
    def describe(self, sit: Optional[Situation] = None, acct: Optional[str] = None) -> List[str]:
        a, _ = self._scope(acct)
        t = a if a.n else self._all
        n = max(1, t.n)
        pct = lambda x: f"{round(100 * x)}%"  # noqa: E731
        lens = Counter()
        for k, v in t.len_all.items():
            lens[k] = v
        med_bucket = 0
        acc = 0
        for k in range(N_LEN):
            acc += lens.get(k, 0)
            if acc >= n / 2:
                med_bucket = k
                break
        from .core import LENGTH_BINS
        lo_w = LENGTH_BINS[med_bucket]
        hi_w = LENGTH_BINS[med_bucket + 1] - 1 if med_bucket < N_LEN - 1 else None
        span = f"{lo_w}+" if hi_w is None else (f"{lo_w}" if hi_w == lo_w else f"{lo_w}-{hi_w}")
        facts = [
            f"Median message length is about {span} words; "
            f"{pct(sum(lens.get(k, 0) for k in range(4)) / n)} of messages are 3 words or fewer.",
            f"{pct(t.surf_all.get('all_lower', 0) / n)} of messages are all lowercase; {pct(t.surf_all.get('no_end_punct', 0) / n)} end without punctuation.",
        ]
        if t.surf_all.get("emoji"):
            facts.append(f"{pct(t.surf_all['emoji'] / n)} contain an emoji (most used: {' '.join(e for e, _ in t.emoji.most_common(5))}).")
        if t.surf_all.get("hinglish", 0) / n >= 0.05:
            facts.append(f"{pct(t.surf_all['hinglish'] / n)} mix in romanised Hindi (Hinglish).")
        if t.surf_all.get("elongated", 0) / n >= 0.03:
            facts.append(f"{pct(t.surf_all['elongated'] / n)} stretch a word ('noooo').")
        if t.surf_all.get("question", 0) / n >= 0.03:
            facts.append(f"{pct(t.surf_all['question'] / n)} contain a question mark.")
        if sit is not None:
            dist = self.reply_act_dist(sit, acct)
            top = sorted(dist.items(), key=lambda kv: -kv[1])[:3]
            label = lambda k: "statement/other" if k == "other" else k  # noqa: E731
            facts.append(f"When the message is a {sit.act.replace('_', ' ')}, their replies were: "
                         + ", ".join(f"{label(k)} {pct(v)}" for k, v in top if v >= 0.05) + ".")
        un = self.unusual_words(10)
        if un:
            facts.append("Words they use unusually often (use only when natural): " + ", ".join(un) + ".")
        return facts   # chat/account effects are applied inside the sampled plan (validated features only), not asserted in prose
