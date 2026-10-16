"""Two-step persona API for callers that run their own model (compatible with the v2.11 PERSONA_ENGINE overlay).

    engine = get_engine("tanvi")
    payload = prepare_persona_reply(engine, user_text, chat_id=str(chat.id))
    if payload["direct_reply"]:                 # identity question: fixed disclosure, no model call
        reply = payload["direct_reply"]
    else:
        raw = await my_llm.chat(payload["messages"])
        reply = engine.finalize(str(chat.id), raw)   # None -> a safety gate rejected it: fall back (e.g. retrieval persona)

Differences from the overlay this replaces (it told the model "Do NOT claim to be an AI" and had no identity handling):

* the prompt is the main engine's: the bot says plainly that it is a bot imitating the person's chat style;
* ``finalize`` returns the cleaned reply, or None if a hard gate rejects it (it never forwards an unsafe reply);
* ``get_engine`` requires the person to be in ``registry.json`` (the registry is the identity authority; display names never are);
* ``payload["evidence"]`` carries ids and scores only (the engine never exposes dataset text outside the prompt);
* ``rebuild_profile`` rebuilds the behaviour profile in memory (``save=True`` also rewrites ``behavior_profile.json``).

Opt-out is a database state the main reply path checks; callers of this API must check it too:
``if not await is_imitation_allowed(person_id): ...`` (fails closed when the state is unknowable).
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from bot.persona_engine import EngineConfig, EngineRequest, PersonaEngine
from bot.persona_engine.antirepeat import RepetitionGuard
from bot.persona_engine.memory import ConversationMemory
from bot.persona_engine.profile import BehaviorProfile
from bot.persona_engine.runtime import PROFILE_FILE, account_tags, build_components, sha256_file
from bot.persona_engine.store import from_corpus

_lock = threading.Lock()
_ENGINES: Dict[str, "LegacyEngine"] = {}
_GUARD = RepetitionGuard()
_MEMORY = ConversationMemory()


def _persons_module():
    """``bot.personality.persons`` (stdlib-only) without requiring the rest of the package's dependencies."""
    try:
        from bot.personality import persons as mod
        return mod
    except Exception:  # dependency stack missing (offline tools, tests): load the file by path
        name = "_neko_persons_standalone"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / "persons.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _chat_int(chat_id: Union[int, str]) -> int:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return zlib.crc32(str(chat_id).encode("utf-8")) or 1  # stable positive id for non-numeric keys


class LegacyEngine:
    """Thin facade over :class:`bot.persona_engine.PersonaEngine` with the overlay's method names."""

    def __init__(self, person_id: str, real_name: str, core: PersonaEngine, data: Any) -> None:
        self.person_id, self.real_name, self.core, self.data = person_id, real_name, core, data
        self._evidence: Dict[int, List[str]] = {}

    @property
    def profile(self) -> BehaviorProfile:
        return self.core.profile

    def prepare(self, user_message: str, chat_id: Union[int, str] = "default", extra_context: str = "") -> Dict[str, Any]:
        cid = _chat_int(chat_id)
        notes = [" ".join(extra_context.split())[:300]] if extra_context and extra_context.strip() else []
        req = EngineRequest(chat_id=cid, text=user_message or "", notes=notes, chat_kind="group" if cid < 0 else "dm")
        self.core.observe_user(req)
        prep = self.core.prepare(req)
        self._evidence[cid] = [ev.example.id for ev in prep.evidence]
        if len(self._evidence) > 500:  # bounded: forget the oldest chats
            for k in list(self._evidence)[:100]:
                self._evidence.pop(k, None)
        return {
            "person_id": self.person_id,
            "messages": prep.messages,
            "direct_reply": prep.direct_reply,
            "evidence": [{"id": ev.example.id, "score": round(ev.score, 3)} for ev in prep.evidence],
            "profile_summary": {
                "messages": self.profile.n_messages, "unusual_words": self.profile.unusual_words(8),
                "latency": self.profile.latency(), "style_lines": self.profile.describe(None, None)[:12],
            },
        }

    def finalize(self, chat_id: Union[int, str], response: str) -> Optional[str]:
        cid = _chat_int(chat_id)
        return self.core.finalize(cid, response, self._evidence.get(cid, ()))

    def rebuild_profile(self, save: bool = False) -> BehaviorProfile:
        corpus = Path(self.data.dir) / "corpus.jsonl"
        blocked = self.core.blocked
        examples = from_corpus(corpus, self.person_id, account_tags(self.data.person), blocked)
        prof = BehaviorProfile.build(examples, person=self.person_id, blocked=blocked, background=[e.trigger for e in examples if e.trigger],
                                     source={"corpus_sha256": sha256_file(corpus), "corpus_rows": len(examples)})
        self.core.profile = prof
        if save:
            (Path(self.data.dir) / PROFILE_FILE).write_text(prof.to_json() + "\n", encoding="utf-8")
        return prof


def get_engine(person_id: str, real_name: str = "") -> LegacyEngine:
    """The (cached) engine for a registered person.  The first call reads the corpus: about 2-4 s for ~10k messages."""
    with _lock:
        eng = _ENGINES.get(person_id)
        if eng is not None:
            return eng
        svc = _persons_module().get_service()
        data = svc.data(person_id)
        if data is None:
            raise KeyError(f"'{person_id}' is not in the persons registry")
        if not (Path(data.dir) / "corpus.jsonl").exists():
            raise FileNotFoundError(f"no corpus for '{person_id}' in {data.dir}")
        core = build_components(data, svc.registry.abusive_tokens, None, EngineConfig(use_long_term_memory=False),
                                guard=_GUARD, short=_MEMORY, long=None)
        eng = _ENGINES[person_id] = LegacyEngine(person_id, real_name or data.person.real_name, core, data)
        return eng


def prepare_persona_reply(engine: LegacyEngine, user_message: str, chat_id: Union[int, str] = "default", extra_context: str = "") -> Dict[str, Any]:
    return engine.prepare(user_message, chat_id=chat_id, extra_context=extra_context)


def list_available_persons() -> List[str]:
    """Ids of registered persons that have a corpus on disk."""
    svc = _persons_module().get_service()
    return sorted(p.id for p in svc.registry.persons() if (Path(svc.dir) / p.id / "corpus.jsonl").exists())


async def is_imitation_allowed(person_id: str) -> bool:
    """False when the person has opted out of being imitated (or the state cannot be read: fail closed)."""
    try:
        from bot.personality import person_consent
        return not await person_consent.is_opted_out(person_id)
    except Exception:
        return False


def _reset_for_tests() -> None:
    with _lock:
        _ENGINES.clear()
