"""
Shared economy data-access helpers.

Factored out of bot/plugins/economy.py so bot/plugins/games.py (betting
commands) can transact coins through the same functions instead of
duplicating balance-check-and-deduct logic in two places.
"""
from __future__ import annotations

import math

from sqlalchemy import case, select, update

from bot.config import settings
from bot.core.database import ChatMember, async_session


def level_from_xp(xp: int) -> int:
    return int(math.sqrt(max(0, xp) / 100))


def xp_for_level(level: int) -> int:
    return 100 * (level ** 2)


async def get_or_create_member(session, chat_id: int, user_id: int) -> ChatMember:
    result = await session.execute(
        select(ChatMember).where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
    )
    member = result.scalar_one_or_none()
    if member is None:
        member = ChatMember(chat_id=chat_id, user_id=user_id, coins=settings.economy_starting_balance)
        session.add(member)
        await session.flush()
    return member


async def get_balance(chat_id: int, user_id: int) -> int:
    async with async_session() as session:
        member = await get_or_create_member(session, chat_id, user_id)
        balance = member.coins or 0
        await session.commit()
        return balance


async def try_spend(chat_id: int, user_id: int, amount: int) -> bool:
    """Atomically deduct `amount` coins if the balance covers it.
    Returns whether the deduction happened.

    The check-and-deduct is a single conditional SQL UPDATE (row only
    matches, and only then decrements, if coins >= amount) rather than a
    Python read-then-write, so two concurrent spends for the same member
    can't both pass the balance check against a stale value and drive
    the balance negative — the same idiom memory.py's increment_and_check()
    uses for its counter.
    """
    async with async_session() as session:
        await get_or_create_member(session, chat_id, user_id)  # ensure the row exists
        stmt = (
            update(ChatMember)
            .where(
                ChatMember.chat_id == chat_id,
                ChatMember.user_id == user_id,
                ChatMember.coins >= amount,
            )
            .values(coins=ChatMember.coins - amount)
            .returning(ChatMember.coins)
        )
        result = await session.execute(stmt)
        spent = result.first() is not None
        await session.commit()
        return spent


async def add_coins(chat_id: int, user_id: int, amount: int) -> int:
    """Add (or, with a negative amount, remove) coins, clamped at 0.
    Returns the new balance.

    Single atomic SQL UPDATE (with a portable CASE clamp instead of a
    Python max()) so concurrent credits/debits for the same member can't
    lose an update the way a Python read-then-write would.
    """
    async with async_session() as session:
        await get_or_create_member(session, chat_id, user_id)  # ensure the row exists
        new_total = ChatMember.coins + amount
        clamped = case((new_total < 0, 0), else_=new_total)
        stmt = (
            update(ChatMember)
            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
            .values(coins=clamped)
            .returning(ChatMember.coins)
        )
        result = await session.execute(stmt)
        new_balance = result.scalar_one()
        await session.commit()
        return new_balance


async def transfer_coins(chat_id: int, from_user_id: int, to_user_id: int, amount: int) -> bool:
    """Move `amount` coins from one member to another in a single
    transaction. Returns whether the transfer happened (False if the
    sender's balance didn't cover it).

    Both legs use the same conditional/atomic UPDATE as try_spend() and
    add_coins(), but share one session and one commit — so a crash or
    error between the debit and credit rolls back the whole transfer
    instead of leaving coins deducted from the sender but never
    credited to the receiver.
    """
    if amount <= 0:
        return False
    async with async_session() as session:
        await get_or_create_member(session, chat_id, from_user_id)
        await get_or_create_member(session, chat_id, to_user_id)

        debit_stmt = (
            update(ChatMember)
            .where(
                ChatMember.chat_id == chat_id,
                ChatMember.user_id == from_user_id,
                ChatMember.coins >= amount,
            )
            .values(coins=ChatMember.coins - amount)
            .returning(ChatMember.coins)
        )
        debited = (await session.execute(debit_stmt)).first() is not None
        if not debited:
            await session.rollback()
            return False

        credit_stmt = (
            update(ChatMember)
            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == to_user_id)
            .values(coins=ChatMember.coins + amount)
        )
        await session.execute(credit_stmt)
        await session.commit()
        return True


async def add_xp(chat_id: int, user_id: int, amount: int) -> tuple[int, bool]:
    """Add XP. Returns (new_level, did_level_up)."""
    async with async_session() as session:
        member = await get_or_create_member(session, chat_id, user_id)
        old_level = level_from_xp(member.xp or 0)
        member.xp = (member.xp or 0) + amount
        new_level = level_from_xp(member.xp)
        member.level = new_level
        await session.commit()
        return new_level, new_level > old_level
