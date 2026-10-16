"""Loads ``persona_text`` without importing the heavy ``bot.personality`` package when its dependencies are absent."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    try:
        from bot.personality import persona_text as mod  # normal runtime
        return mod
    except Exception:  # dependency stack missing (offline tools, tests): load the stdlib-only file by path
        name = "persona_text"
        if name in sys.modules:
            return sys.modules[name]
        path = Path(__file__).resolve().parent.parent / "personality" / "persona_text.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


persona_text = _load()
