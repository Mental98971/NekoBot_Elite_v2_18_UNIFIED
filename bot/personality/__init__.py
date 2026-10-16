"""The Personality Layer package.

Exports the rewrite engine, memory manager, joke database, trigger engine,
and sub-plugin system.
"""
from .personality import personality, PersonalityLayer, PersonalityConfig
from .memory import memory, MemoryManager
from .jokes import jokes_db, JokeDatabase, Joke
from .triggers import triggers, TriggerEngine

__all__ = [
    "personality", "PersonalityLayer", "PersonalityConfig",
    "memory", "MemoryManager",
    "jokes_db", "JokeDatabase", "Joke",
    "triggers", "TriggerEngine",
]
