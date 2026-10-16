"""
Tests for bot/services/economy_service.py — coin balance transactions.

The concurrency tests in this file exist for a specific reason: an
earlier revision of try_spend()/add_coins() read a member's balance in
Python, did the arithmetic in Python, then wrote it back — a classic
read-modify-write race that a docstring incorrectly labeled "atomic".
Under concurrent calls it could let a balance go negative (an
exploitable double-spend). The fix moved the check-and-mutate into a
single conditional SQL UPDATE. Per this project's own hard-won rule —
atomicity claims need verification, not just inspection — these tests
actually fire concurrent requests through asyncio.gather() and check
the invariant that matters: total coins in the system are conserved,
and a balance can never be spent below zero, no matter how many
requests race for it.

Uses a real (isolated, disposable) SQLite database for the same reason
test_collector.py does: this logic is inherently DB-driven, so mocking
it out would test very little.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must happen before any `bot.*` import — bot/core/database.py builds its
# engine from settings.database_url at import time.
_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_economy_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import async_session, init_db  # noqa: E402
from bot.services.economy_service import (  # noqa: E402
    add_coins,
    get_or_create_member,
    try_spend,
    transfer_coins,
)

CHAT_ID = -100777


async def _set_balance(user_id: int, coins: int) -> None:
    async with async_session() as session:
        member = await get_or_create_member(session, CHAT_ID, user_id)
        member.coins = coins
        await session.commit()


async def _get_balance(user_id: int) -> int:
    async with async_session() as session:
        member = await get_or_create_member(session, CHAT_ID, user_id)
        await session.commit()
        return member.coins or 0


class EconomyTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())


class TestTrySpendBasics(EconomyTestCase):
    async def test_spend_within_balance_succeeds(self):
        user_id = 1001
        await _set_balance(user_id, 100)
        self.assertTrue(await try_spend(CHAT_ID, user_id, 40))
        self.assertEqual(await _get_balance(user_id), 60)

    async def test_spend_beyond_balance_fails_and_leaves_balance_untouched(self):
        user_id = 1002
        await _set_balance(user_id, 50)
        self.assertFalse(await try_spend(CHAT_ID, user_id, 51))
        self.assertEqual(await _get_balance(user_id), 50)

    async def test_add_coins_clamps_at_zero(self):
        user_id = 1003
        await _set_balance(user_id, 10)
        new_balance = await add_coins(CHAT_ID, user_id, -999)
        self.assertEqual(new_balance, 0)


class TestTransferCoinsBasics(EconomyTestCase):
    async def test_successful_transfer_moves_coins(self):
        sender, receiver = 2001, 2002
        await _set_balance(sender, 100)
        await _set_balance(receiver, 0)
        self.assertTrue(await transfer_coins(CHAT_ID, sender, receiver, 30))
        self.assertEqual(await _get_balance(sender), 70)
        self.assertEqual(await _get_balance(receiver), 30)

    async def test_insufficient_balance_moves_nothing(self):
        sender, receiver = 2003, 2004
        await _set_balance(sender, 10)
        await _set_balance(receiver, 5)
        self.assertFalse(await transfer_coins(CHAT_ID, sender, receiver, 11))
        self.assertEqual(await _get_balance(sender), 10)
        self.assertEqual(await _get_balance(receiver), 5)

    async def test_non_positive_amount_rejected(self):
        sender, receiver = 2005, 2006
        await _set_balance(sender, 10)
        self.assertFalse(await transfer_coins(CHAT_ID, sender, receiver, 0))
        self.assertFalse(await transfer_coins(CHAT_ID, sender, receiver, -5))
        self.assertEqual(await _get_balance(sender), 10)


class TestConcurrency(EconomyTestCase):
    """The regression tests: fire real concurrent requests and check the
    invariant, rather than trusting that a single sequential call works."""

    async def test_concurrent_spends_never_overdraw_the_balance(self):
        user_id = 3001
        starting_balance = 100
        spend_amount = 10
        concurrent_requests = 50  # far more than the 10 the balance can cover

        await _set_balance(user_id, starting_balance)

        results = await asyncio.gather(
            *[try_spend(CHAT_ID, user_id, spend_amount) for _ in range(concurrent_requests)]
        )

        successes = sum(1 for r in results if r)
        final_balance = await _get_balance(user_id)

        # The critical invariant: exactly as many spends succeeded as the
        # starting balance could actually cover, no more — and the
        # balance never went negative. A racy read-then-write
        # implementation would intermittently let more than 10 succeed.
        self.assertEqual(successes, starting_balance // spend_amount)
        self.assertEqual(final_balance, starting_balance - successes * spend_amount)
        self.assertGreaterEqual(final_balance, 0)

    async def test_concurrent_transfers_conserve_total_coins(self):
        alice, bob = 3002, 3003
        await _set_balance(alice, 200)
        await _set_balance(bob, 200)
        total_before = 400

        # Fire transfers in both directions simultaneously — a race an
        # unguarded read-then-write could lose an update on.
        alice_to_bob = [transfer_coins(CHAT_ID, alice, bob, 5) for _ in range(20)]
        bob_to_alice = [transfer_coins(CHAT_ID, bob, alice, 3) for _ in range(20)]
        await asyncio.gather(*alice_to_bob, *bob_to_alice)

        total_after = await _get_balance(alice) + await _get_balance(bob)
        self.assertEqual(total_after, total_before)
        self.assertGreaterEqual(await _get_balance(alice), 0)
        self.assertGreaterEqual(await _get_balance(bob), 0)

    async def test_concurrent_add_coins_loses_no_updates(self):
        user_id = 3004
        await _set_balance(user_id, 0)
        concurrent_requests = 50

        await asyncio.gather(*[add_coins(CHAT_ID, user_id, 1) for _ in range(concurrent_requests)])

        # A racy read-then-write implementation would lose some of these
        # concurrent +1s; the atomic UPDATE must not.
        self.assertEqual(await _get_balance(user_id), concurrent_requests)


if __name__ == "__main__":
    unittest.main()
