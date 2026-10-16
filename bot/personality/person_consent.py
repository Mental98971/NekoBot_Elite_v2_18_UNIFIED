"""Opt-out of being imitated, per registered person (not per Telegram account).

A person who has opted out is never imitated: their skins fall back to the chat's default skin
(see PersonalityLayer._resolve_active_skin) and cannot be selected.  State lives in the
``persona_optouts`` table; reads are cached for a short time so the reply path stays cheap, and a
database failure leaves imitation OFF rather than ON (fail closed).
"""
from __future__ import annotations

import time
from typing import Optional, Set, Tuple

from bot.core.database import PersonaOptOut, async_session
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_TTL_SECONDS = 30.0
_cache: Tuple[float, Set[str]] = (0.0, set())
_last_good: Optional[Set[str]] = None


def invalidate() -> None:
    global _cache
    _cache = (0.0, set())


async def opted_out_ids() -> Set[str]:
    """Person ids that have opted out.  On a DB error, returns the last known set, or raises if none."""
    global _cache, _last_good
    now = time.monotonic()
    if _cache[0] and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    try:
        from sqlalchemy import select
        async with async_session() as session:
            rows = (await session.execute(select(PersonaOptOut.person_id))).scalars().all()
        ids = set(rows)
        _cache = (now, ids)
        _last_good = ids
        return ids
    except Exception as exc:
        logger.warning("persona_optout_read_failed", error=str(exc))
        if _last_good is not None:
            return _last_good
        raise


async def is_opted_out(person_id: str) -> bool:
    """True if imitation of ``person_id`` must not happen.  Fails closed (True) if state is unknowable."""
    try:
        return person_id in await opted_out_ids()
    except Exception:
        return True


async def set_opt_out(person_id: str, opted_out: bool, by_user_id: Optional[int]) -> None:
    async with async_session() as session:
        row = await session.get(PersonaOptOut, person_id)
        if opted_out and row is None:
            session.add(PersonaOptOut(person_id=person_id, by_user_id=by_user_id))
        elif not opted_out and row is not None:
            await session.delete(row)
        await session.commit()
    invalidate()
