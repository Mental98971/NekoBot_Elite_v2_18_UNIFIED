"""Anime plugin — for the weebs. And the weebs in denial.

Improvements:
  - Loads response data from the skin JSON via the shared loader.
  - ``triggers`` is now a tuple.
  - Word-boundary keyword matching (a bare "naruto" substring check, for
    instance, is harmless, but the shared helper keeps every keyword list
    here consistent with the others instead of mixing matching styles).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import Plugin, PluginResult, contains_word, load_skin_json


class AnimePlugin(Plugin):
    """Recognizes anime references and responds appropriately."""

    name = "anime"
    triggers = ("anime", "anime_greeting")
    priority = 70

    def __init__(self) -> None:
        self._defaults = load_skin_json("neko").get("subplugin_responses", {}).get("anime", {})

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
        d = skin_data.get("subplugin_responses", {}).get("anime", self._defaults)
        naruto_response = d.get("naruto", "Believe it? No. I believe in caffeine and consistent indentation.")
        senpai_keywords = d.get("senpai", ["senpai", "notice me"])
        senpai_response = d.get("senpai_response", "I noticed you. I'm not happy about it, but I noticed.")
        power_keywords = d.get("power_level", ["power level", "over 9000"])
        power_response = d.get("power_level_response", "My power level is over 9000... lines of technical debt.")
        filler_keywords = d.get("filler", ["filler", "arc"])
        filler_response = d.get("filler_response", "My life is 90% filler arc and 10% existential crisis. No beach episodes.")
        generic_keywords = d.get("generic_keywords", ["anime", "manga", "weeb", "otaku", "waifu"])
        generic_response = d.get("generic_response", "Anime? I prefer my fiction to have better plot armor than my code.")

        if contains_word(message_lower, ("naruto",)):
            return PluginResult(response=naruto_response, confidence=0.9, intent="anime")

        if contains_word(message_lower, senpai_keywords):
            return PluginResult(response=senpai_response, confidence=0.9, intent="anime")

        if contains_word(message_lower, power_keywords):
            return PluginResult(response=power_response, confidence=0.9, intent="anime")

        if contains_word(message_lower, filler_keywords):
            return PluginResult(response=filler_response, confidence=0.85, intent="anime")

        if contains_word(message_lower, generic_keywords):
            return PluginResult(response=generic_response, confidence=0.7, intent="anime")

        return None
