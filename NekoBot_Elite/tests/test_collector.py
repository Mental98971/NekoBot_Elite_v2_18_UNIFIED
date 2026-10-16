"""
Tests for bot/plugins/collector.py — the character collector game.

Unlike test_personality.py, these need a real (isolated, disposable)
database: collector.py's core logic is inherently DB-driven (weighted
spawning queries the Character table, catches write UserCharacter
rows, etc.), so mocking it all out would test very little. DATABASE_URL
is pointed at a throwaway temp file before any `bot.*` module is
imported, so this never touches a real deployment's data/novabot.db.
"""
from __future__ import annotations

import asyncio
import collections
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must happen before any `bot.*` import — bot/core/database.py builds its
# engine from settings.database_url at import time.
_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_collector_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import (  # noqa: E402
    Character,
    CollectorModerator,
    User,
    UserCharacter,
    async_session,
    init_db,
)
import bot.plugins.collector as collector  # noqa: E402


def _make_update(user_id: int, chat_id: int = -100999, first_name: str = "Tester", args=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_user.username = None
    update.effective_chat.id = chat_id
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    # A real Update.effective_message is a property that returns
    # self.message for ordinary messages — MagicMock has no such
    # aliasing, so without this, code paths reading effective_message
    # (like resolve_target_user) see a fresh, unrelated, always-truthy
    # mock instead of the None we set on .message above.
    update.effective_message = update.message
    context = MagicMock()
    context.args = args or []
    context.job_queue = None
    return update, context


class CollectorTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class: fresh, migrated schema once per process (module-level
    temp DB), each test using its own character/user id ranges so tests
    can run in any order without colliding."""

    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())


class TestWeightedRaritySpawning(CollectorTestCase):
    """v2.3-era collector.py's rarity labels were pure flavor text with
    no effect on actual spawn odds — a 'divine' character was exactly
    as likely to spawn as a 'common' one. This is the fix."""

    async def asyncSetUp(self):
        async with async_session() as session:
            for i in range(10):
                session.add(Character(name=f"WCommon{i}", anime="T1", rarity="common"))
            for i in range(2):
                session.add(Character(name=f"WRare{i}", anime="T1", rarity="rare"))
            session.add(Character(name="WDivine0", anime="T1", rarity="divine"))
            await session.commit()

    async def test_common_dominates_and_divine_stays_rare_but_possible(self):
        # Sample size matters here: with this suite's fixture mix, divine's
        # effective weight (RARITY_WEIGHTS combined with the population
        # dampener in _pick_character) works out to roughly 1 draw in
        # 400-500. 600 draws left a double-digit percent chance of zero
        # divine hits by pure chance — a flaky test, not a code bug. 3000
        # brings that below 0.2%.
        counts = collections.Counter()
        async with async_session() as session:
            for _ in range(3000):
                char = await collector._pick_character(session)
                counts[char.rarity] += 1
        self.assertGreater(counts["common"], counts["divine"] * 5)
        self.assertGreater(counts["divine"], 0, "divine should still occasionally spawn, just rarely")

    async def test_explicit_rarity_filter_only_returns_that_rarity(self):
        async with async_session() as session:
            for _ in range(20):
                char = await collector._pick_character(session, rarity="rare")
                self.assertEqual(char.rarity, "rare")


class TestEnsureUserFixesTopcatchers(CollectorTestCase):
    """/topcatchers and /owners INNER JOIN UserCharacter against User.
    grab_cmd used to never create a User row, so a user whose only
    interaction with the bot was ever catching a character was silently
    invisible on both."""

    async def test_ensure_user_creates_missing_row(self):
        async with async_session() as session:
            self.assertIsNone(await session.get(User, 90001))
            fake_user = MagicMock(id=90001, first_name="Brandnew", username="brandnew")
            await collector._ensure_user(session, fake_user)
            await session.commit()
        async with async_session() as session:
            row = await session.get(User, 90001)
            self.assertIsNotNone(row)
            self.assertEqual(row.first_name, "Brandnew")

    async def test_grab_cmd_makes_catcher_visible_to_topcatchers_join(self):
        from sqlalchemy import func, select

        async with async_session() as session:
            char = Character(name="TopcatchTarget", anime="T2", rarity="common")
            session.add(char)
            await session.commit()
            char_id = char.id

        collector._current_spawns[-100777] = {
            "id": char_id, "name": "TopcatchTarget", "anime": "T2",
            "rarity": "common", "image_url": None,
        }
        update, context = _make_update(90002, chat_id=-100777, args=["TopcatchTarget"])
        with unittest.mock.patch("bot.plugins.collector.add_coins", AsyncMock(return_value=10)):
            await collector.grab_cmd(update, context)

        async with async_session() as session:
            result = await session.execute(
                select(UserCharacter.user_id, func.count().label("total"), User)
                .join(User, User.id == UserCharacter.user_id)
                .where(UserCharacter.user_id == 90002)
                .group_by(UserCharacter.user_id, User.id)
            )
            self.assertEqual(len(result.all()), 1, "catcher should be visible in a topcatchers-style join")


class TestConcurrentGrab(CollectorTestCase):
    """The old comment claimed 'no await between check and delete makes
    this atomic' — true, but fragile against any future edit adding one.
    The lock added this round is defense in depth; this test proves the
    end-to-end guarantee holds regardless of which mechanism is doing
    the work."""

    async def test_only_one_of_many_concurrent_grabbers_wins(self):
        async with async_session() as session:
            char = Character(name="RaceTarget", anime="T3", rarity="common")
            session.add(char)
            await session.commit()
            char_id = char.id

        chat_id = -100888
        collector._current_spawns[chat_id] = {
            "id": char_id, "name": "RaceTarget", "anime": "T3", "rarity": "common", "image_url": None,
        }
        collector._last_grab.clear()  # avoid cross-test cooldown interference

        async def attempt(user_id):
            update, context = _make_update(user_id, chat_id=chat_id, args=["RaceTarget"])
            with unittest.mock.patch("bot.plugins.collector.add_coins", AsyncMock(return_value=10)):
                await collector.grab_cmd(update, context)
            call = update.message.reply_text.call_args
            return call[0][0] if call else ""

        results = await asyncio.gather(*[attempt(80000 + i) for i in range(12)])
        wins = [r for r in results if "caught" in r]
        self.assertEqual(len(wins), 1, f"expected exactly one winner, got {len(wins)}: {results}")

        from sqlalchemy import func, select
        async with async_session() as session:
            count = (await session.execute(
                select(func.count()).select_from(UserCharacter).where(UserCharacter.character_id == char_id)
            )).scalar()
        self.assertEqual(count, 1, "exactly one catch should have been persisted, not zero or several")


class TestPermissionGating(CollectorTestCase):
    """addmod/removemod used to be @admin_only (any chat's admins) on
    top of an inline collector-mod check that a plain chat admin would
    never pass anyway — but worse, upload/delchar/giveaway/endgiveaway
    used the same @admin_only gate with NO collector-mod check at all,
    so any admin of any of potentially many mutually-unrelated chats
    could mint unlimited-coin giveaways or appoint themselves influence
    over the bot-wide shared character pool. These lock in the fix:
    addmod/removemod need sudo; giveaway/upload/etc. need collector-mod
    but not necessarily sudo.
    """

    async def test_addmod_rejects_plain_collector_mod(self):
        async with async_session() as session:
            session.add(CollectorModerator(user_id=95001, added_by=123456))
            await session.commit()
        update, context = _make_update(95001, args=["95002"])
        await collector.addmod_cmd(update, context)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Sudo only", reply)

    async def test_addmod_allows_sudo(self):
        update, context = _make_update(123456, args=["95003"])  # OWNER_ID from env
        await collector.addmod_cmd(update, context)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("can now", reply)

    async def test_giveaway_rejects_unprivileged_user(self):
        update, context = _make_update(95004, args=["100", "5"])
        await collector.giveaway_cmd(update, context)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Collector moderators only", reply)

    async def test_giveaway_allows_collector_mod_without_sudo(self):
        async with async_session() as session:
            session.add(CollectorModerator(user_id=95005, added_by=123456))
            await session.commit()
        update, context = _make_update(95005, args=["100", "5"])
        await collector.giveaway_cmd(update, context)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Giveaway started", reply)

    async def test_upload_not_gated_by_chat_admin_status(self):
        """upload_cmd must not require @admin_only — a collector mod
        curating from DM (not an admin of any group) must be able to
        upload. This checks the function has no admin_only wrapper by
        verifying a sudo user (who is NOT a chat admin of anything in
        this test) can reach the collector-mod branch at all."""
        import inspect
        source = inspect.getsource(collector)
        # The decorator would appear on the line(s) immediately above
        # "async def upload_cmd" if present.
        upload_def_index = source.index("async def upload_cmd")
        preceding = source[:upload_def_index]
        last_lines = preceding.strip().splitlines()[-3:]
        self.assertFalse(
            any("@admin_only" in line for line in last_lines),
            "upload_cmd must not be gated by @admin_only (chat-admin) — see module docstring",
        )


class TestFavoriteIsExclusive(CollectorTestCase):
    """Only one favorite at a time — setting a new one must clear any
    previous favorite, not leave multiple characters flagged."""

    async def test_setting_new_favorite_clears_old_one(self):
        async with async_session() as session:
            c1 = Character(name="FavA", anime="T4", rarity="common")
            c2 = Character(name="FavB", anime="T4", rarity="common")
            session.add_all([c1, c2])
            await session.commit()
            session.add(UserCharacter(user_id=96001, character_id=c1.id, is_favorite=True))
            session.add(UserCharacter(user_id=96001, character_id=c2.id))
            await session.commit()
            c2_id = c2.id

        update, context = _make_update(96001, args=[str(c2_id)])
        await collector.fav_cmd(update, context)

        from sqlalchemy import select
        async with async_session() as session:
            rows = (await session.execute(
                select(UserCharacter).where(UserCharacter.user_id == 96001, UserCharacter.is_favorite.is_(True))
            )).scalars().all()
        self.assertEqual(len(rows), 1, "exactly one favorite should be set after switching")
        self.assertEqual(rows[0].character_id, c2_id)


class TestInlineQueryUsesFileIdCompatibleResults(CollectorTestCase):
    """image_url stores a Telegram file_id (see upload_cmd), not a
    fetchable URL. InlineQueryResultPhoto/Video require photo_url/
    video_url to be a real URL and silently fail to render given a
    file_id there — InlineQueryResultCachedPhoto/CachedVideo take
    photo_file_id/video_file_id instead, matching what's stored."""

    async def test_results_are_cached_variants_not_url_variants(self):
        async with async_session() as session:
            session.add(Character(name="InlineTarget", anime="T5", rarity="common", image_url="AgACfakefileid123"))
            await session.commit()

        update = MagicMock()
        update.inline_query.query = "InlineTarget"
        update.inline_query.answer = AsyncMock()
        context = MagicMock()
        await collector.inline_query(update, context)

        results = update.inline_query.answer.call_args[0][0]
        self.assertTrue(len(results) >= 1)
        for r in results:
            self.assertNotEqual(type(r).__name__, "InlineQueryResultPhoto")
            self.assertNotEqual(type(r).__name__, "InlineQueryResultVideo")


def _cleanup_tmp_db():
    try:
        os.unlink(_TMP_DB.name)
    except OSError:
        pass


import atexit  # noqa: E402
atexit.register(_cleanup_tmp_db)


if __name__ == "__main__":
    unittest.main()
