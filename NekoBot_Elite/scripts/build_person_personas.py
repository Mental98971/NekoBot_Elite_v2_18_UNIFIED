#!/usr/bin/env python3
"""Build the per-person persona data (Tanvi, Yashmeet) from raw chat exports.

Stdlib only; deterministic (same inputs -> byte-identical outputs).

    python scripts/build_person_personas.py \
        --source path/to/export1.jsonl --source path/to/export2.db ... \
        [--out data/personality/persons] [--skins-dir data/personality/skins] [--merge-existing] [--dry-run]

``--merge-existing`` updates an existing corpus instead of rebuilding it: rows are unioned by id (the id is derived from person, chat and
message id, so the same message in an older and a newer export is the same row).  Nothing in the old corpus is removed or rewritten - a row
present in both only gains provenance and context fields it lacked - and an old row's replay flag (``rp``) can be withdrawn but never granted
by a newer, possibly smaller, roster.  Use it whenever the new exports are not a superset of everything that was built before.

Accepted inputs (schema is detected per record, so mixed files are fine):

* ``.jsonl`` / ``.json`` (list) / ``.csv`` / ``.db`` (SQLite, table ``messages``)
* message-pair export  : msg_id, chat_id, sender_id, sender_username, sender_name,
                         message_text, date, reply_to_text, ...
* plain export         : id, chat_id, sender_id, sender_username, message, date, reply_to_message_id
* rich export          : plain + parent_message_text, local_chat_flow, sender_fullname
* member roster rows   : the same columns as a plain export but SHIFTED (sender_id holds the
                         chat title, sender_username holds the numeric user id, message holds the
                         username, date holds the display name).  These are NOT messages; they are
                         used only as identity evidence and are never added to a corpus.

What is written per person (``<out>/<person>/``):

* ``corpus.jsonl``  every message the person sent, scrubbed, with provenance
* ``profile.json``  statistics and behaviour tables computed from the corpus (no hand-written traits)

plus ``<out>/build_report.json`` and one skin JSON per persona in ``--skins-dir``.

Privacy: URLs, e-mail addresses, phone-like numbers, long ids, ``tg://`` links and @handles are
replaced by placeholders; chat ids are stored as one-way short hashes; only the target person's
own messages are stored (other members' text appears at most as a truncated, scrubbed reply
context).  The raw exports are never copied.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "personality" / "persons"
DEFAULT_SKINS = ROOT / "data" / "personality" / "skins"


csv.field_size_limit(2**31 - 1)

# ── shared text helpers (single source of truth: bot/personality/persona_text.py) ──
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("persona_text", ROOT / "bot" / "personality" / "persona_text.py")
persona_text = _ilu.module_from_spec(_spec)
sys.modules["persona_text"] = persona_text
_spec.loader.exec_module(persona_text)  # type: ignore[union-attr]
MEDIA_MARKER = persona_text.MEDIA_MARKER
OWN_MAX_CHARS = persona_text.OWN_MAX_CHARS
CTX_MAX_CHARS = persona_text.CTX_MAX_CHARS
scrub = persona_text.scrub
classify_utterance = persona_text.classify_utterance
classify_context = persona_text.classify_context
message_kind = persona_text.message_kind
norm_key = persona_text.norm_key
_RE_TOKEN = re.compile(r"[a-z']{2,}")

# ── measurement-only lexicons (build-time statistics) ──
_RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2764\u2B06\u2B07\u2705\u274C\u2728\u2639\u263A]"
)
_RE_DEVANAGARI = re.compile("[\u0900-\u097F]")
_RE_ALPHA = re.compile(r"[A-Za-z]")
_RE_ELONG = re.compile(r"([A-Za-z])\1{2,}")

# Common romanised-Hindi function words: used ONLY to measure how much of a person's
# text is Hinglish.  This is a measurement lexicon, not a personality claim.
HINGLISH_MARKERS = frozenset(
    "hai hain nahi nhi ni kya kyu kyun kaise kab kon kaun mai main mei me meko mujhe mera meri "
    "tera teri tu tum tha thi the ye yeh wo woh ka ki ke ko se bhi aur ya toh to hu hoon ho raha "
    "rahi rha rhi kr kar karo krna pta pata bs bas abhi kuch koi sab bhut bahut accha acha haan "
    "han ha nai nahin yaar bhai behen".split()
)
_STOP_FOR_TOKENS = frozenset({"non", "media", "text", "user", "url", "num", "email"})

HABITUAL_MIN = 4          # replay-safe lines use only words the person used at least this often
NAME_VOCAB_OVERRIDE = 60  # a roster-name token used this often is ordinary vocabulary, not a name

# ── source reading ───────────────────────────────────────────────────────────
def _iter_raw(path: Path) -> Iterator[Dict[str, Any]]:
    suf = path.suffix.lower()
    if suf == ".jsonl":
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    yield {"__corrupt__": True, "__line__": n}
                    continue
                if isinstance(obj, dict):
                    yield obj
    elif suf == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("messages") or data.get("rows") or []
        for obj in data:
            if isinstance(obj, dict):
                yield obj
    elif suf == ".csv":
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                yield dict(row)
    elif suf == ".db":
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute("SELECT * FROM messages"):
                yield dict(row)
        finally:
            con.close()
    else:
        raise ValueError(f"unsupported source type: {path}")


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    if s in ("", "None", "null", "nan"):
        return None
    return s


def _is_int(v: Optional[str]) -> bool:
    return v is not None and re.fullmatch(r"-?\d+", v) is not None


def parse_date(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    s = v.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f%z"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


class Record:
    __slots__ = ("chat_id", "msg_id", "sender_id", "username", "name", "text", "ts",
                 "reply_id", "ctx_text", "flow_prev", "sources", "chat_title")

    def __init__(self, **kw: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.sources = set(kw.get("sources") or [])

    def merge(self, other: "Record") -> None:
        for k in ("username", "name", "text", "ts", "reply_id", "ctx_text", "flow_prev", "chat_title"):
            if getattr(self, k) in (None, "") and getattr(other, k) not in (None, ""):
                setattr(self, k, getattr(other, k))
        self.sources |= other.sources


def normalise(raw: Dict[str, Any], source: str) -> Tuple[str, Any]:
    """Return ("message", Record) | ("roster", dict) | ("corrupt", None)."""
    if raw.get("__corrupt__"):
        return "corrupt", None
    sid = _clean(raw.get("sender_id"))
    sun = _clean(raw.get("sender_username"))
    chat = _clean(raw.get("chat_id"))
    if chat is None:
        return "corrupt", None
    # roster rows: shifted columns (sender_id = chat title, sender_username = numeric id)
    if "id" in raw and "message" in raw and not _is_int(sid) and _is_int(sun):
        return "roster", {
            "chat_id": chat, "user_id": sun,
            "username": _clean(raw.get("message")), "display": _clean(raw.get("date")),
        }
    if "id" in raw and "message" in raw and "message_text" not in raw and not _is_int(sid):
        return "roster_unparsed", None  # roster-shaped row without a numeric user id
    if not _is_int(sid):
        return "unattributed", None  # sender unknown (anonymous/deleted/channel): cannot belong to a person
    mid = _clean(raw.get("msg_id")) or _clean(raw.get("id"))
    if mid is None:
        return "corrupt", None
    text = raw.get("message_text") if "message_text" in raw else raw.get("message")
    text = "" if text is None or str(text) == "None" else str(text)
    flow_prev = None
    flow = _clean(raw.get("local_chat_flow"))
    if flow:
        try:
            items = json.loads(flow)
            prev = [i for i in items if _is_int(str(i.get("id"))) and int(i["id"]) < int(mid) and (i.get("text") or "").strip()]
            if prev:
                flow_prev = str(prev[-1]["text"])
        except (ValueError, TypeError, KeyError):
            flow_prev = None
    reply_id = _clean(raw.get("reply_to_msg_id")) or _clean(raw.get("reply_to_message_id")) or _clean(raw.get("parent_message_id"))
    ctx = _clean(raw.get("reply_to_text")) or _clean(raw.get("parent_message_text"))
    return "message", Record(
        chat_id=chat, msg_id=mid, sender_id=sid, username=sun if sun and not _is_int(sun) else None,
        name=_clean(raw.get("sender_name")) or _clean(raw.get("sender_fullname")),
        text=text, ts=parse_date(_clean(raw.get("date"))), reply_id=reply_id, ctx_text=ctx,
        flow_prev=flow_prev, chat_title=_clean(raw.get("chat_title")), sources={source},
    )


# ── building ─────────────────────────────────────────────────────────────────
def chat_ref(chat_id: str) -> str:
    return hashlib.sha1(f"chat:{chat_id}".encode()).hexdigest()[:8]


def pct(sorted_vals: List[int], p: float) -> int:
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def share(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def text_stats(texts: List[str]) -> Dict[str, Any]:
    n = len(texts)
    if not n:
        return {"n": 0}
    words = sorted(len(t.split()) for t in texts)
    chars = sorted(len(t) for t in texts)
    alpha = [t for t in texts if _RE_ALPHA.search(t)]
    emo: Counter = Counter()
    for t in texts:
        emo.update(_RE_EMOJI.findall(t))
    toks: Counter = Counter()
    bigr: Counter = Counter()
    first: Counter = Counter()
    hing = 0
    for t in texts:
        ws = _RE_TOKEN.findall(t.lower())
        ws = [w for w in ws if w not in _STOP_FOR_TOKENS]
        toks.update(ws)
        bigr.update(" ".join(p) for p in zip(ws, ws[1:]))
        if ws:
            first[ws[0]] += 1
        if sum(1 for w in ws if w in HINGLISH_MARKERS) >= 1:
            hing += 1
    return {
        "n": n,
        "words": {"p10": pct(words, .10), "p50": pct(words, .50), "p90": pct(words, .90),
                  "p99": pct(words, .99), "mean": round(sum(words) / n, 2)},
        "chars": {"p50": pct(chars, .50), "p90": pct(chars, .90), "p99": pct(chars, .99)},
        "share_le3_words": share(sum(1 for w in words if w <= 3), n),
        "share_ge15_words": share(sum(1 for w in words if w >= 15), n),
        "share_emoji": share(sum(1 for t in texts if _RE_EMOJI.search(t)), n),
        "share_all_lowercase": share(sum(1 for t in alpha if t == t.lower()), len(alpha)),
        "share_starts_upper": share(sum(1 for t in alpha if t.lstrip()[:1].isupper()), len(alpha)),
        "share_end_punct": share(sum(1 for t in texts if t.rstrip()[-1:] in ".!?"), n),
        "share_question": share(sum(1 for t in texts if "?" in t), n),
        "share_exclaim": share(sum(1 for t in texts if "!" in t), n),
        "share_ellipsis": share(sum(1 for t in texts if "..." in t or "…" in t), n),
        "share_elongation": share(sum(1 for t in texts if _RE_ELONG.search(t)), n),
        "share_devanagari": share(sum(1 for t in texts if _RE_DEVANAGARI.search(t)), n),
        "share_romanised_hindi": share(hing, n),
        "top_emoji": [[e, c] for e, c in emo.most_common(15)],
        "top_tokens": [[w, c] for w, c in toks.most_common(60)],
        "top_bigrams": [[w, c] for w, c in bigr.most_common(30) if c >= 3],
        "top_openers": [[w, c] for w, c in first.most_common(20)],
    }


def replay_guard(rows: List[Dict[str, Any]], blocked_names: "set[str]", abusive: "set[str]") -> "callable":
    """Return ``ok(text) -> bool``: may this own message be replayed verbatim as a canned reply?

    A line is replay-safe only if it (a) carries no placeholder/handle, (b) contains no token
    that is a *member name* from the supplied rosters (unless that token is ordinary vocabulary
    for this person: >= NAME_VOCAB_OVERRIDE uses), and (c) is built only from this person's
    habitual vocabulary (every word used >= HABITUAL_MIN times in their corpus).  Rare words
    are where third-party names and one-off personal details live.
    """
    freq: Counter = Counter()
    for r in rows:
        if r["kind"] == "text":
            freq.update(_RE_TOKEN.findall(r["text"].lower()))
    blocked = {n for n in blocked_names if freq[n] < NAME_VOCAB_OVERRIDE}

    def ok(text: str) -> bool:
        if "<" in text or "@" in text or "\n" in text:
            return False
        toks = _RE_TOKEN.findall(text.lower())
        if any(t in blocked or t in abusive for t in toks):
            return False
        return all(freq[t] >= HABITUAL_MIN for t in toks)

    return ok


def short_reply_table(rows: List[Dict[str, Any]], ok, max_words: int = 8) -> Dict[str, List[List[Any]]]:
    """Class -> [[text, count], ...] built from the person's own short text replies."""
    by_class: Dict[str, Counter] = defaultdict(Counter)
    display: Dict[str, str] = {}
    for r in rows:
        if r["kind"] != "text":
            continue
        t = r["text"]
        if len(t.split()) > max_words or not ok(t):
            continue
        k = norm_key(t)
        display.setdefault(k, t)
        by_class[classify_utterance(t)][k] += 1
    out: Dict[str, List[List[Any]]] = {}
    for cls, ctr in sorted(by_class.items()):
        items = sorted(ctr.items(), key=lambda kv: (-kv[1], kv[0]))
        # replay-safe: said at least twice, or very short (<= 3 words)
        keep = [[display[k], c] for k, c in items if c >= 2 or len(k.split()) <= 3]
        out[cls] = keep[:60]
    return out


def reply_by_context(rows: List[Dict[str, Any]], ok) -> Dict[str, List[List[Any]]]:
    by_ctx: Dict[str, Counter] = defaultdict(Counter)
    display: Dict[str, str] = {}
    for r in rows:
        if r["kind"] != "text" or not r.get("ctx") or r.get("ctx_kind") in (None, "structured", "empty"):
            continue
        t = r["text"]
        if len(t.split()) > 8 or not ok(t):
            continue
        k = norm_key(t)
        display.setdefault(k, t)
        by_ctx[r["ctx_kind"]][k] += 1
    out = {}
    for cls, ctr in sorted(by_ctx.items()):
        items = sorted(ctr.items(), key=lambda kv: (-kv[1], kv[0]))
        out[cls] = [[display[k], c] for k, c in items if c >= 2 or len(k.split()) <= 3][:40]
    return out


def _load_corpus(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def merge_existing(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Union by id.  The old row always wins; it only gains ``src`` entries and ``ctx`` / ``ctx_kind`` / ``reply`` it lacked."""
    by_id: Dict[str, Dict[str, Any]] = {r["id"]: r for r in old_rows}
    added = enriched = text_differs = 0
    for r in new_rows:
        cur = by_id.get(r["id"])
        if cur is None:
            by_id[r["id"]] = r
            added += 1
            continue
        changed = False
        if r.get("text") != cur.get("text"):
            text_differs += 1  # an edited message: the older text is kept
        src = sorted(set(cur.get("src", [])) | set(r.get("src", [])))
        if src != cur.get("src"):
            cur["src"] = src
            changed = True
        for k in ("ctx", "ctx_kind", "reply"):
            if k not in cur and k in r:
                cur[k] = r[k]
                changed = True
        enriched += changed
    present = len(new_rows) - added
    return list(by_id.values()), {
        "existing": len(old_rows), "from_sources": len(new_rows), "added": added, "already_present": present,
        "enriched": enriched, "text_differs_kept_old": text_differs, "only_in_old": len(old_rows) - present,
        "merged_total": len(by_id),
    }


def _merge_identity_evidence(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Keep every (account, username, name) ever observed; for message counts keep the larger of the two."""
    out: Dict[str, Any] = {}
    for section, name_key in (("observed_in_messages", "name"), ("observed_in_rosters", "display")):
        merged: Dict[str, Dict[Tuple[Any, Any], Dict[str, Any]]] = {}
        for src in (old.get(section, {}), new.get(section, {})):
            for aid, entries in src.items():
                bucket = merged.setdefault(aid, {})
                for e in entries:
                    k = (e.get("username"), e.get(name_key))
                    if k not in bucket or e.get("n", 0) > bucket[k].get("n", 0):
                        bucket[k] = e
        out[section] = {aid: [b[k] for k in sorted(b, key=str)] for aid, b in sorted(merged.items())}
    return out


def build(args: argparse.Namespace) -> int:
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    acct_person: Dict[str, str] = {}
    acct_meta: Dict[str, Dict[str, Any]] = {}
    for pid, p in registry["persons"].items():
        for a in p["accounts"]:
            acct_person[str(a["id"])] = pid
            acct_meta[str(a["id"])] = a

    report: Dict[str, Any] = {"sources": {}, "warnings": []}
    messages: Dict[Tuple[str, str], Record] = {}
    roster_evidence: Dict[str, set] = defaultdict(set)
    roster_all: List[Dict[str, Any]] = []
    roster_seen: set = set()

    for src in args.source:
        path = Path(src)
        if not path.exists():
            print(f"error: source not found: {path}", file=sys.stderr)
            return 2
        counts: Counter = Counter()
        for raw in _iter_raw(path):
            kind, obj = normalise(raw, path.name)
            counts[kind] += 1
            if kind == "message":
                key = (obj.chat_id, obj.msg_id)
                if key in messages:
                    messages[key].merge(obj)
                else:
                    messages[key] = obj
            elif kind == "roster":
                rk = (obj["user_id"], obj["username"], obj["display"])
                if rk not in roster_seen:
                    roster_seen.add(rk)
                    roster_all.append(obj)
                if obj["user_id"] in acct_person:
                    roster_evidence[obj["user_id"]].add((obj["username"], obj["display"]))
        report["sources"][path.name] = dict(counts)

    # parent lookup for sources that only carry reply ids
    by_id = {(r.chat_id, r.msg_id): r for r in messages.values()}

    # identity validation against the registry (deterministic; warnings only)
    seen_ident: Dict[str, Counter] = defaultdict(Counter)
    for r in messages.values():
        if r.sender_id in acct_person:
            seen_ident[r.sender_id][(r.username, r.name)] += 1
    for aid, ctr in sorted(seen_ident.items()):
        expect = (acct_meta[aid].get("username") or "").lower()
        for (un, _nm), n in ctr.items():
            if un and expect and un.lower() != expect:
                report["warnings"].append(f"account {aid}: username '{un}' seen {n}x but registry says '{expect}'")
    for aid, pairs in sorted(roster_evidence.items()):
        expect = (acct_meta[aid].get("username") or "").lower()
        for un, _disp in sorted(pairs, key=lambda x: str(x)):
            if un and expect and un.lower() != expect:
                report["warnings"].append(f"roster: account {aid} listed as '{un}', registry says '{expect}'")

    per_person_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in messages.values():
        pid = acct_person.get(r.sender_id)
        if pid is None:
            continue
        raw_text = (r.text or "").strip()
        kind = message_kind(raw_text)
        text = MEDIA_MARKER if kind == "media" else scrub(raw_text, OWN_MAX_CHARS)
        ctx = r.ctx_text
        if not ctx and r.reply_id:
            parent = by_id.get((r.chat_id, r.reply_id))
            if parent and (parent.text or "").strip():
                ctx = parent.text
        if not ctx:
            ctx = r.flow_prev
        ctx_s = scrub(ctx, CTX_MAX_CHARS) if ctx else ""
        row: Dict[str, Any] = {
            "acct": r.sender_id,
            "chat": chat_ref(r.chat_id),
            "id": hashlib.sha1(f"{pid}|{r.chat_id}|{r.msg_id}".encode()).hexdigest()[:12],
            "kind": kind,
            "src": sorted(r.sources),
            "text": text,
            "ts": r.ts,
        }
        if ctx_s:
            row["ctx"] = ctx_s
            row["ctx_kind"] = classify_context(ctx_s)
        if r.reply_id:
            row["reply"] = True
        per_person_rows[pid].append(row)

    # Names/handles of every roster member that is not one of the registered persons.
    own_ids = set(acct_person)
    blocked_names: set = set()
    for r in roster_all:
        if r["user_id"] in own_ids:
            continue
        for field in ("username", "display"):
            for tok in _RE_TOKEN.findall((r.get(field) or "").lower()):
                if len(tok) >= 3:
                    blocked_names.add(tok)
    for p_ in registry["persons"].values():  # registered people's names are not canned vocabulary either
        names = [p_["real_name"], *p_.get("aliases", [])]
        for a_ in p_["accounts"]:
            names += [a_.get("username") or "", *a_.get("display_names", [])]
        for nm in names:
            blocked_names.update(t for t in _RE_TOKEN.findall(nm.lower()) if len(t) >= 3)
    report["blocked_name_tokens"] = len(blocked_names)
    abusive = set(registry.get("replay_filter", {}).get("abusive_tokens", []))

    out_root = Path(args.out)
    for pid in sorted(registry["persons"]):
        rows = sorted(per_person_rows.get(pid, []), key=lambda x: (x["ts"] or "", x["chat"], x["id"]))
        old_ids: set = set()
        old_rp: set = set()
        old_profile: Dict[str, Any] = {}
        merge_stats: Optional[Dict[str, int]] = None
        if args.merge_existing and (out_root / pid / "corpus.jsonl").exists():
            old_rows = _load_corpus(out_root / pid / "corpus.jsonl")
            old_ids = {r["id"] for r in old_rows}
            old_rp = {r["id"] for r in old_rows if r.get("rp")}
            if (out_root / pid / "profile.json").exists():
                old_profile = json.loads((out_root / pid / "profile.json").read_text(encoding="utf-8"))
            rows, merge_stats = merge_existing(old_rows, rows)
            rows.sort(key=lambda x: (x["ts"] or "", x["chat"], x["id"]))
        if not rows:
            report["warnings"].append(f"person '{pid}': no messages found in the supplied sources")
            continue
        texts = [r["text"] for r in rows if r["kind"] == "text"]
        ok = replay_guard(rows, blocked_names, abusive)
        for r in rows:  # "rp": this message may be replayed verbatim / used as a few-shot exemplar
            fresh = r["kind"] == "text" and len(r["text"].split()) <= 8 and ok(r["text"])
            if fresh and (r["id"] not in old_ids or r["id"] in old_rp):  # an old row is never newly granted replay rights
                r["rp"] = 1
            else:
                r.pop("rp", None)
        profile: Dict[str, Any] = {
            "schema": 1,
            "person": pid,
            "real_name": registry["persons"][pid]["real_name"],
            "records": {
                "total": len(rows),
                "by_kind": dict(sorted(Counter(r["kind"] for r in rows).items())),
                "by_account": dict(sorted(Counter(r["acct"] for r in rows).items())),
                "with_reply_context": sum(1 for r in rows if r.get("ctx")),
                "first": rows[0]["ts"], "last": rows[-1]["ts"],
                "chats": len({r["chat"] for r in rows}),
            },
            "style": text_stats(texts),
            "media_share": share(sum(1 for r in rows if r["kind"] == "media"), len(rows)),
            "command_share": share(sum(1 for r in rows if r["kind"] == "command"), len(rows)),
            "reply_share": share(sum(1 for r in rows if r.get("reply")), len(rows)),
            "hour_utc": [sum(1 for r in rows if r["ts"] and int(r["ts"][11:13]) == h) for h in range(24)],
            "short_replies": short_reply_table(rows, ok),
            "reply_by_context": reply_by_context(rows, ok),
            "accounts": {},
            "chats": {},
        }
        for aid in sorted({r["acct"] for r in rows}):
            arows = [r for r in rows if r["acct"] == aid]
            atexts = [r["text"] for r in arows if r["kind"] == "text"]
            profile["accounts"][aid] = {
                "username": acct_meta[aid].get("username"),
                "records": len(arows),
                "media_share": share(sum(1 for r in arows if r["kind"] == "media"), len(arows)),
                "command_share": share(sum(1 for r in arows if r["kind"] == "command"), len(arows)),
                "style": text_stats(atexts),
                "short_replies": short_reply_table(arows, ok),
                "reply_by_context": reply_by_context(arows, ok),
            }
        for cref, n in Counter(r["chat"] for r in rows).most_common(10):
            if n < 50:
                continue
            ctexts = [r["text"] for r in rows if r["chat"] == cref and r["kind"] == "text"]
            st = text_stats(ctexts)
            profile["chats"][cref] = {
                "records": n, "words_p50": st["words"]["p50"], "share_emoji": st["share_emoji"],
                "share_romanised_hindi": st["share_romanised_hindi"],
            }
        profile["identity_evidence"] = {
            "observed_in_messages": {
                aid: [{"username": u, "name": nm, "n": n} for (u, nm), n in sorted(ctr.items(), key=str)]
                for aid, ctr in sorted(seen_ident.items()) if acct_person[aid] == pid
            },
            "observed_in_rosters": {
                aid: [{"username": u, "display": d} for u, d in sorted(pairs, key=str)]
                for aid, pairs in sorted(roster_evidence.items()) if acct_person[aid] == pid
            },
        }

        if old_profile.get("identity_evidence"):
            profile["identity_evidence"] = _merge_identity_evidence(old_profile["identity_evidence"], profile["identity_evidence"])
        report.setdefault("persons", {})[pid] = {
            "records": profile["records"]["total"], "by_kind": profile["records"]["by_kind"],
            "by_account": profile["records"]["by_account"],
        }
        if merge_stats is not None:
            report["persons"][pid]["merge"] = merge_stats
        if args.dry_run:
            continue
        pdir = out_root / pid
        pdir.mkdir(parents=True, exist_ok=True)
        with (pdir / "corpus.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        (pdir / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        write_skins(pid, registry["persons"][pid], profile, Path(args.skins_dir))

    report["messages_total"] = len(messages)
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "build_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


# ── skins ────────────────────────────────────────────────────────────────────
_SKIN_CLASSES = {
    # table class -> skin direct_responses key understood by the trigger engine
    "greeting": "greeting", "goodbye": "goodbye", "thanks": "thanks",
    "agree": "casual_agree", "laughter": "happy",
}

DISCLOSURE = (
    "Imitation notice: this skin imitates the chat style of a real person, built from chat "
    "exports supplied to the bot owner. Replies are generated by a bot — they are not written "
    "by {name}, and must not be presented as if they were. {name} can opt out at any time with /pconsent optout."
)


def _pool(table: Dict[str, List[List[Any]]], cls: str, limit: int = 40) -> List[str]:
    return [t for t, _c in table.get(cls, [])][:limit]


def write_skins(pid: str, person: Dict[str, Any], profile: Dict[str, Any], skins_dir: Path) -> None:
    name = person["real_name"]
    st = profile["style"]
    max_len = max(60, min(400, st["chars"]["p99"] + 20)) if st.get("n") else 160

    def make(skin_name: str, table: Dict[str, List[List[Any]]], style: Dict[str, Any], acct: Optional[str]) -> Dict[str, Any]:
        direct: Dict[str, List[str]] = {}
        for cls, key in _SKIN_CLASSES.items():
            pool = _pool(table, cls)
            if pool:
                direct[key] = pool
        general = _pool(table, "other", 60) + _pool(table, "agree", 20) + _pool(table, "question", 20)
        direct["general"] = general or ["..."]
        direct["short_message"] = general[:30] or ["..."]
        desc = f"Chat-style persona of {name}, derived from {style.get('n', 0)} of their messages."
        if acct:
            desc = f"{name} as seen on account {acct} ({style.get('n', 0)} messages)."
        return {
            "name": skin_name,
            "description": desc,
            "persona_kind": "person",
            "person_id": pid,
            "variant_account": acct,
            "disclosure": DISCLOSURE.format(name=name),
            "default_config": {
                "sarcasm_level": 0.0, "metaphor_frequency": 0.0, "exaggeration_level": 0.0,
                "deadpan": False, "programmer_humor": 0.0, "anime_references": 0.0,
                "max_response_length": max_len,
            },
            "direct_responses": direct,
        }

    skins_dir.mkdir(parents=True, exist_ok=True)
    base = make(person["skin"], profile["short_replies"], st, None)
    (skins_dir / f"{person['skin']}.json").write_text(
        json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for a in person["accounts"]:
        vs = a.get("variant_skin")
        aid = str(a["id"])
        if not vs or aid not in profile["accounts"] or len(person["accounts"]) < 2:
            continue
        ap = profile["accounts"][aid]
        skin = make(vs, ap["short_replies"], ap["style"], aid)
        (skins_dir / f"{vs}.json").write_text(json.dumps(skin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", required=True, help="raw export (repeatable)")
    ap.add_argument("--registry", default=str(DEFAULT_OUT / "registry.json"))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--skins-dir", default=str(DEFAULT_SKINS))
    ap.add_argument("--merge-existing", action="store_true", help="union with the existing corpora instead of rebuilding them (nothing old is dropped)")
    ap.add_argument("--dry-run", action="store_true")
    return build(ap.parse_args(list(argv) if argv is not None else None))


if __name__ == "__main__":
    sys.exit(main())
