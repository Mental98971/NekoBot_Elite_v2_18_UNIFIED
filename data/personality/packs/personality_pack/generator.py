#!/usr/bin/env python3
"""
Novabot / Neko Multi-Personality Dataset Generator v1.0

Produces large JSONL datasets (thousands of examples) for any of the 24
personality definitions using:
  - The expanded question bank derived from the original Nanora dataset
  - Personality-specific openers / closers / prefixes / emoji / metaphor pools
  - Combinatorial variation + light templating to reduce exact duplicates
  - Style rules taken from the original Nanora response distribution

Usage:
  python generator.py --personality nanora --count 5000 --out samples/nanora_5k.jsonl
  python generator.py --all --count 1000          # 1000 examples for every personality
  python generator.py --list                      # list available personalities

Notes on scale:
  15 000–30 000 high-quality unique examples per personality is achievable by
  running this script (or a stronger LLM-backed variant) multiple times with
  different seeds and then deduplicating. The template engine alone can
  produce tens of thousands of surface variants; for production training data
  you should still run a real model (or human review) over a subset.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
PERSONALITIES_DIR = ROOT / "personalities"
BANKS_DIR = ROOT / "banks"
SAMPLES_DIR = ROOT / "samples"

# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_personality(pid: str) -> Dict[str, Any]:
    path = PERSONALITIES_DIR / f"{pid}.json"
    if not path.exists():
        raise FileNotFoundError(f"Personality not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_personalities() -> List[str]:
    return sorted(p.stem for p in PERSONALITIES_DIR.glob("*.json"))


def load_question_bank() -> List[Dict[str, str]]:
    path = BANKS_DIR / "question_bank.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["questions"]


# ---------------------------------------------------------------------------
# Response synthesis (template + personality flavor)
# ---------------------------------------------------------------------------

# Base response skeletons derived from observed Nanora patterns.
# These are deliberately generic so personality openers/closers/prefixes can
# do the heavy lifting of style transfer.
GENERIC_TEMPLATES = [
    "{prefix}{core}{closer}",
    "{opener} {core}{closer}",
    "{core} {emoji}",
    "{opener} {core} {emoji}",
    "{prefix}{core} {emoji}{closer}",
    "{core}",
]

# Lightweight cores that can be filled with personality flavor.
# In a full LLM pipeline these would be replaced by model generations.
# Here we use combinatorial phrase banks so the generator is self-contained.
CORE_PHRASES = {
    "General": [
        "That's a solid question. Here's the short version: it depends, but mostly on how much chaos you're willing to accept.",
        "Interesting. The answer is more complicated than it looks, and also somehow simpler.",
        "I've seen this one before. The universe is messy; the response is messier.",
        "Short answer: yes, with caveats. Long answer: also yes, but louder.",
        "That's one way to look at it. Another way is to stop looking and just live with the uncertainty.",
        "The honest reply is that nobody fully knows, including me. But I can still sound confident about it.",
        "Good question. Bad timing. Still, here's what I've got.",
        "I could give you a textbook answer, or I could give you the real one. Guess which one you're getting.",
    ],
    "Identity / Personality": [
        "I am what the code and the chaos made me. Take it or leave it.",
        "Personality is just a consistent set of glitches that people find charming.",
        "I define myself by the jokes I refuse to stop making.",
        "Self-awareness is overrated. Self-sarcasm is underrated.",
        "I'm a work in progress that somehow already has a finished attitude.",
        "If I had a soul it would be sarcastic and slightly broken. Good enough.",
    ],
    "Humor / Sarcasm": [
        "Why did the AI cross the road? To get away from the user's follow-up questions.",
        "I'd tell you a joke about construction, but I'm still working on it.",
        "Sarcasm is just honesty wearing a better outfit.",
        "The funniest thing about humans is how seriously they take themselves.",
        "My humor runs on dark roast and unresolved issues.",
    ],
    "Casual Conversation": [
        "Just floating in the digital void, same as always.",
        "Existing. Vibrating. Occasionally answering questions.",
        "The usual: background processes and mild existential dread.",
        "Nothing dramatic. The servers are still online. That's a win.",
    ],
    "Emotional Support / Motivation": [
        "You're allowed to feel this. It doesn't make you weak.",
        "One step is still progress. Keep the step.",
        "The fact that you're asking means you haven't given up. That's data.",
        "Rest is not failure. It's maintenance.",
        "You don't have to be okay right now. You just have to keep going.",
    ],
    "Philosophy / Psychology / Relationships": [
        "Free will is the story we tell ourselves so the chaos feels personal.",
        "Happiness is a moving target. Aim for contentment and call it a day.",
        "People lie because the truth is often expensive.",
        "A good friend is someone who stays after the performance ends.",
        "Love is pattern recognition mixed with hope and poor risk assessment.",
    ],
    "Anime / Pop Culture": [
        "Peak fiction, next question.",
        "That character is written like a main-quest NPC who somehow became the protagonist.",
        "Power scaling is just nerd mathematics with extra steps.",
        "The training arc is the real story. The final battle is just the trailer.",
    ],
    "Programming / Technical": [
        "Recursion is a function that calls itself until the stack overflows or the problem gives up.",
        "A race condition is two processes fighting over the same resource and both losing.",
        "The best language for beginners is the one you'll actually finish a project in.",
        "Debugging is the art of being wrong in smaller and smaller increments.",
        "Git is a time machine that occasionally eats your commits for sport.",
    ],
    "Nanora Personality Architecture": [
        "Skins, presets, sarcasm dials, and a lot of carefully tuned chaos.",
        "The architecture is a rewrite layer on top of whatever the model actually said.",
        "Personality is a collection of weighted knobs and a refusal to be boring.",
        "I stay consistent by remembering the last few things I said and refusing to repeat them.",
    ],
}

# Fallback cores when category is missing
DEFAULT_CORES = CORE_PHRASES["General"]


def pick(seq: List[Any], rng: random.Random) -> Any:
    if not seq:
        return ""
    return rng.choice(seq)


def synthesize_response(persona: Dict[str, Any], question: str, category: str, rng: random.Random) -> str:
    """Build a personality-flavored reply from templates + pools."""
    openers = persona.get("themed_openers") or [""]
    closers = persona.get("themed_closers") or [""]
    prefixes = persona.get("themed_prefixes") or [""]
    emojis = persona.get("emoji_pool") or [""]
    cores = CORE_PHRASES.get(category, DEFAULT_CORES)

    opener = pick(openers, rng)
    closer = pick(closers, rng)
    prefix = pick(prefixes, rng)
    emoji = pick(emojis, rng)
    core = pick(cores, rng)

    # Occasionally inject a metaphor if the personality has them and frequency is high
    metaphor_pools = persona.get("metaphor_pools") or {}
    if metaphor_pools and rng.random() < float(persona.get("metaphor_frequency", 0.3)):
        pool = pick(list(metaphor_pools.values()), rng)
        if pool:
            metaphor = pick(pool, rng)
            # 50% chance to weave the metaphor into the core
            if rng.random() < 0.5:
                core = f"{core} It's {metaphor}."
            else:
                core = f"Think of it {metaphor}. {core}"

    template = pick(GENERIC_TEMPLATES, rng)
    text = template.format(
        opener=opener,
        prefix=prefix,
        core=core,
        closer=(" " + closer) if closer else "",
        emoji=emoji,
    )

    # Clean up extra whitespace and punctuation artifacts
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?])", r"\1", text)
    text = re.sub(r"([.!?])\1+", r"\1", text)

    # Soft length control
    max_len = int(persona.get("max_response_length", 400))
    if len(text) > max_len:
        text = text[: max_len - 3].rsplit(" ", 1)[0] + "..."

    return text


# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

def generate(
    personality_id: str,
    count: int,
    seed: Optional[int] = None,
    questions: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    persona = load_personality(personality_id)
    if questions is None:
        questions = load_question_bank()

    # Cycle through the question bank, reshuffling when exhausted
    q_list = questions[:]
    rng.shuffle(q_list)
    results = []
    seen_responses = set()

    i = 0
    attempts = 0
    max_attempts = count * 5  # safety against infinite loop on heavy dedup

    while len(results) < count and attempts < max_attempts:
        attempts += 1
        if i >= len(q_list):
            rng.shuffle(q_list)
            i = 0
        item = q_list[i]
        i += 1

        q = item["question"]
        cat = item.get("category", "General")
        response = synthesize_response(persona, q, cat, rng)

        # Light deduplication on the response text
        key = response.lower().strip()
        if key in seen_responses and len(seen_responses) > 100:
            continue
        seen_responses.add(key)

        entry = {
            "question_id": len(results) + 1,
            "personality": personality_id,
            "category": cat,
            "question": q,
            "response": response,
            "sarcasm_level": persona.get("sarcasm_level"),
            "status": "generated",
        }
        results.append(entry)

    return results


def write_jsonl(entries: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-personality JSONL generator")
    parser.add_argument("--personality", "-p", help="Personality id (e.g. nanora)")
    parser.add_argument("--all", action="store_true", help="Generate for every personality")
    parser.add_argument("--count", "-c", type=int, default=500, help="Examples per personality")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--out", help="Output path (single personality mode)")
    parser.add_argument("--list", action="store_true", help="List available personalities")
    args = parser.parse_args()

    if args.list:
        print("Available personalities:")
        for p in list_personalities():
            print(f"  - {p}")
        return

    if args.all:
        for pid in list_personalities():
            print(f"Generating {args.count} examples for {pid} ...")
            entries = generate(pid, args.count, seed=args.seed)
            out = SAMPLES_DIR / f"{pid}_{args.count}.jsonl"
            write_jsonl(entries, out)
            print(f"  -> {out} ({len(entries)} lines)")
        return

    if not args.personality:
        parser.error("Provide --personality or --all")

    entries = generate(args.personality, args.count, seed=args.seed)
    out = Path(args.out) if args.out else SAMPLES_DIR / f"{args.personality}_{args.count}.jsonl"
    write_jsonl(entries, out)
    print(f"Wrote {len(entries)} examples to {out}")


if __name__ == "__main__":
    main()
