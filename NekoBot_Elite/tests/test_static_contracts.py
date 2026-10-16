"""Static contracts — invariants that have broken in earlier builds, checked from source alone.

Nothing here imports the bot, python-telegram-bot, SQLAlchemy or the network: the checks
parse source files with ``ast`` / ``re``, so they run anywhere and fail with a pointer to
the exact thing that drifted. Earlier merges repeatedly shipped (a) commands advertised in
/help and the Telegram command menu with no handler behind them, (b) a dashboard image that
could not start because a module it imports was not copied in, (c) settings missing from
.env.example, and (d) Python 3.12-only syntax in a project that documents 3.11 support.
"""
from __future__ import annotations

import ast
import re
import sys
import tokenize
import io
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "bot" / "plugins"
SOURCE_DIRS = ("bot", "dashboard", "scripts", "tests")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _py_files():
    for d in SOURCE_DIRS:
        yield from sorted((ROOT / d).rglob("*.py"))


def _module_assignments(path: Path) -> dict:
    env = {}
    for node in ast.parse(_read(path)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            env[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            env[node.target.id] = node.value
    return env


def _literal_keys(node) -> list:
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant)]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant)]
    raise AssertionError(f"cannot resolve literal collection: {ast.dump(node)[:80]}")


def registered_commands() -> tuple[dict, list]:
    """Return ({command: [plugin, ...]}, unresolved registrations).

    Handles literal names/lists plus the three dynamic families (font styles, font
    effects, reaction actions). Any other computed registration is reported as
    unresolved so a new dynamic family cannot silently escape these checks.
    """
    font_keys = _literal_keys(_module_assignments(ROOT / "bot/fonts/catalog.py")["FONT_DEFS"])
    effect_keys = _literal_keys(_module_assignments(ROOT / "bot/fonts/catalog.py")["EFFECTS"])
    rx = _module_assignments(PLUGINS / "reactions.py")
    actions = sorted(set(_literal_keys(rx["ACTION_VERBS"])) | set(_literal_keys(rx["SOLO_ACTIONS"])))

    registry, unresolved = defaultdict(list), []
    for path in sorted(PLUGINS.glob("*.py")):
        for node in ast.walk(ast.parse(_read(path))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CommandHandler" and node.args):
                continue
            arg = node.args[0]
            names = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names = [arg.value]
            elif isinstance(arg, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in arg.elts):
                names = [e.value for e in arg.elts]
            elif path.stem == "fonts" and isinstance(arg, ast.Name) and arg.id == "fk":
                names = font_keys
            elif path.stem == "fonts" and "SHORTCUT_OVERRIDES" in ast.unparse(arg):
                names = ["prohibit" if k == "stop" else k for k in effect_keys]
            elif path.stem == "reactions" and isinstance(arg, ast.Name) and arg.id == "ACTIONS":
                names = actions
            if names is None:
                unresolved.append(f"{path.name}:{node.lineno}: {ast.unparse(arg)[:60]}")
                continue
            for name in names:
                registry[name].append(path.name)
    return dict(registry), unresolved


class TestPythonPortability(unittest.TestCase):
    def test_sources_parse_as_python_311(self):
        bad = []
        for path in _py_files():
            try:
                ast.parse(_read(path), feature_version=(3, 11))
            except SyntaxError as e:
                bad.append(f"{path.relative_to(ROOT)}:{e.lineno}: {e.msg}")
        self.assertEqual(bad, [])

    @unittest.skipUnless(sys.version_info >= (3, 12), "tokenizer only reports f-string parts on 3.12+")
    def test_no_quote_reuse_inside_fstring_fields(self):
        """PEP 701 allows f"{d["k"]}" on 3.12+, which is a SyntaxError on 3.11."""
        offenders = []
        for path in _py_files():
            stack = []
            for tok in tokenize.generate_tokens(io.StringIO(_read(path)).readline):
                name = tokenize.tok_name.get(tok.type, "")
                if name == "FSTRING_START":
                    quote = tok.string[-3:] if tok.string.endswith(('"""', "'''")) else tok.string[-1]
                    if stack and stack[-1]["depth"] > 0 and stack[-1]["quote"] == quote and len(quote) == 1:
                        offenders.append(f"{path.relative_to(ROOT)}:{tok.start[0]}")
                    stack.append({"quote": quote, "depth": 0})
                elif name == "FSTRING_END":
                    stack.pop()
                elif stack and name == "OP" and tok.string == "{":
                    stack[-1]["depth"] += 1
                elif stack and name == "OP" and tok.string == "}":
                    stack[-1]["depth"] = max(0, stack[-1]["depth"] - 1)
                elif stack and stack[-1]["depth"] > 0 and name == "STRING":
                    if tok.string.lstrip("rbufRBUF")[:1] == stack[-1]["quote"] and len(stack[-1]["quote"]) == 1:
                        offenders.append(f"{path.relative_to(ROOT)}:{tok.start[0]}")
        self.assertEqual(offenders, [])

    def test_no_removed_forward_attributes(self):
        """forward_date/forward_from are deprecated in PTB 21 and gone in later majors."""
        hits = []
        for path in (ROOT / "bot").rglob("*.py"):
            for i, line in enumerate(_read(path).splitlines(), 1):
                if re.search(r"\.(forward_date|forward_from|forward_from_chat|forward_sender_name)\b", line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}")
        self.assertEqual(hits, [], "use message.forward_origin")


class TestCommandSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry, cls.unresolved = registered_commands()

    def test_every_registration_is_resolvable(self):
        self.assertEqual(self.unresolved, [], "teach registered_commands() about the new dynamic family")

    def test_no_command_is_registered_twice_including_dynamic_ones(self):
        dupes = {c: p for c, p in self.registry.items() if len(p) > 1}
        self.assertEqual(dupes, {}, "PTB runs only the first handler per command; the rest are dead code")

    def test_help_and_menu_only_advertise_registered_commands(self):
        tree = ast.parse(_read(PLUGINS / "start.py"))
        phantom = defaultdict(set)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in re.finditer(r"(?<![\w/:.<])/([a-z][a-z0-9_]{1,31})\b", node.value):
                    if m.group(1) not in self.registry:
                        phantom[m.group(1)].add(node.lineno)
        self.assertEqual({k: sorted(v) for k, v in phantom.items()}, {},
                         "commands shown in /help or /menu that no handler answers")

    def test_telegram_command_menu_is_valid_and_registered(self):
        tree = ast.parse(_read(ROOT / "bot/core/bot.py"))
        entries = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "commands" and isinstance(node.value, ast.List)):
                entries = [e.elts[0].value for e in node.value.elts if isinstance(e, ast.Tuple)]
        self.assertGreater(len(entries), 20, "could not find the set_my_commands list")
        self.assertLessEqual(len(entries), 100, "Telegram accepts at most 100 menu commands")
        self.assertEqual(len(entries), len(set(entries)), "duplicate menu entries")
        self.assertEqual([c for c in entries if c not in self.registry], [])
        self.assertEqual([c for c in entries if not re.fullmatch(r"[a-z0-9_]{1,32}", c)], [])


class TestGeneratedDocs(unittest.TestCase):
    def test_command_reference_is_current(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("gen_command_reference", ROOT / "scripts/gen_command_reference.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        on_disk = (ROOT / "docs/COMMANDS.md").read_text(encoding="utf-8")
        self.assertEqual(on_disk, module.build(),
                         "docs/COMMANDS.md is stale — run: python scripts/gen_command_reference.py")
        registry, _ = registered_commands()
        self.assertIn(f"{len(registry)} commands", on_disk)


class TestHandlerWiring(unittest.TestCase):
    def test_message_handlers_never_share_a_group_unless_filters_are_disjoint(self):
        """PTB invokes only the first matching handler per group."""
        by_group = defaultdict(list)
        for path in list((ROOT / "bot").rglob("*.py")):
            if path.name == "music_client.py":
                continue  # Pyrogram client: a different dispatcher with its own groups
            for node in ast.walk(ast.parse(_read(path))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_handler" and node.args
                        and isinstance(node.args[0], ast.Call)
                        and getattr(node.args[0].func, "id", "") == "MessageHandler"):
                    continue
                group = 0
                for kw in node.keywords:
                    if kw.arg == "group":
                        group = ast.literal_eval(kw.value)
                by_group[group].append(f"{path.stem}:{ast.unparse(node.args[0].args[0])}")
        shared = {g: v for g, v in by_group.items() if len(v) > 1}
        # group 8: collector's (PHOTO|VIDEO) & CAPTION and personality's TEXT & ~COMMAND never match the same update
        # group 0: automod's NEW_CHAT_MEMBERS raid listener and whisper's PRIVATE & ~COMMAND composer are disjoint
        self.assertEqual(set(shared), {0, 8}, shared)

    def test_every_button_prefix_has_a_callback_handler(self):
        patterns = []
        for path in (ROOT / "bot").rglob("*.py"):
            if path.name == "music_client.py":
                continue
            for node in ast.walk(ast.parse(_read(path))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CallbackQueryHandler":
                    pat = next((k.value for k in node.keywords if k.arg == "pattern"), None)
                    if pat is None and len(node.args) > 1:
                        pat = node.args[1]
                    patterns.append(re.compile(ast.literal_eval(pat)) if pat is not None else re.compile(""))
        dead = []
        for path in (ROOT / "bot").rglob("*.py"):
            for node in ast.walk(ast.parse(_read(path))):
                if isinstance(node, ast.keyword) and node.arg == "callback_data":
                    v = node.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        sample = v.value
                    elif isinstance(v, ast.JoinedStr) and v.values and isinstance(v.values[0], ast.Constant):
                        sample = v.values[0].value + "x"
                    else:
                        continue
                    if not any(p.match(sample) for p in patterns):
                        dead.append(f"{path.relative_to(ROOT)}:{node.value.lineno}: {sample!r}")
        self.assertEqual(dead, [], "buttons whose callbacks nothing handles")


class TestHtmlSafety(unittest.TestCase):
    def test_member_names_and_reasons_are_escaped_in_html_messages(self):
        send_methods = {"reply_text", "send_message", "edit_message_text", "edit_text", "reply_photo",
                        "reply_video", "send_photo", "reply_animation", "reply_document",
                        "edit_message_caption", "edit_caption", "send_video", "send_animation",
                        "send_document", "answer"}
        risky = re.compile(r"\.(?:first_name|last_name|full_name)\b|^reason$")
        safe_calls = {"escape_html", "mention_html", "_esc", "escape", "quote"}
        hits = []
        for path in sorted((ROOT / "bot").rglob("*.py")):
            for node in ast.walk(ast.parse(_read(path))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in send_methods):
                    continue
                parse_mode = [k for k in node.keywords if k.arg == "parse_mode"]
                if not parse_mode or "HTML" not in ast.unparse(parse_mode[0].value):
                    continue
                texts = list(node.args) + [k.value for k in node.keywords if k.arg in ("text", "caption")]
                for text in texts:
                    for joined in [x for x in ast.walk(text) if isinstance(x, ast.JoinedStr)]:
                        for part in joined.values:
                            if not isinstance(part, ast.FormattedValue):
                                continue
                            expr = part.value
                            if isinstance(expr, ast.Call) and (
                                getattr(expr.func, "id", None) in safe_calls
                                or getattr(expr.func, "attr", None) in safe_calls | {"mention_html"}
                            ):
                                continue
                            if risky.search(ast.unparse(expr)):
                                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {{{ast.unparse(expr)}}}")
        self.assertEqual(hits, [], "wrap member-controlled values in escape_html()")


class TestConfiguration(unittest.TestCase):
    @staticmethod
    def _settings_fields() -> dict:
        tree = ast.parse(_read(ROOT / "bot/config.py"))
        fields = {}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Settings"]:
            for item in cls.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    alias = None
                    if isinstance(item.value, ast.Call):
                        for kw in item.value.keywords:
                            if kw.arg == "alias" and isinstance(kw.value, ast.Constant):
                                alias = kw.value.value
                    fields[item.target.id] = alias or item.target.id.upper()
        return fields

    def test_every_setting_is_documented_in_env_example(self):
        documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", _read(ROOT / ".env.example"), re.M))
        missing = sorted(v for v in self._settings_fields().values() if v not in documented)
        self.assertEqual(missing, [])

    def test_env_example_has_no_unknown_settings(self):
        known = set(self._settings_fields().values())
        documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", _read(ROOT / ".env.example"), re.M))
        self.assertEqual(sorted(documented - known), [])

    def test_settings_attributes_used_in_code_exist(self):
        known = set(self._settings_fields()) | {
            "live_music_configured", "is_admin_id", "model_dump", "model_fields", "model_config", "model_copy"}
        unknown = defaultdict(set)
        for path in (ROOT / "bot").rglob("*.py"):
            for m in re.finditer(r"\bsettings\.([a-z_][a-z0-9_]*)", _read(path)):
                if m.group(1) not in known:
                    unknown[m.group(1)].add(path.name)
        self.assertEqual({k: sorted(v) for k, v in unknown.items()}, {})

    def test_concurrency_default_is_enabled_and_documented(self):
        self.assertIn("concurrent_updates", self._settings_fields())
        self.assertRegex(_read(ROOT / ".env.example"), r"(?m)^CONCURRENT_UPDATES=\d+")


class TestDeployment(unittest.TestCase):
    def test_dashboard_image_contains_everything_it_imports(self):
        """The dashboard Dockerfile copies an explicit module list; a missing entry means the
        container crashes at import time (bot.utils.retry once went missing this way)."""
        def resolve(mod: str):
            for candidate in (ROOT / (mod.replace(".", "/") + ".py"), ROOT / mod.replace(".", "/") / "__init__.py"):
                if candidate.exists():
                    return candidate
            return None

        needed, third_party, stack = set(), set(), ["dashboard.main"]
        stdlib = set(sys.stdlib_module_names)
        while stack:
            mod = stack.pop()
            path = resolve(mod)
            if path is None or str(path) in needed:
                continue
            needed.add(str(path))
            for node in ast.walk(ast.parse(_read(path))):
                found = []
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.append(node.module)
                    found.extend(f"{node.module}.{a.name}" for a in node.names)
                elif isinstance(node, ast.Import):
                    found.extend(a.name for a in node.names)
                for name in found:
                    top = name.split(".")[0]
                    if top in ("bot", "dashboard"):
                        stack.append(name)
                    elif top not in stdlib:
                        third_party.add(top)
        dockerfile = _read(ROOT / "dashboard/Dockerfile")
        copied = set(re.findall(r"^COPY\s+(\S+)\s+\S+", dockerfile, re.M))
        missing = sorted(Path(p).relative_to(ROOT).as_posix() for p in needed
                         if Path(p).relative_to(ROOT).as_posix() not in copied)
        self.assertEqual(missing, [], "add these to dashboard/Dockerfile")

        wanted = {"fastapi": "fastapi", "sqlalchemy": "sqlalchemy", "pydantic": "pydantic",
                  "pydantic_settings": "pydantic-settings", "structlog": "structlog", "tenacity": "tenacity"}
        reqs = {re.split(r"[<>=!~\[;\s]", line.strip())[0].lower()
                for line in _read(ROOT / "dashboard/requirements.txt").splitlines()
                if line.strip() and not line.startswith("#")}
        lacking = sorted(pkg for mod, pkg in wanted.items() if mod in third_party and pkg not in reqs)
        self.assertEqual(lacking, [], "dashboard/requirements.txt lacks packages its code imports")
        unmapped = sorted(third_party - set(wanted))
        self.assertEqual(unmapped, [], "extend `wanted` so new third-party imports are checked too")

    def test_dockerignore_keeps_secrets_out_of_images(self):
        lines = {l.strip() for l in _read(ROOT / ".dockerignore").splitlines()}
        for entry in (".env", "sessions/", "*.session", "*.db", "*.pkl", ".git/"):
            self.assertIn(entry, lines, f".dockerignore must exclude {entry}")

    def test_gitignore_keeps_secrets_out_of_git(self):
        lines = {l.strip() for l in _read(ROOT / ".gitignore").splitlines()}
        for entry in (".env", "sessions/", "*.db", "*.pkl"):
            self.assertIn(entry, lines)

    def test_compose_publishes_dashboard_on_localhost_only(self):
        text = _read(ROOT / "docker-compose.yml")
        self.assertIn('"127.0.0.1:8080:8080"', text)
        self.assertNotRegex(text, r'(?m)^\s*-\s*"8080:8080"')

    def test_every_requirement_is_imported_or_a_documented_implicit_need(self):
        """The inverse of the check below: no dead weight in the image."""
        implicit = {
            "tgcrypto": "Pyrogram's optional C speedup for MTProto crypto, picked up automatically",
            "aiosqlite": "SQLAlchemy async driver chosen by the sqlite+aiosqlite:// default DATABASE_URL",
            "asyncpg": "SQLAlchemy async driver chosen by a postgresql+asyncpg:// DATABASE_URL",
            "python-dotenv": "pydantic-settings needs it to read the .env file",
            "tzdata": "the IANA time-zone database zoneinfo reads for ACTIVITY_TIMEZONE on images/OSes without /usr/share/zoneinfo",
        }
        import_names = {"python-telegram-bot": "telegram", "pillow": "PIL", "yt-dlp": "yt_dlp", "edge-tts": "edge_tts",
                        "py-tgcalls": "pytgcalls", "google-generativeai": "google", "beautifulsoup4": "bs4",
                        "pydantic-settings": "pydantic_settings", "sqlalchemy": "sqlalchemy"}
        imported = set()
        for directory in SOURCE_DIRS:
            for path in (ROOT / directory).rglob("*.py"):
                for node in ast.walk(ast.parse(_read(path))):
                    if isinstance(node, ast.Import):
                        imported |= {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        imported.add(node.module.split(".")[0])
        unused = []
        for line in _read(ROOT / "requirements.txt").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[<>=!~\[;\s]", line)[0].lower()
            if name in implicit:
                continue
            if import_names.get(name, name.replace("-", "_")) not in imported:
                unused.append(name)
        self.assertEqual(unused, [], "declared in requirements.txt but never imported — remove, or document it in `implicit`")

    def test_requirements_cover_third_party_imports(self):
        aliases = {"telegram": "python-telegram-bot", "PIL": "pillow", "dotenv": "python-dotenv",
                   "bs4": "beautifulsoup4", "yt_dlp": "yt-dlp", "edge_tts": "edge-tts",
                   "pydantic_settings": "pydantic-settings", "pytgcalls": "py-tgcalls",
                   "google": "google-generativeai", "aiohttp_socks": "aiohttp-socks"}
        reqs = {re.split(r"[<>=!~\[;\s]", line.strip())[0].lower().replace("_", "-")
                for name in ("requirements.txt", "requirements-dev.txt")
                for line in _read(ROOT / name).splitlines()
                if line.strip() and not line.startswith(("#", "-"))}
        stdlib = set(sys.stdlib_module_names)
        missing = set()
        for path in (ROOT / "bot").rglob("*.py"):
            for node in ast.walk(ast.parse(_read(path))):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    if mod in stdlib or mod in ("bot", "dashboard"):
                        continue
                    pkg = aliases.get(mod, mod).lower().replace("_", "-")
                    if pkg not in reqs:
                        missing.add(mod)
        self.assertEqual(sorted(missing), [])


if __name__ == "__main__":
    unittest.main()
