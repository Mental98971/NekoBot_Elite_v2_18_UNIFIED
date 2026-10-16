"""Guards against importing names that do not exist in python-telegram-bot (v2.13 shipped `InputMediaGeo`).

Plugins are imported by a fault-tolerant loader, so a bad import is not a crash: it silently removes the whole plugin.
This test therefore checks every ``from telegram[...] import name`` in the project:

* offline: against the vetted list below (a new name fails until someone confirms it exists and adds it here);
* with the library installed: that the name really exists in that module.
"""
from __future__ import annotations

import ast
import importlib
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KNOWN = {
    "telegram": {
        "Bot", "ChatPermissions", "InlineKeyboardButton", "InlineKeyboardMarkup", "InlineQueryResultArticle",
        "InlineQueryResultCachedPhoto", "InlineQueryResultCachedVideo", "InputFile", "InputSticker",
        "InputTextMessageContent", "Message", "MessageEntity", "Update",
        # v2.15 (whispers / shared inline router) - all present since PTB 20.3-21.0
        "InlineQueryResultsButton", "InputMediaAudio", "InputMediaDocument", "InputMediaPhoto", "InputMediaVideo",
        "LinkPreviewOptions", "User",
    },
    "telegram.constants": {"ChatMemberStatus", "ChatType", "StickerFormat"},
    "telegram.error": {"BadRequest", "Forbidden", "NetworkError", "TelegramError", "TimedOut"},
    "telegram.ext": {
        "Application", "ApplicationHandlerStop", "BaseUpdateProcessor", "CallbackQueryHandler", "CommandHandler",
        "ChosenInlineResultHandler", "ContextTypes", "InlineQueryHandler", "MessageHandler", "PicklePersistence", "filters",
    },
}


def _imports():
    found = defaultdict(lambda: defaultdict(set))
    for top in ("bot", "training", "scripts", "tests", "dashboard"):
        for path in (ROOT / top).rglob("*.py"):
            if "__pycache__" in path.parts or path.name == "test_ptb_names.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                        node.module == "telegram" or node.module.startswith("telegram.")):
                    for alias in node.names:
                        found[node.module][alias.name].add(str(path.relative_to(ROOT)))
    return found


class TestTelegramImportNames(unittest.TestCase):
    def test_every_imported_name_is_a_known_real_one(self):
        unknown = [f"{mod}.{name} ({sorted(files)[0]})" for mod, names in _imports().items()
                   for name, files in names.items() if name not in KNOWN.get(mod, set())]
        self.assertEqual(unknown, [], "confirm each name exists in python-telegram-bot >= 21, then add it to KNOWN")

    def test_names_exist_in_the_installed_library(self):
        try:
            importlib.import_module("telegram")
        except ImportError:
            self.skipTest("python-telegram-bot is not installed here")
        missing = []
        for mod, names in _imports().items():
            module = importlib.import_module(mod)
            missing += [f"{mod}.{n}" for n in names if not hasattr(module, n)]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
