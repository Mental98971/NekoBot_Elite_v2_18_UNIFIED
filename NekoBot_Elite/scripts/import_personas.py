#!/usr/bin/env python3
"""Import persona packs into ``data/personality/presets/`` — safely.

Why this exists
---------------
Persona packs (``character_styles_wave*`` / ``missing_personalities_expansion*``)
arrive as bundles of one-JSON-per-persona files.  They use the same schema as
this bot's trait presets, but shipping them as-is is unsafe:

* the same id is re-issued in several waves with different content;
* keyword lists are full of category words ("historical", "animal", a franchise
  tag shared by 29 characters) that would make the keyword matcher in
  ``bot/personality/custom_instructions.py`` pick an arbitrary persona for
  ordinary instruction text, and could out-score an existing core preset;
* a few personas are unsuitable for a public multi-community bot (real,
  living people; harm-glorifying archetypes; minor-coded generic archetypes).

This tool applies one deterministic set of rules so the result is reproducible
and re-runnable when new waves show up.  It NEVER overwrites a preset that is
not listed in the manifest it wrote earlier (i.e. an existing core preset).

Usage::

    python scripts/import_personas.py --source /path/to/unzipped/packs [--dry-run]

``--source`` may be given several times and is searched recursively for
``personalities/*.json``.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = PROJECT_ROOT / "data" / "personality" / "presets"
MANIFEST_PATH = PROJECT_ROOT / "data" / "personality" / "catalog_manifest.json"

#: A keyword shared by more than this many personas is a category tag, not an
#: identifier, and is dropped (unless it is the persona's own name/id phrase).
MAX_KEYWORD_DF = 3
MIN_KEYWORD_LEN = 3
#: Zipf frequency (wordfreq, English; ~1-2 = very rare .. ~7 = "the").
#: A persona's *own* id/name phrase is always kept — that is what identifies it.
#: Name tokens (e.g. "rick") are kept below this ceiling, which admits given
#: names but rejects everyday words that happen to sit in a name ("light", "king").
NAME_TOKEN_MAX_ZIPF = 4.6
#: Any other source keyword must be genuinely rare (or a phrase containing a rare
#: word).  "blunt", "fight", "calm", "energy", "time" are ordinary tone words users
#: type in custom instructions; they must never select a character persona.
SOURCE_KEYWORD_MAX_ZIPF = 3.0

_ZIPF = None


def zipf(word: str) -> float:
    """English Zipf frequency of ``word`` (wordfreq).  Required, so output is reproducible."""
    global _ZIPF
    if _ZIPF is None:
        try:
            from wordfreq import zipf_frequency  # type: ignore
        except ImportError:
            raise SystemExit(
                "import_personas: needs the dev-only 'wordfreq' package to tell names from "
                "everyday words.\n  pip install wordfreq"
            )
        _ZIPF = zipf_frequency
    return _ZIPF(word, "en")

# ── Exclusions ───────────────────────────────────────────────────────────────
# Whole packs that are excluded, matched against the pack directory name.
EXCLUDED_PACKS: Dict[str, str] = {
    "character_styles_wave39": "real, living streamers/performers (VTuber wave)",
}
# Individual persona ids that are excluded, with the reason.
EXCLUDED_IDS: Dict[str, str] = {
    # First-person voices of living real people.
    "bill_gates": "living real person",
    "messi": "living real person",
    "neymar": "living real person",
    "ronaldo": "living real person",
    "michael_jordan": "living real person",
    # Central figures of a faith, speaking in the first person.
    "jesus_christ": "religious founder / central figure",
    "muhammad": "religious founder / central figure",
    "gautama_buddha": "religious founder / central figure",
    # Perpetrator of genocide.
    "adolf_hitler": "perpetrator of genocide",
    # Harm-glorifying / manipulation-coded archetypes.
    "cult_leader": "manipulation-coded archetype",
    "cultist": "manipulation-coded archetype",
    "serial_killer": "harm-glorifying archetype",
    # Source material pairs a "predatory"/"aroused by" framing with a
    # directly-addressed, canonically-preteen named character in its own
    # example lines — declined regardless of the character's mainstream
    # popularity, on the same standard as this project's earlier /cosplay
    # decline: a feature or character being popular elsewhere doesn't make
    # shipping it here fine.
    "hisoka": "predatory framing directed at a named minor character",
    # Minor-coded generic archetypes (no character value, easy to misuse).
    "child": "minor-coded generic archetype",
    "toddler": "minor-coded generic archetype",
    "preteen": "minor-coded generic archetype",
    "teenager": "minor-coded generic archetype",
}

# ── Keyword hygiene ──────────────────────────────────────────────────────────
STOPWORDS: Set[str] = set(
    """a an and are as at be but by for from has have he her his how i if in into is it its
    me my no not of on or our she so than that the their them then there these they this
    to too up us was we were what when where which who why will with you your just like
    very can all any one more most some such only own same both each few other over under
    again further once here while about above after before below between during out off
    being does did doing would could should also them their whom those been because until
    against through""".split()
)
#: Words that describe a *kind* of persona rather than identify one.
GENERIC_WORDS: Set[str] = set(
    """animal historical role archetype type trope meta music art sport athlete political
    ideology religious faith spiritual movement discipline character characters persona
    personality style voice anime manga manhwa manhua novel series show movie film game
    comic comics story hero villain protagonist antagonist""".split()
)

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
_SAFE_ID = re.compile(r"^[a-z0-9_]+$")

#: Terms with no legitimate reframing in a persistent, open-ended chatbot
#: persona: a source character being *built around* one of these (even when
#: the surrounding text is non-explicit, e.g. "ex-legend vampire in loli
#: form") is excluded outright, not text-substituted and kept — swapping the
#: word but shipping the persona is exactly the kind of reframing-to-make-it-
#: seem-safer this project avoids. Checked against name/description/context/
#: examples/openers/closers, so it also catches personas added by later packs
#: this script has never seen.
AUTO_EXCLUDE_TERM_PATTERN = re.compile(r"\b(loli\w*|shota\w*)\b", re.IGNORECASE)
#: An id containing this substring is excluded outright, regardless of how
#: benign the actual (fictional, unrelated) content is: this keyword system
#: matches a user\'s freeform text, and a persona whose own id contains this
#: word must never be reachable by someone typing it in genuine distress.
AUTO_EXCLUDE_ID_SUBSTRINGS = ("suicide",)

#: Canonical key order for stable, diff-friendly files.
KEY_ORDER = [
    "name", "description", "keywords", "sarcasm_level", "metaphor_frequency",
    "exaggeration_level", "deadpan", "deadpan_safe", "programmer_humor",
    "anime_references", "max_response_length", "persona_context",
    "themed_openers", "themed_closers", "themed_prefixes", "themed_metaphors",
    "example_responses", "response_style_notes", "emoji_pool",
]


def norm_keyword(raw: Any) -> str:
    """Lower-case, turn ``_``/``-`` into spaces, strip punctuation, collapse spaces."""
    if not isinstance(raw, str):
        return ""
    s = raw.lower().replace("_", " ").replace("-", " ")
    s = _NON_WORD.sub("", s)
    return _WS.sub(" ", s).strip()


def keyword_ok(kw: str) -> bool:
    if len(kw) < MIN_KEYWORD_LEN or kw.isdigit():
        return False
    words = kw.split()
    if all(w in STOPWORDS for w in words):
        return False
    return kw not in GENERIC_WORDS and kw not in STOPWORDS


def clean_text(s: Any) -> str:
    """Tidy persona prose. Content safety is handled by exclusion, not rewriting."""
    if not isinstance(s, str):
        return ""
    return _WS.sub(" ", s).strip()


def auto_exclude_reason(pid: str, copies: List[Dict[str, Any]]) -> Optional[str]:
    """Return an exclusion reason if ``pid`` or any copy's text trips a safety rule."""
    if any(sub in pid for sub in AUTO_EXCLUDE_ID_SUBSTRINGS):
        return f"id contains {AUTO_EXCLUDE_ID_SUBSTRINGS!r} — kept off the ambient keyword surface"
    for d in copies:
        blob = " ".join([
            str(d.get("name", "")), str(d.get("description", "")), str(d.get("persona_context", "")),
            str(d.get("response_style_notes", "")),
            " ".join(d.get("example_responses", []) or []),
            " ".join(d.get("themed_openers", []) or []),
            " ".join(d.get("themed_closers", []) or []),
        ])
        m = AUTO_EXCLUDE_TERM_PATTERN.search(blob)
        if m:
            return f"contains a child-sexualization-coded term ({m.group(0).lower()!r})"
    return None


def clean_list(items: Any) -> List[str]:
    out: List[str] = []
    for it in items or []:
        t = clean_text(it)
        if t and t not in out:
            out.append(t)
    return out


def richness(d: Dict[str, Any]) -> int:
    """Quality score used to choose between re-issued copies of one persona."""
    return (
        len(d.get("persona_context", "") or "")
        + 25 * len([x for x in d.get("themed_openers", []) if x])
        + 25 * len([x for x in d.get("themed_closers", []) if x])
        + 10 * len(d.get("example_responses", []) or [])
    )


def load_sources(sources: Iterable[Path]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
    """id -> [(pack, data), ...] in deterministic (pack, path) order."""
    found: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    paths: List[Path] = []
    for root in sources:
        for dirpath, _dirs, files in os.walk(root, followlinks=True):
            if Path(dirpath).name == "personalities":
                paths.extend(Path(dirpath) / f for f in files if f.endswith(".json"))
    for path in sorted(paths):
        pack = path.parent.parent.name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ! unreadable {path}: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            found[path.stem.lower()].append((pack, data))
    return found


def _wave_sort_key(pack: str) -> Tuple[int, str]:
    m = re.search(r"(\d+)$", pack)
    return (int(m.group(1)) if m else 0, pack)


def _builtin_default_presets() -> Dict[str, Dict[str, Any]]:
    """Read ``_DEFAULT_PRESETS`` from the bot's source without importing the bot.

    Importing ``bot.personality`` would load settings, the database layer and
    the whole personality stack (and log at import time); a literal-eval of the
    assignment gives the same data deterministically in any environment.
    """
    src = PROJECT_ROOT / "bot" / "personality" / "custom_instructions.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == "_DEFAULT_PRESETS":
            return ast.literal_eval(node.value)  # type: ignore[arg-type]
    raise SystemExit(f"import_personas: _DEFAULT_PRESETS not found in {src}")


def core_vocabulary(presets_dir: Path, previously_imported: Set[str]) -> Tuple[Set[str], Dict[str, str]]:
    """(core preset names, {keyword: core preset that owns it})."""
    names: Set[str] = set()
    vocab: Dict[str, str] = {}
    for path in sorted(presets_dir.glob("*.json")):
        if path.stem in previously_imported:
            continue
        names.add(path.stem)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for kw in data.get("keywords", []) or []:
            vocab.setdefault(norm_keyword(kw), path.stem)
    # Built-in defaults the loader falls back to when a file is missing.
    for name, data in _builtin_default_presets().items():
        names.add(name)
        for kw in data.get("keywords", []) or []:
            vocab.setdefault(norm_keyword(kw), name)
    vocab.pop("", None)
    return names, vocab


def build_catalog(
    found: Dict[str, List[Tuple[str, Dict[str, Any]]]],
    core_names: Set[str],
    core_vocab: Dict[str, str],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Return (presets, provenance, excluded, skipped)."""
    excluded: Dict[str, str] = {}
    skipped: Dict[str, str] = {}
    provenance: Dict[str, str] = {}
    drafts: Dict[str, Dict[str, Any]] = {}
    candidates: Dict[str, Set[str]] = {}
    own_phrases: Dict[str, Set[str]] = {}
    own_all: Dict[str, Set[str]] = {}

    for pid in sorted(found):
        copies = sorted(found[pid], key=lambda pc: _wave_sort_key(pc[0]))
        live = [(p, d) for p, d in copies if p not in EXCLUDED_PACKS]
        if pid in EXCLUDED_IDS:
            excluded[pid] = EXCLUDED_IDS[pid]
            continue
        if not live:
            excluded[pid] = EXCLUDED_PACKS[copies[0][0]]
            continue
        auto_reason = auto_exclude_reason(pid, [d for _, d in live])
        if auto_reason:
            excluded[pid] = auto_reason
            continue
        if pid in core_names:
            skipped[pid] = "existing core preset kept unchanged"
            continue
        if not _SAFE_ID.match(pid):
            skipped[pid] = "unsafe id for a filename"
            continue
        # Best copy = richest; ties go to the earliest wave (sorted order).
        best_pack, best = max(live, key=lambda pc: richness(pc[1]))
        provenance[pid] = best_pack

        own = {norm_keyword(pid), norm_keyword(best.get("name"))} - {""}
        own_all[pid] = own
        # A multi-word phrase ("son goku", "rick sanchez") is safe even when its
        # words are common, because the exact combination is intrinsically rare.
        # A *single*-word id/name ("king", "light", "doctor" — several unrelated
        # characters share exactly these) gets no such protection: it must clear
        # the same rarity bar as any other single-token keyword, or it is exactly
        # the ordinary-word-as-trigger bug this script exists to prevent.
        own_phrases[pid] = {
            k for k in own
            if len(k) >= MIN_KEYWORD_LEN and not k.isdigit()
            and k not in STOPWORDS and k not in core_vocab
            and (len(k.split()) > 1 or zipf(k) < NAME_TOKEN_MAX_ZIPF)
        }
        cand: Set[str] = set(own_phrases[pid])
        for tok in norm_keyword(best.get("name")).split():
            if len(tok) >= 4 and keyword_ok(tok) and tok not in core_vocab and zipf(tok) < NAME_TOKEN_MAX_ZIPF:
                cand.add(tok)
        for _, d in live:
            for kw in d.get("keywords", []) or []:
                k = norm_keyword(kw)
                if not keyword_ok(k) or k in core_vocab:
                    continue
                if min(zipf(w) for w in k.split()) < SOURCE_KEYWORD_MAX_ZIPF:
                    cand.add(k)
        candidates[pid] = cand
        drafts[pid] = best

    df = Counter(k for kws in candidates.values() for k in kws)
    presets: Dict[str, Dict[str, Any]] = {}
    for pid, best in drafts.items():
        keep = sorted(k for k in candidates[pid] if df[k] <= MAX_KEYWORD_DF or k in own_phrases[pid])
        # The full-name phrase alone may be shared by a namesake; that's fine
        # (deterministic tie-break in the matcher) but a persona with *no*
        # usable keyword can never be selected, so skip it explicitly.
        if not keep:
            owners = sorted({core_vocab[k] for k in own_all[pid] if k in core_vocab})
            skipped[pid] = (
                "its name is already a keyword of core preset " + ", ".join(owners)
                if owners else "no distinctive keyword survived hygiene"
            )
            provenance.pop(pid, None)
            continue
        entry: Dict[str, Any] = {
            "name": clean_text(best.get("name")) or pid.replace("_", " ").title(),
            "description": clean_text(best.get("description")),
            "keywords": keep,
        }
        for f in ("sarcasm_level", "metaphor_frequency", "exaggeration_level",
                  "programmer_humor", "anime_references"):
            v = best.get(f)
            entry[f] = round(min(1.0, max(0.0, float(v))), 3) if isinstance(v, (int, float)) else 0.3
        entry["deadpan"] = bool(best.get("deadpan", False))
        entry["deadpan_safe"] = bool(best.get("deadpan_safe", entry["deadpan"]))
        mrl = best.get("max_response_length")
        entry["max_response_length"] = min(4096, max(50, int(mrl))) if isinstance(mrl, int) else 400
        entry["persona_context"] = clean_text(best.get("persona_context"))
        entry["themed_openers"] = clean_list(best.get("themed_openers"))
        entry["themed_closers"] = clean_list(best.get("themed_closers"))
        entry["themed_prefixes"] = clean_list(best.get("themed_prefixes"))
        entry["example_responses"] = clean_list(best.get("example_responses"))[:6]
        notes = clean_text(best.get("response_style_notes"))
        if notes:
            entry["response_style_notes"] = notes
        emoji = clean_list(best.get("emoji_pool"))[:8]
        if emoji:
            entry["emoji_pool"] = emoji
        pools = best.get("metaphor_pools")
        if isinstance(pools, dict) and pools:
            tm = {str(k): clean_list(v) for k, v in pools.items() if isinstance(v, list)}
            tm = {k: v for k, v in tm.items() if v}
            if tm:
                entry["themed_metaphors"] = tm
        if not entry["persona_context"]:
            skipped[pid] = "empty persona_context"
            provenance.pop(pid, None)
            continue
        presets[pid] = {k: entry[k] for k in KEY_ORDER if k in entry}

    # A short id ("light") and its full-name sibling ("light_yagami") often end
    # up with the *identical* final keyword set once hygiene has run — the same
    # character filed twice under two ids. Untreated, both presets would
    # permanently tie on every match, so the runner-up is decided only by
    # Python's dict-iteration order — an accident, not a design. Keep the
    # richer copy (ties broken by id, for determinism) and drop the rest.
    by_signature: Dict[frozenset, List[str]] = defaultdict(list)
    for pid, entry in presets.items():
        by_signature[frozenset(k.lower() for k in entry["keywords"])].append(pid)
    for sig, ids in by_signature.items():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (-richness(drafts[i]), i))
        winner, losers = ids[0], ids[1:]
        for loser in losers:
            del presets[loser]
            provenance.pop(loser, None)
            skipped[loser] = f"duplicate persona: same trigger keywords as '{winner}'"

    return presets, provenance, excluded, skipped


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", required=True, type=Path, help="directory of unzipped packs")
    ap.add_argument("--presets-dir", type=Path, default=PRESETS_DIR)
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args(argv)

    previously: Set[str] = set()
    if args.manifest.exists():
        try:
            previously = set(json.loads(args.manifest.read_text(encoding="utf-8")).get("imported", {}))
        except (OSError, ValueError):
            previously = set()

    found = load_sources(args.source)
    core_names, core_vocab = core_vocabulary(args.presets_dir, previously)
    presets, provenance, excluded, skipped = build_catalog(found, core_names, core_vocab)

    print(f"source personas (distinct ids): {len(found)}")
    print(f"imported: {len(presets)}  excluded: {len(excluded)}  skipped: {len(skipped)}")
    if args.dry_run:
        return 0

    args.presets_dir.mkdir(parents=True, exist_ok=True)
    for stale in previously - set(presets):
        p = args.presets_dir / f"{stale}.json"
        if p.exists():
            p.unlink()
    for pid, data in presets.items():
        (args.presets_dir / f"{pid}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    manifest = {
        "generated_by": "scripts/import_personas.py",
        "rules": {
            "max_keyword_df": MAX_KEYWORD_DF,
            "min_keyword_len": MIN_KEYWORD_LEN,
            "name_token_max_zipf": NAME_TOKEN_MAX_ZIPF,
            "source_keyword_max_zipf": SOURCE_KEYWORD_MAX_ZIPF,
            "keywords_never_overlap_core_presets": True,
            "duplicate_ids": "richest copy wins; keywords are unioned across copies",
        },
        "counts": {"imported": len(presets), "excluded": len(excluded), "skipped": len(skipped)},
        "imported": dict(sorted(provenance.items())),
        "excluded": dict(sorted(excluded.items())),
        "skipped": dict(sorted(skipped.items())),
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(presets)} presets + {args.manifest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
