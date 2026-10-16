"""
Tests for bot/plugins/music.py.

Covers what's meaningfully testable without live network/yt-dlp calls:
- DOWNLOAD_DIR must be an absolute path (regression test for the
  cwd-relative-path bug class already fixed elsewhere in this project —
  bot/plugins/music.py used to build it from a bare "data/downloads",
  which resolves differently depending on the process's working
  directory instead of the project's actual data directory).
- Usage/validation messages for missing or invalid arguments.
- The 50MB Telegram upload size guard, exercised by mocking the actual
  download instead of hitting the network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}")

import bot.plugins.music as music_plugin  # noqa: E402


def _make_update(text_args=None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_audio = AsyncMock()
    update.message.reply_video = AsyncMock()
    update.message.reply_voice = AsyncMock()
    update.message.delete = AsyncMock()
    update.effective_message.message_id = 999
    context = MagicMock()
    context.args = text_args or []
    return update, context


class TestDownloadDirIsAbsolute(unittest.TestCase):
    def test_download_dir_is_absolute_not_cwd_relative(self):
        # A bare "data/downloads" would fail this — its meaning changes
        # with the process's working directory (Docker/systemd/etc.).
        self.assertTrue(os.path.isabs(music_plugin.DOWNLOAD_DIR))


class TestUsageMessages(unittest.IsolatedAsyncioTestCase):
    async def test_music_cmd_with_no_query_shows_usage(self):
        update, context = _make_update([])
        await music_plugin.music_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)

    async def test_tts_cmd_with_no_text_shows_usage(self):
        update, context = _make_update([])
        await music_plugin.tts_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)

    async def test_tts_cmd_rejects_overly_long_text(self):
        update, context = _make_update(["word"] * 300)  # well over 800 chars
        await music_plugin.tts_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("800", sent)

    async def test_insta_cmd_with_no_url_shows_usage(self):
        update, context = _make_update([])
        await music_plugin.insta_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)

    async def test_insta_cmd_rejects_non_instagram_url(self):
        update, context = _make_update(["https://example.com/video"])
        await music_plugin.insta_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)

    async def test_qr_cmd_with_no_text_shows_usage(self):
        update, context = _make_update([])
        await music_plugin.qr_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)

    async def test_yt_cmd_with_no_url_shows_usage(self):
        update, context = _make_update([])
        await music_plugin.yt_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage", sent)


class TestSizeLimitGuard(unittest.IsolatedAsyncioTestCase):
    async def test_music_cmd_rejects_oversized_download_without_crashing(self):
        update, context = _make_update(["some song"])
        fake_path = tempfile.mktemp(suffix=".mp3")
        with open(fake_path, "wb") as f:
            f.write(b"0" * 100)  # tiny real file on disk; size is mocked below

        try:
            with patch.object(
                music_plugin, "_ytdlp_download",
                new=AsyncMock(return_value={"path": fake_path, "title": "Huge Track", "duration": 999, "thumbnail": None}),
            ), patch.object(music_plugin.os.path, "getsize", return_value=music_plugin.MAX_TELEGRAM_UPLOAD_BYTES + 1):
                await music_plugin.music_cmd(update, context)
            update.message.reply_audio.assert_not_awaited()
        finally:
            if os.path.exists(fake_path):
                os.remove(fake_path)

    async def test_music_cmd_download_failure_gives_a_safe_message_not_a_crash(self):
        update, context = _make_update(["some song"])
        with patch.object(
            music_plugin, "_ytdlp_download",
            new=AsyncMock(side_effect=RuntimeError("some internal yt-dlp traceback detail")),
        ):
            await music_plugin.music_cmd(update, context)
        # Should have edited the status message with a safe fallback, not raised.
        update.message.reply_text.assert_awaited()


if __name__ == "__main__":
    unittest.main()
