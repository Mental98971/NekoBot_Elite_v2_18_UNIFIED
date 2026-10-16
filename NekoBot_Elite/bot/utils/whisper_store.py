"""Database access for the whisper plugin: whispers, per-user preferences, recent-recipient history.

Single source of truth is the database (PsstRobot kept a second in-memory copy that could disagree
with it). Reading a whisper is a *claim*: a single DELETE decides who got it, so two people opening
an "everyone" whisper at the same instant cannot both read it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from bot.core.database import Whisper, WhisperHistory, WhisperUser, async_session
from bot.utils.whisper_text import MAX_TEXT_LENGTH, WhisperType


@dataclass
class MediaItem:
    type: int
    file_id: str
    caption: str = ""


@dataclass
class WhisperData:
    """Detached copy of a Whisper row (safe to use after the session closed)."""

    unique_id: str
    sender: int
    text: str = ""
    recipient: int = 0
    recipient_username: str = ""
    file_id: str = ""
    type: int = int(WhisperType.PLAIN_TEXT)
    inline_message_id: str = ""
    created_ts: int = 0
    album: List[MediaItem] = field(default_factory=list)

    # ── semantics ported from PsstRobot's Whisper methods ──
    @property
    def is_for_everyone(self) -> bool:
        return self.recipient == 0 and not self.recipient_username

    @property
    def is_album(self) -> bool:
        return bool(self.album)

    @property
    def is_too_long(self) -> bool:
        return len(self.text) > MAX_TEXT_LENGTH

    @property
    def should_redirect(self) -> bool:
        """Media and long text cannot be shown in a callback alert: read them in the bot's PM."""
        return self.type != int(WhisperType.PLAIN_TEXT) or self.is_too_long

    def can_read(self, user_id: int, username: Optional[str]) -> bool:
        if user_id == self.sender:
            return True
        if self.recipient == 0:
            if not self.recipient_username:
                return True
            return bool(username) and self.recipient_username.lstrip("@").lower() == username.lstrip("@").lower()
        return user_id == self.recipient

    def should_mark_as_read(self, user_id: int) -> bool:
        return user_id != self.sender

    def can_send_inline_advanced(self, user_id: int) -> bool:
        return not self.inline_message_id and user_id == self.sender


def _to_data(row: Whisper) -> WhisperData:
    album: List[MediaItem] = []
    if row.media_json:
        try:
            album = [MediaItem(int(i["type"]), str(i["file_id"]), str(i.get("caption", ""))) for i in json.loads(row.media_json)]
        except (ValueError, KeyError, TypeError):
            album = []
    return WhisperData(
        unique_id=row.unique_id, sender=row.sender, text=row.text or "", recipient=row.recipient or 0,
        recipient_username=row.recipient_username or "", file_id=row.file_id or "", type=row.type,
        inline_message_id=row.inline_message_id or "", created_ts=row.created_ts, album=album,
    )


def _to_row(w: WhisperData) -> Whisper:
    return Whisper(
        unique_id=w.unique_id, sender=w.sender, text=w.text, recipient=w.recipient,
        recipient_username=w.recipient_username, file_id=w.file_id, type=w.type,
        inline_message_id=w.inline_message_id, created_ts=w.created_ts or int(time.time()),
        media_json=json.dumps([{"type": i.type, "file_id": i.file_id, "caption": i.caption} for i in w.album]) if w.album else None,
    )


# ── whispers ──

async def add_whisper(w: WhisperData) -> WhisperData:
    if not w.created_ts:
        w.created_ts = int(time.time())
    async with async_session() as session:
        session.add(_to_row(w))
        await session.commit()
    return w


async def get_whisper(unique_id: str) -> Optional[WhisperData]:
    if not unique_id:
        return None
    async with async_session() as session:
        row = await session.get(Whisper, unique_id)
        return _to_data(row) if row else None


async def set_inline_message_id(unique_id: str, inline_message_id: str) -> None:
    async with async_session() as session:
        await session.execute(update(Whisper).where(Whisper.unique_id == unique_id).values(inline_message_id=inline_message_id))
        await session.commit()


async def claim_whisper(unique_id: str) -> bool:
    """Delete a whisper; True only for the caller whose DELETE actually removed it."""
    async with async_session() as session:
        result = await session.execute(delete(Whisper).where(Whisper.unique_id == unique_id))
        await session.commit()
        return bool(result.rowcount)


async def restore_whisper(w: WhisperData) -> None:
    """Put a claimed whisper back after delivery failed (best effort; ignores a duplicate)."""
    try:
        await add_whisper(w)
    except IntegrityError:
        pass


async def delete_whisper_of_sender(unique_id: str, sender: int) -> bool:
    async with async_session() as session:
        result = await session.execute(delete(Whisper).where(Whisper.unique_id == unique_id, Whisper.sender == sender))
        await session.commit()
        return bool(result.rowcount)


async def list_whispers(sender: int, limit: int = 10) -> List[WhisperData]:
    async with async_session() as session:
        rows = (await session.execute(
            select(Whisper).where(Whisper.sender == sender).order_by(Whisper.created_ts.desc()).limit(limit)
        )).scalars().all()
        return [_to_data(r) for r in rows]


async def count_whispers(sender: int) -> int:
    async with async_session() as session:
        return int((await session.execute(select(func.count()).select_from(Whisper).where(Whisper.sender == sender))).scalar() or 0)


async def purge_expired(max_age_seconds: int, now: Optional[float] = None) -> int:
    cutoff = int((now if now is not None else time.time()) - max_age_seconds)
    async with async_session() as session:
        result = await session.execute(delete(Whisper).where(Whisper.created_ts < cutoff))
        await session.commit()
        return int(result.rowcount or 0)


# ── preferences ──

_PREFS = ("banned", "privacy_mode", "history_disabled", "notify_on_read")


async def get_prefs(user_id: int) -> WhisperUser:
    async with async_session() as session:
        row = await session.get(WhisperUser, user_id)
        if row is None:
            return WhisperUser(user_id=user_id, banned=False, privacy_mode=False, history_disabled=False, notify_on_read=False)
        return row


async def set_pref(user_id: int, name: str, value: bool) -> None:
    if name not in _PREFS:
        raise ValueError(f"unknown preference {name!r}")
    async with async_session() as session:
        row = await session.get(WhisperUser, user_id)
        if row is None:
            row = WhisperUser(user_id=user_id, banned=False, privacy_mode=False, history_disabled=False, notify_on_read=False)
            session.add(row)
        setattr(row, name, bool(value))
        try:
            await session.commit()
        except IntegrityError:  # concurrent first write for this user: apply to the row that won
            await session.rollback()
            await session.execute(update(WhisperUser).where(WhisperUser.user_id == user_id).values(**{name: bool(value)}))
            await session.commit()


# ── recent recipients ──

async def save_history(owner_id: int, target_id: int, target_name: str, keep: int) -> None:
    """Remember that ``target`` read one of ``owner``'s whispers (most recent ``keep`` distinct people)."""
    if owner_id == target_id or (await get_prefs(owner_id)).history_disabled:
        return
    now = int(time.time())
    async with async_session() as session:
        res = await session.execute(
            update(WhisperHistory).where(WhisperHistory.owner_id == owner_id, WhisperHistory.target_id == target_id)
            .values(target_name=target_name, last_used_ts=now)
        )
        if not res.rowcount:
            try:
                async with session.begin_nested():
                    session.add(WhisperHistory(owner_id=owner_id, target_id=target_id, target_name=target_name, last_used_ts=now))
            except IntegrityError:
                pass
        stale = (await session.execute(
            select(WhisperHistory.id).where(WhisperHistory.owner_id == owner_id)
            .order_by(WhisperHistory.last_used_ts.desc(), WhisperHistory.id.desc()).offset(keep)
        )).scalars().all()
        if stale:
            await session.execute(delete(WhisperHistory).where(WhisperHistory.id.in_(stale)))
        await session.commit()


async def get_history(owner_id: int, keep: int) -> List[WhisperHistory]:
    if (await get_prefs(owner_id)).history_disabled:
        return []
    async with async_session() as session:
        rows = (await session.execute(
            select(WhisperHistory).where(WhisperHistory.owner_id == owner_id)
            .order_by(WhisperHistory.last_used_ts.desc(), WhisperHistory.id.desc()).limit(keep)
        )).scalars().all()
        return list(rows)


async def clear_history(owner_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(delete(WhisperHistory).where(WhisperHistory.owner_id == owner_id))
        await session.commit()
        return bool(result.rowcount)



