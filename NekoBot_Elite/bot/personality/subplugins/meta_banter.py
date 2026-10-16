"""Meta-banter subplugin — handles conversational dynamics.

This plugin detects meta-conversational patterns observed in real chat
logs: tag/mention debates, emotion challenges, introvert reveals,
off-topic call-outs, and elaborate requests. These are higher-level
social dynamics that the topical plugins (programming, anime, gaming)
don't cover.

Priority is high (85) so these social patterns win over topical content
when they're the dominant intent.
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

from .base import Plugin, PluginResult, load_skin_json


class MetaBanterPlugin(Plugin):
    """Handles conversational meta-dynamics: tags, emotions, introverts, etc.

    v2.3: Accepts skin_data via kwargs at handle() time so per-chat skins
    are respected instead of always using nanora.
    """

    name = "meta_banter"
    triggers = (
        "tag_meta",
        "emotion_challenge",
        "introvert_reveal",
        "sticker_activity",
        "off_topic",
        "casual_agree",
        "elaborate",
        "short_message",
    )
    priority = 85

    def __init__(self) -> None:
        self._default_responses = load_skin_json("neko").get("direct_responses", {})

    async def handle(
        self,
        message: str,
        chat_id: int,
        context: List[Dict],
        **kwargs,
    ) -> Optional[PluginResult]:
        # v2.3: Use skin_data from kwargs if available
        responses = kwargs.get("skin_data", {}).get("direct_responses", self._default_responses)

        intents = kwargs.get("intents", [])
        if not intents:
            return None

        primary = intents[0]

        response_pools = {
            "tag_meta": responses.get("tag_meta", []),
            "emotion_challenge": responses.get("emotion_challenge", []),
            "introvert_reveal": responses.get("introvert_reveal", []),
            "sticker_activity": responses.get("sticker_activity", []),
            "off_topic": responses.get("off_topic", []),
            "casual_agree": responses.get("casual_agree", []),
            "elaborate": responses.get("elaborate", []),
            "short_message": responses.get("short_message", []),
        }

        pool = response_pools.get(primary)
        if not pool:
            return None

        confidence_map = {
            "tag_meta": 0.9,
            "emotion_challenge": 0.9,
            "introvert_reveal": 0.85,
            "sticker_activity": 0.85,
            "off_topic": 0.9,
            "casual_agree": 0.8,
            "elaborate": 0.85,
            "short_message": 0.6,
        }

        response = random.choice(pool)
        return PluginResult(
            response=response,
            confidence=confidence_map.get(primary, 0.7),
            intent=primary,
        )
