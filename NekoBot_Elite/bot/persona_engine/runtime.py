"""Process-wide wiring: builds one engine per person from the shipped data and shares memory/guard/provider.

Duck-typed on purpose: ``data`` is bot.personality.persons.PersonaData (needs ``.dir``, ``.person`` with ``id``, ``real_name``,
``aliases``, ``accounts[].id`` and ``.max_chars(account_id)``), so this module imports nothing from the bot's heavy packages.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from .antirepeat import RepetitionGuard
from .core import words
from .engine import EngineConfig, PersonaEngine
from .memory import ConversationMemory, LongTermMemory
from .profile import BehaviorProfile
from .providers import LLMProvider, choose_provider, describe_provider
from .resilience import CircuitBreaker
from .scoring import CandidateScorer, PersonaLM
from .store import ExampleStore, from_corpus

log = logging.getLogger("bot.persona_engine")
PROFILE_FILE = "behavior_profile.json"


def account_tags(person: Any) -> Dict[str, str]:
    return {str(a.id): f"a{i + 1}" for i, a in enumerate(person.accounts)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_components(data: Any, blocked: Iterable[str], provider: Optional[LLMProvider], cfg: EngineConfig, *,
                     guard: RepetitionGuard, short: ConversationMemory, long: Optional[LongTermMemory],
                     n_perm: int = 300, use_shipped_profile: bool = True) -> PersonaEngine:
    """Blocking (about 2-4 s for ~10k messages): run in a thread."""
    person, d = data.person, Path(data.dir)
    corpus = d / "corpus.jsonl"
    blocked = tuple(blocked)
    examples = from_corpus(corpus, person.id, account_tags(person), blocked)
    background = [e.trigger for e in examples if e.trigger]
    corpus_hash = sha256_file(corpus)
    profile: Optional[BehaviorProfile] = None
    shipped = d / PROFILE_FILE
    if use_shipped_profile and shipped.exists():
        try:
            cand = BehaviorProfile.from_json(shipped.read_text(encoding="utf-8"))
            if cand.source.get("corpus_sha256") == corpus_hash:
                profile = cand
            else:
                log.warning("behaviour profile for %s was built from a different corpus; rebuilding in memory", person.id)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("behaviour profile for %s unreadable (%s); rebuilding in memory", person.id, exc)
    if profile is None:
        profile = BehaviorProfile.build(examples, person=person.id, blocked=blocked, background=background, n_perm=n_perm,
                                        source={"corpus_sha256": corpus_hash})
    own = [e.reply for e in examples if e.kind in ("text", "emoji_only") and e.reply.strip()]
    lm = PersonaLM(own, background)
    names = [person.real_name, *getattr(person, "aliases", ())]
    max_chars = max([data.max_chars(a.id) for a in person.accounts] or [160])
    scorer = CandidateScorer(
        profile, lm, frozenset(w for t in own for w in words(t)), frozenset(w for t in background for w in words(t)),
        blocked=blocked, names=names, max_chars=max_chars, weights=cfg.weights,
    )
    return PersonaEngine(
        person_id=person.id, real_name=person.real_name, names=names, store=ExampleStore(examples), profile=profile, scorer=scorer,
        provider=provider, cfg=cfg, guard=guard, short_memory=short, long_memory=long, blocked=blocked, max_chars=max_chars,
        breaker=CircuitBreaker(3, 60.0),
    )


def config_from_settings(settings: Any) -> EngineConfig:
    g = lambda name, default: getattr(settings, name, default)  # noqa: E731
    return EngineConfig(
        candidates=int(g("persona_engine_candidates", 3)), examples=int(g("persona_engine_examples", 4)),
        temperature=float(g("persona_engine_temperature", 0.85)), timeout=float(g("persona_engine_timeout_seconds", 14.0)),
        regenerate=int(g("persona_engine_regenerate", 1)), use_long_term_memory=bool(g("persona_engine_memory_enabled", False)),
    )


class EngineService:
    def __init__(self, settings: Any, abusive_tokens: Iterable[str] = (), provider: Optional[LLMProvider] = None) -> None:
        self.settings = settings
        self.cfg = config_from_settings(settings)
        self.abusive = tuple(abusive_tokens)
        self._provider = provider
        self._provider_resolved = provider is not None
        self.guard, self.short = RepetitionGuard(), ConversationMemory()
        self.long: Optional[LongTermMemory] = None
        if bool(getattr(settings, "persona_engine_memory_enabled", False)):
            self.long = LongTermMemory(Path(settings.data_dir) / "persona_engine" / "memory.sqlite3")
        self._engines: Dict[str, PersonaEngine] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.debug_chats: Set[int] = set()

    @property
    def provider(self) -> Optional[LLMProvider]:
        if not self._provider_resolved:
            self._provider = choose_provider(self.settings)
            self._provider_resolved = True
        return self._provider

    async def engine_for(self, data: Any) -> Optional[PersonaEngine]:
        pid = data.person.id
        eng = self._engines.get(pid)
        if eng is not None:
            eng.provider = self.provider  # picks up a provider that became available later
            return eng
        if not (Path(data.dir) / "corpus.jsonl").exists():
            return None
        lock = self._locks.setdefault(pid, asyncio.Lock())
        async with lock:
            if pid not in self._engines:
                try:
                    self._engines[pid] = await asyncio.to_thread(
                        build_components, data, self.abusive, self.provider, self.cfg,
                        guard=self.guard, short=self.short, long=self.long)
                except Exception as exc:  # a broken data file must never break chat replies
                    log.warning("persona engine for %s could not be built: %s: %s", pid, type(exc).__name__, exc)
                    return None
            return self._engines[pid]

    def status(self) -> Dict[str, Any]:
        """Operator view: configuration and counters only - never dataset or conversation content."""
        return {
            "provider": describe_provider(self.provider), "candidates": self.cfg.candidates, "examples": self.cfg.examples,
            "long_term_memory": self.long is not None,
            "engines": {pid: {"examples": len(e.store), "evidence_pool": e.store.n_trigger_examples, "profile_messages": e.profile.n_messages,
                              "breaker_open": e.breaker.is_open, **e.stats} for pid, e in self._engines.items()},
        }


_service: Optional[EngineService] = None
_service_lock = threading.Lock()


def get_service(settings: Any, abusive_tokens: Iterable[str] = ()) -> EngineService:
    global _service
    with _service_lock:
        if _service is None:
            _service = EngineService(settings, abusive_tokens)
        return _service


def reset_service() -> None:
    global _service
    with _service_lock:
        _service = None
