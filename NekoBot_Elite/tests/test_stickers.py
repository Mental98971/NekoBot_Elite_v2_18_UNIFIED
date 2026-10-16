"""
Tests for bot/plugins/stickers.py.

_resize_for_sticker is pure local image processing (PIL), so it's
tested against real generated images, not mocks — this is the one part
of this feature this sandbox can verify end-to-end. The Bot API calls
(create_new_sticker_set / add_sticker_to_set / get_sticker_set) are
mocked, since they need a live bot token against real Telegram servers.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}")

from telegram.error import BadRequest  # noqa: E402
import bot.plugins.stickers as stickers_plugin  # noqa: E402


def _make_png_bytes(width, height) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestPackName(unittest.TestCase):
    def test_pack_name_is_deterministic_and_valid_format(self):
        name = stickers_plugin._pack_name(12345, "NekoBot")
        self.assertEqual(name, stickers_plugin._pack_name(12345, "NekoBot"))
        self.assertTrue(name.endswith("_by_NekoBot"))
        self.assertTrue(name[0].isalpha())

    def test_different_users_get_different_pack_names(self):
        self.assertNotEqual(
            stickers_plugin._pack_name(111, "NekoBot"),
            stickers_plugin._pack_name(222, "NekoBot"),
        )


class TestResizeForSticker(unittest.IsolatedAsyncioTestCase):
    async def test_wide_image_resizes_longer_side_to_512(self):
        raw = _make_png_bytes(1000, 400)
        out = await stickers_plugin._resize_for_sticker(raw)
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(max(img.size), 512)
        self.assertAlmostEqual(img.size[0] / img.size[1], 1000 / 400, places=2)

    async def test_tall_image_resizes_longer_side_to_512(self):
        raw = _make_png_bytes(300, 900)
        out = await stickers_plugin._resize_for_sticker(raw)
        img = Image.open(io.BytesIO(out))
        self.assertEqual(max(img.size), 512)

    async def test_small_image_is_upscaled_to_512(self):
        raw = _make_png_bytes(64, 64)
        out = await stickers_plugin._resize_for_sticker(raw)
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.size, (512, 512))


def _make_update(args=None, has_reply=True):
    update = MagicMock()
    update.effective_user.id = 555
    update.effective_user.first_name = "Tester"
    update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    if has_reply:
        update.message.reply_to_message.photo = [MagicMock(file_id="photo123")]
        update.message.reply_to_message.sticker = None
        update.message.reply_to_message.document = None
    else:
        update.message.reply_to_message = None
    context = MagicMock()
    context.args = args or []
    context.bot.username = "TestBot"

    async def _fake_download_to_memory(out):
        out.write(_make_png_bytes(200, 200))

    fake_file = MagicMock()
    fake_file.download_to_memory = AsyncMock(side_effect=_fake_download_to_memory)
    context.bot.get_file = AsyncMock(return_value=fake_file)
    return update, context


class TestKangCmd(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_shows_usage(self):
        update, context = _make_update(has_reply=False)
        await stickers_plugin.kang_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Reply to", sent)

    async def test_creates_new_pack_when_none_exists(self):
        update, context = _make_update()
        context.bot.get_sticker_set = AsyncMock(side_effect=BadRequest("Stickerset_invalid"))
        context.bot.create_new_sticker_set = AsyncMock()
        context.bot.add_sticker_to_set = AsyncMock()
        await stickers_plugin.kang_cmd(update, context)
        context.bot.create_new_sticker_set.assert_awaited_once()
        context.bot.add_sticker_to_set.assert_not_called()

    async def test_adds_to_existing_pack(self):
        update, context = _make_update()
        context.bot.get_sticker_set = AsyncMock(return_value=MagicMock())
        context.bot.create_new_sticker_set = AsyncMock()
        context.bot.add_sticker_to_set = AsyncMock()
        await stickers_plugin.kang_cmd(update, context)
        context.bot.add_sticker_to_set.assert_awaited_once()
        context.bot.create_new_sticker_set.assert_not_called()


class TestPackinfoCmd(unittest.IsolatedAsyncioTestCase):
    async def test_no_pack_yet(self):
        update, context = _make_update(has_reply=False)
        context.bot.get_sticker_set = AsyncMock(side_effect=BadRequest("Stickerset_invalid"))
        await stickers_plugin.packinfo_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("don't have a pack", sent)

    async def test_existing_pack_reports_count(self):
        update, context = _make_update(has_reply=False)
        context.bot.get_sticker_set = AsyncMock(return_value=MagicMock(stickers=[1, 2, 3]))
        await stickers_plugin.packinfo_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("3 sticker", sent)


if __name__ == "__main__":
    unittest.main()
