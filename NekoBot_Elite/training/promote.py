#!/usr/bin/env python3
"""Mark a trained adapter "ready" for the bot - only if its evaluation passed the gates.

    python -m training.promote --person tanvi --eval training/_private/data/tanvi/eval.json

Checks, in order: the person is still authorized; the evaluation report is for this person and this
exact dataset build; the report contains a gate verdict for the candidate and it passed; the adapter
directory is complete and was trained on that dataset.  Only then does registry.json flip
status -> ready (atomic write).  `--demote` returns an adapter to "trained" without deleting it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402
from training.train_lora import validate_adapter_dir  # noqa: E402

DEFAULT_STORE = common.ROOT / "data" / "models" / "persona_adapters"


def promote(person: str, report: dict, store: Path, auth_path: Optional[Path]) -> dict:
    common.require_authorization(person, auth_path)
    if report.get("person") != person:
        raise common.TrainingDataError(f"evaluation report is for '{report.get('person')}', not '{person}'")
    gate = report.get("gate")
    if not gate or "candidate" not in gate:
        raise common.TrainingDataError("evaluation report has no gate verdict (run evaluate with --candidate and --gates)")
    if not gate.get("passed"):
        failed = [c["name"] for c in gate.get("checks", []) if not c["passed"]]
        raise common.TrainingDataError("adapter did not pass the gates: " + ", ".join(failed or ["no checks recorded"]))
    adapter_dir = store / person / "current"
    meta = validate_adapter_dir(adapter_dir)
    if meta["dataset_sha256"] != report.get("dataset_sha256"):
        raise common.TrainingDataError("evaluation was run on a different dataset build than the adapter was trained on")
    reg_path = store / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {"schema": 1, "persons": {}}
    rec = reg["persons"].get(person)
    if not rec:
        raise common.TrainingDataError(f"no registry entry for '{person}'; train first")
    if rec.get("run_id") != meta.get("run_id"):
        raise common.TrainingDataError("registry entry and adapter directory are from different runs")
    rec.update(
        status="ready", promoted_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        evaluation={"candidate": gate["candidate"], "test_sha256": report.get("test_sha256"),
                    "checks": [{"name": c["name"], "passed": c["passed"]} for c in gate["checks"]]},
    )
    common.atomic_write_json(reg_path, reg)
    return rec


def demote(person: str, store: Path) -> None:
    reg_path = store / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    if person not in reg.get("persons", {}):
        raise common.TrainingDataError(f"no registry entry for '{person}'")
    reg["persons"][person]["status"] = "trained"
    reg["persons"][person].pop("promoted_utc", None)
    common.atomic_write_json(reg_path, reg)


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", required=True)
    ap.add_argument("--eval", help="evaluation report from training.evaluate (with a gate verdict)")
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--authorization", default=str(common.DEFAULT_AUTH))
    ap.add_argument("--demote", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.demote:
            demote(args.person, Path(args.store))
            print(f"{args.person}: adapter demoted to 'trained' (the bot will not use it)")
            return 0
        if not args.eval:
            raise common.TrainingDataError("--eval is required")
        report = json.loads(Path(args.eval).read_text(encoding="utf-8"))
        rec = promote(args.person, report, Path(args.store), Path(args.authorization))
        print(f"{args.person}: adapter '{rec['model']}' is now ready")
    except (common.TrainingDataError, common.AuthorizationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
