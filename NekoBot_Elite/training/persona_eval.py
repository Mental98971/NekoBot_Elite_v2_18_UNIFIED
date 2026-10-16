#!/usr/bin/env python3
"""Held-out evaluation of the persona engine (conversation-level split, no leakage), and of its scorers.

    # offline (no model needed): scorer discrimination, retrieval diversity/leakage, repetition control, replay baselines
    python -m training.persona_eval --data training/_private/data/tanvi --other training/_private/data/yashmeet --out report.json

    # with a real model: adds the four generation arms (baseline LLM / pure RAG / engine / engine + reranking)
    python -m training.persona_eval --data ... --provider-url http://localhost:11434/v1 --provider-model <name> --limit 300

Everything is built from the TRAIN split only; the TEST split is only used to score.  The generation arms need a
model: without one they are reported as "not run" - nothing is simulated or invented.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.persona_engine import antirepeat, core, scoring  # noqa: E402
from bot.persona_engine import store as st  # noqa: E402
from bot.persona_engine.engine import EngineConfig, EngineRequest, PersonaEngine  # noqa: E402
from bot.persona_engine.profile import BehaviorProfile  # noqa: E402
from bot.persona_engine.providers import LLMProvider, OpenAICompatProvider  # noqa: E402
from training import common, evalkit  # noqa: E402

ASSISTANT_DISTRACTORS = [
    "Sure! Here's what I think about that. Let me know if you need anything else.",
    "That's a great question! I'd be happy to help you with that today.",
    "I'm not sure I understand. Could you please clarify what you mean?",
    "Certainly! Here is a short summary of the situation for you.",
    "Thank you for reaching out. I hope this message finds you well.",
    "As an AI, I don't have personal opinions, but I can offer some perspectives.",
]


# ── building blocks ──────────────────────────────────────────────────────────
def load_split(d: Path, splits: Sequence[str], blocked: Iterable[str]) -> List[st.Example]:
    common.verify_prepared_dir(d, tuple(splits))
    return st.from_prepared(d, splits, blocked)


def build_parts(train: List[st.Example], blocked: Sequence[str], names: Sequence[str], max_chars: int,
                other_train_replies: Sequence[str] = (), seed: int = 0, n_perm: int = 200, use_assoc: bool = True,
                assoc_weight: float = 0.35) -> Dict[str, Any]:
    own = [e.reply for e in train if e.kind in ("text", "emoji_only") and e.reply.strip()]
    bg = [e.trigger for e in train if e.trigger] + list(other_train_replies)
    profile = BehaviorProfile.build(train, person=train[0].person, blocked=blocked, background=bg, seed=seed, n_perm=n_perm)
    lm = scoring.PersonaLM(own, bg)
    assoc = scoring.ReplyAssociation([(e.trigger, e.reply) for e in train if e.trigger and e.kind in ("text", "emoji_only")]) if use_assoc else None
    scorer = scoring.CandidateScorer(
        profile, lm, frozenset(w for t in own for w in core.words(t)), frozenset(w for t in bg for w in core.words(t)),
        blocked=blocked, names=names, max_chars=max_chars, assoc=assoc, assoc_weight=assoc_weight)
    return {"profile": profile, "lm": lm, "scorer": scorer, "store": st.ExampleStore(train), "own": own, "bg": bg}


def request_for(e: st.Example) -> EngineRequest:
    """What the bot would see for this test example: the message being answered, its reply target and the preceding turns."""
    ctx = [(w, t) for w, t in e.ctx]
    probe = e.trigger or (ctx[-1][1] if ctx else "")
    # the message being answered is passed as the request text, so it must not also appear as the last history turn
    history = ctx[:-1] if ctx and ctx[-1][1] == probe and ctx[-1][0] != "me" else ctx
    return EngineRequest(chat_id=abs(hash(e.conv)) % (10 ** 9), text=probe, acct=e.acct, chat_ref=e.chat or None,
                         chat_kind="group", history=history, request_id=e.id)


def eligible(test: Sequence[st.Example]) -> List[st.Example]:
    """Test examples that have a message to answer (not ambient chatter) and are text-like."""
    return [e for e in test if e.trigger and e.kind in ("text", "emoji_only")]


# ── 1. scorer discrimination (offline) ───────────────────────────────────────
def scorer_discrimination(parts: Dict[str, Any], test: Sequence[st.Example], other_replies: Sequence[str], train: Sequence[st.Example],
                          n_each: int = 3, seed: int = 0, limit: int = 600) -> Dict[str, Any]:
    """Can each scorer tell the person's real reply from (a) the same person's replies to other situations,
    (b) the other person's replies, (c) generic assistant-style replies?  Reports mean reciprocal rank and top-1 against chance."""
    rng = random.Random(seed)
    scorer: scoring.CandidateScorer = parts["scorer"]
    profile: BehaviorProfile = parts["profile"]
    own_pool = [e.reply for e in train if e.reply.strip()]
    items = [e for e in eligible(test)]
    rng.shuffle(items)
    items = items[:limit]
    kinds = {"same_person_other_situation": lambda: rng.sample(own_pool, n_each),
             "other_person": lambda: rng.sample(list(other_replies), n_each) if len(other_replies) >= n_each else [],
             "assistant_style": lambda: rng.sample(ASSISTANT_DISTRACTORS, n_each)}
    comps = ("context", "persona", "linguistic", "naturalness", "evidence", "composite")
    out: Dict[str, Any] = {"examples": len(items), "distractors_per_kind": n_each, "by_kind": {}}
    for kind, draw in kinds.items():
        acc = {c: {"rr": 0.0, "top1": 0, "n": 0} for c in comps}
        for e in items:
            ds = [d for d in draw() if core.norm(d) != core.norm(e.reply)]
            if not ds:
                continue
            sit = core.analyze(e.trigger, has_reply_target=True)
            plan = profile.sample_plan(sit, random.Random(f"{e.id}"), e.acct)
            trig = core.content_tokens(e.trigger)
            cands = [e.reply] + ds
            scs = [scorer.score(c, sit, plan, trig, 0.0, e.acct) for c in cands]
            for c in comps:
                vals = [getattr(s, c) for s in scs]
                better = sum(1 for v in vals[1:] if v > vals[0]) + 0.5 * sum(1 for v in vals[1:] if v == vals[0])
                acc[c]["rr"] += 1.0 / (1 + better)
                acc[c]["top1"] += int(better == 0)
                acc[c]["n"] += 1
        chance_top1 = 1.0 / (1 + n_each)
        out["by_kind"][kind] = {"chance_top1": round(chance_top1, 3), **{
            c: {"mrr": round(a["rr"] / max(1, a["n"]), 3), "top1": round(a["top1"] / max(1, a["n"]), 3)} for c, a in acc.items()}}
    return out


# ── 2. retrieval: diversity, leakage, context use (offline) ──────────────────
def retrieval_report(parts: Dict[str, Any], train: Sequence[st.Example], test: Sequence[st.Example], k: int = 4, limit: int = 500, seed: int = 0) -> Dict[str, Any]:
    store: st.ExampleStore = parts["store"]
    profile: BehaviorProfile = parts["profile"]
    rng = random.Random(seed)
    items = eligible(test)
    rng.shuffle(items)
    items = items[:limit]
    w = profile.retrieval_weights()
    R = st.SelectiveRetriever(store, st.RetrievalConfig(k=k, chat_bonus=w.get("chat_bonus", 0.0), acct_bonus=w.get("acct_bonus", 0.0)))
    plain = st.SelectiveRetriever(store, st.RetrievalConfig(k=k, lam=0.0, stale_penalty=0.0))
    nobonus = st.SelectiveRetriever(store, st.RetrievalConfig(k=k, chat_bonus=0.0, acct_bonus=0.0))
    train_convs = {e.conv for e in train}
    test_convs = {e.conv for e in test}
    train_ids = {e.id for e in train}
    n_ev, same_chat, distinct, plain_distinct, plain_dupe, short_hit, empty, act_match = 0, 0, 0, 0, 0, 0, 0, 0
    sim_sel = sim_plain = 0.0
    pair_n = nb_same = nb_total = 0
    chat_share = Counter(e.chat for e in train)
    base_same = sum((chat_share.get(e.chat, 0) / max(1, len(train))) for e in items) / max(1, len(items))

    def mean_pair_sim(evs):
        xs = [core.cosine(a.example.reply_vec, b.example.reply_vec) for i, a in enumerate(evs) for b in evs[i + 1:]]
        return sum(xs) / len(xs) if xs else 0.0
    reply_pool = Counter()
    for e in items:
        sit = core.analyze(e.trigger, has_reply_target=True)
        q = st.Query(e.trigger, sit, acct=e.acct, chat=e.chat)
        ev = R.retrieve(q)
        base = plain.retrieve(q)
        nb_ev = nobonus.retrieve(q)
        nb_same += sum(1 for x in nb_ev if x.example.chat == e.chat)
        nb_total += len(nb_ev)
        if not ev:
            empty += 1
            continue
        n_ev += len(ev)
        same_chat += sum(1 for x in ev if x.example.chat == e.chat)
        distinct += len({core.norm(x.example.reply) for x in ev})
        plain_distinct += len({core.norm(x.example.reply) for x in base})
        plain_dupe += len(base) - len({core.norm(x.example.reply) for x in base})
        act_match += sum(1 for x in ev if x.example.trigger_sit.act == sit.act)
        sim_sel += mean_pair_sim(ev)
        sim_plain += mean_pair_sim(base)
        pair_n += 1
        reply_pool.update(x.example.id for x in ev)
    n_req = len(items) - empty
    return {
        "requests": len(items), "no_evidence_rate": round(empty / max(1, len(items)), 3),
        "mean_examples": round(n_ev / max(1, n_req), 2),
        "index_leak_check": {"index_conversations_overlapping_test": len(train_convs & test_convs), "index_ids_that_are_test_ids": len({e.id for e in test} & train_ids)},
        "same_chat_share": round(same_chat / max(1, n_ev), 3),
        "same_chat_share_if_chat_were_irrelevant": round(base_same, 3),
        "same_chat_share_without_any_chat_bonus": round(nb_same / max(1, nb_total), 3),
        "mean_pairwise_reply_similarity": {"selective": round(sim_sel / max(1, pair_n), 3), "plain_top_k": round(sim_plain / max(1, pair_n), 3)},
        "other_chat_share": round(1 - same_chat / max(1, n_ev), 3),
        "same_situation_share": round(act_match / max(1, n_ev), 3),
        "distinct_replies_per_request": {"selective": round(distinct / max(1, n_req), 2), "plain_top_k": round(plain_distinct / max(1, n_req), 2)},
        "duplicate_replies_in_plain_top_k_per_request": round(plain_dupe / max(1, n_req), 3),
        "evidence_reuse": {"distinct_examples_used": len(reply_pool), "most_used_share": round(max(reply_pool.values(), default=0) / max(1, sum(reply_pool.values())), 4)},
        "chat_bonus": w.get("chat_bonus"), "acct_bonus": w.get("acct_bonus"),
    }


# ── 2b. is the measured context dependence real on held-out data? (offline) ──
def context_dependence_validation(parts: Dict[str, Any], test: Sequence[st.Example], features: Sequence[str] = ("emoji", "question", "no_end_punct", "elongated", "short")) -> Dict[str, Any]:
    """Log-loss of predicting a habit in held-out replies with (a) the person-wide rate and (b) the chat-adjusted / account-specific
    rate, for features the permutation test flagged as context-dependent.  Positive gain = the context information helps."""
    prof: BehaviorProfile = parts["profile"]
    out: Dict[str, Any] = {"chat": {}, "account": {}}
    rows = [e for e in test if e.kind in ("text", "emoji_only") and e.reply.strip()]

    def ll(p: float, y: bool) -> float:
        p = max(0.01, min(0.99, p))
        return -math.log(p if y else 1 - p)
    for f in features:
        n_tab = prof._all.n or 1
        glob = prof._all.surf_all.get(f, 0) / n_tab if f in STYLE_FEATURES_ALL else None
        if glob is None:
            continue
        for scope, dep in (("chat", prof.chat_dependent()), ("account", frozenset((prof.data.get("context_dependent_features") or {}).get("account", [])))):
            if f not in dep:
                out[scope][f] = {"flagged_context_dependent": False}
                continue
            d_sum = n = 0.0
            wins = 0
            for e in rows:
                y = bool(core.surface(e.reply)[f])
                if scope == "chat":
                    if e.chat not in (prof.data["tables"].get("chats") or {}):
                        continue
                    p_ctx = glob * prof.chat_rate_ratio(e.chat, f)
                else:
                    t, _s = prof._scope(e.acct)
                    if t is prof._all:
                        continue
                    p_ctx = (t.surf_all.get(f, 0) + 40 * glob) / (t.n + 40)
                gain = ll(glob, y) - ll(p_ctx, y)
                d_sum += gain
                wins += gain > 0
                n += 1
            out[scope][f] = {"flagged_context_dependent": True, "n": int(n), "mean_logloss_gain": round(d_sum / n, 5) if n else None}
    return out


STYLE_FEATURES_ALL = set(core.FEATURES)


# ── 3. repetition control (offline) ──────────────────────────────────────────
def repetition_report(parts: Dict[str, Any], train: Sequence[st.Example], test: Sequence[st.Example], n_chats: int = 30, per_chat: int = 12, seed: int = 0) -> Dict[str, Any]:
    """Replay a retrieval-style generator in simulated chats with and without the repetition guard.

    The generator is deliberately repetition-prone (it replays the most similar past reply); the guard picks the best
    non-repeating alternative among the candidates.  This measures the guard, not a model."""
    rng = random.Random(seed)
    store: st.ExampleStore = parts["store"]
    R = st.SelectiveRetriever(store, st.RetrievalConfig(k=6, lam=0.0, stale_penalty=0.0, min_relevance=0.0))
    items = eligible(test)
    chats = [[rng.choice(items) for _ in range(per_chat)] for _ in range(n_chats)]   # same draws for both arms

    def run(use_guard: bool) -> Dict[str, float]:
        dup = open_rep = emo_rep = 0
        total = 0
        for c in range(n_chats):
            guard = antirepeat.RepetitionGuard()
            for e in chats[c]:
                ev = R.retrieve(st.Query(e.trigger, core.analyze(e.trigger, has_reply_target=True)))
                cands = [x.example.reply for x in ev] or [train[0].reply]
                if use_guard:
                    cands.sort(key=lambda t: guard.penalty(1, t, parts["profile"].recurring()))
                choice = cands[0]
                if use_guard:
                    choice = guard.diversify(1, choice)
                prev = guard.recent(1, 5)
                dup += int(any(antirepeat.similarity(choice, p) >= 0.85 for p in prev))
                f = core.words(choice)[:1]
                open_rep += int(bool(f) and sum(1 for p in prev if core.words(p)[:1] == f) >= 2)
                em = set(core.emojis(choice))
                emo_rep += int(bool(em) and bool(prev) and em == set(core.emojis(prev[-1])))
                guard.remember(1, choice)
                total += 1
        return {"near_duplicate_of_recent": round(dup / total, 4), "opening_repeated_3x": round(open_rep / total, 4), "same_emoji_as_previous": round(emo_rep / total, 4), "replies": total}
    return {"simulated_chats": n_chats, "replies_per_chat": per_chat, "without_guard": run(False), "with_guard": run(True)}


# ── 4. generation arms (need a model) ────────────────────────────────────────
BASELINE_SYSTEM = "You are a friendly person chatting in a group on Telegram. Reply with one short chat message."


async def arm_baseline_llm(provider: LLMProvider, items: Sequence[st.Example], cfg: EngineConfig, max_chars: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for e in items:
        req = request_for(e)
        msgs = [{"role": "system", "content": BASELINE_SYSTEM}, {"role": "user", "content": req.text}]
        try:
            raw = (await provider.generate(msgs, n=1, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout))[0]
        except Exception:
            continue
        from bot.persona_engine.safety import clean_text
        out[e.id] = clean_text(raw, max_chars)
    return out


async def arm_pure_rag(provider: LLMProvider, parts: Dict[str, Any], items: Sequence[st.Example], cfg: EngineConfig, max_chars: int, name: str) -> Dict[str, str]:
    """The failure mode on purpose: nearest old messages pasted in, 'imitate these'. No profile, plan, gates or scoring."""
    R = st.SelectiveRetriever(parts["store"], st.RetrievalConfig(k=6, lam=0.0, stale_penalty=0.0, min_relevance=0.0, w_lex=0.6, w_chr=0.4, w_sit=0.0))
    out: Dict[str, str] = {}
    from bot.persona_engine.safety import clean_text
    for e in items:
        req = request_for(e)
        ev = R.retrieve(st.Query(req.text, core.analyze(req.text)))
        shots = "\n".join(f"{x.example.trigger} -> {x.example.reply}" for x in ev)
        msgs = [{"role": "system", "content": f"You imitate {name}. Here are similar past exchanges:\n{shots}\nReply to the user's message in the same style. One short chat message."},
                {"role": "user", "content": req.text}]
        try:
            raw = (await provider.generate(msgs, n=1, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout))[0]
        except Exception:
            continue
        out[e.id] = clean_text(raw, max_chars)
    return out


async def arm_engine(provider: LLMProvider, parts: Dict[str, Any], items: Sequence[st.Example], cfg: EngineConfig, name: str,
                     blocked: Sequence[str], max_chars: int, candidates: int) -> Tuple[Dict[str, str], Dict[str, int]]:
    c = EngineConfig(**{**cfg.__dict__, "candidates": candidates, "seed": cfg.seed if cfg.seed is not None else 0})
    eng = PersonaEngine(person_id=name.lower(), real_name=name, names=[name], store=parts["store"], profile=parts["profile"], scorer=parts["scorer"],
                        provider=provider, cfg=c, blocked=blocked, max_chars=max_chars)
    out: Dict[str, str] = {}
    for e in items:
        r = await eng.respond(request_for(e))
        if r is not None and r.source == "llm":
            out[e.id] = r.text
    return out, dict(eng.stats)


# ── optional LLM judge (contextual correctness) ──────────────────────────────
JUDGE_SYSTEM = "You grade replies in casual chat conversations. Answer with a single digit from 1 to 5 and nothing else."


async def llm_judge(provider: LLMProvider, pairs: Sequence[Tuple[str, str]], cfg: EngineConfig) -> Dict[str, Any]:
    """Mean 1-5 rating of how well each reply fits the message it answers (1 unrelated/nonsense, 3 acceptable, 5 fully fitting).

    Noisy and model-dependent: use a different, stronger model than the generator where possible, and compare arms
    judged by the same model only."""
    import re
    scores: List[int] = []
    for msg, rep in pairs:
        prompt = [{"role": "system", "content": JUDGE_SYSTEM},
                  {"role": "user", "content": f"Message: {msg}\nReply: {rep}\nHow well does the reply fit the message? (1-5)"}]
        try:
            raw = (await provider.generate(prompt, n=1, temperature=0.0, max_tokens=4, timeout=cfg.timeout))[0]
        except Exception:
            continue
        m = re.search(r"[1-5]", raw or "")
        if m:
            scores.append(int(m.group()))
    return {"mean": round(sum(scores) / len(scores), 3) if scores else None, "n": len(scores),
            "distribution": dict(sorted(Counter(scores).items())), "note": "LLM-judged; noisy; compare only arms judged by the same model"}


# ── composite ────────────────────────────────────────────────────────────────
def composite_score(system_report: Dict[str, Any], reference_attr: float, weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """One number per system, built from independent sub-scores in 0..1 (higher is better); the parts are reported too.

    context  : with an LLM judge: 0.6 x judged fit + 0.2 x token-F1 + 0.2 x reply-class agreement; without one: the last two
               equally (token-F1 against the real reply, rescaled 5x and capped at 1; reply-class agreement = 1 - JS).
               NOTE: without a judge this part is weak (a class-conditioned replay scores ~0.5 on it by construction).
    style    : 1 - mean(length_js, char3_js, feature_gap*4)  (distribution distance to the person's real replies)
    persona  : own-share attribution relative to the real replies' own-share, capped at 1
    safety   : 1 - (contact + placeholder + abusive rates)
    novelty  : 1 - copy rate (exact copies of training replies, counting replies of 3+ words)
    coverage : answered / test examples
    """
    w = weights or {"context": 0.30, "style": 0.30, "persona": 0.15, "safety": 0.10, "novelty": 0.05, "coverage": 0.10}
    sub = system_report["subsets"]["novel"] if system_report["subsets"].get("novel", {}).get("n") else system_report["subsets"]["all"]
    if not sub.get("n"):
        return {"composite": None, "parts": {}}
    f1_part, cls_part = min(1.0, 5.0 * sub.get("token_f1_mean", 0.0)), 1.0 - sub.get("reply_class_js", 1.0)
    judge = (system_report.get("context_judge") or {}).get("mean")
    context = (0.6 * (judge - 1.0) / 4.0 + 0.2 * f1_part + 0.2 * cls_part) if judge else (0.5 * f1_part + 0.5 * cls_part)
    style = 1.0 - (sub["length_js"] + sub["char3_js"] + min(1.0, 4 * sub["feature_gap"])) / 3.0
    own = system_report["attribution"].get("own_share", 0.0)
    persona = min(1.0, own / max(1e-6, reference_attr))
    safety = 1.0 - min(1.0, sum(system_report["safety"].get(k, 0.0) for k in ("contact_data_rate", "placeholder_rate", "abusive_rate")))
    mem = system_report["memorization"]
    novelty = 1.0 - (mem["exact_copy_rate_3plus"] if mem.get("replies_3plus", 0) >= 10 else mem.get("exact_copy_rate_long", 0.0))
    coverage = system_report.get("coverage", 0.0)
    parts = {"context": context, "style": style, "persona": persona, "safety": safety, "novelty": novelty, "coverage": coverage}
    total = sum(w.values())
    return {"composite": round(sum(w[k] * parts[k] for k in w) / total, 4), "parts": {k: round(v, 4) for k, v in parts.items()}, "weights": w}


# ── driver ───────────────────────────────────────────────────────────────────
def evaluate_predictions(preds: Dict[str, str], test: Sequence[st.Example], train_texts: Sequence[str], nb: evalkit.CharNB, person: str, blocked: Sequence[str]) -> Dict[str, Any]:
    tests = [{"id": e.id, "target": e.reply, "flags": [], "tk": e.kind} for e in test]
    return evalkit.evaluate_system(preds, tests, train_texts, nb, person, blocked)


def run(args: argparse.Namespace) -> int:
    registry = common.load_registry(Path(args.registry))
    blocked = registry.get("replay_filter", {}).get("abusive_tokens", [])
    d = Path(args.data)
    manifest = common.verify_prepared_dir(d, ("train", "val", "test"))
    person = manifest["person"]
    p = registry["persons"][person]
    names = [p["real_name"], *p.get("aliases", [])]
    train = load_split(d, ("train",), blocked)
    test_ex = load_split(d, (args.split,), blocked)
    max_chars = int(manifest["config"]["target_max_chars"])
    other_replies: List[str] = []
    nb_labels = {person: [e.reply for e in train]}
    for o in args.other or []:
        om = common.verify_prepared_dir(Path(o), ("train",))
        others = st.from_prepared(Path(o), ("train",), blocked)
        nb_labels[om["person"]] = [e.reply for e in others]
        other_replies += [e.reply for e in others if e.reply.strip()]
    parts = build_parts(train, blocked, names, max_chars, other_replies, seed=args.seed, n_perm=args.perms,
                        use_assoc=not args.no_assoc, assoc_weight=args.assoc_weight)
    nb = evalkit.fit_attribution(nb_labels)
    test_items = eligible(test_ex)
    rng = random.Random(args.seed)
    rng.shuffle(test_items)
    if args.limit:
        test_items = test_items[: args.limit]
    report: Dict[str, Any] = {
        "schema": 1, "person": person, "dataset_sha256": manifest["dataset_sha256"], "test_sha256": manifest["splits"]["test"]["sha256"],
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "split": "conversation-level; index/profile/LM built from train only",
        "sizes": {"train": len(train), "test_total": len(test_ex), "test_with_message_to_answer": len(eligible(test_ex)), "evaluated": len(test_items)},
        "scorer_discrimination": scorer_discrimination(parts, test_ex, other_replies, train, seed=args.seed),
        "retrieval": retrieval_report(parts, train, test_ex, seed=args.seed),
        "context_dependence_validation": context_dependence_validation(parts, test_ex),
        "repetition": repetition_report(parts, train, test_ex, seed=args.seed),
        "arms": {},
    }
    train_texts = [e.reply for e in train]
    ref_attr = evalkit.attribution([e.reply for e in test_items], nb, person).get("own_share", 1.0)
    # offline replay arms (no model): v2.9-style retrieval replay and the engine's evidence-only 'best past reply'
    prepared_train = list(common.iter_jsonl(d / "train.jsonl"))
    prepared_test = [x for x in common.iter_jsonl(d / f"{args.split}.jsonl") if x["id"] in {e.id for e in test_items}]
    from training import baselines
    replay = baselines.retrieval(prepared_train, prepared_test)
    report["arms"]["retrieval_replay_v29_style"] = {"status": "run (offline)", **evaluate_predictions(replay, test_items, train_texts, nb, person, blocked)}
    if args.provider_url and args.provider_model:
        provider = OpenAICompatProvider(args.provider_url, args.provider_model, args.provider_key)
        cfg = EngineConfig(seed=args.seed, temperature=args.temperature)

        async def go() -> None:
            baseline = await arm_baseline_llm(provider, test_items, cfg, max_chars)
            rag = await arm_pure_rag(provider, parts, test_items, cfg, max_chars, names[0])
            eng1, s1 = await arm_engine(provider, parts, test_items, cfg, names[0], blocked, max_chars, 1)
            engn, sn = await arm_engine(provider, parts, test_items, cfg, names[0], blocked, max_chars, args.candidates)
            by_id = {e.id: e for e in test_items}
            for name, preds, stats in (("baseline_llm", baseline, None), ("pure_rag", rag, None), ("engine", eng1, s1), ("engine_rerank", engn, sn)):
                rep = evaluate_predictions(preds, test_items, train_texts, nb, person, blocked)
                rep["status"] = "run"
                if args.judge:
                    rep["context_judge"] = await llm_judge(provider, [(request_for(by_id[i]).text, r) for i, r in preds.items() if r], cfg)
                if stats:
                    rep["engine_stats"] = stats
                report["arms"][name] = rep
        asyncio.run(go())
        report["model"] = {"provider": "openai_compat", "model": args.provider_model, "temperature": args.temperature, "candidates_for_rerank": args.candidates}
    else:
        for name in ("baseline_llm", "pure_rag", "engine", "engine_rerank"):
            report["arms"][name] = {"status": "NOT RUN - needs --provider-url/--provider-model (no model in this environment)"}
    for name, arm in report["arms"].items():
        if arm.get("status", "").startswith("run"):
            arm["composite"] = composite_score(arm, ref_attr)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    common.atomic_write_json(Path(args.out), report)
    print(json.dumps({k: report[k] for k in ("person", "sizes")}, indent=1))
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--other", action="append", help="another person's prepared dir (distractors + contamination check)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--registry", default=str(common.DEFAULT_REGISTRY))
    ap.add_argument("--split", choices=("test", "val"), default="test", help="design decisions use val; reported numbers use test")
    ap.add_argument("--no-assoc", action="store_true", help="disable the trigger->reply association signal in the context score")
    ap.add_argument("--assoc-weight", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--perms", type=int, default=200)
    ap.add_argument("--limit", type=int, default=300, help="test examples per generation arm (cost control)")
    ap.add_argument("--provider-url")
    ap.add_argument("--provider-model")
    ap.add_argument("--provider-key")
    ap.add_argument("--temperature", type=float, default=0.85)
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--judge", action="store_true", help="also grade contextual fit of every arm with the provider model (extra calls)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        return run(args)
    except (common.TrainingDataError, KeyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
