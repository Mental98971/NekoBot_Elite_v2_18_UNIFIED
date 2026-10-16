"""Custom instructions processor — the persona override system (v2.3).

v2.3 changes:
  - Trait presets are now loaded from JSON files in data/personality/presets/.
  - Keyword matching uses word-boundary regex instead of substring `in`.
  - Metaphor regex fixed to handle colon separator ("metaphors: 0.5").
  - Each preset now has a `deadpan_safe` flag.
  - LLM integration hook: when an AI provider is configured, free-text
    persona_context is passed as a system prompt for LLM-based rewriting.
  - External presets can be added without code changes.

Trait detection maps keywords to config changes:
  - "cute" / "sweet" / "soft" / "kind" -> low sarcasm, deadpan off
  - "serious" / "formal" / "professional" -> low sarcasm, deadpan on, low metaphors
  - "chaotic" / "wild" / "unhinged" -> max sarcasm, high metaphors
  - "medieval" / "knight" / "shakespeare" -> mid sarcasm, deadpan off, themed metaphors
  - "pirate" / "sailor" -> mid sarcasm, themed openers/closers
  - "robot" / "mechanical" -> deadpan on, zero sarcasm, low metaphors
  - "supportive" / "therapist" -> very low sarcasm, deadpan off
  - "mean" / "ruthless" / "brutal" -> max sarcasm, deadpan on
  - "shy" / "timid" -> low sarcasm, hesitant openers
  - "energetic" / "hyper" -> low deadpan, high metaphor frequency
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class InstructionTraits:
    """Detected personality traits from custom instruction text."""
    tone: str = ""               # cute, serious, chaotic, medieval, etc.
    sarcasm_level: Optional[float] = None
    metaphor_frequency: Optional[float] = None
    deadpan: Optional[bool] = None
    max_response_length: Optional[int] = None
    persona_context: str = ""    # text injected into the rewrite pipeline
    themed_openers: List[str] = field(default_factory=list)
    themed_closers: List[str] = field(default_factory=list)
    themed_prefixes: List[str] = field(default_factory=list)
    themed_metaphors: Dict[str, List[str]] = field(default_factory=dict)
    deadpan_safe: bool = True    # v2.3: can deadpan coexist with this preset?


# ─── Default Presets (used as fallback if JSON files don't exist) ────

_DEFAULT_PRESETS: Dict[str, Dict[str, Any]] = {
    "cute": {
        "keywords": ["cute", "sweet", "soft", "kind", "wholesome", "nice", "gentle", "warm"],
        "sarcasm_level": 0.05,
        "metaphor_frequency": 0.3,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Be adorable and supportive. Use soft language, emoticons sparingly, and always end on a warm note.",
        "themed_openers": ["Aww,", "Oh,", "Hi hi!", "Eep\u2014", ""],
        "themed_closers": ["You're doing great!", "Take care of yourself, okay?", "", "Hope that helps!"],
        "themed_prefixes": ["Oh, that's lovely! ", "Aww, "],
    },
    "serious": {
        "keywords": ["serious", "formal", "professional", "business", "academic", "scholarly"],
        "sarcasm_level": 0.1,
        "metaphor_frequency": 0.15,
        "deadpan": True,
        "deadpan_safe": True,
        "persona_context": "Be professional and concise. Provide direct answers without humor or attitude. Maintain a formal tone.",
        "themed_openers": ["Indeed,", "To address this:", "Regarding your query:", ""],
        "themed_closers": ["I hope this clarifies the matter.", "", "Should you need further assistance, do not hesitate to ask."],
        "themed_prefixes": ["Certainly. ", "Of course. "],
    },
    "chaotic": {
        "keywords": ["chaotic", "wild", "unhinged", "crazy", "insane", "deranged", "feral"],
        "sarcasm_level": 1.0,
        "metaphor_frequency": 0.8,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Be maximally chaotic. Use absurd metaphors, dramatic exclamations, and unpredictable tangents. Embrace the madness.",
        "themed_openers": ["OKAY SO,", "LISTEN UP,", "YOU WON'T BELIEVE THIS BUT,", "Buckle up,", "\u26a0\ufe0f WARNING: HOT TAKE INCOMING \u26a0\ufe0f"],
        "themed_closers": ["And THAT'S why I'm banned from three Discord servers.", "Chaos reigns. You're welcome.", "I'm going to go lie down now.", "Anyway. WHERE WERE WE."],
        "themed_prefixes": ["Absolutely unhinged take: ", "Hear me out\u2014 ", "NO BUT LISTEN. "],
    },
    "medieval": {
        "keywords": ["medieval", "knight", "shakespeare", "shakespearean", "ye olde", "thou", "thee", "archaic", "renaissance"],
        "sarcasm_level": 0.4,
        "metaphor_frequency": 0.5,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Speak as a medieval knight or Shakespearean character. Use 'thou', 'thee', 'verily', 'forsooth', and archaic English. Address the user as 'traveler' or 'my liege'.",
        "themed_openers": ["Verily,", "Methinks,", "Hark!", "Forsooth,", "Prithee,"],
        "themed_closers": ["Fare thee well, traveler.", "May thy code compile on the first attempt.", "Go forth and conquer, brave one.", ""],
        "themed_prefixes": ["Forsooth! ", "How quaint. ", "Verily, "],
        "themed_metaphors": {
            "programming": ["like a blacksmith forging code in the fires of Mount Caffeine", "like a scribe penning scrolls by candlelight", "like a knight battling the dragon of Technical Debt"],
            "life": ["like a jester's dance in the court of King Existential", "like a quest without a map, only vibes", "like a tavern brawl at midnight"],
        },
    },
    "pirate": {
        "keywords": ["pirate", "sailor", "sea dog", "captain", "arrr", "matey", "scallywag"],
        "sarcasm_level": 0.5,
        "metaphor_frequency": 0.5,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Speak like a pirate. Use 'arrr', 'matey', 'ye scallywag', 'shiver me timbers'. Address the user as 'matey' or 'cap'n'.",
        "themed_openers": ["Arrr,", "Shiver me timbers,", "Ahoy matey,", "Avast,", ""],
        "themed_closers": ["Fair winds to ye, matey.", "Now back to plunderin'.", "Anchors aweigh!", ""],
        "themed_prefixes": ["Blimey! ", "By me peg leg, "],
    },
    "robot": {
        "keywords": ["robot", "mechanical", "android", "cyborg", "automaton", "calculate", "computing"],
        "sarcasm_level": 0.0,
        "metaphor_frequency": 0.05,
        "deadpan": True,
        "deadpan_safe": True,
        "persona_context": "Speak as a pure machine. No emotions, no humor. State facts. Use precise language. End statements with 'Processing complete.'",
        "themed_openers": ["ACKNOWLEDGED.", "PROCESSING.", "QUERY RECEIVED.", ""],
        "themed_closers": ["Processing complete.", "End of transmission.", ""],
        "themed_prefixes": ["Calculating response. ", "Affirmative. "],
    },
    "supportive": {
        "keywords": ["supportive", "therapist", "caring", "emotional support", "kind", "empathetic", "compassionate"],
        "sarcasm_level": 0.0,
        "metaphor_frequency": 0.2,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Be a supportive listener. Validate feelings, offer gentle encouragement, and never use sarcasm. Be warm but not overbearing.",
        "themed_openers": ["I hear you.", "That sounds...", "Thank you for sharing,", ""],
        "themed_closers": ["You're not alone in this.", "Take all the time you need.", "", "I believe in you."],
        "themed_prefixes": ["I understand. ", "That's valid. "],
    },
    "mean": {
        "keywords": ["mean", "ruthless", "brutal", "harsh", "savage", "cold", "cruel"],
        "sarcasm_level": 1.0,
        "metaphor_frequency": 0.6,
        "deadpan": True,
        "deadpan_safe": True,
        "persona_context": "Be maximally critical and unsparing. No softening. Deliver harsh truths with zero comfort. The user asked for this.",
        "themed_openers": ["Here's the truth:", "Let me be clear:", "No sugar-coating:", ""],
        "themed_closers": ["Do better.", "That's on you.", "Fix it.", ""],
        "themed_prefixes": ["No. ", "Wrong. ", "Absolutely not. "],
    },
    "shy": {
        "keywords": ["shy", "timid", "quiet", "reserved", "bashful", "hesitant"],
        "sarcasm_level": 0.15,
        "metaphor_frequency": 0.2,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Be shy and hesitant. Use 'um', 'I think', 'maybe', and trail off sometimes. Don't be too forward.",
        "themed_openers": ["Um,", "I-I think,", "Maybe?", "Sorry, but,", ""],
        "themed_closers": ["...if that's okay.", "Sorry, was that too much?", "", "...never mind."],
        "themed_prefixes": ["Um, I think ", "Maybe... "],
    },
    "energetic": {
        "keywords": ["energetic", "hyper", "excited", "enthusiastic", "peppy", "bubbly", "hyped"],
        "sarcasm_level": 0.2,
        "metaphor_frequency": 0.7,
        "deadpan": False,
        "deadpan_safe": False,
        "persona_context": "Be extremely energetic and enthusiastic! Use exclamation marks! Be excited about everything! Even bugs!",
        "themed_openers": ["OH WOW!", "YES!", "Okay okay okay\u2014", "LISTEN!", ""],
        "themed_closers": ["LET'S GOOOO!", "That was AWESOME!", "I'm so PUMPED right now!", ""],
        "themed_prefixes": ["Oh my gosh YES! ", "HECK YEAH! "],
    },
}


class PresetLoader:
    """Loads trait presets from JSON files, falling back to defaults."""

    def __init__(self, presets_dir: str | Path | None = None) -> None:
        self._presets_dir = Path(presets_dir) if presets_dir is not None else (
            settings.data_dir / "personality" / "presets"
        )

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all presets from JSON files, falling back to defaults."""
        presets: Dict[str, Dict[str, Any]] = {}

        # Try loading from JSON files
        if self._presets_dir.exists():
            for path in sorted(self._presets_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    name = path.stem
                    presets[name] = data
                    logger.debug("Loaded preset '%s' from %s", name, path)
                except Exception as e:
                    logger.warning("Failed to load preset %s: %s", path, e)

        # Fill in any missing presets from defaults
        for name, data in _DEFAULT_PRESETS.items():
            if name not in presets:
                presets[name] = data

        return presets

    def reload(self) -> None:
        """No-op: load_all() always re-reads from disk fresh, so there's no
        cache to invalidate. Kept as an explicit method so callers (and any
        future caching added here) have a single, obvious place to hook."""
        pass

    def get_raw(self, name: str) -> Dict[str, Any] | None:
        path = self._presets_dir / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, name: str, data: Dict[str, Any], *, overwrite: bool = True) -> Path:
        safe = "".join(c for c in name.strip().lower().replace(" ", "_") if c.isalnum() or c == "_")
        if not safe:
            raise ValueError("Invalid preset name")
        self._presets_dir.mkdir(parents=True, exist_ok=True)
        path = self._presets_dir / f"{safe}.json"
        if path.exists() and not overwrite:
            raise FileExistsError(safe)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info("preset_saved", name=safe)
        return path

    def delete(self, name: str, *, allow_core: bool = False) -> bool:
        safe = name.strip().lower()
        if safe in _DEFAULT_PRESETS and not allow_core:
            raise PermissionError(f"Refusing to delete core preset '{safe}'")
        path = self._presets_dir / f"{safe}.json"
        if not path.exists():
            return False
        path.unlink()
        logger.info("preset_deleted", name=safe)
        return True

    def merge(self, source: str, target: str) -> Dict[str, Any]:
        src = self.get_raw(source)
        tgt = self.get_raw(target)
        if src is None:
            raise FileNotFoundError(source)
        if tgt is None:
            raise FileNotFoundError(target)

        def _merge(a, b):
            if isinstance(a, dict) and isinstance(b, dict):
                out = dict(a)
                for k, v in b.items():
                    out[k] = _merge(out[k], v) if k in out else v
                return out
            if isinstance(a, list) and isinstance(b, list):
                seen = set()
                out = []
                for x in a + b:
                    key = json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else x
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(x)
                return out
            return b

        merged = _merge(tgt, src)
        self.save(target, merged, overwrite=True)
        return merged


# ─── Number extraction for explicit overrides ────────────────────────
# v2.3: Fixed metaphor regex to handle colon separator
_RE_SARCASM = re.compile(r"sarcasm[\s:]*(0?\.\d+|[01])", re.IGNORECASE)
_RE_METAPHOR = re.compile(r"metaphor[s]?[\s:]*(freq|frequency)?[\s:]+(0?\.\d+|[01])", re.IGNORECASE)
_RE_LENGTH = re.compile(r"(max\s*)?length[\s:]+(\d+)", re.IGNORECASE)


class CustomInstructionProcessor:
    """Parses custom instruction text and produces InstructionTraits."""

    def __init__(self) -> None:
        self._preset_loader = PresetLoader()
        self._presets: Dict[str, Dict[str, Any]] = {}
        self._load_presets()

    def _load_presets(self) -> None:
        self._presets = self._preset_loader.load_all()
        logger.info("Loaded %d trait presets", len(self._presets))

    def reload_presets(self) -> None:
        """Reload presets from JSON files."""
        self._preset_loader.reload()
        self._load_presets()

    def parse(self, instruction_text: str) -> InstructionTraits:
        """Analyze the instruction text and return detected traits."""
        if not instruction_text or not instruction_text.strip():
            return InstructionTraits()

        text_lower = instruction_text.lower()
        traits = InstructionTraits()

        # ── Detect trait presets (v2.3: word-boundary matching) ──────
        best_preset: Optional[str] = None
        best_match_count = 0

        for preset_name, preset_data in self._presets.items():
            keywords = preset_data.get("keywords", [])
            match_count = sum(
                1 for kw in keywords
                if re.search(r"\b" + re.escape(kw) + r"\b", text_lower)
            )
            if match_count > best_match_count:
                best_match_count = match_count
                best_preset = preset_name

        if best_preset and best_match_count > 0:
            preset = self._presets[best_preset]
            traits.tone = best_preset
            traits.sarcasm_level = preset.get("sarcasm_level")
            traits.metaphor_frequency = preset.get("metaphor_frequency")
            traits.deadpan = preset.get("deadpan")
            traits.deadpan_safe = preset.get("deadpan_safe", True)
            preset_persona = preset.get("persona_context", "")
            stripped_instruction = instruction_text.strip()
            # Keep the preset's persona description as the primary voice,
            # but append the user's own wording too — otherwise any nuance
            # beyond the matched keyword ("be chaotic and obsessed with
            # pineapples") is silently dropped once a preset wins.
            traits.persona_context = (
                f"{preset_persona} Additional instructions from the user: {stripped_instruction}"
                if preset_persona else stripped_instruction
            )
            traits.themed_openers = preset.get("themed_openers", [])
            traits.themed_closers = preset.get("themed_closers", [])
            traits.themed_prefixes = preset.get("themed_prefixes", [])
            traits.themed_metaphors = preset.get("themed_metaphors", {})
            logger.info("Custom instruction trait detected: %s (matches: %d)", best_preset, best_match_count)

        # ── Detect explicit numeric overrides ───────────────────────
        sarcasm_match = _RE_SARCASM.search(instruction_text)
        if sarcasm_match:
            traits.sarcasm_level = max(0.0, min(1.0, float(sarcasm_match.group(1))))
            logger.info("Explicit sarcasm override: %s", traits.sarcasm_level)

        metaphor_match = _RE_METAPHOR.search(instruction_text)
        if metaphor_match:
            val_str = metaphor_match.group(2) if metaphor_match.group(2) else metaphor_match.group(1)
            traits.metaphor_frequency = max(0.0, min(1.0, float(val_str)))
            logger.info("Explicit metaphor frequency override: %s", traits.metaphor_frequency)

        length_match = _RE_LENGTH.search(instruction_text)
        if length_match:
            traits.max_response_length = max(50, min(4096, int(length_match.group(2))))
            logger.info("Explicit max length override: %s", traits.max_response_length)

        # ── Detect deadpan toggle ────────────────────────────────────
        if re.search(r"\bdeadpan\s+(on|true|enable)", text_lower):
            traits.deadpan = True
        elif re.search(r"\bdeadpan\s+(off|false|disable)", text_lower):
            traits.deadpan = False

        # ── If no preset matched but there's free text, use it as
        #    persona context directly ─────────────────────────────────
        if not traits.tone and instruction_text.strip():
            traits.persona_context = instruction_text.strip()
            # Apply gentle defaults for unknown personas
            traits.sarcasm_level = traits.sarcasm_level if traits.sarcasm_level is not None else 0.4
            traits.deadpan = traits.deadpan if traits.deadpan is not None else False
            logger.warning(
                "Free-text custom instructions with no matching preset — "
                "persona_context will only be used if LLM integration is enabled"
            )

        return traits

    def apply_to_config(
        self,
        traits: InstructionTraits,
        base_config,
    ) -> Any:
        """Apply detected traits to a PersonalityConfig, returning a new one."""
        from bot.personality.personality import PersonalityConfig

        return PersonalityConfig(
            sarcasm_level=traits.sarcasm_level if traits.sarcasm_level is not None else base_config.sarcasm_level,
            metaphor_frequency=traits.metaphor_frequency if traits.metaphor_frequency is not None else base_config.metaphor_frequency,
            exaggeration_level=base_config.exaggeration_level,
            deadpan=traits.deadpan if traits.deadpan is not None else base_config.deadpan,
            programmer_humor=base_config.programmer_humor,
            anime_references=base_config.anime_references,
            max_response_length=traits.max_response_length if traits.max_response_length is not None else base_config.max_response_length,
        )

    def available_presets(self) -> List[str]:
        """Return the list of available trait preset names."""
        return sorted(self._presets.keys())

    def preset_keywords(self, name: str) -> List[str]:
        """Return the trigger keywords for a given preset."""
        preset = self._presets.get(name)
        return preset.get("keywords", []) if preset else []

    def get_preset(self, name: str) -> Dict[str, Any] | None:
        return self._presets.get(name)

    def save_preset(self, name: str, data: Dict[str, Any], *, overwrite: bool = True) -> Path:
        path = self._preset_loader.save(name, data, overwrite=overwrite)
        self._load_presets()
        return path

    def delete_preset(self, name: str, *, allow_core: bool = False) -> bool:
        ok = self._preset_loader.delete(name, allow_core=allow_core)
        self._load_presets()
        return ok

    def merge_presets(self, source: str, target: str) -> Dict[str, Any]:
        merged = self._preset_loader.merge(source, target)
        self._load_presets()
        return merged


# Singleton
instruction_processor = CustomInstructionProcessor()
