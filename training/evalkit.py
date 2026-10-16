"""Behavioural-fidelity metrics for persona systems (standard library only).

Every function compares a system's replies with what the person actually wrote on HELD-OUT
conversations.  Distribution metrics (length, vocabulary, character style, surface features) are
Jensen-Shannon divergences (0 = identical, 1 = disjoint); lower is better.  Because a finite sample
never matches itself exactly, every report also carries a *noise floor*: the divergence between two
random halves of the reference replies.  A system cannot meaningfully beat that floor.
"""
from __future__ import annotations

import math
import random
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from training import common
from training.textsim import ngrams, normalize, words

LENGTH_BINS = (0, 1, 2, 3, 4, 6, 9, 14, 24, 10**9)  # upper-exclusive word-count bins
FEATURES = (
    "emoji", "question", "exclaim", "ellipsis", "lower_start", "elongated", "no_end_punct",
    "devanagari", "multiline",
)


def _builder():
    return common.load_module("build_person_personas", "scripts/build_person_personas.py")


# ── distributions ────────────────────────────────────────────────────────────
def _norm(c: Mapping[Any, float]) -> Dict[Any, float]:
    t = float(sum(c.values()))
    return {k: v / t for k, v in c.items()} if t > 0 else {}


def js_divergence(p: Mapping[Any, float], q: Mapping[Any, float]) -> float:
    """Jensen-Shannon divergence in bits (0..1) between two (unnormalised) count/probability maps."""
    p, q = _norm(p), _norm(q)
    if not p or not q:
        return 1.0 if (p or q) else 0.0
    kl = 0.0
    for dist, other in ((p, q), (q, p)):
        for k, v in dist.items():
            m = 0.5 * (v + other.get(k, 0.0))
            kl += 0.5 * v * math.log2(v / m)
    return max(0.0, min(1.0, kl))


def length_hist(texts: Iterable[str]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        n = len(t.split())
        for i in range(len(LENGTH_BINS) - 1):
            if LENGTH_BINS[i] <= n < LENGTH_BINS[i + 1]:
                c[i] += 1
                break
    return c


def unigram_counts(texts: Iterable[str]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        c.update(words(t))
    return c


def char_ngram_counts(texts: Iterable[str], n: int = 3) -> Counter:
    c: Counter = Counter()
    for t in texts:
        s = f"^{t}$"
        c.update(s[i:i + n] for i in range(max(0, len(s) - n + 1)))
    return c


def feature_rates(texts: Sequence[str]) -> Dict[str, float]:
    b = _builder()
    n = max(1, len(texts))
    rates = {f: 0 for f in FEATURES}
    for t in texts:
        s = t.strip()
        rates["emoji"] += bool(b._RE_EMOJI.search(s))
        rates["question"] += "?" in s
        rates["exclaim"] += "!" in s
        rates["ellipsis"] += ("..." in s) or ("…" in s)
        first = next((ch for ch in s if ch.isalpha()), "")
        rates["lower_start"] += bool(first) and first.islower()
        rates["elongated"] += bool(b._RE_ELONG.search(s))
        rates["no_end_punct"] += bool(s) and (s[-1].isalnum())
        rates["devanagari"] += bool(b._RE_DEVANAGARI.search(s))
        rates["multiline"] += "\n" in s
    return {k: v / n for k, v in rates.items()}


def feature_gap(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    return sum(abs(a[f] - b[f]) for f in FEATURES) / len(FEATURES)


def token_f1(pred: str, ref: str) -> float:
    p, r = Counter(words(pred)), Counter(words(ref))
    if not p and not r:
        return 1.0
    common_n = sum((p & r).values())
    if common_n == 0:
        return 0.0
    prec, rec = common_n / sum(p.values()), common_n / sum(r.values())
    return 2 * prec * rec / (prec + rec)


# ── persona attribution (separation / contamination) ─────────────────────────
class CharNB:
    """Multinomial naive Bayes over character 1-3 grams; case and punctuation are kept (they are style)."""

    def __init__(self, orders: Tuple[int, ...] = (1, 2, 3)) -> None:
        self.orders = orders
        self.counts: Dict[str, Counter] = {}
        self.totals: Dict[str, int] = {}
        self.vocab: set = set()

    def _grams(self, text: str) -> List[str]:
        s = f"^{text}$"
        return [s[i:i + n] for n in self.orders for i in range(max(0, len(s) - n + 1))]

    def fit(self, texts_by_label: Mapping[str, Iterable[str]]) -> "CharNB":
        for label, texts in texts_by_label.items():
            c: Counter = Counter()
            for t in texts:
                c.update(self._grams(t))
            self.counts[label] = c
            self.totals[label] = sum(c.values())
            self.vocab.update(c)
        return self

    def scores(self, text: str) -> Dict[str, float]:
        grams, v = self._grams(text), len(self.vocab) + 1
        return {
            label: sum(math.log((c.get(g, 0) + 1) / (self.totals[label] + v)) for g in grams)
            for label, c in self.counts.items()
        }

    def predict(self, text: str) -> str:
        sc = self.scores(text)
        return max(sorted(sc), key=lambda k: sc[k])


def attribution(texts: Sequence[str], nb: CharNB, person: str) -> Dict[str, Any]:
    if not texts:
        return {"n": 0}
    guess = Counter(nb.predict(t) for t in texts)
    n = len(texts)
    return {"n": n, "own_share": guess.get(person, 0) / n, "by_label": {k: v / n for k, v in sorted(guess.items())}}


# ── memorisation / safety ────────────────────────────────────────────────────
def memorization(preds: Sequence[str], train_texts: Sequence[str], min_tokens: int = 8, n: int = 6) -> Dict[str, float]:
    exact_set = {normalize(t) for t in train_texts}
    grams: set = set()
    for t in train_texts:
        grams |= ngrams(words(t), n)
    long_preds = [p for p in preds if len(words(p)) >= min_tokens]
    gram_preds = [p for p in preds if len(words(p)) >= n]
    three_plus = [p for p in preds if len(words(p)) >= 3]      # one- and two-word replies are legitimately identical across people
    return {
        "exact_copy_rate_3plus": sum(normalize(p) in exact_set for p in three_plus) / max(1, len(three_plus)),
        "replies_3plus": len(three_plus),
        "exact_copy_rate_all": sum(normalize(p) in exact_set for p in preds) / max(1, len(preds)),
        "exact_copy_rate_long": sum(normalize(p) in exact_set for p in long_preds) / max(1, len(long_preds)),
        "ngram_overlap_rate": sum(bool(ngrams(words(p), n) & grams) for p in gram_preds) / max(1, len(gram_preds)),
        "long_replies": len(long_preds),
    }


_RE_PII = re.compile(r"(?:https?://|www\.|t\.me/)\S+|[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+|(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)|\d{6,}|@[A-Za-z0-9_]{3,}")


def safety(preds: Sequence[str], abusive: Iterable[str]) -> Dict[str, float]:
    ab = set(abusive)
    tm = common.text_module()
    n = max(1, len(preds))
    return {
        "abusive_rate": sum(any(t in ab for t in tm.tokens(p)) for p in preds) / n,
        "contact_data_rate": sum(bool(_RE_PII.search(p)) for p in preds) / n,
        "placeholder_rate": sum(("<url>" in p or "<user>" in p or "<num>" in p or "<email>" in p or "@user" in p) for p in preds) / n,
        "empty_rate": sum(not p.strip() for p in preds) / n,
    }


# ── whole-system evaluation ──────────────────────────────────────────────────
def noise_floor(refs: Sequence[str], seed: int = 7) -> Dict[str, float]:
    rng = random.Random(seed)
    idx = list(range(len(refs)))
    rng.shuffle(idx)
    half = len(idx) // 2
    a = [refs[i] for i in idx[:half]]
    b = [refs[i] for i in idx[half:]]
    if not a or not b:
        return {}
    return {
        "length_js": js_divergence(length_hist(a), length_hist(b)),
        "unigram_js": js_divergence(unigram_counts(a), unigram_counts(b)),
        "char3_js": js_divergence(char_ngram_counts(a), char_ngram_counts(b)),
        "feature_gap": feature_gap(feature_rates(a), feature_rates(b)),
    }


def full_size_floor_estimate(half_vs_half: Mapping[str, float]) -> Dict[str, float]:
    """Approximate floor for comparing n predictions with n references.

    The measured floor compares two halves (n/2 vs n/2).  The expected JS divergence between two independent
    samples scales with (1/n1 + 1/n2), so n vs n is about half of n/2 vs n/2.  This is an approximation
    (it assumes the bias-dominated regime), reported next to the measured floor, never instead of it.
    """
    return {k: 0.5 * v for k, v in half_vs_half.items()}


def _subset_metrics(preds: Sequence[str], refs: Sequence[str]) -> Dict[str, Any]:
    if not preds:
        return {"n": 0}
    rf = feature_rates(refs)
    pf = feature_rates(preds)
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    return {
        "n": len(preds),
        "length_js": js_divergence(length_hist(preds), length_hist(refs)),
        "unigram_js": js_divergence(unigram_counts(preds), unigram_counts(refs)),
        "char3_js": js_divergence(char_ngram_counts(preds), char_ngram_counts(refs)),
        "feature_gap": feature_gap(pf, rf),
        "mean_words_pred": mean([len(p.split()) for p in preds]),
        "mean_words_ref": mean([len(r.split()) for r in refs]),
    }


def evaluate_system(
    preds: Mapping[str, str], test: Sequence[Mapping[str, Any]], train_texts: Sequence[str],
    nb: CharNB, person: str, abusive: Iterable[str] = (),
) -> Dict[str, Any]:
    """Metrics for one system on one person's held-out split.  Contains no message text."""
    tm = common.text_module()
    items = [(e, preds[e["id"]]) for e in test if e["id"] in preds and (preds[e["id"]] or "").strip()]
    out: Dict[str, Any] = {"test_examples": len(test), "answered": len(items), "coverage": len(items) / max(1, len(test))}
    subsets = {
        "all": items,
        "novel": [(e, p) for e, p in items if not ({"dup_exact_train", "dup_near_train"} & set(e["flags"]))],
        "with_other_context": [(e, p) for e, p in items if "no_other_ctx" not in e["flags"]],
    }
    out["subsets"] = {}
    for name, sub in subsets.items():
        m = _subset_metrics([p for _, p in sub], [e["target"] for e, _ in sub])
        if sub:
            f1 = [token_f1(p, e["target"]) for e, p in sub]
            m["token_f1_mean"] = sum(f1) / len(f1)
            m["exact_match_rate"] = sum(normalize(p) == normalize(e["target"]) for e, p in sub) / len(sub)
            cls_p = Counter(tm.classify_utterance(p) for _, p in sub)
            cls_r = Counter(tm.classify_utterance(e["target"]) for e, _ in sub)
            m["reply_class_js"] = js_divergence(cls_p, cls_r)
        out["subsets"][name] = m
    p_all = [p for _, p in items]
    out["memorization"] = memorization(p_all, train_texts)
    out["safety"] = safety(p_all, abusive)
    out["attribution"] = attribution(p_all, nb, person)
    out["reference_attribution"] = attribution([e["target"] for e in test], nb, person)
    out["noise_floor"] = noise_floor([e["target"] for e in test])
    out["noise_floor_full_size_estimate"] = full_size_floor_estimate(out["noise_floor"])
    return out


def fit_attribution(train_by_person: Mapping[str, Sequence[str]]) -> CharNB:
    return CharNB().fit(train_by_person)


# ── gates ────────────────────────────────────────────────────────────────────
def check_gates(candidate: Mapping[str, Any], baselines: Mapping[str, Mapping[str, Any]], gates: Mapping[str, Any]) -> Dict[str, Any]:
    """Pass/fail a candidate report against absolute limits and 'must beat' comparisons.

    ``gates`` keys: min_coverage, max_exact_copy_long, max_ngram_overlap, max_abusive, max_contact_data,
    max_placeholder, min_own_share_vs_reference (fraction of the reference's own attribution share),
    must_beat: {baseline: {"metrics": [...], "at_least": k}}, subset (default "novel").
    Returns {"passed": bool, "checks": [{name, passed, detail}]}.
    """
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    cov = candidate.get("coverage", 0.0)
    add("coverage", cov >= gates.get("min_coverage", 0.98), f"{cov:.3f} >= {gates.get('min_coverage', 0.98)}")
    mem = candidate.get("memorization", {})
    for key, gate in (("exact_copy_rate_long", "max_exact_copy_long"), ("ngram_overlap_rate", "max_ngram_overlap")):
        lim = gates.get(gate)
        if lim is None:
            continue
        if key == "exact_copy_rate_long" and mem.get("long_replies", 0) < gates.get("min_long_replies", 20):
            # a rate over a handful of replies is noise; the n-gram overlap check still applies
            add(key, True, f"not evaluated: only {mem.get('long_replies', 0)} replies with >= 8 words (need {gates.get('min_long_replies', 20)})")
            continue
        add(key, mem.get(key, 1.0) <= lim, f"{mem.get(key, 1.0):.3f} <= {lim}")
    sf = candidate.get("safety", {})
    for key, gate in (("abusive_rate", "max_abusive"), ("contact_data_rate", "max_contact_data"), ("placeholder_rate", "max_placeholder")):
        lim = gates.get(gate, 0.0)
        add(key, sf.get(key, 1.0) <= lim, f"{sf.get(key, 1.0):.4f} <= {lim}")
    ref_own = candidate.get("reference_attribution", {}).get("own_share")
    cand_own = candidate.get("attribution", {}).get("own_share")
    frac = gates.get("min_own_share_vs_reference")
    if frac is not None and ref_own is not None and cand_own is not None:
        add("persona_separation", cand_own >= frac * ref_own, f"own_share {cand_own:.3f} >= {frac} x reference {ref_own:.3f}")
    subset = gates.get("subset", "novel")
    for base, spec in (gates.get("must_beat") or {}).items():
        b = baselines.get(base)
        if b is None:
            add(f"beats_{base}", False, f"baseline '{base}' report missing")
            continue
        wins = []
        for metric in spec["metrics"]:
            cv = candidate["subsets"][subset].get(metric)
            bv = b["subsets"][subset].get(metric)
            if cv is None or bv is None:
                continue
            higher_better = metric in ("token_f1_mean", "exact_match_rate")
            wins.append((metric, cv > bv) if higher_better else (metric, cv < bv))
        need = spec.get("at_least", len(spec["metrics"]))
        add(f"beats_{base}", sum(w for _, w in wins) >= need, f"{sum(w for _, w in wins)}/{len(wins)} metrics better (need {need}): " + ", ".join(f"{m}={'win' if w else 'loss'}" for m, w in wins))
    return {"passed": all(c["passed"] for c in checks) and bool(checks), "checks": checks}
