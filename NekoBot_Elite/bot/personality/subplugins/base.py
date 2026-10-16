"""Base plugin architecture.

If you want to add more chaos, inherit from Plugin.

Improvements over the original:
  - ``PluginResult.metadata`` uses ``field(default_factory=dict)`` instead
    of the ``None``-then-``__post_init__`` pattern. Cleaner, no boilerplate.
  - ``Plugin.triggers`` is a tuple (immutable) instead of a list, preventing
    the shared-mutable-default footgun.
  - ``PluginManager.route`` replaces the broken two-pass threshold system
    with a single-pass weighted scorer. The original's second pass was dead
    code — all plugins' low-confidence returns failed the ``> 0.7`` gate.
    Now every plugin is called once, scored with an intent-match boost, and
    the highest-scoring result wins.
  - ``contains_word``/``load_skin_json`` are shared here so every subplugin
    uses the same word-boundary matching and skin-loading fallback instead
    of four slightly-different copies of the same logic.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.config import settings


def contains_word(text_lower: str, keywords: Tuple[str, ...] | List[str]) -> bool:
    """True if any keyword appears as a whole word/phrase in text_lower.

    Plain ``kw in text_lower`` substring checks false-positive constantly —
    "gg" matches inside "struggling", "play" matches inside "display", "lag"
    matches inside "flag". Word-boundary matching only narrows what already
    matched, so it can't break a genuine single-word trigger like "gg" typed
    on its own; it only stops it from firing inside unrelated words.
    """
    return any(re.search(rf"\b{re.escape(kw)}\b", text_lower) for kw in keywords)


@lru_cache(maxsize=8)
def _read_skin_file(path_str: str) -> Dict[str, Any]:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def load_skin_json(skin_name: str = "neko") -> Dict[str, Any]:
    """Load a skin JSON file by name, falling back to 'neko' if the
    requested skin doesn't exist on disk. Cached — this is only used for
    each subplugin's static startup-time defaults, not per-message, so a
    tiny process-lifetime cache is safe (skins are edited by admins via
    files, not through a live-reloading UI here; PersonalityLayer's own
    SkinLoader is the one that serves live per-message lookups and has
    its own reload() for that).
    """
    skins_dir = settings.data_dir / "personality" / "skins"
    path = skins_dir / f"{skin_name}.json"
    if not path.exists():
        path = skins_dir / "neko.json"
    if not path.exists():
        return {}
    try:
        return _read_skin_file(str(path))
    except (json.JSONDecodeError, OSError):
        return {}


@dataclass
class PluginResult:
    """What a plugin returns after processing."""
    response: Optional[str] = None
    confidence: float = 0.0
    intent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class Plugin(ABC):
    """Base class for all personality sub-plugins."""

    name: str = "base"
    triggers: Tuple[str, ...] = ()   # immutable — subclasses override
    priority: int = 50              # higher = checked first in tie-breaking

    @abstractmethod
    async def handle(
        self,
        message: str,
        chat_id: int,
        context: List[Dict],
        **kwargs,
    ) -> Optional[PluginResult]:
        """Process a message. Return None if this plugin doesn't handle it."""
        pass

    async def on_load(self) -> None:
        """Called when plugin is loaded. Override if needed."""
        pass


class PluginManager:
    """Routes messages to the right plugin. Or the wrong one. It's chaotic.

    Uses a single-pass weighted scorer: every registered plugin is called
    once, its result is scored (confidence + intent-match boost), and the
    highest-scoring result wins. This replaces the original two-pass system
    whose second pass was dead code.
    """

    def __init__(self) -> None:
        self._plugins: List[Plugin] = []

    def register(self, plugin: Plugin) -> None:
        self._plugins.append(plugin)
        self._plugins.sort(key=lambda p: p.priority, reverse=True)

    async def route(
        self,
        message: str,
        chat_id: int,
        context: List[Dict],
        intents: List[str],
        **kwargs,
    ) -> Optional[PluginResult]:
        """Find the best plugin for this message via weighted scoring."""
        candidates: List[Tuple[float, PluginResult]] = []
        intents_set = set(intents)

        for plugin in self._plugins:
            result = await plugin.handle(message, chat_id, context, intents=intents, **kwargs)
            if result is None:
                continue

            # Boost score when the plugin's triggers overlap with detected intents
            intent_match_count = len(set(plugin.triggers) & intents_set)
            score = result.confidence + (0.15 * intent_match_count)

            candidates.append((score, result))

        if not candidates:
            return None

        # Sort by score descending; on tie, higher-priority plugin wins
        # (already pre-sorted by priority in register()).
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]

        # Only return if the best result is meaningfully confident
        if best.confidence <= 0.0:
            return None
        return best
