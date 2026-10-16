#!/usr/bin/env python3
"""Raw chat exports -> private per-person supervised fine-tuning data (train / val / test + manifest).

    python -m training.prepare_sft --source <export> [--source <export> ...] --out training/_private/data

What it does
  * Reads the same export formats as scripts/build_person_personas.py (jsonl / json / csv / sqlite) and
    merges duplicate rows of the same message across files, so nothing is counted twice or lost.
  * Resolves every message to a person by NUMERIC ACCOUNT ID only (data/personality/persons/registry.json).
    Display names are never used, and one person's messages never enter another person's files.
  * Keeps conversation structure: each example is "recent transcript (+ reply target) -> the person's
    actual next message".  Other participants are pseudonymised (u1, u2 ...); contact data in text is
    scrubbed with the same code the live bot uses.
  * Splits by CONVERSATION (never by message), so a test example's context cannot contain training
    targets; flags test/val targets that duplicate a training target instead of silently dropping them.
  * Records every exclusion with a reason and a count in manifest.json; nothing is dropped silently.

The output holds private chat content.  It is written under training/_private/ (git- and docker-ignored)
and must never be added to a release archive.  manifest.json / prepare_report.json carry only counts and
hashes and are safe to share.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):  # allow "python training/prepare_sft.py"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402
from training.textsim import NearDupIndex, normalize  # noqa: E402

DEFAULT_OUT = common.ROOT / "training" / "_private" / "data"
CODE_FILES = (
    "training/prepare_sft.py", "training/common.py", "training/textsim.py",
    "bot/personality/persona_sft_format.py", "bot/personality/persona_text.py",
    "scripts/build_person_personas.py",
)
DEFAULT_CONFIG: Dict[str, Any] = {
    "seed": 1337,
    "split": {"train": 0.80, "val": 0.10, "test": 0.10},
    "conv_gap_minutes": 30,
    "max_ctx_turns": 6,
    "target_max_chars": 600,
    "max_dup_train": 25,          # identical normalised replies kept per person in train (frequency stays visible)
    "near_dup_jaccard": 0.8,
    "near_dup_min_tokens": 8,
    "exclude_abusive": True,      # registry replay_filter.abusive_tokens; edit that list to change what counts
    "exclude_third_party_names": True,
    "name_vocab_override": 60,    # a roster-name token used this often by the person is ordinary vocabulary
    "min_val_examples": 50,
}
PLACEHOLDER = re.compile(r"<(?:url|user|num|email)>|@user\b")
ID_TOKEN = re.compile(r"-?\d+")


# ── loading ──────────────────────────────────────────────────────────────────
class Loaded:
    def __init__(self) -> None:
        self.messages: Dict[Tuple[str, str], Any] = {}
        self.flows: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.roster: List[Dict[str, Any]] = []
        self.sources: List[Dict[str, Any]] = []


def load_sources(paths: Iterable[Path], builder: Any) -> Loaded:
    out = Loaded()
    roster_seen = set()
    resolved = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            raise common.TrainingDataError(f"source not found: {path}")
        resolved.append(path)
    # Merging is order-sensitive (first non-empty field wins), so fix the order by content, not by CLI order.
    for path in sorted(resolved, key=lambda q: (q.name, common.sha256_file(q))):
        counts: Counter = Counter()
        for raw in builder._iter_raw(path):
            kind, obj = builder.normalise(raw, path.name)
            counts[kind] += 1
            if kind == "message":
                key = (obj.chat_id, obj.msg_id)
                if key in out.messages:
                    out.messages[key].merge(obj)
                else:
                    out.messages[key] = obj
                flow = raw.get("local_chat_flow")
                if flow and key not in out.flows:
                    try:
                        items = json.loads(flow) if isinstance(flow, str) else flow
                        out.flows[key] = [i for i in items if isinstance(i, dict)]
                    except (ValueError, TypeError):
                        counts["flow_unparsed"] += 1
            elif kind == "roster":
                rk = (obj["user_id"], obj["username"], obj["display"])
                if rk not in roster_seen:
                    roster_seen.add(rk)
                    out.roster.append(obj)
        out.sources.append({
            "name": path.name, "bytes": path.stat().st_size,
            "sha256": common.sha256_file(path), "rows": dict(sorted(counts.items())),
        })
    return out


def _mid(r: Any) -> int:
    return int(r.msg_id) if ID_TOKEN.fullmatch(str(r.msg_id)) else 0


def _epoch(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        import calendar
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def split_conversations(rows: List[Any], gap_seconds: int) -> List[List[Any]]:
    """Split one chat's rows (already ordered by message id) at gaps longer than ``gap_seconds``."""
    convs: List[List[Any]] = []
    cur: List[Any] = []
    last: Optional[int] = None
    for r in rows:
        t = _epoch(r.ts)
        if cur and t is not None and last is not None and t - last > gap_seconds:
            convs.append(cur)
            cur = []
        cur.append(r)
        if t is not None:
            last = t
    if cur:
        convs.append(cur)
    return convs


def blocked_name_tokens(roster: List[Dict[str, Any]], registry: Dict[str, Any], by_id: Dict[str, str], tokre: "re.Pattern[str]") -> set:
    """Tokens that name a chat member or a registered person; the model must not learn to emit them."""
    blocked: set = set()
    for r in roster:
        if r["user_id"] in by_id:
            continue
        for field in ("username", "display"):
            blocked.update(t for t in tokre.findall((r.get(field) or "").lower()) if len(t) >= 3)
    for p in registry["persons"].values():
        names = [p["real_name"], *p.get("aliases", [])]
        for a in p["accounts"]:
            names += [a.get("username") or "", *a.get("display_names", [])]
        for nm in names:
            blocked.update(t for t in tokre.findall(nm.lower()) if len(t) >= 3)
    return blocked


# ── example construction ─────────────────────────────────────────────────────
class _Speakers:
    """Per-conversation pseudonyms: the modelled person is 'me', everyone else u1, u2 ... by first appearance."""

    def __init__(self, pid: str, by_id: Dict[str, str], by_user: Dict[str, str]) -> None:
        self.pid, self.by_id, self.by_user = pid, by_id, by_user
        self._map: Dict[str, str] = {}

    def label(self, sender: str) -> str:
        s = str(sender)
        if self.by_id.get(s) == self.pid or self.by_user.get(s.lower()) == self.pid:
            return "me"
        if s not in self._map:
            self._map[s] = f"u{len(self._map) + 1}"
        return self._map[s]


def _turn_text(scrub, raw: Optional[str], media: str, limit: int) -> str:
    t = (raw or "").strip()
    if not t or t.lower() == media.lower():
        return ""
    return scrub(t, limit)


def make_example(
    pid: str, conv: List[Any], idx: int, conv_id: str, chat_id: str, cfg: Dict[str, Any],
    ld: Loaded, by_id: Dict[str, str], by_user: Dict[str, str], tag: Dict[str, str],
    tm: Any, fmt: Any, target: str, tk: str,
) -> Dict[str, Any]:
    r = conv[idx]
    spk = _Speakers(pid, by_id, by_user)
    k = int(cfg["max_ctx_turns"])
    prior: Dict[int, Tuple[str, str]] = {}
    for j in range(max(0, idx - k - 2), idx):
        rr = conv[j]
        prior[_mid(rr)] = (rr.sender_id, rr.text or "")
    mid = _mid(r)
    for it in ld.flows.get((r.chat_id, r.msg_id), ()):
        iid = str(it.get("id", ""))
        if not (ID_TOKEN.fullmatch(iid) and int(iid) < mid and int(iid) not in prior):
            continue
        rec = ld.messages.get((r.chat_id, iid))
        if rec is not None:  # who wrote it is decided by the numeric id on the real record, never by the flow's display name
            prior[int(iid)] = (rec.sender_id, rec.text or "")
        else:
            prior[int(iid)] = (str(it.get("sender", "")), str(it.get("text") or ""))
    ctx = []
    for _id, (sender, text) in sorted(prior.items())[-k:]:
        ctx.append([spk.label(sender), _turn_text(tm.scrub, text, tm.MEDIA_MARKER, fmt.TURN_MAX_CHARS)])
    reply_to = None
    parent_text = r.ctx_text
    parent_sender = None
    if r.reply_id:
        parent = ld.messages.get((r.chat_id, r.reply_id))
        if parent is not None:
            parent_sender = parent.sender_id
            parent_text = parent_text or parent.text
    if parent_text or parent_sender:
        who = spk.label(parent_sender) if parent_sender else spk.label(f"unknown-parent:{r.reply_id or idx}")
        reply_to = [who, _turn_text(tm.scrub, parent_text, tm.MEDIA_MARKER, fmt.TURN_MAX_CHARS)]
    lat = None  # seconds since the previous message in the same conversation (any sender), when both timestamps exist
    if idx > 0:
        t1, t0 = _epoch(r.ts), _epoch(conv[idx - 1].ts)
        if t1 is not None and t0 is not None and t1 >= t0:
            lat = t1 - t0
    flags = []
    if reply_to:
        flags.append("reply")
    if not any(w != "me" for w, _ in ctx) and not (reply_to and reply_to[0] != "me"):
        flags.append("no_other_ctx")
    return {
        "id": hashlib.sha1(f"{pid}|{r.chat_id}|{r.msg_id}".encode()).hexdigest()[:12],
        "conv": conv_id, "acct": tag[r.sender_id],
        "chat_kind": "group" if str(chat_id).startswith("-") else "dm",
        "ctx": ctx, "reply_to": reply_to, "target": target, "tk": tk, "ts": r.ts, "flags": flags,
        "chat": hashlib.sha1(f"chat:{chat_id}".encode()).hexdigest()[:8],  # same one-way reference as the shipped corpus
        "lat": lat,
    }


def assign_splits(sizes: Dict[str, int], seed: int, pid: str, ratios: Dict[str, float]) -> Dict[str, str]:
    """Assign whole conversations to splits so each split lands close to its target share of EXAMPLES.

    Conversations are ordered by a seeded hash and handed to test, then val, until each quota is met;
    the rest is train.  Hash ordering keeps the result deterministic and independent of file order, and
    a few very long conversations can overshoot a quota by at most one conversation.
    """
    total = sum(sizes.values())
    quota = {s: ratios.get(s, 0.0) * total for s in ("test", "val")}
    filled = {"test": 0, "val": 0}
    out: Dict[str, str] = {}
    for conv in sorted(sizes, key=lambda c: hashlib.sha256(f"{seed}|{pid}|{c}".encode()).hexdigest()):
        for s in ("test", "val"):
            if quota[s] > 0 and filled[s] < quota[s]:
                out[conv] = s
                filled[s] += sizes[conv]
                break
        else:
            out[conv] = "train"
    return out


def _quant(vals: List[int]) -> Dict[str, Any]:
    if not vals:
        return {}
    s = sorted(vals)
    q = lambda p: s[min(len(s) - 1, int(p * (len(s) - 1)))]  # noqa: E731
    return {"n": len(s), "p10": q(.1), "p50": q(.5), "p90": q(.9), "max": s[-1], "mean": round(sum(s) / len(s), 2)}


def prepare(
    ld: Loaded, registry: Dict[str, Any], cfg: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, List[Dict[str, Any]]]], Dict[str, Any]]:
    """Return ({person: {split: [examples]}}, per-person accounting).  Pure: writes nothing."""
    tm, fmt, builder = common.text_module(), common.fmt_module(), common.load_module("build_person_personas", "scripts/build_person_personas.py")
    by_id, tag, by_user = common.account_index(registry)
    ratios = cfg["split"]
    if abs(sum(ratios.values()) - 1.0) > 1e-6 or any(v < 0 for v in ratios.values()):
        raise common.TrainingDataError(f"split ratios must be non-negative and sum to 1: {ratios}")
    abusive = set(registry.get("replay_filter", {}).get("abusive_tokens", [])) if cfg["exclude_abusive"] else set()
    blocked_all = blocked_name_tokens(ld.roster, registry, by_id, builder._RE_TOKEN) if cfg["exclude_third_party_names"] else set()

    chats: Dict[str, List[Any]] = defaultdict(list)
    for r in ld.messages.values():
        chats[r.chat_id].append(r)
    gap = int(cfg["conv_gap_minutes"]) * 60

    # token frequency of each person's own scrubbed text (ordinary vocabulary is not a "name")
    freq: Dict[str, Counter] = defaultdict(Counter)
    for r in ld.messages.values():
        pid = by_id.get(r.sender_id)
        if pid and tm.message_kind((r.text or "").strip()) == "text":
            freq[pid].update(tm.tokens(tm.scrub(r.text, 10**6)))

    raw_examples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    acct: Dict[str, Dict[str, Any]] = {p: {"messages": 0, "excluded": Counter(), "by_account": Counter()} for p in registry["persons"]}
    for chat_id, rows in chats.items():
        rows.sort(key=lambda r: (_mid(r), str(r.msg_id)))
        for conv in split_conversations(rows, gap):
            conv_id = hashlib.sha1(f"{chat_id}|{conv[0].msg_id}".encode()).hexdigest()[:10]
            for idx, r in enumerate(conv):
                pid = by_id.get(r.sender_id)
                if pid is None:
                    continue
                a = acct[pid]
                a["messages"] += 1
                a["by_account"][r.sender_id] += 1
                raw_text = (r.text or "").strip()
                kind = tm.message_kind(raw_text)
                if kind in ("media", "command"):
                    a["excluded"][f"target_{kind}"] += 1
                    continue
                target = tm.scrub(raw_text, 10**6)
                if not target:
                    a["excluded"]["target_empty"] += 1
                elif len(target) > int(cfg["target_max_chars"]):
                    a["excluded"]["target_too_long"] += 1
                elif PLACEHOLDER.search(target):
                    a["excluded"]["target_has_contact_placeholder"] += 1
                else:
                    toks = tm.tokens(target)
                    if abusive and any(t in abusive for t in toks):
                        a["excluded"]["target_abusive_token"] += 1
                    elif blocked_all and any(t in blocked_all and freq[pid][t] < int(cfg["name_vocab_override"]) for t in toks):
                        a["excluded"]["target_third_party_name"] += 1
                    else:
                        raw_examples[pid].append(make_example(pid, conv, idx, conv_id, chat_id, cfg, ld, by_id, by_user, tag, tm, fmt, target, kind))

    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for pid in sorted(registry["persons"]):
        sp: Dict[str, List[Dict[str, Any]]] = {"train": [], "val": [], "test": []}
        sizes = Counter(ex["conv"] for ex in raw_examples.get(pid, []))
        where = assign_splits(dict(sizes), int(cfg["seed"]), pid, ratios)
        for ex in raw_examples.get(pid, []):
            ex["split"] = where[ex["conv"]]
            sp[ex["split"]].append(ex)
        # near/exact duplicates of training targets are FLAGGED on val/test (evaluate.py reports a "novel" subset)
        train_idx = NearDupIndex((e["target"] for e in sp["train"]), cfg["near_dup_jaccard"], cfg["near_dup_min_tokens"])
        for s in ("val", "test"):
            for ex in sp[s]:
                if train_idx.exact(ex["target"]):
                    ex["flags"].append("dup_exact_train")
                elif train_idx.near(ex["target"]):
                    ex["flags"].append("dup_near_train")
        # cap identical replies in train (keeps frequency signal, stops "hmm" x 400 from dominating)
        cap = int(cfg["max_dup_train"])
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for ex in sp["train"]:
            groups[normalize(ex["target"])].append(ex)
        kept, dropped = [], 0
        for key, exs in groups.items():
            exs.sort(key=lambda e: hashlib.sha1(f"{cfg['seed']}|{e['id']}".encode()).hexdigest())
            kept.extend(exs[:cap])
            dropped += max(0, len(exs) - cap)
        sp["train"] = kept
        acct[pid]["excluded"]["train_capped_duplicate"] = dropped
        acct[pid]["capped_groups"] = sum(1 for g in groups.values() if len(g) > cap)
        for s in sp:
            sp[s].sort(key=lambda e: (e["ts"] or "", e["id"]))
        out[pid] = sp
    return out, acct


def check_splits(per_split: Dict[str, List[Dict[str, Any]]]) -> Dict[str, bool]:
    """Leakage self-check: ids and conversations are disjoint across splits."""
    ids = {s: {e["id"] for e in v} for s, v in per_split.items()}
    convs = {s: {e["conv"] for e in v} for s, v in per_split.items()}
    names = list(per_split)
    return {
        "ids_disjoint": all(not (ids[a] & ids[b]) for i, a in enumerate(names) for b in names[i + 1:]),
        "conversations_disjoint": all(not (convs[a] & convs[b]) for i, a in enumerate(names) for b in names[i + 1:]),
    }


def summarize(per_split: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    for s, exs in per_split.items():
        flags = Counter(f for e in exs for f in e["flags"])
        stats[s] = {
            "examples": len(exs),
            "conversations": len({e["conv"] for e in exs}),
            "by_account": dict(sorted(Counter(e["acct"] for e in exs).items())),
            "by_kind": dict(sorted(Counter(e["tk"] for e in exs).items())),
            "by_chat_kind": dict(sorted(Counter(e["chat_kind"] for e in exs).items())),
            "flags": dict(sorted(flags.items())),
            "target_words": _quant([len(e["target"].split()) for e in exs]),
            "ctx_turns": _quant([len(e["ctx"]) for e in exs]),
        }
    return stats


def run(args: argparse.Namespace) -> int:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config:
        cfg.update(json.loads(Path(args.config).read_text(encoding="utf-8")))
    if args.seed is not None:
        cfg["seed"] = args.seed
    registry = common.load_registry(Path(args.registry))
    builder = common.load_module("build_person_personas", "scripts/build_person_personas.py")
    ld = load_sources([Path(s) for s in args.source], builder)
    per_person, acct = prepare(ld, registry, cfg)
    out_root = Path(args.out)
    report: Dict[str, Any] = {"schema": common.MANIFEST_SCHEMA, "persons": {}, "sources": ld.sources}
    for pid, sp in per_person.items():
        checks = check_splits(sp)
        if not all(checks.values()):
            raise common.TrainingDataError(f"{pid}: split leakage check failed: {checks}")
        stats = summarize(sp)
        warnings = []
        if stats["val"]["examples"] < int(cfg["min_val_examples"]):
            warnings.append(f"validation split has {stats['val']['examples']} examples (< {cfg['min_val_examples']}); training will refuse")
        if not sp["train"]:
            warnings.append("no training examples")
        entry: Dict[str, Any] = {
            "messages_from_registered_accounts": acct[pid]["messages"],
            "by_account": dict(sorted(acct[pid]["by_account"].items())),
            "excluded": dict(sorted(acct[pid]["excluded"].items())),
            "capped_duplicate_groups": acct[pid].get("capped_groups", 0),
            "splits": stats, "checks": checks, "warnings": warnings,
        }
        entry["split_hashes"] = {s: {"sha256": common.sha256_bytes(common.jsonl_bytes(sp[s]))} for s in ("train", "val", "test")}
        report["persons"][pid] = entry
        if args.dry_run:
            continue
        d = out_root / pid
        splits_meta, h = {}, __import__("hashlib").sha256()
        for s in ("train", "val", "test"):
            n, digest = common.write_jsonl(d / f"{s}.jsonl", sp[s])
            splits_meta[s] = {"examples": n, "sha256": digest}
            h.update(digest.encode())
        manifest = {
            "schema": common.MANIFEST_SCHEMA, "person": pid, "format_version": common.fmt_module().FORMAT_VERSION,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config": cfg, "code_sha256": common.code_hash(CODE_FILES),
            "registry_sha256": common.sha256_file(Path(args.registry)),
            "sources": ld.sources, "splits": splits_meta, "dataset_sha256": h.hexdigest(),
            "accounting": entry, "contains_private_data": True, "distributable": False,
        }
        common.atomic_write_json(d / "manifest.json", manifest)
        if args.write_reference_manifests:
            safe = json.loads(json.dumps(manifest))
            safe.update(contains_private_data=False, distributable=True,
                        note="Hash-only description of a private dataset: counts and sha256 values, no message text.")
            ref_file = Path(args.write_reference_manifests) / f"{pid}.json"
            common.atomic_write_json(ref_file, safe)
            ref_file.chmod(0o644)  # shareable, unlike the 0600 private data files
    if not args.dry_run:
        common.atomic_write_json(out_root / "prepare_report.json", report)
    rc = 0
    if args.reference_manifests:
        for pid, entry in report["persons"].items():
            ref_path = Path(args.reference_manifests) / f"{pid}.json"
            if not ref_path.exists():
                print(f"reference: {pid}: no reference manifest")
                continue
            ref = json.loads(ref_path.read_text(encoding="utf-8"))
            mine = {s: v["sha256"] for s, v in entry.get("split_hashes", {}).items()} if "split_hashes" in entry else None
            same = mine is not None and mine == {s: v["sha256"] for s, v in ref["splits"].items()}
            print(f"reference: {pid}: {'MATCH - identical dataset' if same else 'DIFFERENT - raw data, registry, config or code changed'}")
            rc = rc or (0 if same else 3)
    print(json.dumps({p: {"excluded": e["excluded"], "train": e["splits"]["train"]["examples"], "val": e["splits"]["val"]["examples"],
                          "test": e["splits"]["test"]["examples"], "warnings": e["warnings"]} for p, e in report["persons"].items()}, indent=1))
    return rc


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", required=True, help="raw export (repeatable)")
    ap.add_argument("--registry", default=str(common.DEFAULT_REGISTRY))
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="private output directory (default: training/_private/data)")
    ap.add_argument("--config", help="JSON file overriding DEFAULT_CONFIG keys")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dry-run", action="store_true", help="compute and print accounting without writing files")
    ap.add_argument("--write-reference-manifests", metavar="DIR",
                    help="write hash-only manifests (no message text; safe to commit) to DIR, e.g. training/reference_manifests")
    ap.add_argument("--reference-manifests", help="directory of <person>.json hash-only manifests (training/reference_manifests); "
                    "reports whether this build reproduces the dataset they describe")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        return run(args)
    except common.TrainingDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
