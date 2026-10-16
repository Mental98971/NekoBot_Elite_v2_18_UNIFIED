"""Shared helpers for the persona_ft entry points.

The three scripts here (01_preprocess / 02_train_lora / 03_evaluate) keep the command names and flags of the v2.11
PERSONA_FT overlay but delegate to the ``training/`` toolkit, which adds what the overlay lacked: the authorization gate,
conversation-level (leak-free) splits, private output, hash-verified manifests, baselines, evaluation gates and a registry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PERSONS_DIR = ROOT / "data" / "personality" / "persons"
PRIVATE_DATA = ROOT / "training" / "_private" / "data"
_NO_CONTEXT = (None, "structured", "empty")
CONTEXT_SENDER = "1"  # numeric id that belongs to nobody in the registry: the "other side" of a stored context message


def shipped_persons() -> List[str]:
    return sorted(p.parent.name for p in PERSONS_DIR.glob("*/corpus.jsonl"))


ID_BLOCK = 10 ** 9  # message ids of person number k live in [k * ID_BLOCK, (k + 1) * ID_BLOCK)


def export_rows(person: str, persons_dir: Path = PERSONS_DIR, id_base: int = 0) -> Iterator[Dict[str, Any]]:
    """Rows of the shipped, scrubbed corpus in the raw-export shape that ``training.prepare_sft`` reads.

    The corpus stores, per message, the text it answered (``ctx``; ``reply`` marks an explicit reply, otherwise it is the previous
    message of the flow).  Each stored context becomes a message from a placeholder sender placed just before the person's own
    message, so conversation grouping, context windows and leak-free splitting work exactly as for a raw export.

    ``id_base`` keeps synthetic message ids unique when several people are loaded in one run: people share chats (chat references
    are the same hash), and ``training.prepare_sft`` keys messages by (chat, message id), so colliding ids would overwrite each
    other.  Give every person of a run a different block (``index * ID_BLOCK``).
    """
    path = persons_dir / person / "corpus.jsonl"
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    rows.sort(key=lambda r: (str(r.get("chat") or ""), str(r.get("ts") or ""), str(r.get("id") or "")))
    counters: Dict[str, int] = {}
    for r in rows:
        chat = str(r.get("chat") or "unknown")
        n = counters.get(chat, id_base)
        ts = r.get("ts")
        has_ctx = bool(r.get("ctx")) and r.get("ctx_kind") not in _NO_CONTEXT
        if has_ctx:
            yield {"chat_id": chat, "msg_id": str(n + 1), "sender_id": CONTEXT_SENDER, "message_text": str(r["ctx"]), "date": ts}
        yield {"chat_id": chat, "msg_id": str(n + 2), "sender_id": str(r.get("acct")), "message_text": str(r.get("text") or ""),
               "date": ts, "reply_to_msg_id": str(n + 1) if (has_ctx and r.get("reply")) else None}
        counters[chat] = n + 2
