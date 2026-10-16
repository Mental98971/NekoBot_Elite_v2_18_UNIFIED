#!/usr/bin/env python3
"""Generate docs/COMMANDS.md from the plugin sources (stdlib only; no bot imports).

    python scripts/gen_command_reference.py            # rewrite docs/COMMANDS.md
    python scripts/gen_command_reference.py --check    # exit 1 if the file is stale

The reference is derived from the same CommandHandler registrations the bot
executes at startup, so it cannot drift from the code. tests/test_static_contracts.py
runs the --check logic, so a new or renamed command fails CI until the file is
regenerated. Descriptions come from each handler's docstring (first line).
"""
from __future__ import annotations

import ast
import re
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "bot" / "plugins"
OUT = ROOT / "docs" / "COMMANDS.md"


def _assignments(path: Path) -> dict:
    env = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            env[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            env[node.target.id] = node.value
    return env


def _keys(node) -> list:
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant)]
    return [e.value for e in node.elts if isinstance(e, ast.Constant)]


def _clean(text: str) -> str:
    """Make a description safe inside a Markdown table cell (no raw tags, no pipes)."""
    text = re.sub(r"</?(?:b|i|code|pre|u|s)>", "", text.strip())
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def _first_line(fn) -> str:
    doc = ast.get_docstring(fn) if fn is not None else None
    if not doc:
        return ""
    return _clean(doc.strip().splitlines()[0])


_HELP_LINE = re.compile(r"^\s*/([a-z0-9_]+)\b[^\n—]*?\s+—\s+(.+?)\s*$")


def _help_descriptions() -> dict:
    """`/cmd … — description` lines from the bot's own /help text (start.py), used when a
    handler has no docstring. First occurrence wins."""
    found = {}
    tree = ast.parse((PLUGINS / "start.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for line in node.value.splitlines():
                m = _HELP_LINE.match(re.sub(r"</?(?:b|i|code)>", "", line))
                if m:
                    found.setdefault(m.group(1), m.group(2).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    return found


def collect() -> "OrderedDict[str, list[tuple[str, str]]]":
    """{plugin_stem: [(command, description), ...]} in registration order."""
    font_keys = _keys(_assignments(ROOT / "bot/fonts/catalog.py")["FONT_DEFS"])
    effect_keys = _keys(_assignments(ROOT / "bot/fonts/catalog.py")["EFFECTS"])
    rx = _assignments(PLUGINS / "reactions.py")
    actions = sorted(set(_keys(rx["ACTION_VERBS"])) | set(_keys(rx["SOLO_ACTIONS"])))
    families = {
        "fonts": {
            "fk": (font_keys, "Text style `{c}` (see `/fonts`)"),
            "SHORTCUT_OVERRIDES": (["prohibit" if k == "stop" else k for k in effect_keys], "Combining-diacritic text effect"),
        },
        "reactions": {"ACTIONS": (actions, "Reaction GIF (`/reactions on|off` toggles them per chat)")},
    }

    help_desc = _help_descriptions()
    result = OrderedDict()
    for path in sorted(PLUGINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        rows = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CommandHandler" and len(node.args) >= 2):
                continue
            arg, cb = node.args[0], node.args[1]
            handler = funcs.get(cb.id) if isinstance(cb, ast.Name) else None
            desc = _first_line(handler)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                rows.append((arg.value, desc))
            elif isinstance(arg, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in arg.elts):
                rows.extend((e.value, desc) for e in arg.elts)
            else:
                src = ast.unparse(arg)
                for key, (names, template) in families.get(path.stem, {}).items():
                    if key in src:
                        rows.extend((n, template.format(c=n)) for n in names)
                        break
                else:
                    raise SystemExit(f"{path.name}:{node.lineno}: unresolvable command registration {src!r}")
        if rows:
            seen, unique = set(), []
            for cmd, desc in sorted(rows, key=lambda r: r[0]):
                if cmd not in seen:
                    seen.add(cmd)
                    unique.append((cmd, desc or _clean(help_desc.get(cmd, ""))))
            result[path.stem] = unique
    return result


def build() -> str:
    data = collect()
    total = sum(len(v) for v in data.values())
    described = sum(1 for rows in data.values() for _, d in rows if d)
    out = [
        "# Command reference",
        "",
        f"_Generated by `scripts/gen_command_reference.py` — {total} commands across {len(data)} plugins ({described} with a description)._",
        "_Do not edit by hand; CI fails when this file is stale._",
        "",
        "Descriptions come from each handler's docstring, falling back to the bot's own `/help` text; a dash means neither has one yet.",
        "Admin-only, group-only and owner-only gating is enforced by the decorators in `bot/core/decorators.py`.",
        "",
        "| Plugin | Commands |",
        "|---|---|",
    ]
    for plugin, rows in data.items():
        out.append(f"| [`{plugin}`](#{plugin}) | {len(rows)} |")
    for plugin, rows in data.items():
        out += ["", f"## {plugin}", "", "| Command | Description |", "|---|---|"]
        out += [f"| `/{cmd}` | {desc or '—'} |" for cmd, desc in rows]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    text = build()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("docs/COMMANDS.md is stale — run: python scripts/gen_command_reference.py", file=sys.stderr)
            return 1
        print("docs/COMMANDS.md is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
