"""The persona engine: situation -> plan -> selective evidence -> generation -> checks -> rerank -> memory.

    message + conversation
        -> situation analysis                      (dialogue act, language, cues)
        -> response plan sampled from the person's own conditional distributions
        -> selective, diverse retrieval of *behavioural* evidence (never the personality itself)
        -> LLM generation of NEW candidate replies
        -> hard safety / identity gates, copy detection
        -> scoring: context, persona, linguistic, diversity, naturalness, evidence
        -> anti-repetition, then the best candidate (or None: the caller falls back to the v2.9 retrieval persona)
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .antirepeat import RepetitionGuard
from .core import analyze, content_tokens, norm
from .identity import disclosure_reply, is_identity_probe
from .memory import ConversationMemory, LongTermMemory, Turn
from .profile import BehaviorProfile
from .prompt import PromptBuilder
from .providers import LLMProvider
from .resilience import CircuitBreaker
from .safety import clean_text, hard_gates
from .scoring import CandidateScorer, ScoreWeights, Scores
from .store import ExampleStore, Query, RetrievalConfig, SelectiveRetriever


@dataclass
class EngineConfig:
    candidates: int = 3
    examples: int = 4
    temperature: float = 0.85
    max_tokens: int = 100
    timeout: float = 14.0
    regenerate: int = 1
    min_composite: float = 0.40
    similarity_threshold: float = 0.85
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    seed: Optional[int] = None          # None = fresh randomness per request; set for reproducible evaluation
    use_long_term_memory: bool = True


@dataclass
class EngineRequest:
    chat_id: int
    text: str
    user_key: Optional[str] = None
    reply_to_text: Optional[str] = None
    acct: Optional[str] = None          # "a1", "a2", ... (registry order) or None
    chat_ref: Optional[str] = None      # one-way chat reference, only used as a soft retrieval signal
    chat_kind: str = "group"
    history: Optional[Sequence[Tuple[str, str]]] = None   # override of the short-term memory (evaluation)
    request_id: str = ""
    notes: Sequence[str] = ()           # extra instructions for this request (e.g. who the user is)


@dataclass
class EngineResult:
    text: str
    source: str                          # "llm" | "identity_probe"
    scores: Optional[Scores] = None
    delay: float = 0.0
    trace: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedRequest:
    """Everything that happens before the model call: situation, plan, evidence and the finished prompt.

    ``direct_reply`` is set instead of ``messages`` when the message sincerely asks what the bot is: the fixed disclosure
    answers and no model must be called.
    """
    messages: List[Dict[str, str]]
    sit: Any = None
    plan: Any = None
    evidence: List[Any] = field(default_factory=list)
    memory_facts: List[str] = field(default_factory=list)
    trace: Dict[str, Any] = field(default_factory=dict)
    direct_reply: Optional[str] = None


class PersonaEngine:
    def __init__(
        self, *, person_id: str, real_name: str, names: Sequence[str], store: ExampleStore, profile: BehaviorProfile,
        scorer: CandidateScorer, provider: Optional[LLMProvider], cfg: Optional[EngineConfig] = None,
        guard: Optional[RepetitionGuard] = None, short_memory: Optional[ConversationMemory] = None,
        long_memory: Optional[LongTermMemory] = None, blocked: Iterable[str] = (), max_chars: int = 160,
        breaker: Optional[CircuitBreaker] = None, retrieval: Optional[RetrievalConfig] = None,
    ) -> None:
        self.person_id, self.real_name, self.names = person_id, real_name, tuple(names)
        self.store, self.profile, self.scorer, self.provider = store, profile, scorer, provider
        self.cfg = cfg or EngineConfig()
        self.guard = guard or RepetitionGuard()
        self.short = short_memory or ConversationMemory()
        self.long = long_memory
        self.blocked = tuple(blocked)
        self.max_chars = max_chars
        self.breaker = breaker or CircuitBreaker(3, 60.0)
        w = profile.retrieval_weights()
        rc = retrieval or RetrievalConfig()
        rc.k = self.cfg.examples
        rc.chat_bonus, rc.acct_bonus = w.get("chat_bonus", 0.0), w.get("acct_bonus", 0.0)
        self.retriever = SelectiveRetriever(store, rc)
        self.prompts = PromptBuilder(real_name)
        self.stats: Dict[str, int] = {"requests": 0, "identity_probe": 0, "llm_ok": 0, "no_provider": 0, "breaker_open": 0,
                                      "provider_error": 0, "all_rejected": 0, "regenerated": 0, "repeat_avoided": 0}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _rng(self, req: EngineRequest) -> random.Random:
        if self.cfg.seed is None:
            return random.Random()
        return random.Random(int(hashlib.sha256(f"{self.cfg.seed}|{req.request_id or req.chat_id}|{req.text}".encode()).hexdigest()[:12], 16))

    def _turns(self, req: EngineRequest) -> List[Turn]:
        """Conversation BEFORE the message being answered (the message itself is passed separately)."""
        if req.history is not None:
            return [Turn("persona" if w in ("me", "persona", "bot") else w, t, 0.0, "CURRENT_CONTEXT") for w, t in req.history]
        turns = self.short.turns(req.chat_id)
        if turns and turns[-1].who != "persona" and norm(turns[-1].text) == norm(req.text[: self.short.turn_chars]):
            turns = turns[:-1]
        return turns

    @staticmethod
    def _open_question(turns: Sequence[Turn]) -> Optional[str]:
        for t in reversed(turns):
            if t.who == "persona":
                return None
            return t.text if analyze(t.text).family == "question" else None
        return None

    # ── two-step API for callers that run their own model ────────────────────
    def prepare(self, req: EngineRequest, memory_facts: Optional[Sequence[str]] = None, rng: Optional[random.Random] = None) -> PreparedRequest:
        """Situation -> plan -> selective evidence -> finished prompt.  No model call, no side effects on the reply history.

        ``memory_facts`` lets async callers pass facts they already fetched; when omitted and long-term memory is on they
        are read here (one small SQLite query).  A sincere "are you really <person>/a bot?" returns ``direct_reply``.
        """
        if is_identity_probe(req.text, self.names):
            return PreparedRequest(messages=[], direct_reply=disclosure_reply(self.real_name), trace={"stage": "identity_probe"})
        rng = rng or self._rng(req)
        if memory_facts is None:
            memory_facts = self.long.search(req.chat_id, self.person_id, req.text, 3) if (self.long is not None and self.cfg.use_long_term_memory) else []
        facts = list(memory_facts)
        sit = analyze(req.text, has_reply_target=bool(req.reply_to_text))
        plan = self.profile.sample_plan(sit, rng, req.acct, req.chat_ref)
        turns = self._turns(req)
        used = self.guard.used_evidence(req.chat_id)
        q = Query(text=req.text, sit=sit, ctx_text=req.reply_to_text or " ".join(t.text for t in turns[-3:] if t.who != "persona"),
                  acct=req.acct, chat=req.chat_ref, plan=plan, used_ids=used)
        evidence = self.retriever.retrieve(q)
        messages = self.prompts.build(
            current=req.text, reply_to=req.reply_to_text, turns=turns, sit=sit, plan=plan,
            profile_facts=self.profile.describe(sit, req.acct), evidence=evidence, memory_facts=facts,
            topic=self.short.topic(req.chat_id) if req.history is None else (), open_question=self._open_question(turns),
            extra=req.notes,
        )
        trace: Dict[str, Any] = {
            "situation": sit.act, "plan": plan.lines(), "evidence": [{"id": ev.example.id, "score": round(ev.score, 3)} for ev in evidence],
            "memory_facts": len(facts), "attempts": [],
        }
        return PreparedRequest(messages=messages, sit=sit, plan=plan, evidence=list(evidence), memory_facts=facts, trace=trace)

    def finalize(self, chat_id: int, raw_reply: str, evidence_ids: Sequence[str] = ()) -> Optional[str]:
        """Check and register a reply that the caller's own model produced from ``prepare(...).messages``.

        Applies the same cleaning and hard gates as ``respond`` (contact data, blocked words, claims to be the real person,
        AI talk, length), then records the reply for the repetition guard and short-term memory.  Returns the cleaned text,
        or None when a gate rejects it - the caller should then fall back (e.g. to the retrieval persona).
        """
        text = clean_text(raw_reply, self.max_chars, self.names)
        if hard_gates(text, blocked=self.blocked, names=self.names, max_chars=self.max_chars):
            return None
        text = self.guard.diversify(chat_id, text)
        if hard_gates(text, blocked=self.blocked, names=self.names, max_chars=self.max_chars) or self.guard.too_similar(chat_id, text, self.cfg.similarity_threshold):
            return None
        self.guard.remember(chat_id, text, list(evidence_ids))
        self.short.observe_bot(chat_id, text)
        return text

    # ── main entry ───────────────────────────────────────────────────────────
    async def respond(self, req: EngineRequest) -> Optional[EngineResult]:
        """A reply, or None meaning "use the v2.9 retrieval persona" (no provider, breaker open, all candidates rejected)."""
        t0 = time.monotonic()
        self.stats["requests"] += 1
        if is_identity_probe(req.text, self.names):
            self.stats["identity_probe"] += 1
            text = disclosure_reply(self.real_name)
            if req.history is None:
                self.short.observe_bot(req.chat_id, text)
            return EngineResult(text, "identity_probe", trace={"stage": "identity_probe"})
        if self.provider is None:
            self.stats["no_provider"] += 1
            return None
        if not self.breaker.allow():
            self.stats["breaker_open"] += 1
            return None
        rng = self._rng(req)
        memory_facts: List[str] = []
        if self.long is not None and self.cfg.use_long_term_memory:
            memory_facts = await asyncio.to_thread(self.long.search, req.chat_id, self.person_id, req.text, 3)
        prep = self.prepare(req, memory_facts, rng=rng)
        sit, plan, evidence, messages, trace = prep.sit, prep.plan, prep.evidence, prep.messages, prep.trace
        trigger_tokens = content_tokens(req.text)
        ev_replies = [ev.example.reply for ev in evidence]
        best: Optional[Tuple[str, Scores]] = None
        attempts = 1 + max(0, self.cfg.regenerate)
        for attempt in range(attempts):
            temp = min(1.3, self.cfg.temperature + 0.2 * attempt)
            try:
                raws = await self.provider.generate(messages, n=max(1, self.cfg.candidates), temperature=temp,
                                                    max_tokens=self.cfg.max_tokens, timeout=self.cfg.timeout)
            except Exception as exc:
                self.breaker.fail()
                self.stats["provider_error"] += 1
                trace["attempts"].append({"error": type(exc).__name__})
                break
            self.breaker.ok()
            scored: List[Tuple[str, Scores]] = []
            seen: set = set()
            rejected: Dict[str, int] = {}
            for raw in raws:
                text = clean_text(raw, self.max_chars, self.names)
                if not text or norm(text) in seen:
                    continue
                seen.add(norm(text))
                text = self.guard.diversify(req.chat_id, text)
                pen = self.guard.penalty(req.chat_id, text, self.profile.recurring())
                sc = self.scorer.score(text, sit, plan, trigger_tokens, pen, req.acct, ev_replies)
                if self.guard.too_similar(req.chat_id, text, self.cfg.similarity_threshold):
                    sc.gates.append("repeats_recent")
                    self.stats["repeat_avoided"] += 1
                if sc.gates:
                    for g in sc.gates:
                        rejected[g] = rejected.get(g, 0) + 1
                    continue
                scored.append((text, sc))
            trace["attempts"].append({"candidates": len(raws), "kept": len(scored), "rejected": rejected,
                                      "scores": [s.as_dict() for _t, s in scored]})
            if scored:
                scored.sort(key=lambda ts: (-ts[1].composite, ts[0]))
                cand = scored[0]
                if best is None or cand[1].composite > best[1].composite:
                    best = cand
                if cand[1].composite >= self.cfg.min_composite:
                    break
            if attempt + 1 < attempts:
                self.stats["regenerated"] += 1
        if best is None or best[1].composite < self.cfg.min_composite:
            self.stats["all_rejected"] += 1
            trace["stage"] = "rejected"
            return None
        text, sc = best
        self.stats["llm_ok"] += 1
        self.guard.remember(req.chat_id, text, [ev.example.id for ev in evidence])
        if req.history is None:
            self.short.observe_bot(req.chat_id, text)
        trace.update(stage="llm", provider=self.provider.name, composite=round(sc.composite, 3), ms=int((time.monotonic() - t0) * 1000))
        return EngineResult(text, "llm", sc, self.profile.suggest_delay(rng), trace)

    def observe_user(self, req: EngineRequest) -> None:
        """Record the incoming message in short-term memory (call before ``respond``)."""
        if req.history is None:
            self.short.observe_user(req.chat_id, req.user_key, req.text)
