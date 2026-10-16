"""Graceful restart, host stats, and the sudo gate on /update and /restart."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")

from _fakes import FakeBot, make_context, make_update  # noqa: E402

from bot.core import restart  # noqa: E402
from bot.plugins import chatfight as cf  # noqa: E402
from bot.utils import sysstats  # noqa: E402


class TestRestart(unittest.TestCase):
    def setUp(self):
        restart.reset_for_tests()

    def test_request_stops_the_loop_and_defers_exec(self):
        app = MagicMock()
        self.assertTrue(restart.request(app))
        app.stop_running.assert_called_once()
        with patch("bot.core.restart.os.execv") as execv:
            restart.exec_if_requested()
        self.assertEqual(execv.call_args[0][1][1:], ["-m", "bot"])

    def test_no_exec_unless_requested_and_unsupported_app(self):
        with patch("bot.core.restart.os.execv") as execv:
            restart.exec_if_requested()
        execv.assert_not_called()
        self.assertFalse(restart.request(object()))
        self.assertFalse(restart.is_requested())


class TestSysstats(unittest.IsolatedAsyncioTestCase):
    def test_uptime_format(self):
        self.assertEqual(sysstats.format_uptime(3725), "1h:2m:5s")
        self.assertEqual(sysstats.format_uptime(-4), "0h:0m:0s")

    async def test_collect_is_bounded(self):
        data = await sysstats.collect()
        for key in ("cpu_percent", "ram_percent", "disk_percent"):
            self.assertTrue(0.0 <= data[key] <= 100.0)


class TestOwnerCommands(unittest.IsolatedAsyncioTestCase):
    async def test_non_sudo_is_refused(self):
        for handler in (cf.update_cmd, cf.restart_cmd):
            update = make_update("/x", user_id=5)
            with patch.object(cf, "_restart", AsyncMock()) as restart_mock:
                await handler(update, make_context(FakeBot()))
            restart_mock.assert_not_called()
            self.assertIn("not allowed", update.effective_message.reply_text.call_args[0][0])

    async def test_restart_by_owner_flushes_and_requests(self):
        update = make_update("/restart", user_id=int(os.environ["OWNER_ID"]))
        with patch.object(cf, "_restart", AsyncMock(return_value=True)) as restart_mock:
            await cf.restart_cmd(update, make_context(FakeBot()))
        restart_mock.assert_awaited_once()

    async def test_update_refuses_without_git_checkout(self):
        update = make_update("/update", user_id=int(os.environ["OWNER_ID"]))
        with patch.object(cf, "REPO_DIR", Path("/nonexistent-checkout")):
            await cf.update_cmd(update, make_context(FakeBot()))
        self.assertIn("not a git checkout", update.effective_message.reply_text.call_args[0][0])

    async def test_git_token_never_reaches_argv_or_output(self):
        captured = {}

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"fatal: could not read https://x-access-token:SECRETTOKEN@host", None

        async def fake_exec(*args, **kwargs):
            captured["args"], captured["env"] = args, kwargs["env"]
            return Proc()

        fake_settings = NS(git_token=NS(get_secret_value=lambda: "SECRETTOKEN"), upstream_repo="https://example.com/r.git",
                           upstream_branch="main")
        with patch.object(cf, "settings", fake_settings), patch("asyncio.create_subprocess_exec", fake_exec):
            code, out = await cf._run_git_pull()
        self.assertNotIn("SECRETTOKEN", " ".join(captured["args"]))
        self.assertIn("--ff-only", captured["args"])
        self.assertNotIn("SECRETTOKEN", out)
        self.assertEqual(captured["env"]["GIT_CONFIG_COUNT"], "1")


if __name__ == "__main__":
    unittest.main()
