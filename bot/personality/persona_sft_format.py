"""Prompt format shared by persona fine-tuning and the live runtime (standard library only).

``training/prepare_sft.py`` and ``training/train_lora.py`` build training examples with these
functions and ``bot/personality/persona_model.py`` builds the inference prompt with the same ones, so
the model is trained on exactly the text shape it is later served with.  Bump ``FORMAT_VERSION``
whenever the rendering changes; adapters record the version they were trained with and the runtime
refuses an adapter whose version differs.

Speakers are pseudonymous: ``me`` is the person being modelled, every other participant is ``u1``,
``u2`` ... in order of first appearance.  Names, usernames and numeric ids never enter a prompt.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

FORMAT_VERSION = 1
SYSTEM_PROMPT = (
    "You write the next chat message for one specific person, in the way that person writes. "
    "Answer with the message text only."
)
SELF = "me"
MAX_CTX_TURNS = 6
TURN_MAX_CHARS = 200
CHAT_KINDS = ("group", "dm")
MEDIA_TURN = "[media]"

Turn = Tuple[str, str]  # (speaker label, text)


def account_tag(index: int) -> str:
    """Stable per-person account label (``a1``, ``a2`` ...) from the registry order; never the numeric id."""
    return f"a{int(index) + 1}"


def _one_line(text: str, limit: int) -> str:
    t = " ⏎ ".join(part.strip() for part in str(text or "").replace("\r", "").split("\n") if part.strip())
    if len(t) > limit:
        cut = t[:limit]
        if " " in cut and not t[limit].isspace():
            cut = cut.rsplit(" ", 1)[0]
        t = cut.rstrip() + "…"
    return t


def render_user_block(
    ctx: Sequence[Sequence[str]],
    reply_to: Optional[Sequence[str]] = None,
    chat_kind: str = "group",
    acct: Optional[str] = None,
) -> str:
    """The user-side text of one example: header lines, the recent transcript, the reply target."""
    kind = chat_kind if chat_kind in CHAT_KINDS else "group"
    lines: List[str] = [f"[chat: {kind}]"]
    if acct:
        lines.append(f"[account: {acct}]")
    turns = list(ctx)[-MAX_CTX_TURNS:]
    if turns:
        lines.append("[recent messages]")
        for who, text in turns:
            lines.append(f"{who}: {_one_line(text, TURN_MAX_CHARS) or MEDIA_TURN}")
    else:
        lines.append("[no earlier messages]")
    if reply_to:
        who, text = reply_to[0], reply_to[1]
        lines.append(f"[replying to {who}: {_one_line(text, TURN_MAX_CHARS) or MEDIA_TURN}]")
    lines.append(f"Write the next message from {SELF}.")
    return "\n".join(lines)


def build_messages(
    ctx: Sequence[Sequence[str]],
    reply_to: Optional[Sequence[str]] = None,
    chat_kind: str = "group",
    acct: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Chat-template input (system + user).  The assistant turn is the person's actual/predicted message."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_block(ctx, reply_to, chat_kind, acct)},
    ]


def example_messages(example: Dict[str, Any]) -> List[Dict[str, str]]:
    """Messages for one prepared example dict (see training/prepare_sft.py for the schema)."""
    return build_messages(
        example.get("ctx") or [], example.get("reply_to"), example.get("chat_kind", "group"), example.get("acct"),
    )


def runtime_messages(
    user_message: str,
    reply_to_text: Optional[str] = None,
    chat_kind: str = "group",
    acct: Optional[str] = None,
    recent: Iterable[Sequence[str]] = (),
) -> List[Dict[str, str]]:
    """Inference prompt for a live chat: ``recent`` is already-pseudonymised (speaker, text) history."""
    ctx: List[Turn] = [(str(w), str(t)) for w, t in recent]
    ctx.append(("u1", user_message))
    reply = ("u1", reply_to_text) if reply_to_text else None
    return build_messages(ctx, reply, chat_kind, acct)
