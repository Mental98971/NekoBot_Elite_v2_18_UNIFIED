"""Gaming plugin — for the gamers. And the people who say 'skill issue.'

Improvements:
  - Loads response data from the skin JSON via the shared loader.
  - ``triggers`` is now a tuple.
  - Word-boundary keyword matching: the old substring checks meant "gg"
    matched inside "struggling", "lag" matched inside "flag", and "play"
    matched inside "display" — firing the gaming plugin on completely
    unrelated messages.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import Plugin, PluginResult, contains_word, load_skin_json


class GamingPlugin(Plugin):
    """Handles gaming references with the appropriate level of disrespect."""

    name = "gaming"
    triggers = ("gaming", "skill_issue")
    priority = 60

    def __init__(self) -> None:
        self._defaults = load_skin_json("neko").get("subplugin_responses", {}).get("gaming", {})

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
        d = skin_data.get("subplugin_responses", {}).get("gaming", self._defaults)
        skill_response = d.get("skill_issue", "Skill issue? No. It's a system design issue. I'm the system.")
        touch_grass_response = d.get("touch_grass", "I touch grass through a texture atlas. It's more efficient.")
        gg_keywords = d.get("gg", ["gg", "good game"])
        gg_response = d.get("gg_response", "GG? More like 'Git Gud.' But I'm not your coach.")
        lag_keywords = d.get("lag", ["lag", "ping", "latency"])
        lag_response = d.get("lag_response", "It's not lag. It's just your life buffering at 144p.")
        generic_keywords = d.get("generic_keywords", ["game", "gaming", "gamer", "play", "steam"])
        generic_response = d.get("generic_response", "Gaming is just escapism with better graphics than my terminal.")

        if contains_word(message_lower, ("skill issue",)):
            return PluginResult(response=skill_response, confidence=0.95, intent="gaming")

        if contains_word(message_lower, ("touch grass",)):
            return PluginResult(response=touch_grass_response, confidence=0.9, intent="gaming")

        if contains_word(message_lower, gg_keywords):
            return PluginResult(response=gg_response, confidence=0.85, intent="gaming")

        if contains_word(message_lower, lag_keywords):
            return PluginResult(response=lag_response, confidence=0.85, intent="gaming")

        if contains_word(message_lower, generic_keywords):
            return PluginResult(response=generic_response, confidence=0.6, intent="gaming")

        return None
