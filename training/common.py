"""Shared helpers for the persona-training toolkit (standard library only)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = ROOT / "data" / "personality" / "persons" / "registry.json"
DEFAULT_AUTH = ROOT / "training" / "authorizations.json"
SCOPE_WEIGHTS = "weights_training"
MANIFEST_SCHEMA = 1


class TrainingDataError(RuntimeError):
    """The prepared data is missing, corrupt, or does not match its manifest."""


class AuthorizationError(RuntimeError):
    """Weight training for a person was attempted without a recorded authorization."""


# ── module loading ───────────────────────────────────────────────────────────
def load_module(name: str, rel: str):
    """Load a project file by path (so offline tools never import the bot's dependency stack)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def fmt_module():
    return load_module("persona_sft_format", "bot/personality/persona_sft_format.py")


def text_module():
    return load_module("persona_text", "bot/personality/persona_text.py")


# ── hashing / atomic IO ──────────────────────────────────────────────────────
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_hash(rels: Iterable[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(rels):
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory + fsync + rename: readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(Path(path), (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrainingDataError(f"{path}:{n}: invalid JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise TrainingDataError(f"{path}:{n}: expected an object")
            yield obj


def jsonl_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    return b"".join((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for r in rows)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> Tuple[int, str]:
    data = jsonl_bytes(rows)
    atomic_write_bytes(Path(path), data)
    return data.count(b"\n"), sha256_bytes(data)


# ── registry / identity ──────────────────────────────────────────────────────
def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path or DEFAULT_REGISTRY)
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrainingDataError(f"cannot read registry {p}: {exc}") from exc
    if not isinstance(reg.get("persons"), dict) or not reg["persons"]:
        raise TrainingDataError(f"registry {p} has no persons")
    return reg


def account_index(registry: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """(numeric account id -> person id, id -> account tag, lower-case username -> person id).

    Identity comes ONLY from numeric ids (and, for flow rows that carry no id, usernames).  Display
    names are never consulted.  An id claimed by two persons is a registry error, not a tie to break.
    """
    fmt = fmt_module()
    by_id: Dict[str, str] = {}
    tag: Dict[str, str] = {}
    by_user: Dict[str, str] = {}
    for pid, p in registry["persons"].items():
        for i, a in enumerate(p["accounts"]):
            aid = str(a["id"])
            if aid in by_id and by_id[aid] != pid:
                raise TrainingDataError(f"account {aid} is registered to both '{by_id[aid]}' and '{pid}'")
            by_id[aid] = pid
            tag[aid] = fmt.account_tag(i)
            un = (a.get("username") or "").strip().lower()
            if un:
                if by_user.get(un, pid) != pid:
                    raise TrainingDataError(f"username '{un}' is registered to both '{by_user[un]}' and '{pid}'")
                by_user[un] = pid
    return by_id, tag, by_user


# ── authorization gate ───────────────────────────────────────────────────────
def load_authorizations(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path or DEFAULT_AUTH)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthorizationError(f"cannot read authorization file {p}: {exc}") from exc


def require_authorization(person_id: str, path: Optional[Path] = None, scope: str = SCOPE_WEIGHTS) -> Dict[str, Any]:
    """Refuse unless the person's record says status=granted for ``scope`` with named evidence.

    Weights cannot be edited per person after the fact (an opt-out cannot remove what the model has
    absorbed), so training requires an affirmative, recorded authorization from the person - not just
    the absence of an opt-out.  The record is copied into the adapter metadata.
    """
    rec = (load_authorizations(path).get("persons") or {}).get(person_id)
    if not isinstance(rec, dict):
        raise AuthorizationError(f"no authorization record for '{person_id}' in {path or DEFAULT_AUTH}")
    problems: List[str] = []
    if rec.get("status") != "granted":
        problems.append(f"status is '{rec.get('status')}', not 'granted'")
    if scope not in (rec.get("scope") or []):
        problems.append(f"scope does not include '{scope}'")
    for field in ("granted_by", "evidence", "date"):
        if not str(rec.get(field) or "").strip():
            problems.append(f"'{field}' is empty")
    if problems:
        raise AuthorizationError(f"weight training for '{person_id}' is not authorized: " + "; ".join(problems))
    return dict(rec)


# ── manifest verification ────────────────────────────────────────────────────
def verify_prepared_dir(directory: Path, splits: Tuple[str, ...] = ("train", "val")) -> Dict[str, Any]:
    """Check that the listed split files exist and hash to what manifest.json recorded."""
    d = Path(directory)
    try:
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrainingDataError(f"{d}: manifest.json unreadable ({exc})") from exc
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise TrainingDataError(f"{d}: unsupported manifest schema {manifest.get('schema')}")
    for s in splits:
        meta = (manifest.get("splits") or {}).get(s)
        if not meta:
            raise TrainingDataError(f"{d}: manifest has no '{s}' split")
        f = d / f"{s}.jsonl"
        if not f.exists():
            raise TrainingDataError(f"{f} is missing")
        if sha256_file(f) != meta["sha256"]:
            raise TrainingDataError(f"{f} does not match the manifest hash (modified or corrupted)")
    return manifest
