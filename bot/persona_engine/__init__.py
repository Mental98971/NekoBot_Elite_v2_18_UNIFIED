"""Persona engine: behaviour-profile + situation-aware retrieval + LLM generation + scoring (v2.11).

Standard library only at import time; the model provider is pluggable and optional (no provider = the bot keeps
answering from the v2.9 retrieval persona).  See docs/PERSONA_ENGINE.md.
"""
from .core import BOT_MEMORY, CURRENT_CONTEXT, GENERATED_CONTENT, HISTORICAL_EVIDENCE, Situation, analyze  # noqa: F401
from .engine import EngineConfig, EngineRequest, EngineResult, PersonaEngine  # noqa: F401
from .profile import BehaviorProfile, ResponsePlan  # noqa: F401
from .runtime import EngineService, get_service, reset_service  # noqa: F401
