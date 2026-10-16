"""Few-shot example bank for Neko multi-personality system.

Loads the curated dataset (original conversations + personality_pack
samples) and provides retrieval by category, personality skin, or
keyword overlap. Used for:

- Optional few-shot grounding when an LLM path is active
- Tone / style regression tests
- Inspiration for template responses
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Resolve against settings.data_dir so this works regardless of process cwd.
_DEFAULT_PATH = settings.data_dir / "personality" / "dataset" / "fewshot_bank.json"


class ExampleBank:
    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._examples: List[Dict[str, Any]] = []
        self._by_category: Dict[str, List[Dict[str, Any]]] = {}
        self._by_personality: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded = False

    def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        self._examples = []
        self._by_category = {}
        self._by_personality = {}
        if not self._path.exists():
            logger.warning("Few-shot bank not found at %s — continuing without examples", self._path)
            self._loaded = True
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._examples = data.get("examples") or []
            for e in self._examples:
                cat = e.get("category") or "General"
                pid = e.get("personality") or "neko"
                self._by_category.setdefault(cat, []).append(e)
                self._by_personality.setdefault(pid, []).append(e)
            self._loaded = True
            logger.info(
                "Loaded %d few-shot examples across %d categories / %d personalities",
                len(self._examples),
                len(self._by_category),
                len(self._by_personality),
            )
        except Exception as exc:
            logger.error("Failed to load few-shot bank: %s", exc)
            self._loaded = True

    @property
    def count(self) -> int:
        self.load()
        return len(self._examples)

    def categories(self) -> List[str]:
        self.load()
        return sorted(self._by_category.keys())

    def personalities(self) -> List[str]:
        self.load()
        return sorted(self._by_personality.keys())

    def get_by_category(self, category: str, limit: int = 5) -> List[Dict[str, Any]]:
        self.load()
        pool = list(self._by_category.get(category, []))
        random.shuffle(pool)
        return pool[: max(0, limit)]

    def get_by_personality(self, personality: str, limit: int = 5) -> List[Dict[str, Any]]:
        self.load()
        # nanora is an alias for neko
        key = "neko" if personality in ("nanora", "neko") else personality
        pool = list(self._by_personality.get(key, []))
        if not pool and key != "neko":
            pool = list(self._by_personality.get("neko", []))
        random.shuffle(pool)
        return pool[: max(0, limit)]

    def get_relevant(
        self,
        query: str,
        limit: int = 4,
        personality: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Keyword-overlap retrieval, optionally biased to a skin/personality."""
        self.load()
        pool = self._examples
        if personality:
            key = "neko" if personality in ("nanora", "neko") else personality
            biased = self._by_personality.get(key) or []
            if biased:
                pool = biased

        if not query or not pool:
            return self.sample(limit) if not personality else self.get_by_personality(personality or "neko", limit)

        q_tokens = {t.lower() for t in query.split() if len(t) > 2}
        if not q_tokens:
            return random.sample(pool, min(limit, len(pool))) if pool else []

        scored: List[tuple[float, Dict[str, Any]]] = []
        for e in pool:
            text = f"{e.get('question', '')} {e.get('response', '')}".lower()
            overlap = sum(1 for t in q_tokens if t in text)
            if overlap:
                scored.append((float(overlap), e))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return random.sample(pool, min(limit, len(pool))) if pool else []
        return [e for _, e in scored[:limit]]

    def sample(self, n: int = 3) -> List[Dict[str, Any]]:
        self.load()
        if not self._examples:
            return []
        return random.sample(self._examples, min(n, len(self._examples)))


    def search_full_corpus(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Optional deeper search over full_cleaned.jsonl (capped reads)."""
        path = settings.data_dir / "personality" / "dataset" / "full_cleaned.jsonl"
        if not path.exists() or not query:
            return []
        q_tokens = {t.lower() for t in query.split() if len(t) > 2}
        if not q_tokens:
            return []
        scored = []
        try:
            with path.open(encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i > 8000:  # hard cap scan
                        break
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    text = f"{e.get('question','')} {e.get('response','')}".lower()
                    overlap = sum(1 for t in q_tokens if t in text)
                    if overlap >= 2:
                        scored.append((overlap, e))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:limit]]
        except Exception as exc:
            logger.debug("full corpus search failed: %s", exc)
            return []

    def format_for_prompt(
        self,
        examples: List[Dict[str, Any]],
        max_chars: int = 1200,
        speaker: str = "Neko",
    ) -> str:
        """Render examples as a compact few-shot block for LLM prompts."""
        if not examples:
            return ""
        parts: List[str] = []
        total = 0
        for e in examples:
            # Prefer skin-specific label when present
            label = speaker
            pid = e.get("personality")
            if pid and pid not in ("neko", "nanora"):
                label = pid.replace("_", " ").title()
            block = f"User: {e.get('question', '').strip()}\n{label}: {e.get('response', '').strip()}"
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block) + 2
        if not parts:
            return ""
        return f"Examples of {speaker}'s voice:\n\n" + "\n\n".join(parts)


# Singleton for convenience
example_bank = ExampleBank()
