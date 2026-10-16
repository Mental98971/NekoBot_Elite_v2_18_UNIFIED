"""General plugin — the catch-all for when nothing else fits.

Like a default case in a switch statement. Boring but necessary.

Improvements over the original:
  - Confidence raised from 0.5 to 0.55, and PluginManager.route rewritten
    to a single-pass weighted scorer (see base.py) — under the original
    two-pass ``> 0.5`` gate this plugin's 0.5 return was dead code for
    every intent-matched message.
  - Removed the unused ``message_lower`` variable.
  - Uses tuple for triggers (matching the fixed base class).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import Plugin, PluginResult


class GeneralPlugin(Plugin):
    """Fallback plugin. Handles everything the cool plugins ignore."""

    name = "general"
    triggers = (
        "greeting", "goodbye", "thanks", "who_are_you", "help",
        "insult", "compliment", "sad", "happy", "angry", "advice", "how_to",
    )
    priority = 10  # Low priority — only wins if others don't catch it

    async def handle(
        self,
        message: str,
        chat_id: int,
        context: List[Dict],
        **kwargs,
    ) -> Optional[PluginResult]:
        intents = kwargs.get("intents", [])

        # Signal to the personality layer that generate_direct should handle it
        if intents:
            return PluginResult(
                response=None,
                confidence=0.55,
                intent=intents[0],
            )

        # Absolute fallback
        return PluginResult(
            response="I'm not sure what you're asking, and honestly? I'm not sure I care.",
            confidence=0.3,
            intent="general",
        )
