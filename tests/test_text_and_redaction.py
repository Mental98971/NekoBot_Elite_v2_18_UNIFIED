"""Pure-Python tests (no Telegram, no database): template rendering and log redaction."""
from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.utils import redact  # noqa: E402
from bot.utils.textutil import render_template  # noqa: E402

BOT_TOKEN = "123456789:AAH" + "x" * 32


class TestRenderTemplate(unittest.TestCase):
    def test_known_tokens_are_substituted(self):
        self.assertEqual(render_template("Hi {first} {last}!", {"first": "A", "last": "B"}), "Hi A B!")

    def test_unknown_tokens_and_stray_braces_stay_literal(self):
        out = render_template("{nope} { } {first.__class__} {{x}} {", {"first": "A"})
        self.assertEqual(out, "{nope} { } {first.__class__} {{x}} {")

    def test_attribute_and_index_lookups_are_not_evaluated(self):
        self.assertEqual(render_template("{first.__class__}{first[0]}", {"first": "A"}), "{first.__class__}{first[0]}")

    def test_none_and_empty(self):
        self.assertEqual(render_template("", {}), "")
        self.assertEqual(render_template(None, {}), "")


class TestRedactText(unittest.TestCase):
    def test_bot_token_in_url(self):
        out = redact.redact_text(f"POST https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
        self.assertEqual(out, "POST https://api.telegram.org/bot<redacted-bot-token>/getUpdates")

    def test_provider_keys_and_bearer(self):
        self.assertNotIn("abcdefghijklmnopqrstuvwx", redact.redact_text("key sk-abcdefghijklmnopqrstuvwx used"))
        self.assertEqual(redact.redact_text("Authorization: Bearer abc123def456"), "Authorization: Bearer ***")

    def test_ordinary_text_is_untouched(self):
        for text in ("ratio 12:30", "order 1234567 : 99 ok", "see https://example.com/a:b", "id 123456789"):
            self.assertEqual(redact.redact_text(text), text)

    def test_empty(self):
        self.assertEqual(redact.redact_text(""), "")


class TestRedactValue(unittest.TestCase):
    def test_secret_keys_are_masked_by_name(self):
        self.assertEqual(redact.redact_value("api_key", "sk-abcdefghijklmnop"), "sk-a…op")
        self.assertEqual(redact.redact_value("password", "short"), "***")

    def test_plain_keys_pass_through(self):
        self.assertEqual(redact.redact_value("note", "hello"), "hello")
        self.assertEqual(redact.redact_value("count", 3), 3)
        self.assertIsNone(redact.redact_value("x", None))

    def test_nested_containers(self):
        out = redact.redact_value("data", {"token": "123456789012", "urls": [f"bot{BOT_TOKEN}"]})
        self.assertEqual(out, {"token": "1234…12", "urls": ["bot<redacted-bot-token>"]})

    def test_structlog_processor_shape(self):
        out = redact.redact_event(None, "info", {"event": "x", "error": f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"})
        self.assertNotIn(BOT_TOKEN, out["error"])


class TestStdlibFilter(unittest.TestCase):
    def _logger(self, name):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        redact.install_stdlib_redaction(logger)
        return logger, handler, buf

    def test_message_args_and_traceback_are_scrubbed(self):
        logger, _, buf = self._logger("redaction.test.a")
        logger.info("HTTP Request: %s %s", "POST", f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
        try:
            raise RuntimeError(f"failed https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
        except RuntimeError:
            logger.exception("boom")
        out = buf.getvalue()
        self.assertNotIn(BOT_TOKEN, out)
        self.assertEqual(out.count("<redacted-bot-token>"), 2)

    def test_install_is_idempotent(self):
        logger, handler, _ = self._logger("redaction.test.b")
        redact.install_stdlib_redaction(logger)
        self.assertEqual(sum(isinstance(f, redact.RedactingFilter) for f in handler.filters), 1)


if __name__ == "__main__":
    unittest.main()
