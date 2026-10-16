"""Short-lived, commit-invalidated cache of per-chat settings and approved users (v2.12).

Why this exists: every group message already ran several passive listeners (locks, filters,
anti-flood, anti-spam, access control, ...) and each one opened its own database session just to
read one ``Chat`` row. v2.12 adds more per-message checks (word blacklist, approvals, per-chat
flood settings), so they share this cache instead of adding more round-trips.

Correctness model
-----------------
* Readers get a *read-only snapshot* (``SimpleNamespace`` of the column values). Never mutate it;
  writers load their own row inside their own session as before.
* Entries live at most ``CHAT_TTL`` / ``APPROVED_TTL`` seconds *and* are dropped the moment a
  session that changed a ``Chat`` or ``Approval`` row commits (SQLAlchemy ``after_flush`` ->
  ``after_commit`` hooks below), so a ``/lock`` or ``/approve`` takes effect on the next message.
  The hook also covers writes made by plugins that know nothing about this module.
* Bulk ``session.execute(update/delete(...))`` statements bypass the unit of work and therefore
  the hook: such code must call ``invalidate_chat(chat_id)`` itself after committing.
* A read that overlaps an invalidation is returned to its caller but not stored (``_epoch``), so
  a slow read of pre-commit data can never be cached over the invalidation.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Dict, FrozenSet, Optional, Tuple

from sqlalchemy import event, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from bot.core.database import Approval, Chat, async_session

CHAT_TTL = 20.0
APPROVED_TTL = 60.0
_MAX_ENTRIES = 20000

_chat: Dict[int, Tuple[float, Optional[SimpleNamespace]]] = {}
_approved: Dict[int, Tuple[float, FrozenSet[int]]] = {}
_epoch = 0
_CHAT_COLUMNS = tuple(a.key for a in sa_inspect(Chat).mapper.column_attrs)
_INFO_KEY = "_chatcache_ids"


def _store(table: dict, key: int, value: tuple) -> None:
    if len(table) >= _MAX_ENTRIES:
        now = time.monotonic()
        for k in [k for k, v in table.items() if v[0] <= now]:
            table.pop(k, None)
        if len(table) >= _MAX_ENTRIES:  # nothing expired: drop the oldest half rather than grow
            for k in list(table)[: _MAX_ENTRIES // 2]:
                table.pop(k, None)
    table[key] = value


def _snapshot(row: Chat) -> SimpleNamespace:
    return SimpleNamespace(**{k: getattr(row, k) for k in _CHAT_COLUMNS})


def invalidate_chat(chat_id: int) -> None:
    """Forget everything cached for ``chat_id`` (call after bulk DML; commits do it automatically)."""
    global _epoch
    _epoch += 1
    _chat.pop(chat_id, None)
    _approved.pop(chat_id, None)


def clear() -> None:
    """Drop every entry (tests, and a safe reset after restoring a database backup)."""
    global _epoch
    _epoch += 1
    _chat.clear()
    _approved.clear()


async def get_chat_settings(chat_id: int) -> Optional[SimpleNamespace]:
    """Read-only snapshot of the chat's row, or ``None`` when the chat has no row yet."""
    now = time.monotonic()
    hit = _chat.get(chat_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    epoch = _epoch
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        snap = _snapshot(row) if row is not None else None
    if epoch == _epoch:
        _store(_chat, chat_id, (now + CHAT_TTL, snap))
    return snap


async def get_approved_ids(chat_id: int) -> FrozenSet[int]:
    now = time.monotonic()
    hit = _approved.get(chat_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    epoch = _epoch
    async with async_session() as session:
        ids = frozenset((await session.execute(select(Approval.user_id).where(Approval.chat_id == chat_id))).scalars().all())
    if epoch == _epoch:
        _store(_approved, chat_id, (now + APPROVED_TTL, ids))
    return ids


async def is_approved(chat_id: int, user_id: int) -> bool:
    return user_id in await get_approved_ids(chat_id)


# ── commit-time invalidation ─────────────────────────────────────────────────
def _collect(session: Session, flush_context) -> None:
    ids = session.info.setdefault(_INFO_KEY, set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Chat):
            ids.add(obj.id)
        elif isinstance(obj, Approval):
            ids.add(obj.chat_id)


def _flush_invalidations(session: Session) -> None:
    for chat_id in session.info.pop(_INFO_KEY, ()):
        invalidate_chat(chat_id)


def _discard(session: Session, *_args) -> None:
    session.info.pop(_INFO_KEY, None)


event.listen(Session, "after_flush", _collect)
event.listen(Session, "after_commit", _flush_invalidations)
event.listen(Session, "after_rollback", _discard)
