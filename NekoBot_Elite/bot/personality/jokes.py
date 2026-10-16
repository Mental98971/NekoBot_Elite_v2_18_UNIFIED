"""Joke database with categories, cooldowns, and callback tracking.

Jokes are loaded from an external JSON file (data/jokes.json) so they can
be added, edited, or reloaded at runtime without touching source code.
Supports runtime addition via add() for admin-submitted jokes.

Improvements over the original:
  - Externalized to JSON instead of hardcoded Python literals.
  - exclude_ids uses a set for O(1) lookup instead of list scanning.
  - add() is public so /addjoke can inject live without restart.
  - reload() picks up file changes without restarting the process.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from bot.config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Joke:
    id: str
    category: str
    text: str
    requires_context: Optional[str] = None
    callback_to: Optional[str] = None


class JokeDatabase:
    """The humor engine. Categorized, weighted, and slightly unhinged."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else settings.data_dir / "jokes.json"
        self._jokes: List[Joke] = []
        self._by_category: Dict[str, List[Joke]] = {}
        self._id_set: set[str] = set()
        self._load()

    def _load(self) -> None:
        """Load jokes from the JSON file."""
        if not self._path.exists():
            logger.warning("Jokes file not found: %s — starting empty", self._path)
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for entry in data:
            self._add(Joke(
                id=entry["id"],
                category=entry["category"],
                text=entry["text"],
                requires_context=entry.get("requires_context"),
                callback_to=entry.get("callback_to"),
            ))
        logger.info("Loaded %d jokes from %s", len(self._jokes), self._path)

    def reload(self) -> None:
        """Clear and reload from file. Safe to call at runtime."""
        self._jokes.clear()
        self._by_category.clear()
        self._id_set.clear()
        self._load()

    def _add(self, joke: Joke) -> None:
        """Internal add — used during load and by public add()."""
        if joke.id in self._id_set:
            logger.warning("Duplicate joke id skipped: %s", joke.id)
            return
        self._jokes.append(joke)
        self._by_category.setdefault(joke.category, []).append(joke)
        self._id_set.add(joke.id)

    def add(self, joke: Joke) -> bool:
        """Add a joke at runtime (e.g. via /addjoke). Returns True if added."""
        before = len(self._jokes)
        self._add(joke)
        added = len(self._jokes) > before
        if added:
            logger.info("Runtime joke added: %s (%s)", joke.id, joke.category)
        return added

    def get_random(
        self,
        category: Optional[str] = None,
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[Joke]:
        """Get a random joke, optionally filtered by category and exclusion list."""
        pool = self._by_category.get(category, self._jokes) if category else self._jokes
        if exclude_ids:
            # Use a set for O(1) lookup instead of O(n) list scanning.
            exclude_set = set(exclude_ids)
            pool = [j for j in pool if j.id not in exclude_set]
        return random.choice(pool) if pool else None

    def get_by_context(
        self,
        context_hint: str,
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[Joke]:
        """Find a joke matching a context hint."""
        pool = [j for j in self._jokes if j.requires_context == context_hint]
        if exclude_ids:
            exclude_set = set(exclude_ids)
            pool = [j for j in pool if j.id not in exclude_set]
        return random.choice(pool) if pool else None

    def get_categories(self) -> List[str]:
        return list(self._by_category.keys())

    def count(self) -> int:
        return len(self._jokes)

    def has_id(self, joke_id: str) -> bool:
        return joke_id in self._id_set


jokes_db = JokeDatabase()
