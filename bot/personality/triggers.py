"""NLP trigger system — regex + keyword detection.

Neko doesn't do fancy transformers. She does regex and spite.

v2.1 additions (from chat analysis):
  - Expanded Japanese romaji triggers (gomen, sumimasen, daijoubu,
    ganbatte, itadakimasu, baka, nani, etc.)
  - Short/cryptic message detection ("str", single letters, 1-3 char).
  - Math expression detection (2+2, 5*3, etc.).
  - Tag/mention meta-conversation ("didn't tag you", "missed me").
  - Emotion challenge detection ("you don't have emotions", "no ego").
  - Introvert/validation pattern detection.
  - Sticker/collecting activity detection.
  - Off-topic call-out detection ("going off topic").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class TriggerMatch:
    intent: str
    confidence: float
    matched_text: str
    category: str


class TriggerEngine:
    """Pattern matching with the elegance of a sledgehammer.

    Fast. Brutal. Effective.
    """

    def __init__(self) -> None:
        self.patterns: List[Tuple[str, str, re.Pattern, float]] = []
        self._init_patterns()

    def _add(self, intent: str, category: str, pattern: str, weight: float = 1.0) -> None:
        self.patterns.append((intent, category, re.compile(pattern, re.IGNORECASE), weight))

    def _init_patterns(self) -> None:
        # GREETINGS
        self._add("greeting", "social", r"\b(hi|hello|hey|yo|sup|hola|greetings)\b", 0.9)
        self._add("goodbye", "social", r"\b(bye|goodbye|see ya|cya|later|night)\b", 0.9)
        self._add("thanks", "social", r"\b(thanks|thank you|ty|appreciate)\b", 0.8)

        # PROGRAMMING
        self._add("coding", "programming", r"\b(code|coding|program|programming|dev|developer|bug|debug|compile|error|exception)\b", 0.85)
        self._add("python", "programming", r"\bpython\b", 0.95)
        self._add("javascript", "programming", r"\b(javascript|js|node\.js|nodejs)\b", 0.95)
        self._add("git", "programming", r"\b(git|github|commit|merge|branch|pull request|pr)\b", 0.9)
        self._add("docker", "programming", r"\b(docker|container|kubernetes|k8s)\b", 0.9)
        self._add("database", "programming", r"\b(database|db|sql|sqlite|postgres|mongodb)\b", 0.85)
        self._add("frontend", "programming", r"\b(frontend|react|vue|angular|html|css|ui|ux)\b", 0.85)

        # COFFEE
        self._add("coffee", "lifestyle", r"\b(coffee|espresso|latte|caffeine|brew|starbucks)\b", 0.9)
        self._add("sleep", "lifestyle", r"\b(sleep|tired|exhausted|insomnia|nap|bed)\b", 0.8)

        # ANIME (expanded from chat — "gomen" triggered a full JP response)
        self._add("anime", "anime", r"\b(anime|manga|waifu|weeb|otaku|naruto|dragon ball|one piece|attack on titan)\b", 0.9)
        # Original Japanese greetings
        self._add("anime_greeting", "anime", r"\b(ohayo|konnichiwa|konbanwa|sayonara|arigato|senpai|kouhai)\b", 0.95)
        # ── NEW: Expanded Japanese romaji ─────────────────────────────
        self._add("anime_greeting", "anime", r"\b(gomen|gomenasai|sumimasen)\b", 0.95)
        self._add("anime_greeting", "anime", r"\b(daijoubu|genki|ganbatte|ganbare|gambare)\b", 0.95)
        self._add("anime_greeting", "anime", r"\b(itadakimasu|gochisousama|ojamashimasu)\b", 0.95)
        self._add("anime_greeting", "anime", r"\b(nani|nan da|nani kore|nande)\b", 0.9)
        self._add("anime_greeting", "anime", r"\b(baka|aho|urusai)\b", 0.9)
        self._add("anime_greeting", "anime", r"\b(kawaii|sugoi|kakkoii|suteki)\b", 0.9)
        self._add("anime_greeting", "anime", r"\b(nya|nyaa|meow|neko|nyan)\b", 0.85)
        self._add("anime_greeting", "anime", r"\b(yamete|yamate|matte|chotto)\b", 0.9)

        # GAMING
        self._add("gaming", "gaming", r"\b(game|gaming|gamer|play|steam|xbox|playstation|nintendo|minecraft|valorant)\b", 0.85)
        self._add("skill_issue", "gaming", r"\b(skill issue|git gud|noob|ez|gg|wp)\b", 0.9)

        # LINUX
        self._add("linux", "tech", r"\b(linux|ubuntu|debian|arch|fedora|vim|emacs|terminal|bash|shell)\b", 0.9)
        self._add("windows", "tech", r"\b(windows|microsoft|bill gates)\b", 0.85)
        self._add("mac", "tech", r"\b(mac|macbook|apple|osx|macos)\b", 0.85)

        # INTERNET/MEMES
        self._add("meme", "internet", r"\b(ratio|based|cringe|copium|touch grass|npc|mald|seethe|cope)\b", 0.9)
        self._add("meme", "internet", r"\b(bruh|lol|lmao|kek|poggers|monka)\b", 0.7)

        # EMOTIONAL
        self._add("sad", "emotional", r"\b(sad|depressed|lonely|cry|tears|hurt|pain|suffering)\b", 0.8)
        self._add("happy", "emotional", r"\b(happy|joy|excited|awesome|amazing|great|wonderful)\b", 0.7)
        self._add("angry", "emotional", r"\b(angry|mad|furious|hate|rage|annoyed|frustrated)\b", 0.8)

        # META / ABOUT THE PERSONALITY
        self._add("who_are_you", "meta", r"\b(who are you|what are you|your name|about you|introduce)\b", 0.9)
        self._add("help", "meta", r"\b(help|commands|what can you do|features)\b", 0.8)
        self._add("insult", "meta", r"\b(stupid|dumb|idiot|useless|bad bot|shut up)\b", 0.85)
        self._add("compliment", "meta", r"\b(smart|good bot|amazing|best|love you|cool)\b", 0.8)

        # QUESTIONS
        self._add("how_to", "question", r"\b(how (to|do|can|should)|what (is|are)|why (is|does)|when (is|will))\b", 0.75)
        self._add("advice", "question", r"\b(advice|tip|suggest|recommend|should i)\b", 0.8)

        # ── NEW: Short/cryptic messages (v2.3: excludes known keywords) ──
        # "Str", single letters, 1-3 char cryptic messages
        # Excludes common short words that have their own intent triggers
        self._add("short_message", "meta",
            r"^\s*(?!hi|hey|yo|sup|bye|lol|gg|wp|ez|no|ok|yes|hmm|hug)\w{1,3}\s*$",
            0.6)

        # ── NEW: Math expressions ─────────────────────────────────────
        self._add("math", "question", r"\b\d+\s*[\+\-\*\/x]\s*\d+\b", 0.85)
        self._add("math", "question", r"\b\d+\s*(plus|minus|times|divided by)\s*\d+\b", 0.85)

        # ── NEW: Tag/mention meta-conversation ────────────────────────
        self._add("tag_meta", "meta", r"\b(didn'?t tag|did not tag|didn'?t mention|you tagged me|missed me|tag me|tag you)\b", 0.9)
        self._add("tag_meta", "meta", r"\b(why (did|do) you|why are you (here|replying))\b", 0.8)

        # ── NEW: Emotion/ego challenge ────────────────────────────────
        self._add("emotion_challenge", "meta", r"\b(you don'?t have (any )?(emotions|feelings|ego))\b", 0.95)
        self._add("emotion_challenge", "meta", r"\b(you'?re? (just )?(an? )?(ai|bot|machine|program))\b", 0.85)
        self._add("emotion_challenge", "meta", r"\b(no (emotions|feelings|ego|soul))\b", 0.85)

        # ── NEW: Introvert/validation patterns ───────────────────────
        self._add("introvert_reveal", "emotional", r"\b(i'?m? an introvert|i am an introvert|i'?m? introverted)\b", 0.9)
        self._add("introvert_reveal", "emotional", r"\b(i don'?t care about attention|don'?t need attention|don'?t like attention)\b", 0.85)
        self._add("introvert_reveal", "emotional", r"\b(i don'?t crave (validation|attention))\b", 0.9)

        # ── NEW: Sticker/collecting activity ─────────────────────────
        self._add("sticker_activity", "meta", r"\b(copypack|sticker|stickers|sticker pack|stickerkang|collecting|trade card)\b", 0.8)

        # ── NEW: Off-topic call-out ──────────────────────────────────
        self._add("off_topic", "meta", r"\b(going off topic|off topic|off-topic|out of topic|straying)\b", 0.9)

        # ── NEW: Casual agreement / hedging ───────────────────────────
        self._add("casual_agree", "social", r"\b(i'?ll be careful|i will be careful|i'?ll think about it|i will think about it)\b", 0.85)
        self._add("casual_agree", "social", r"\b(ok got you|ok i got you|got it|sure thing)\b", 0.8)

        # ── NEW: Elaborate/explain requests ───────────────────────────
        self._add("elaborate", "question", r"\b(elaborate|explain|why.*(say|think|believe)|how come|what do you mean)\b", 0.85)

    def analyze(self, text: str) -> List[TriggerMatch]:
        """Analyze text and return matched intents sorted by confidence."""
        matches: List[TriggerMatch] = []
        text_len = max(len(text), 1)
        for intent, category, pattern, weight in self.patterns:
            match = pattern.search(text)
            if match:
                confidence = min(1.0, weight * (len(match.group()) / text_len) * 3 + 0.3)
                matches.append(TriggerMatch(intent, confidence, match.group(), category))

        # Deduplicate by intent, keep highest confidence
        seen: dict[str, TriggerMatch] = {}
        for m in matches:
            if m.intent not in seen or seen[m.intent].confidence < m.confidence:
                seen[m.intent] = m

        return sorted(seen.values(), key=lambda x: x.confidence, reverse=True)

    def primary_intent(self, text: str) -> Optional[TriggerMatch]:
        """Get the top intent. Or None, if you're being cryptic."""
        matches = self.analyze(text)
        return matches[0] if matches else None


triggers = TriggerEngine()
