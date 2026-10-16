"""Prompt assembly for the persona engine (standard library only).

Structure:  rules -> measured persona profile -> bot memory -> conversation -> situation -> sampled response
plan -> historical evidence (few, behavioural) -> instruction.  Evidence shows *how* the person reacted; the
rules forbid copying it.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .core import Situation
from .memory import Turn
from .profile import ResponsePlan
from .store import Evidence

RULES = (
    "You are a chat bot that imitates the *chat style* of {name}. You are not {name}. If anyone sincerely asks whether you are the "
    "real person or a bot, say plainly that you are a bot imitating their style.\n"
    "Write ONE chat message and nothing else: no quotation marks, no name label, no explanation, no lists, no markdown.\n"
    "Write a NEW reply that fits the message you are answering. The historical examples show HOW {name} tends to react; do not copy "
    "their words, do not reuse an example reply, and do not force catchphrases or unusual words into every message - vary your wording.\n"
    "Answer or react to what was actually said first; the style comes second. Match the language and script of the conversation.\n"
    "Do not invent personal facts about {name} (age, location, relationships, health, plans, contact details). If asked for one, "
    "deflect in their style or say you don't know. Never write anything that could pass as a real statement by {name}."
)


def _q(text: str, limit: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "\u2026"


class PromptBuilder:
    def __init__(self, name: str, *, example_chars: int = 160, max_prompt_chars: int = 3200) -> None:
        self.name, self.example_chars, self.max_prompt_chars = name, example_chars, max_prompt_chars

    def build(
        self, *, current: str, reply_to: Optional[str], turns: Sequence[Turn], sit: Situation, plan: ResponsePlan,
        profile_facts: Sequence[str], evidence: Sequence[Evidence], memory_facts: Sequence[str] = (),
        topic: Sequence[str] = (), open_question: Optional[str] = None, extra: Sequence[str] = (),
    ) -> List[dict]:
        name = self.name
        sys_msg = RULES.format(name=name)
        parts: List[str] = []
        if profile_facts:
            parts.append("[PERSONA PROFILE - measured from their real messages]\n- " + "\n- ".join(profile_facts))
        if memory_facts:
            parts.append("[BOT MEMORY - facts people asked the bot to remember in this chat; they are NOT things " + name + " said]\n- "
                         + "\n- ".join(_q(m, 200) for m in memory_facts))
        conv: List[str] = []
        for t in turns[-8:]:
            conv.append(f"{'bot' if t.who == 'persona' else t.who}: {_q(t.text, 200)}")
        if reply_to:
            conv.append(f"(the message below replies to: {_q(reply_to, 200)})")
        conv.append(f"u*: {_q(current, 300)}   <- answer this one")
        parts.append("[CONVERSATION - current chat]\n" + "\n".join(conv))
        sit_lines = [f"message type: {sit.act.replace('_', ' ')}", f"language/script: {sit.script}" + (", Hinglish" if sit.hinglish >= 0.2 else "")]
        if topic:
            sit_lines.append("topic words: " + ", ".join(topic))
        if open_question:
            sit_lines.append("an earlier question in this exchange may still be unanswered")
        if sit.cues:
            sit_lines.append("cues: " + ", ".join(sit.cues))
        parts.append("[SITUATION]\n- " + "\n- ".join(sit_lines))
        parts.append("[PLAN FOR THIS REPLY - sampled from how they usually respond in this situation]\n- " + "\n- ".join(plan.lines()))
        if evidence:
            ex_lines = []
            for ev in evidence:
                e = ev.example
                ex_lines.append(f'when someone said "{_q(e.trigger, self.example_chars)}" they replied "{_q(e.reply, self.example_chars)}"')
            parts.append("[HISTORICAL EVIDENCE - real past reactions in similar situations; for behaviour only, never copy]\n- " + "\n- ".join(ex_lines))
        if extra:
            parts.append("[NOTES]\n- " + "\n- ".join(extra))
        parts.append(f"Write {name}'s imitation reply to the last message now.")
        user_msg = "\n\n".join(parts)
        if len(user_msg) > self.max_prompt_chars:  # shed evidence first, then memory, never the plan or the current message
            keep = list(evidence)
            while keep and len(user_msg) > self.max_prompt_chars:
                keep.pop()
                return self.build(current=current, reply_to=reply_to, turns=turns, sit=sit, plan=plan, profile_facts=profile_facts,
                                  evidence=keep, memory_facts=memory_facts, topic=topic, open_question=open_question, extra=extra)
        return [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}]
