"""Tests for bot/plugins/fedtools.py (v2.16): ban-list parsing/export, join/message-time enforcement of
gbans and fbans, federation subscriptions, and the owner-only management commands."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_fedtools_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB.name}")

from sqlalchemy import select  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from bot.core.database import Chat, FedBan, Federation, GlobalBan, async_session, init_db  # noqa: E402
import bot.plugins.fedtools as ft  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

F_A = "aaaaaaaa-0000-4000-8000-000000000001"
F_B = "bbbbbbbb-0000-4000-8000-000000000002"
F_DEL = "dddddddd-0000-4000-8000-000000000004"
F_NONE = "eeeeeeee-0000-4000-8000-000000000005"   # never created
OWNER_A = 880_001
ADMIN_A = 880_002


class TestParsing(unittest.TestCase):
    def test_csv_with_header_and_reasons(self):
        rows = ft.parse_ban_rows(b"user_id,reason,banned_by\n101,spam,5\n102,,5\n")
        self.assertEqual(rows, [(101, "spam"), (102, None)])

    def test_json_list_and_object_forms(self):
        self.assertEqual(ft.parse_ban_rows(b'[{"user_id": 5, "reason": "x"}, 6]'), [(5, "x"), (6, None)])
        self.assertEqual(ft.parse_ban_rows(b'{"7": "a", "8": "b"}'), [(7, "a"), (8, "b")])
        self.assertEqual(ft.parse_ban_rows(b'{"bans": [{"id": 9}]}'), [(9, None)])

    def test_plain_lines_dedupe_and_junk_are_handled(self):
        rows = ft.parse_ban_rows("10 first reason\n10 again\nnot-a-number\n-4 neg\n0 zero\n11\n".encode())
        self.assertEqual(rows, [(10, "first reason"), (11, None)])

    def test_bom_is_accepted(self):
        self.assertEqual(ft.parse_ban_rows(b"\xef\xbb\xbf55 r"), [(55, "r")])

    def test_bad_inputs_raise_value_error(self):
        for raw in (b"", b"   \n", b"\xff\xfe\x00bad", b"[not json", b"header\nonly,words"):
            with self.assertRaises(ValueError, msg=raw):
                ft.parse_ban_rows(raw)

    def test_row_limit(self):
        raw = "\n".join(str(i) for i in range(1, 50)).encode()
        self.assertEqual(len(ft.parse_ban_rows(raw, limit=10)), 10)

    def test_export_roundtrips_and_neutralises_formulas(self):
        rows = [(1, "=HYPERLINK(\"x\")", 9), (2, None, None), (3, "line\nbreak", 9)]
        data = ft.render_ban_export(rows)
        text = data.decode()
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn("\nbreak", text)
        self.assertEqual([u for u, _ in ft.parse_ban_rows(data)], [1, 2, 3])
        self.assertEqual([u for u, _ in ft.parse_ban_rows(ft.render_ban_export(rows, "json"))], [1, 2, 3])


async def _seed() -> None:
    """Idempotent on purpose: other test modules share the process-wide database and may recreate or clean it."""
    await init_db()
    async with async_session() as s:
        for fid, name, owner, subs in ((F_A, "Alpha", OWNER_A, [F_B]), (F_B, "Beta", 880_099, None)):
            if await s.get(Federation, fid) is None:
                s.add(Federation(id=fid, name=name, owner_id=owner, admins=[ADMIN_A] if fid == F_A else [], subscribed=subs))
        for cid, fid, enforce in ((-880_101, F_A, None), (-880_102, F_A, False), (-880_103, None, None)):
            if await s.get(Chat, cid) is None:
                s.add(Chat(id=cid, type="supergroup", title=f"c{cid}", federation_id=fid, ban_enforce=enforce))
        await s.flush()
        for fid, uid, reason in ((F_A, 880_201, "spam"), (F_B, 880_203, "raid")):
            exists = (await s.execute(select(FedBan).where(FedBan.federation_id == fid, FedBan.user_id == uid))).first()
            if not exists:
                s.add(FedBan(federation_id=fid, user_id=uid, reason=reason, banned_by=1))
        if await s.get(GlobalBan, 880_202) is None:
            s.add(GlobalBan(user_id=880_202, reason="scam", banned_by=1))
        await s.commit()


class TestEnforcement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        ft.invalidate_enforcement_cache()

    async def test_fban_gban_and_subscription_are_found(self):
        self.assertIn("fbanned in Alpha: spam", await ft.ban_reason_for(-880_101, 880_201))
        self.assertIn("gbanned: scam", await ft.ban_reason_for(-880_101, 880_202))
        self.assertIn("fbanned in Beta: raid", await ft.ban_reason_for(-880_101, 880_203))  # via /subfed
        self.assertIsNone(await ft.ban_reason_for(-880_101, 880_204))

    async def test_chat_outside_a_federation_only_applies_gbans(self):
        self.assertIsNone(await ft.ban_reason_for(-880_103, 880_201))
        self.assertIn("gbanned", await ft.ban_reason_for(-880_103, 880_202))

    async def test_chat_can_switch_enforcement_off(self):
        self.assertIsNone(await ft.ban_reason_for(-880_102, 880_201))
        self.assertIsNone(await ft.ban_reason_for(-880_102, 880_202))

    async def test_cache_is_used_until_invalidated(self):
        self.assertIsNone(await ft.ban_reason_for(-880_101, 880_210))
        async with async_session() as s:
            s.add(FedBan(federation_id=F_A, user_id=880_210, reason="late", banned_by=1))
            await s.commit()
        self.assertIsNone(await ft.ban_reason_for(-880_101, 880_210))      # cached
        ft.invalidate_enforcement_cache()
        self.assertIn("late", await ft.ban_reason_for(-880_101, 880_210))  # fresh

    def _join_update(self, user_id, chat_id=-880_101, joined=True):
        update = make_update("", user_id=user_id, chat_id=chat_id)
        user = update.effective_user
        update.effective_message.new_chat_members = [user] if joined else None
        return update

    async def test_banned_joiner_is_banned_and_announced(self):
        bot = FakeBot()
        await ft.ban_enforcer(self._join_update(880_201), make_context(bot))
        self.assertEqual(bot.called("ban_chat_member")[0][0], (-880_101, 880_201))
        self.assertTrue(bot.called("send_message"))

    async def test_banned_speaker_gets_message_deleted(self):
        bot = FakeBot()
        update = self._join_update(880_202, joined=False)
        await ft.ban_enforcer(update, make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)
        update.effective_message.delete.assert_awaited()

    async def test_clean_user_and_sudoer_are_untouched(self):
        bot = FakeBot()
        await ft.ban_enforcer(self._join_update(880_204), make_context(bot))
        from bot.config import settings
        await ft.ban_enforcer(self._join_update(settings.owner_id), make_context(bot))
        self.assertEqual(bot.called("ban_chat_member"), [])

    async def test_failed_ban_is_not_announced_and_is_not_retried_every_message(self):
        bot = FakeBot(fail={"ban_chat_member": BadRequest("Not enough rights")})
        await ft.ban_enforcer(self._join_update(880_201), make_context(bot))
        await ft.ban_enforcer(self._join_update(880_201), make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)
        self.assertEqual(bot.called("send_message"), [])

    async def test_private_chats_are_ignored(self):
        bot = FakeBot()
        update = make_update("hi", user_id=880_201, chat_id=880_201, chat_type="private")
        await ft.ban_enforcer(update, make_context(bot))
        self.assertEqual(bot.calls, [])


class TestManagementCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        ft.invalidate_enforcement_cache()

    async def _fed(self, fid=F_A):
        async with async_session() as s:
            fed = await s.get(Federation, fid)
            return SimpleNamespace(**{c: getattr(fed, c) for c in ("id", "name", "owner_id", "admins", "subscribed", "rules", "notify", "log_channel")})

    async def test_subfed_requires_owner_valid_id_and_respects_limits(self):
        bot = FakeBot()
        # a fed admin who is not the owner is refused
        upd = make_update("/subfed", user_id=ADMIN_A, chat_id=-880_101)
        await ft.subfed_cmd(upd, make_context(bot, [F_DEL]))
        self.assertIn("owner", upd.effective_message.reply_text.await_args.args[0])
        # owner + unknown fed
        upd = make_update("/subfed", user_id=OWNER_A, chat_id=-880_101)
        await ft.subfed_cmd(upd, make_context(bot, [F_NONE]))
        self.assertIn("No federation", upd.effective_message.reply_text.await_args.args[0])
        # garbage id
        upd = make_update("/subfed", user_id=OWNER_A, chat_id=-880_101)
        await ft.subfed_cmd(upd, make_context(bot, ["nope"]))
        self.assertIn("Usage", upd.effective_message.reply_text.await_args.args[0])
        # itself
        upd = make_update("/subfed", user_id=OWNER_A, chat_id=-880_101)
        await ft.subfed_cmd(upd, make_context(bot, [F_A]))
        self.assertIn("itself", upd.effective_message.reply_text.await_args.args[0])
        # already subscribed to B
        upd = make_update("/subfed", user_id=OWNER_A, chat_id=-880_101)
        await ft.subfed_cmd(upd, make_context(bot, [F_B]))
        self.assertIn("Already", upd.effective_message.reply_text.await_args.args[0])

    async def test_setfrules_and_frules_roundtrip(self):
        bot = FakeBot()
        upd = make_update("/setfrules", user_id=ADMIN_A, chat_id=-880_101)
        await ft.setfrules_cmd(upd, make_context(bot, ["be", "kind"]))
        upd = make_update("/frules", user_id=555, chat_id=-880_101)
        await ft.frules_cmd(upd, make_context(bot))
        self.assertIn("be kind", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/setfrules", user_id=555, chat_id=-880_101)  # a plain member
        await ft.setfrules_cmd(upd, make_context(bot, ["x"]))
        self.assertIn("admins only", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual((await self._fed()).rules, "be kind")

    async def test_fedstat_reports_ban_and_clean_status(self):
        bot = FakeBot()
        upd = make_update("/fedstat", user_id=555, chat_id=-880_101)
        await ft.fedstat_cmd(upd, make_context(bot, ["880201"]))
        self.assertIn("fbanned in", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/fedstat", user_id=555, chat_id=-880_101)
        await ft.fedstat_cmd(upd, make_context(bot, ["880299"]))
        self.assertIn("not fbanned", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/fedstat", user_id=555, chat_id=-880_103)  # chat without a federation
        await ft.fedstat_cmd(upd, make_context(bot, ["880201"]))
        self.assertIn("isn't in a federation", upd.effective_message.reply_text.await_args.args[0])

    async def test_fbanlist_exports_for_admins_only(self):
        bot = FakeBot()
        upd = make_update("/fbanlist", user_id=ADMIN_A, chat_id=-880_101)
        upd.effective_message.reply_document = AsyncMock()
        await ft.fbanlist_cmd(upd, make_context(bot, ["json"]))
        upd.effective_message.reply_document.assert_awaited()
        upd = make_update("/fbanlist", user_id=555, chat_id=-880_101)
        upd.effective_message.reply_document = AsyncMock()
        await ft.fbanlist_cmd(upd, make_context(bot))
        upd.effective_message.reply_document.assert_not_awaited()

    async def test_delfed_needs_confirmation_and_cleans_up(self):
        async with async_session() as s:
            if await s.get(Federation, F_DEL) is None:
                s.add(Federation(id=F_DEL, name="Doomed", owner_id=OWNER_A, admins=[]))
                s.add(Chat(id=-880_110, type="supergroup", title="d", federation_id=F_DEL))
                await s.flush()
                s.add(FedBan(federation_id=F_DEL, user_id=880_301, reason="x", banned_by=1))
                await s.commit()
        bot = FakeBot()

        def cb(user_id, choice):
            q = SimpleNamespace(data=f"fedtools:del:{choice}:{F_DEL}", from_user=SimpleNamespace(id=user_id),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
            return SimpleNamespace(callback_query=q, effective_chat=None, effective_user=q.from_user, effective_message=None), q

        upd, q = cb(555, "yes")                       # not the owner
        await ft.delfed_callback(upd, make_context(bot))
        q.answer.assert_awaited_with("Only the federation owner can do that.", show_alert=True)
        async with async_session() as s:
            self.assertIsNotNone(await s.get(Federation, F_DEL))
        upd, q = cb(OWNER_A, "no")
        await ft.delfed_callback(upd, make_context(bot))
        async with async_session() as s:
            self.assertIsNotNone(await s.get(Federation, F_DEL))
        upd, q = cb(OWNER_A, "yes")
        await ft.delfed_callback(upd, make_context(bot))
        async with async_session() as s:
            self.assertIsNone(await s.get(Federation, F_DEL))
            self.assertIsNone((await s.get(Chat, -880_110)).federation_id)
            self.assertEqual((await s.execute(select(FedBan).where(FedBan.federation_id == F_DEL))).all(), [])

    async def test_banenforce_toggle_is_admin_gated_and_persists(self):
        bot = FakeBot(members={(-880_103, 700): admin(700)})
        upd = make_update("/banenforce", user_id=700, chat_id=-880_103)
        await ft.banenforce_cmd(upd, make_context(bot, ["off"], ))
        async with async_session() as s:
            self.assertIs((await s.get(Chat, -880_103)).ban_enforce, False)
        upd = make_update("/banenforce", user_id=700, chat_id=-880_103)
        await ft.banenforce_cmd(upd, make_context(bot, ["on"]))
        async with async_session() as s:
            self.assertIs((await s.get(Chat, -880_103)).ban_enforce, True)


class TestRegistration(unittest.TestCase):
    def test_register_installs_enforcer_before_all_middleware_and_every_command(self):
        added = []
        app = SimpleNamespace(add_handler=lambda h, group=0: added.append((h, group)))
        ft.register(app)
        self.assertIn(-6, [g for _, g in added])
        names = set()
        for h, _ in added:
            names |= set(getattr(h, "commands", ()) or ())
        for cmd in ("banenforce", "fban", "unfban", "myfeds", "chatfed", "fedchats", "renamefed", "delfed", "subfed",
                    "unsubfed", "fedsubs", "setfrules", "frules", "fednotif", "setfedlog", "unsetfedlog", "fbroadcast",
                    "fedstat", "fbanstat", "fbanlist", "importfbans", "fedhelp", "feduserhelp", "fedadminhelp", "fedownerhelp"):
            self.assertIn(cmd, names)


if __name__ == "__main__":
    unittest.main()
