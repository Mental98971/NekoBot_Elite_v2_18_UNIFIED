"""Tests for bot/plugins/group_mgmt.py: member notices, /welcome, /locks, lock enforcement.

The database layer is replaced by a tiny in-memory fake so these run without a
real engine. What is verified is the handler logic: that welcome/goodbye text is
actually *sent* (the welcome listener used to read ``update.chat_member`` on a
message handler and therefore never fired), that member-controlled values are
HTML-escaped, and that lock enforcement only pays for an admin lookup when a
message actually breaks a lock.
"""
from __future__ import annotations

import os
import sys
import tempfile
import contextlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}")

from telegram.error import BadRequest  # noqa: E402

import bot.plugins.group_mgmt as gm  # noqa: E402

CHAT_ID = -100123


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, key):
        return self.rows.get(key)

    async def execute(self, _statement):
        return _FakeResult(self.rows.get(CHAT_ID))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _chat_row(**overrides):
    row = SimpleNamespace(
        id=CHAT_ID, title="Test Group", type="supergroup",
        welcome_enabled=True, welcome_text=None, goodbye_enabled=False, goodbye_text=None, rules=None,
        lock_url=False, lock_forward=False, lock_photo=False, lock_video=False,
        lock_sticker=False, lock_gif=False, lock_contact=False, lock_location=False,
        clean_service=False, clean_welcome=False, last_welcome_id=None, private_rules=False,
    )
    row.__dict__.update(overrides)
    return row


def _user(uid=5, first="Ann", last=None, username="ann", is_bot=False):
    return SimpleNamespace(id=uid, first_name=first, last_name=last, username=username, is_bot=is_bot)


def _message(**overrides):
    msg = SimpleNamespace(
        new_chat_members=None, left_chat_member=None, entities=[], caption_entities=[],
        forward_origin=None, photo=[], video=None, sticker=None, animation=None,
        contact=None, location=None, venue=None,
        delete=AsyncMock(), reply_text=AsyncMock(),
    )
    msg.__dict__.update(overrides)
    return msg


def _update(message=None, user=None, chat_type="supergroup"):
    chat = MagicMock()
    chat.id, chat.type, chat.title = CHAT_ID, chat_type, "Test Group"
    chat.get_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    msg = message or _message()
    update = MagicMock()
    update.effective_chat = chat
    update.effective_message = msg
    update.message = msg
    update.effective_user = user or _user()
    return update


def _context(args=None, caller_status="administrator"):
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock()
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=caller_status))
    return context


@contextlib.contextmanager
def _db_ctx(row, session):
    """Handlers read chat settings via bot.utils.chatcache (v2.12) and the admin cache is process-wide,
    so patch both and start every test with an empty admin cache."""
    gm.mt._admin_cache.clear()
    gm._lock_notice.clear()      # per-user notice cooldown is process-wide too
    with patch.object(gm, "async_session", lambda: session), \
            patch.object(gm.chatcache, "get_chat_settings", AsyncMock(return_value=row)), \
            patch.object(gm.chatcache, "get_approved_ids", AsyncMock(return_value=frozenset())):
        yield


def _patched_db(row=None):
    session = _FakeSession({CHAT_ID: row} if row is not None else {})
    return session, _db_ctx(row, session)


class TestMemberNotices(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_is_sent_to_each_new_human_with_escaped_values(self):
        row = _chat_row(welcome_text="Hi {first}! Welcome to {chat}. {unknown} {mention}")
        _, db = _patched_db(row)
        update = _update(_message(new_chat_members=[_user(7, first="<Eve>"), _user(8, first="Botty", is_bot=True)]))
        context = _context()
        with db:
            await gm.welcome_listener(update, context)
        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.call_args
        self.assertEqual(args[0], CHAT_ID)
        self.assertIn("Hi &lt;Eve&gt;!", args[1])
        self.assertIn("Test Group", args[1])
        self.assertIn("{unknown}", args[1])           # unknown tokens stay literal, nothing raises
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_welcome_silent_when_disabled_or_without_text(self):
        for row in (_chat_row(welcome_enabled=False, welcome_text="Hi"), _chat_row(welcome_text=None)):
            _, db = _patched_db(row)
            context = _context()
            with db:
                await gm.welcome_listener(_update(_message(new_chat_members=[_user()])), context)
            context.bot.send_message.assert_not_awaited()

    async def test_null_flags_follow_the_column_defaults(self):
        """Chat rows migrated from older schemas can carry NULL; welcome defaults on, goodbye off."""
        for listener, message, flag, text_attr, expect in (
            (gm.welcome_listener, _message(new_chat_members=[_user()]), "welcome_enabled", "welcome_text", 1),
            (gm.goodbye_listener, _message(left_chat_member=_user()), "goodbye_enabled", "goodbye_text", 0),
        ):
            row = _chat_row(**{flag: None, text_attr: "Hello {first}"})
            _, db = _patched_db(row)
            context = _context()
            with db:
                await listener(_update(message), context)
            self.assertEqual(context.bot.send_message.await_count, expect, flag)

    async def test_welcome_ignores_updates_without_joiners(self):
        _, db = _patched_db(_chat_row(welcome_text="Hi"))
        context = _context()
        with db:
            await gm.welcome_listener(_update(_message(new_chat_members=None)), context)
        context.bot.send_message.assert_not_awaited()

    async def test_bulk_add_is_capped(self):
        _, db = _patched_db(_chat_row(welcome_text="Hi {first}"))
        context = _context()
        with db:
            await gm.welcome_listener(_update(_message(new_chat_members=[_user(i, first=f"U{i}") for i in range(20)])), context)
        self.assertEqual(context.bot.send_message.await_count, 5)

    async def test_bad_markup_falls_back_to_plain_text(self):
        _, db = _patched_db(_chat_row(welcome_text="Rules: <read them"))
        context = _context()
        context.bot.send_message = AsyncMock(side_effect=[BadRequest("Can't parse entities: unsupported start tag"), None])
        with db:
            await gm.welcome_listener(_update(_message(new_chat_members=[_user()])), context)
        self.assertEqual(context.bot.send_message.await_count, 2)
        _, retry_kwargs = context.bot.send_message.call_args
        self.assertNotIn("parse_mode", retry_kwargs)

    async def test_plain_fallback_shows_real_names_not_html_entities(self):
        _, db = _patched_db(_chat_row(welcome_text="Hi {first}, <broken"))
        context = _context()
        context.bot.send_message = AsyncMock(side_effect=[BadRequest("Can't parse entities"), None])
        with db:
            await gm.welcome_listener(_update(_message(new_chat_members=[_user(first="A&B <3")])), context)
        html_attempt, plain_attempt = context.bot.send_message.call_args_list
        self.assertIn("A&amp;B &lt;3", html_attempt[0][1])
        self.assertEqual(plain_attempt[0][1], "Hi A&B <3, <broken")

    async def test_goodbye_needs_flag_and_text(self):
        gone = _user(9, first="Zed")
        for row, expect_sent in (
            (_chat_row(goodbye_enabled=True, goodbye_text="Bye {first}"), True),
            (_chat_row(goodbye_enabled=False, goodbye_text="Bye {first}"), False),
            (_chat_row(goodbye_enabled=True, goodbye_text=None), False),
        ):
            _, db = _patched_db(row)
            context = _context()
            with db:
                await gm.goodbye_listener(_update(_message(left_chat_member=gone)), context)
            self.assertEqual(context.bot.send_message.await_count, 1 if expect_sent else 0)
            if expect_sent:
                self.assertIn("Bye Zed", context.bot.send_message.call_args[0][1])


class TestWelcomeCommand(unittest.IsolatedAsyncioTestCase):
    async def test_shows_current_text_escaped(self):
        row = _chat_row(welcome_text="Hello <b>{first}</b>")
        _, db = _patched_db(row)
        update, context = _update(), _context()
        with db:
            await gm.welcome_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("&lt;b&gt;{first}&lt;/b&gt;", sent)
        self.assertIn("ON", sent)

    async def test_off_and_on_toggle_the_flag(self):
        row = _chat_row(welcome_text="Hi")
        session, db = _patched_db(row)
        with db:
            await gm.welcome_cmd(_update(), _context(["off"]))
            self.assertFalse(row.welcome_enabled)
            await gm.welcome_cmd(_update(), _context(["on"]))
            self.assertTrue(row.welcome_enabled)
        self.assertEqual(session.commits, 2)

    async def test_rejects_unknown_argument_and_non_admins(self):
        update = _update()
        with patch.object(gm, "async_session", lambda: _FakeSession()):
            await gm.welcome_cmd(update, _context(["maybe"]))
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

        update = _update()
        await gm.welcome_cmd(update, _context(["off"], caller_status="member"))
        self.assertIn("Admin only", update.message.reply_text.call_args[0][0])


class TestRules(unittest.IsolatedAsyncioTestCase):
    async def test_rules_with_broken_markup_still_display(self):
        _, db = _patched_db(_chat_row(rules="1. Be <nice"))
        update, context = _update(), _context()
        update.message.reply_text = AsyncMock(side_effect=[BadRequest("Can't parse entities"), None])
        with db:
            await gm.rules_cmd(update, context)
        self.assertEqual(update.message.reply_text.await_count, 2)
        plain_text = update.message.reply_text.call_args[0][0]
        self.assertIn("1. Be <nice", plain_text)


class TestLocks(unittest.IsolatedAsyncioTestCase):
    async def test_locks_command_lists_locked_and_open_types(self):
        _, db = _patched_db(_chat_row(lock_url=True, lock_photo=True))
        update = _update()
        with db:
            await gm.locks_cmd(update, _context())
        sent = update.message.reply_text.call_args[0][0]
        locked_line = next(line for line in sent.splitlines() if line.startswith("🔒"))
        open_line = next(line for line in sent.splitlines() if line.startswith("🔓"))
        self.assertIn("url", locked_line)
        self.assertIn("photo", locked_line)
        self.assertNotIn("sticker", locked_line)
        self.assertIn("sticker", open_line)

    async def test_locks_command_when_nothing_is_locked(self):
        _, db = _patched_db(_chat_row())
        update = _update()
        with db:
            await gm.locks_cmd(update, _context())
        self.assertIn("nothing", update.message.reply_text.call_args[0][0])

    async def test_no_admin_lookup_when_chat_has_no_locks(self):
        _, db = _patched_db(_chat_row())
        update = _update(_message(photo=[object()]))
        with db:
            await gm.lock_listener(update, _context())
        update.effective_chat.get_member.assert_not_awaited()
        update.message.delete.assert_not_awaited()

    async def test_no_admin_lookup_for_compliant_messages(self):
        _, db = _patched_db(_chat_row(lock_photo=True))
        update = _update(_message())              # plain text: breaks no lock
        with db:
            await gm.lock_listener(update, _context())
        update.effective_chat.get_member.assert_not_awaited()
        update.message.delete.assert_not_awaited()

    async def test_violation_by_member_is_deleted_and_announced(self):
        _, db = _patched_db(_chat_row(lock_photo=True))
        update, context = _update(_message(photo=[object()])), _context(caller_status="member")
        with db:
            await gm.lock_listener(update, context)
        update.message.delete.assert_awaited_once()
        self.assertIn("locked", context.bot.send_message.call_args[0][1])

    async def test_admins_are_exempt(self):
        _, db = _patched_db(_chat_row(lock_photo=True))
        update = _update(_message(photo=[object()]))
        with db:
            await gm.lock_listener(update, _context(caller_status="administrator"))
        update.message.delete.assert_not_awaited()

    async def test_url_lock_also_checks_caption_entities(self):
        _, db = _patched_db(_chat_row(lock_url=True))
        link = SimpleNamespace(type="text_link")
        update = _update(_message(caption_entities=[link]))
        with db:
            await gm.lock_listener(update, _context(caller_status="member"))
        update.message.delete.assert_awaited_once()

    async def test_forward_lock_uses_forward_origin(self):
        _, db = _patched_db(_chat_row(lock_forward=True))
        update = _update(_message(forward_origin=object()))
        with db:
            await gm.lock_listener(update, _context(caller_status="member"))
        update.message.delete.assert_awaited_once()


class TestRegistration(unittest.TestCase):
    def test_new_commands_and_listeners_are_registered(self):
        from telegram.ext import CommandHandler, MessageHandler

        added = []

        class FakeApp:
            job_queue = None

            def add_handler(self, handler, group=0):
                added.append((handler, group))

        gm.register(FakeApp())
        commands = {c for h, _ in added if isinstance(h, CommandHandler) for c in h.commands}
        self.assertTrue({"welcome", "locks", "lock", "unlock", "setwelcome", "setgoodbye"} <= commands)
        callbacks = {(h.callback.__name__, g) for h, g in added if isinstance(h, MessageHandler)}
        self.assertIn(("welcome_listener", 4), callbacks)
        self.assertIn(("goodbye_listener", 12), callbacks)


if __name__ == "__main__":
    unittest.main()
