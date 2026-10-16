"""Wiring checks for the v2.12 plugins: nothing implemented-but-disconnected, groups documented."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")

PLUGINS = PROJECT_ROOT / "bot" / "plugins"
V212 = ("admin", "adminutils", "approvals", "blacklist", "connection", "modsettings", "group_mgmt")


def _registered_callbacks(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        kind = getattr(node.func, "id", getattr(node.func, "attr", "")) if isinstance(node, ast.Call) else ""
        if kind in ("CommandHandler", "CallbackQueryHandler", "MessageHandler"):
            position = 0 if kind == "CallbackQueryHandler" else 1      # (callback, pattern=) vs (commands|filters, callback)
            for arg in node.args[position:position + 1]:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
    return names


class TestEveryHandlerIsRegistered(unittest.TestCase):
    def test_no_command_function_is_left_unregistered(self):
        for plugin in V212:
            tree = ast.parse((PLUGINS / f"{plugin}.py").read_text(encoding="utf-8"))
            registered = _registered_callbacks(tree)
            defined = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                       and (n.name.endswith("_cmd") or n.name.endswith("_callback") or n.name.endswith("_listener"))}
            self.assertEqual(sorted(defined - registered), [], f"{plugin}: implemented but never registered")

    def test_every_new_plugin_exposes_register(self):
        for plugin in ("adminutils", "approvals", "blacklist", "connection", "modsettings"):
            tree = ast.parse((PLUGINS / f"{plugin}.py").read_text(encoding="utf-8"))
            self.assertIn("register", {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}, plugin)


class TestCommandsMovedExactlyOnce(unittest.TestCase):
    def test_moved_commands_have_a_single_owner(self):
        owners = {}
        for path in PLUGINS.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CommandHandler" and node.args:
                    first = node.args[0]
                    names = [first.value] if isinstance(first, ast.Constant) else [e.value for e in getattr(first, "elts", []) if isinstance(e, ast.Constant)]
                    for name in names:
                        owners.setdefault(name, set()).add(path.stem)
        for command, plugin in (("warns", "admin"), ("resetwarns", "admin"), ("report", "modsettings"), ("log", "modsettings"),
                                ("setwarnlimit", "modsettings"), ("slowmode", "modsettings"), ("pin", "adminutils"), ("unpin", "adminutils")):
            self.assertEqual(owners.get(command), {plugin}, command)


class TestHandlerGroupMapIsCurrent(unittest.TestCase):
    def test_group_map_comment_lists_every_message_handler_group(self):
        import re
        text = (PLUGINS / "group_mgmt.py").read_text(encoding="utf-8")
        used = set()
        for path in list(PLUGINS.glob("*.py")) + [PROJECT_ROOT / "bot" / "core" / "bot.py"]:
            used |= {int(m) for m in re.findall(r"group=(-?\d+)", path.read_text(encoding="utf-8"))}
        for group in sorted(used):
            self.assertRegex(text, rf"#\s+{group}\s", f"group {group} missing from the map in group_mgmt.register()")


if __name__ == "__main__":
    unittest.main()
