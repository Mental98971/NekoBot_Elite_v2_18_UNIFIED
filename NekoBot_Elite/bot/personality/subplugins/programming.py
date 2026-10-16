"""Programming plugin — because most users are broken developers.

Improvements:
  - Uses the skin's pattern data so different skins can have different
    programming responses.
  - ``triggers`` is now a tuple (immutable).
  - Loads its skin defaults through the shared ``load_skin_json`` helper
    instead of duplicating the path-fallback logic, and matches generic
    keywords on word boundaries so e.g. "dev" doesn't fire on unrelated
    text.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import Plugin, PluginResult, contains_word, load_skin_json


class ProgrammingPlugin(Plugin):
    """Handles code questions, debugging despair, and Git disasters."""

    name = "programming"
    triggers = (
        "coding", "python", "javascript", "git", "docker",
        "database", "frontend", "linux",
    )
    priority = 80

    def __init__(self) -> None:
        self._defaults = load_skin_json("neko").get("subplugin_responses", {}).get("programming", {})

    async def handle(
        self,
        message: str,
        chat_id: int,
        context: List[Dict],
        **kwargs,
    ) -> Optional[PluginResult]:
        message_lower = message.lower()

        # v2.3: Use skin_data from kwargs if available
        skin_data = kwargs.get("skin_data", {})
        prog_data = skin_data.get("subplugin_responses", {}).get("programming", self._defaults)
        code_patterns = prog_data.get("patterns", {})
        generic_keywords = prog_data.get("generic_keywords", ["code", "program", "dev"])
        generic_response = prog_data.get(
            "generic", "Code is just organized suffering with syntax highlighting."
        )

        # Check for specific code patterns
        for pattern, response in code_patterns.items():
            if re.search(pattern, message_lower):
                return PluginResult(
                    response=response,
                    confidence=0.9,
                    intent="coding",
                    metadata={"pattern": pattern},
                )

        # Generic programming response
        if contains_word(message_lower, generic_keywords):
            return PluginResult(
                response=generic_response,
                confidence=0.6,
                intent="coding",
            )

        return None
