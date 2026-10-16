#!/usr/bin/env python3
"""Withdraw a person's authorization and remove everything trained on their messages.

    python -m training.revoke --person tanvi [--prepared training/_private/data/tanvi] [--yes]

Marks the authorization 'revoked', deletes the person's adapters and registry entry (so the bot stops
using them immediately - the router re-reads registry.json), and with --prepared also deletes the
prepared training data.  Merged or exported copies of the adapter, and any inference-server cache, are
outside this tool's reach and must be removed by the operator.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402

DEFAULT_STORE = common.ROOT / "data" / "models" / "persona_adapters"


def revoke(person: str, store: Path, auth_path: Path, prepared: Optional[Path] = None) -> List[str]:
    done: List[str] = []
    auth = common.load_authorizations(auth_path)
    rec = (auth.get("persons") or {}).get(person)
    if rec is None:
        raise common.AuthorizationError(f"'{person}' is not in {auth_path}")
    rec.update(status="revoked", revoked_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    common.atomic_write_json(auth_path, auth)
    done.append("authorization marked revoked")
    reg_path = store / "registry.json"
    if reg_path.exists():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        if reg.get("persons", {}).pop(person, None) is not None:
            common.atomic_write_json(reg_path, reg)
            done.append("registry entry removed (bot stops using the adapter)")
    pdir = store / person
    if pdir.exists():
        shutil.rmtree(pdir)
        done.append(f"deleted {pdir}")
    if prepared is not None and Path(prepared).exists():
        shutil.rmtree(prepared)
        done.append(f"deleted {prepared}")
    return done


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", required=True)
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--authorization", default=str(common.DEFAULT_AUTH))
    ap.add_argument("--prepared", help="also delete this prepared-data directory")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if not args.yes and input(f"Delete all trained artifacts for '{args.person}'? type the id to confirm: ").strip() != args.person:
        print("aborted")
        return 1
    try:
        for line in revoke(args.person, Path(args.store), Path(args.authorization), Path(args.prepared) if args.prepared else None):
            print("-", line)
        print("- note: this removes trained weights only. The live imitation (retrieval persona and persona engine) is switched off per person "
              "with /pconsent optout in the bot, or by removing data/personality/persons/" + args.person + "/.")
    except (common.AuthorizationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
