"""Collector changes in v2.12: safe /delchar, new /editchar, and the WAIFU-HUSBANDO-CATCHER command names."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_collector212_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import func, select  # noqa: E402
from telegram.ext import CommandHandler  # noqa: E402

from _fakes import FakeBot, make_context, make_update  # noqa: E402
import bot.plugins.collector as col  # noqa: E402
from bot.core.database import Character, CollectorModerator, Giveaway, UserCharacter, async_session, init_db  # noqa: E402

MOD = 7701


def replies(update) -> str:
    text = "\n".join(str(c.args[0]) for c in update.message.reply_text.call_args_list)
    assert "went sideways" not in text, text
    return text


class _Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

        async def seed():
            async with async_session() as s:
                if await s.get(CollectorModerator, MOD) is None:
                    s.add(CollectorModerator(user_id=MOD, added_by=1))
                await s.commit()
        asyncio.run(seed())

    async def run_cmd(self, handler, text, user=MOD):
        update = make_update(text, chat_id=-1008001, user_id=user)
        await handler(update, make_context(FakeBot(), text.split()[1:]))
        return update

    async def add_char(self, name, anime="Show", rarity="common", owners=()) -> int:
        async with async_session() as s:
            ch = Character(name=name, anime=anime, rarity=rarity, image_url="file-id", added_by=MOD)
            s.add(ch)
            await s.flush()
            for uid in owners:
                s.add(UserCharacter(user_id=uid, character_id=ch.id))
            await s.commit()
            return ch.id

    async def exists(self, char_id) -> bool:
        async with async_session() as s:
            return await s.get(Character, char_id) is not None

    async def copies(self, char_id) -> int:
        async with async_session() as s:
            return (await s.execute(select(func.count()).select_from(UserCharacter).where(UserCharacter.character_id == char_id))).scalar()


class TestDelchar(_Base):
    async def test_owned_character_needs_force_and_force_removes_the_copies_everywhere(self):
        """v2.10 promised 'copies are kept' but user_characters is ON DELETE CASCADE (Postgres wiped them)."""
        cid = await self.add_char("Owned One", owners=(1, 2))
        refused = await self.run_cmd(col.delchar_cmd, f"/delchar {cid}")
        self.assertIn("2 copies", replies(refused))
        self.assertTrue(await self.exists(cid))
        self.assertEqual(await self.copies(cid), 2)
        forced = await self.run_cmd(col.delchar_cmd, f"/delchar {cid} force")
        self.assertIn("2 player copies", replies(forced))
        self.assertFalse(await self.exists(cid))
        self.assertEqual(await self.copies(cid), 0, "no orphan rows pointing at a deleted character")

    async def test_unowned_character_is_deleted_directly(self):
        cid = await self.add_char("Nobody Owns")
        update = await self.run_cmd(col.delchar_cmd, f"/delchar {cid}")
        self.assertIn("deleted", replies(update))
        self.assertFalse(await self.exists(cid))

    async def test_a_running_giveaway_blocks_deletion_and_a_finished_one_is_detached(self):
        live_id = await self.add_char("In Giveaway")
        async with async_session() as s:
            s.add(Giveaway(chat_id=-1, character_id=live_id, created_by=MOD, ended=False))
            await s.commit()
        blocked = await self.run_cmd(col.delchar_cmd, f"/delchar {live_id} force")
        self.assertIn("giveaway", replies(blocked))
        self.assertTrue(await self.exists(live_id))
        done_id = await self.add_char("Past Giveaway")
        async with async_session() as s:
            s.add(Giveaway(chat_id=-1, character_id=done_id, created_by=MOD, ended=True))
            await s.commit()
        await self.run_cmd(col.delchar_cmd, f"/delchar {done_id}")
        self.assertFalse(await self.exists(done_id))
        async with async_session() as s:
            row = (await s.execute(select(Giveaway).where(Giveaway.chat_id == -1, Giveaway.ended.is_(True)))).scalars().first()
        self.assertIsNone(row.character_id)

    async def test_permissions_and_input(self):
        cid = await self.add_char("Protected")
        denied = await self.run_cmd(col.delchar_cmd, f"/delchar {cid}", user=424242)
        self.assertIn("moderators only", replies(denied))
        self.assertTrue(await self.exists(cid))
        usage = await self.run_cmd(col.delchar_cmd, "/delchar abc")
        self.assertIn("Usage", replies(usage))
        missing = await self.run_cmd(col.delchar_cmd, "/delchar 99999999")
        self.assertIn("No character", replies(missing))


class TestEditchar(_Base):
    async def test_edit_fields(self):
        cid = await self.add_char("Typo Nmae", anime="Old Show")
        for text, field, expected in ((f"/editchar {cid} name Fixed Name", "name", "Fixed Name"),
                                      (f"/editchar {cid} anime New Show", "anime", "New Show"),
                                      (f"/editchar {cid} rarity EPIC", "rarity", "epic")):
            update = await self.run_cmd(col.editchar_cmd, text)
            self.assertIn(expected, replies(update))
            async with async_session() as s:
                self.assertEqual(getattr(await s.get(Character, cid), field), expected)

    async def test_validation(self):
        cid = await self.add_char("Validate Me", anime="Valid Show")
        other = await self.add_char("Other Char", anime="Valid Show")
        cases = {
            f"/editchar {cid} rarity shiny": "Invalid rarity",
            f"/editchar {cid} image http://x": "Usage",
            f"/editchar {cid} name": "Usage",
            "/editchar x name y": "Usage",
            f"/editchar {cid} name {'n' * 200}": "1-128",
            f"/editchar {cid} name Other Char": "duplicate",
            "/editchar 99999999 name Ghost": "No character",
        }
        for text, needle in cases.items():
            update = await self.run_cmd(col.editchar_cmd, text)
            self.assertIn(needle, replies(update), text)
        async with async_session() as s:
            self.assertEqual((await s.get(Character, cid)).name, "Validate Me")
            self.assertEqual((await s.get(Character, other)).name, "Other Char")

    async def test_non_moderators_are_refused(self):
        cid = await self.add_char("Guarded")
        update = await self.run_cmd(col.editchar_cmd, f"/editchar {cid} name Hacked", user=424242)
        self.assertIn("moderators only", replies(update))
        async with async_session() as s:
            self.assertEqual((await s.get(Character, cid)).name, "Guarded")


class TestDonorCommandNames(unittest.TestCase):
    def test_waifu_husbando_catcher_names_are_registered(self):
        added = []

        class FakeApp:
            job_queue = None

            def add_handler(self, handler, group=0):
                added.append(handler)

        col.register(FakeApp())
        names = {c for h in added if isinstance(h, CommandHandler) for c in h.commands}
        for donor_name in ("protecc", "collect", "hunt", "harem", "ctop", "delete", "changetime"):
            self.assertIn(donor_name, names, donor_name)
        self.assertTrue({"grab", "collection", "topcatchers", "delchar", "setspawnrate", "editchar"} <= names)


if __name__ == "__main__":
    unittest.main()
