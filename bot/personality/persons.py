"""Real-person personas: identity resolution + dataset-driven voice.

Layers (see docs/PERSON_PERSONAS.md)::

    person  ->  accounts (Telegram ids / usernames)      registry.json   (authoritative, hand-maintained)
            ->  conversation history (corpus.jsonl)       built from raw exports, scrubbed, with provenance
            ->  persona profile (profile.json)            statistics computed from the corpus
            ->  behavioural model                         retrieval of the person's own replies + LLM conditioning

Identity resolution is deterministic: numeric account id first, then username.  Display names are
never used to decide who a Telegram user is (they are trivially copied), only as skin aliases.

This module is stdlib-only (no bot/settings imports at import time) so it can be unit-tested and
used by tooling without the bot's dependency stack.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # normal case: imported as part of the bot package
    from bot.personality import persona_text as pt
except Exception:  # pragma: no cover - tooling/tests that load this file standalone
    import importlib.util as _ilu

    _p = Path(__file__).with_name("persona_text.py")
    _s = _ilu.spec_from_file_location("persona_text", _p)
    pt = _ilu.module_from_spec(_s)  # type: ignore[assignment]
    _s.loader.exec_module(pt)  # type: ignore[union-attr]

log = logging.getLogger("bot.personality.persons")

#: Minimum weighted similarity for a retrieved reply to be used verbatim.
RETRIEVAL_MIN_SCORE = 0.40
#: Candidates within this fraction of the best score are interchangeable (variety without drift).
RETRIEVAL_TIE_BAND = 0.90


# ── data model ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Account:
    id: int
    username: Optional[str]
    display_names: Tuple[str, ...] = ()
    variant_skin: Optional[str] = None
    evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    id: str
    real_name: str
    skin: str
    aliases: Tuple[str, ...]
    accounts: Tuple[Account, ...]

    @property
    def primary_account_id(self) -> int:
        return self.accounts[0].id

    def account(self, account_id: int) -> Optional[Account]:
        for a in self.accounts:
            if a.id == account_id:
                return a
        return None


@dataclass(frozen=True)
class Identity:
    person: Person
    account: Account
    via: str  # "id" | "username"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (name or "").strip().lower().replace(" ", "_").replace("-", "_"))


# ── registry ─────────────────────────────────────────────────────────────────
class PersonRegistry:
    """Immutable view over ``registry.json``.  An absent/invalid file yields an empty registry."""

    def __init__(self, persons: Sequence[Person] = (), abusive_tokens: Iterable[str] = ()) -> None:
        self._persons: Dict[str, Person] = {}
        self._by_id: Dict[int, Tuple[Person, Account]] = {}
        self._by_username: Dict[str, Tuple[Person, Account]] = {}
        self._skin_owner: Dict[str, Tuple[Person, Optional[Account]]] = {}
        self._alias: Dict[str, str] = {}
        self.abusive_tokens = frozenset(t.lower() for t in abusive_tokens)
        for p in persons:
            self._add(p)

    def _add(self, p: Person) -> None:
        if p.id in self._persons:
            raise ValueError(f"duplicate person id: {p.id}")
        if not p.accounts:
            raise ValueError(f"person '{p.id}' has no accounts")
        self._persons[p.id] = p
        for a in p.accounts:
            if a.id in self._by_id:
                raise ValueError(f"account id {a.id} belongs to two persons")
            self._by_id[a.id] = (p, a)
            if a.username:
                key = a.username.lower()
                if key in self._by_username:
                    raise ValueError(f"username @{a.username} belongs to two persons")
                self._by_username[key] = (p, a)
            if a.variant_skin:
                self._claim_skin(a.variant_skin, p, a)
        self._claim_skin(p.skin, p, None)
        for alias in (p.skin, p.id, *p.aliases):
            self._claim_alias(_slug(alias), p.skin)

    def _claim_skin(self, skin: str, p: Person, a: Optional[Account]) -> None:
        if skin in self._skin_owner and self._skin_owner[skin][0].id != p.id:
            raise ValueError(f"skin '{skin}' belongs to two persons")
        self._skin_owner[skin] = (p, a)
        self._claim_alias(_slug(skin), skin)

    def _claim_alias(self, alias: str, skin: str) -> None:
        if not alias:
            return
        if alias in self._alias and self._alias[alias] != skin:
            raise ValueError(f"alias '{alias}' maps to two skins")
        self._alias[alias] = skin

    # -- loading
    @classmethod
    def load(cls, path: Path) -> "PersonRegistry":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("person registry not found at %s — person personas disabled", path)
            return cls()
        except (OSError, ValueError) as exc:
            log.error("person registry unreadable (%s): %s — person personas disabled", path, exc)
            return cls()
        try:
            persons = []
            for pid, p in sorted((raw.get("persons") or {}).items()):
                accounts = tuple(
                    Account(
                        id=int(a["id"]),
                        username=(a.get("username") or None),
                        display_names=tuple(a.get("display_names") or ()),
                        variant_skin=a.get("variant_skin") or None,
                        evidence=tuple(a.get("evidence") or ()),
                    )
                    for a in p["accounts"]
                )
                persons.append(Person(
                    id=_slug(pid), real_name=str(p.get("real_name") or pid),
                    skin=_slug(p.get("skin") or pid),
                    aliases=tuple(_slug(x) for x in (p.get("aliases") or ())), accounts=accounts,
                ))
            return cls(persons, (raw.get("replay_filter") or {}).get("abusive_tokens") or ())
        except (KeyError, TypeError, ValueError) as exc:
            log.error("person registry invalid (%s): %s — person personas disabled", path, exc)
            return cls()

    # -- queries
    def persons(self) -> List[Person]:
        return [self._persons[k] for k in sorted(self._persons)]

    def person(self, person_id: str) -> Optional[Person]:
        return self._persons.get(_slug(person_id))

    def resolve(self, user_id: Optional[int] = None, username: Optional[str] = None) -> Optional[Identity]:
        """Account id wins; the username is consulted only when the id is unknown."""
        if user_id is not None:
            hit = self._by_id.get(int(user_id))
            if hit:
                return Identity(hit[0], hit[1], "id")
        if username:
            hit = self._by_username.get(username.lstrip("@").lower())
            if hit:
                return Identity(hit[0], hit[1], "username")
        return None

    def canonical_skin(self, name: str) -> str:
        """Map an alias / display identifier (``galaxy_anhilator``) to its skin name; others pass through."""
        return self._alias.get(_slug(name), name)

    def skin_owner(self, skin: str) -> Optional[Tuple[Person, Optional[Account]]]:
        return self._skin_owner.get(skin)

    def is_person_skin(self, skin: str) -> bool:
        return skin in self._skin_owner

    def skin_names(self) -> List[str]:
        return sorted(self._skin_owner)

    def __bool__(self) -> bool:
        return bool(self._persons)


# ── persona data (profile + retrieval index) ─────────────────────────────────
@dataclass
class _Exemplar:
    ctx: str
    ctx_kind: str
    reply: str
    acct: int
    toks: frozenset = field(default_factory=frozenset)


class PersonaData:
    """Lazy loader/index for one person's ``profile.json`` and ``corpus.jsonl``."""

    def __init__(self, person: Person, directory: Path) -> None:
        self.person = person
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._loaded = False
        self.profile: Dict[str, Any] = {}
        self._pairs: List[_Exemplar] = []
        self._idf: Dict[str, float] = {}
        self._inv: Dict[str, List[int]] = defaultdict(list)
        self.corrupt_lines = 0

    def load(self) -> "PersonaData":
        with self._lock:
            if self._loaded:
                return self
            try:
                self.profile = json.loads((self.dir / "profile.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("persona profile for %s unavailable: %s", self.person.id, exc)
                self.profile = {}
            self._load_corpus()
            self._loaded = True
        return self

    @property
    def available(self) -> bool:
        return bool(self.load().profile)

    def _load_corpus(self) -> None:
        path = self.dir / "corpus.jsonl"
        if not path.exists():
            return
        df: Counter = Counter()
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        self.corrupt_lines += 1
                        continue
                    # only replay-safe rows that carry a usable reply context become exemplars
                    if not r.get("rp") or not r.get("ctx") or r.get("ctx_kind") in (None, "structured", "empty"):
                        continue
                    toks = frozenset(pt.tokens(r["ctx"]))
                    if not toks:
                        continue
                    self._pairs.append(_Exemplar(r["ctx"], r["ctx_kind"], r["text"], int(r.get("acct") or 0), toks))
                    df.update(toks)
        except OSError as exc:
            log.warning("persona corpus for %s unreadable: %s", self.person.id, exc)
            return
        n = max(1, len(self._pairs))
        self._idf = {t: math.log(1 + n / c) for t, c in df.items()}
        for i, ex in enumerate(self._pairs):
            for t in ex.toks:
                self._inv[t].append(i)
        if self.corrupt_lines:
            log.warning("persona corpus for %s: %d unreadable lines skipped", self.person.id, self.corrupt_lines)

    # -- profile slices
    def _section(self, account_id: Optional[int]) -> Dict[str, Any]:
        prof = self.load().profile
        if account_id is not None:
            sec = (prof.get("accounts") or {}).get(str(account_id))
            if sec:
                return sec
        return prof

    def style(self, account_id: Optional[int] = None) -> Dict[str, Any]:
        return self._section(account_id).get("style") or {}

    def max_chars(self, account_id: Optional[int] = None) -> int:
        st = self.style(account_id)
        return max(60, min(400, int((st.get("chars") or {}).get("p99", 120)) + 20))

    # -- retrieval
    def _score(self, q: frozenset, ex: _Exemplar) -> float:
        shared = q & ex.toks
        if not shared:
            return 0.0
        num = sum(self._idf.get(t, 1.0) for t in shared)
        dq = math.sqrt(sum(self._idf.get(t, 1.0) for t in q) or 1.0)
        de = math.sqrt(sum(self._idf.get(t, 1.0) for t in ex.toks) or 1.0)
        return num / (dq * de)

    def retrieve(self, user_message: str, k: int = 4, account_id: Optional[int] = None) -> List[Tuple[float, _Exemplar]]:
        """Top-k (score, exemplar) pairs by IDF-weighted similarity; ties break by corpus order."""
        self.load()
        q = frozenset(pt.tokens(user_message))
        if not q or not self._pairs:
            return []
        cand: set = set()
        for t in q:
            cand.update(self._inv.get(t, ()))
        scored = []
        for i in cand:
            ex = self._pairs[i]
            if account_id is not None and ex.acct != account_id:
                continue
            s = self._score(q, ex)
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(s, self._pairs[i]) for s, i in scored[:k]]

    def reply(self, user_message: str, account_id: Optional[int] = None, rng: Optional[random.Random] = None) -> Optional[str]:
        """An in-voice reply made only of the person's own replay-safe words, or None if no data."""
        rng = rng or random
        self.load()
        hits = self.retrieve(user_message, k=8, account_id=account_id)
        q = frozenset(pt.tokens(user_message))
        need = min(2, len(q))  # one shared word is not evidence of relevance for a multi-word message
        hits = [(s, ex) for s, ex in hits if len(q & ex.toks) >= need]
        if hits and hits[0][0] >= RETRIEVAL_MIN_SCORE:
            best = hits[0][0]
            tied = [ex for s, ex in hits if s >= best * RETRIEVAL_TIE_BAND][:5]
            return rng.choice(tied).reply
        cls = pt.classify_context(user_message)
        sec = self._section(account_id)
        pools: List[List[List[Any]]] = []
        by_ctx = (sec.get("reply_by_context") or {}).get(cls)
        if by_ctx:
            pools.append(by_ctx)
        by_cls = (sec.get("short_replies") or {})
        utter = pt.classify_utterance(user_message)
        if not pools and utter in by_cls:
            pools.append(by_cls[utter])
        if not pools:
            merged = list(by_cls.get("other", [])) + list(by_cls.get("agree", []))
            if merged:
                pools.append(merged)
        if not pools:
            return None
        items = pools[0]
        weights = [max(1, int(c)) for _t, c in items]
        return rng.choices([t for t, _c in items], weights=weights, k=1)[0]

    def exemplars(self, user_message: str, k: int = 4, account_id: Optional[int] = None) -> List[Tuple[str, str]]:
        """(context, reply) pairs for few-shot conditioning: nearest first, topped up by context class."""
        out = [(ex.ctx, ex.reply) for _s, ex in self.retrieve(user_message, k=k, account_id=account_id)]
        if len(out) < k:
            cls = pt.classify_context(user_message)
            seen = {c for c, _r in out}
            for ex in self._pairs:
                if len(out) >= k:
                    break
                if ex.ctx_kind == cls and ex.ctx not in seen and (account_id is None or ex.acct == account_id):
                    out.append((ex.ctx, ex.reply))
                    seen.add(ex.ctx)
        return out[:k]

    # -- prompt material
    def style_facts(self, account_id: Optional[int] = None) -> List[str]:
        """Plain-language statements generated from measured numbers only (no adjectives, no guesses)."""
        st = self.style(account_id)
        if not st or not st.get("n"):
            return []
        w = st["words"]
        pctf = lambda x: f"{round(100 * float(x))}%"  # noqa: E731
        facts = [
            f"Messages are short: median {w['p50']} words, 90% are at most {w['p90']} words, "
            f"{pctf(st['share_le3_words'])} are 3 words or fewer.",
            f"{pctf(st['share_starts_upper'])} of messages start with a capital letter; "
            f"{pctf(st['share_all_lowercase'])} are all lowercase; only {pctf(st['share_end_punct'])} end in . ! or ?.",
        ]
        if st.get("share_emoji"):
            top = " ".join(e for e, _c in st.get("top_emoji", [])[:6])
            facts.append(f"{pctf(st['share_emoji'])} of messages contain an emoji (most used: {top}).")
        if st.get("share_elongation"):
            facts.append(f"{pctf(st['share_elongation'])} of messages stretch a word, e.g. 'hieee', 'nooo'.")
        if st.get("share_romanised_hindi", 0) >= 0.02:
            facts.append(f"{pctf(st['share_romanised_hindi'])} of messages mix in romanised Hindi (Hinglish).")
        openers = ", ".join(w_ for w_, _c in st.get("top_openers", [])[:8])
        if openers:
            facts.append(f"Most common first words: {openers}.")
        sr = self._section(account_id).get("short_replies") or {}
        sample = []
        for cls in ("agree", "disagree", "laughter", "question"):
            sample += [t for t, _c in (sr.get(cls) or [])[:3]]
        if sample:
            facts.append("Typical quick replies: " + " | ".join(sample) + ".")
        return facts


# ── LLM conditioning ─────────────────────────────────────────────────────────
_SAFETY = (
    "You are an AI chatbot imitating the *chat style* of {name}. You are not {name}. If anyone sincerely "
    "asks whether you are the real person or a bot, say plainly that you are a bot imitating their style.\n"
    "Do not invent personal facts (age, location, relationships, health, contact details, plans) and do not "
    "repeat private details that appear in the examples. Do not write anything defamatory or that could pass "
    "as a real statement by {name}. Keep to their typical message length. Reply with ONE chat message only: "
    "no quotation marks, no name label, no explanation."
)


def build_system_prompt(
    data: PersonaData, user_message: str, *, account_id: Optional[int] = None,
    speaker: Optional[Identity] = None, shots: int = 4, max_chars: int = 1400,
) -> str:
    name = data.person.real_name
    parts = [_SAFETY.format(name=name)]
    facts = data.style_facts(account_id)
    if facts:
        parts.append("Measured from their real messages:\n- " + "\n- ".join(facts))
    if speaker is not None:
        if speaker.person.id == data.person.id:
            parts.append(f"The user you are talking to is the real {name}; stay clearly a bot imitation.")
        else:
            parts.append(f"The user you are talking to is {speaker.person.real_name}.")
    shot_pairs = data.exemplars(user_message, k=shots, account_id=account_id)
    if shot_pairs:
        lines, total = [], 0
        for ctx, rep in shot_pairs:
            block = f"Them: {ctx}\n{name}: {rep}"
            if total + len(block) > max_chars:
                break
            lines.append(block)
            total += len(block) + 2
        if lines:
            parts.append("Style reference (real exchanges):\n\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


_RE_MD = re.compile(r"(\*\*|__|`+)")


def conform(text: str, person_name: str, max_chars: int) -> str:
    """Normalise an LLM reply to one short chat message (strip label/quotes/markdown, bound length)."""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.split("\n\n")[0].strip()
    t = _RE_MD.sub("", t).strip()
    t = re.sub(rf"^\s*{re.escape(person_name)}\s*[:：-]\s*", "", t, flags=re.IGNORECASE)
    if len(t) >= 2 and t[0] in "\"'“‘" and t[-1] in "\"'”’":
        t = t[1:-1].strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: stop + 1] if stop >= max_chars // 3 else cut.rsplit(" ", 1)[0]
    return t.strip()


# ── process-wide accessors ───────────────────────────────────────────────────
def default_persons_dir() -> Path:
    env = os.environ.get("PERSONS_DIR")
    if env:
        return Path(env)
    try:
        from bot.config import settings  # local import: keeps this module stdlib-only at import time

        return Path(settings.data_dir) / "personality" / "persons"
    except Exception:  # pragma: no cover
        return Path(__file__).resolve().parents[2] / "data" / "personality" / "persons"


class PersonService:
    """Registry + per-person data, loaded lazily from one directory."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory) if directory else default_persons_dir()
        self.registry = PersonRegistry.load(self.dir / "registry.json")
        self._data: Dict[str, PersonaData] = {}

    def data(self, person_id: str) -> Optional[PersonaData]:
        p = self.registry.person(person_id)
        if p is None:
            return None
        if p.id not in self._data:
            self._data[p.id] = PersonaData(p, self.dir / p.id)
        return self._data[p.id]

    def preload(self) -> int:
        """Load every person's profile + corpus now (call off the event loop).  Returns how many are usable."""
        n = 0
        for p in self.registry.persons():
            d = self.data(p.id)
            if d is not None and d.load().profile:
                n += 1
        return n

    def data_for_skin(self, skin: dict) -> Optional[Tuple[PersonaData, Optional[int]]]:
        """(data, variant account id) for a ``persona_kind == "person"`` skin dict, else None."""
        if not isinstance(skin, dict) or skin.get("persona_kind") != "person":
            return None
        d = self.data(str(skin.get("person_id") or ""))
        if d is None:
            return None
        acct = skin.get("variant_account")
        return d, (int(acct) if acct else None)

    def reply_for_skin(self, skin: dict, user_message: str, rng: Optional[random.Random] = None) -> Optional[str]:
        got = self.data_for_skin(skin)
        if not got:
            return None
        data, acct = got
        text = data.reply(user_message, acct, rng)
        return conform(text or "", data.person.real_name, data.max_chars(acct)) or None


_service: Optional[PersonService] = None
_service_lock = threading.Lock()


def get_service() -> PersonService:
    global _service
    with _service_lock:
        if _service is None:
            _service = PersonService()
        return _service


def reset_service(directory: Optional[Path] = None) -> PersonService:
    """Replace the process-wide service (tests, /preload)."""
    global _service
    with _service_lock:
        _service = PersonService(directory)
        return _service
