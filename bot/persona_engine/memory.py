"""Short-term conversation memory and opt-in long-term bot memory (standard library only).

Every piece of text carries a provenance label and the labels are never mixed:
  HISTORICAL_EVIDENCE  something the real person wrote (the example store)
  BOT_MEMORY           facts stored during the bot's own interactions (this module, long-term)
  CURRENT_CONTEXT      the live conversation (this module, short-term)
  GENERATED_CONTENT    something the bot wrote
Nothing the bot generates is ever written into the evidence store.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .core import BOT_MEMORY, CURRENT_CONTEXT, GENERATED_CONTENT, analyze, content_tokens
from .safety import has_contact_data


@dataclass(frozen=True)
class Turn:
    who: str            # u1, u2 ... for chat members, "persona" for the bot
    text: str
    ts: float
    provenance: str     # CURRENT_CONTEXT | GENERATED_CONTENT


class ConversationMemory:
    """Per-chat rolling window of the exchange with the bot (RAM only, expires after ``ttl`` seconds of silence)."""

    def __init__(self, max_turns: int = 12, ttl_seconds: float = 1800.0, max_chats: int = 500, turn_chars: int = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_turns, self.ttl, self.max_chats, self.turn_chars, self._clock = max_turns, ttl_seconds, max_chats, turn_chars, clock
        self._chats: "OrderedDict[int, Deque[Turn]]" = OrderedDict()
        self._users: Dict[int, Dict[str, str]] = {}

    def _dq(self, chat: int) -> Deque[Turn]:
        now = self._clock()
        dq = self._chats.get(chat)
        if dq is not None and dq and now - dq[-1].ts > self.ttl:
            dq.clear()
            self._users.pop(chat, None)
        if dq is None:
            dq = deque(maxlen=self.max_turns)
            self._chats[chat] = dq
            while len(self._chats) > self.max_chats:
                old, _ = self._chats.popitem(last=False)
                self._users.pop(old, None)
        else:
            self._chats.move_to_end(chat)
        return dq

    def speaker(self, chat: int, user_key: Optional[str]) -> str:
        """Pseudonym of a chat member inside this chat's window (the Telegram id never enters a prompt)."""
        users = self._users.setdefault(chat, {})
        key = user_key or "?"
        if key not in users:
            users[key] = f"u{len(users) + 1}"
        return users[key]

    def observe_user(self, chat: int, user_key: Optional[str], text: str) -> None:
        self._dq(chat).append(Turn(self.speaker(chat, user_key), (text or "")[: self.turn_chars], self._clock(), CURRENT_CONTEXT))

    def observe_bot(self, chat: int, text: str) -> None:
        self._dq(chat).append(Turn("persona", (text or "")[: self.turn_chars], self._clock(), GENERATED_CONTENT))

    def turns(self, chat: int) -> List[Turn]:
        return list(self._dq(chat))

    def previous_bot_reply(self, chat: int) -> Optional[str]:
        for t in reversed(self._dq(chat)):
            if t.who == "persona":
                return t.text
        return None

    def open_question(self, chat: int) -> Optional[str]:
        """The latest user message if it is a question and the bot has not answered since."""
        for t in reversed(self._dq(chat)):
            if t.who == "persona":
                return None
            if analyze(t.text).family == "question":
                return t.text
            return None
        return None

    def topic(self, chat: int, k: int = 5) -> List[str]:
        counts: Dict[str, int] = {}
        for t in list(self._dq(chat))[-6:]:
            if t.who == "persona":
                continue
            for w in content_tokens(t.text):
                counts[w] = counts.get(w, 0) + 1
        return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]

    def forget(self, chat: int) -> None:
        self._chats.pop(chat, None)
        self._users.pop(chat, None)


class MemoryError_(ValueError):
    """The fact was refused (empty, too long, looks like contact data, store full)."""


class LongTermMemory:
    """Facts that chat admins explicitly ask the bot to remember (``/premember``), per chat and persona.

    Opt-in by design: the bot never mines passive conversation for personal facts.  Stored text is bounded,
    contact data is refused, entries may expire, and ``/pforget`` removes them for good.
    """

    MAX_CHARS = 200
    MAX_PER_CHAT = 50

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS bot_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "person_id TEXT NOT NULL, text TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT 'BOT_MEMORY', "
                "created REAL NOT NULL, expires REAL)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_bot_memory_chat ON bot_memory(chat_id, person_id)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def add(self, chat_id: int, person_id: str, text: str, ttl_days: Optional[float] = None) -> int:
        t = " ".join((text or "").split())
        if not t:
            raise MemoryError_("nothing to remember")
        if len(t) > self.MAX_CHARS:
            raise MemoryError_(f"too long (max {self.MAX_CHARS} characters)")
        if has_contact_data(t):
            raise MemoryError_("it looks like contact data (links, handles, numbers)")
        now = self._clock()
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM bot_memory WHERE expires IS NOT NULL AND expires < ?", (now,))
            n = c.execute("SELECT COUNT(*) FROM bot_memory WHERE chat_id=? AND person_id=?", (chat_id, person_id)).fetchone()[0]
            if n >= self.MAX_PER_CHAT:
                raise MemoryError_(f"memory full ({self.MAX_PER_CHAT} facts); use /pforget first")
            cur = c.execute("INSERT INTO bot_memory(chat_id, person_id, text, provenance, created, expires) VALUES (?,?,?,?,?,?)",
                            (chat_id, person_id, t, BOT_MEMORY, now, now + ttl_days * 86400 if ttl_days else None))
            return int(cur.lastrowid)

    def list(self, chat_id: int, person_id: str) -> List[Tuple[int, str]]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT id, text FROM bot_memory WHERE chat_id=? AND person_id=? AND (expires IS NULL OR expires >= ?) ORDER BY id",
                             (chat_id, person_id, self._clock())).fetchall()
        return [(int(i), str(t)) for i, t in rows]

    def forget(self, chat_id: int, person_id: Optional[str] = None, memory_id: Optional[int] = None) -> int:
        q, args = "DELETE FROM bot_memory WHERE chat_id=?", [chat_id]
        if person_id is not None:
            q += " AND person_id=?"
            args.append(person_id)
        if memory_id is not None:
            q += " AND id=?"
            args.append(memory_id)
        with self._lock, self._conn() as c:
            return int(c.execute(q, args).rowcount)

    def search(self, chat_id: int, person_id: str, query: str, k: int = 3) -> List[str]:
        """Stored facts that share content words with ``query`` (most overlap first)."""
        q = content_tokens(query)
        if not q:
            return []
        scored = []
        for _id, t in self.list(chat_id, person_id):
            ov = len(q & content_tokens(t))
            if ov:
                scored.append((-ov, _id, t))
        return [t for _o, _i, t in sorted(scored)[:k]]
