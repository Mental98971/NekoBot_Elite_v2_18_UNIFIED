"""Drop-in API of the v2.11 PERSONA_ENGINE overlay, re-implemented on ``bot.persona_engine`` (v2.14).

Import path and entry points are the ones that overlay documented::

    from bot.personality.persona_engine.integration import get_engine, prepare_persona_reply

Behind them is the main engine, so every safety property of the project applies (fixed disclosure for identity questions,
contact-data / blocked-word / "claims to be the real person" gates, repetition guard, provenance-labelled memory).
See integration.py for the (small) differences from the overlay's behaviour.
"""
from .integration import LegacyEngine, get_engine, is_imitation_allowed, list_available_persons, prepare_persona_reply  # noqa: F401

__all__ = ["LegacyEngine", "get_engine", "is_imitation_allowed", "list_available_persons", "prepare_persona_reply"]
