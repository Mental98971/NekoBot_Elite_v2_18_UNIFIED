"""Runtime router for fine-tuned persona adapters (v2.10).

A person's adapter is served by an OpenAI-compatible inference server (vLLM ``--enable-lora``,
llama.cpp, Ollama ...) under the model name recorded in ``<PERSONA_ADAPTER_DIR>/registry.json``.  This
module decides *whether* to use it and cleans what comes back; it never loads weights itself.

Order of precedence for a real-person skin (``Personality._person_reply``)::

    identity question  ->  fixed honest disclosure (adapter is never asked)
    adapter ready      ->  fine-tuned reply (cleaned, filtered, marked)
    otherwise / failure -> the v2.9 path: LLM conditioned on retrieved exchanges, then retrieval replay

With ``PERSONA_MODEL_ENABLED=false`` (default) none of this runs and behaviour is exactly v2.9.

Safety properties enforced here, in code, not in a prompt (a fine-tuned model cannot be relied on to
keep a system-prompt rule such as "you are not the real person"):
  * a message that asks who/what the bot is gets a deterministic disclosure, never a model reply;
  * only adapters whose registry status is ``ready`` (they passed the evaluation gates) are used, and
    only when their prompt-format version matches this code;
  * replies containing contact data, template placeholders or the person's blocked (abusive) tokens are
    discarded and the v2.9 path answers instead;
  * a circuit breaker stops hammering a down/slow inference server (no retry storms, no stacked timeouts).

Standard library only at import time (``httpx`` is imported lazily when a request is made).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

try:  # normal case: imported as part of the bot package
    from bot.personality import persona_sft_format as fmt
except Exception:  # pragma: no cover - tooling/tests that load this file standalone
    import importlib.util as _ilu

    _p = Path(__file__).with_name("persona_sft_format.py")
    _s = _ilu.spec_from_file_location("persona_sft_format", _p)
    fmt = _ilu.module_from_spec(_s)  # type: ignore[assignment]
    _s.loader.exec_module(fmt)  # type: ignore[union-attr]

from bot.persona_engine.identity import disclosure_reply, is_identity_probe  # noqa: E402,F401  (re-exported)
from bot.persona_engine.resilience import CircuitBreaker  # noqa: E402,F401  (re-exported)

log = logging.getLogger("bot.personality.persona_model")

STATUS_READY = "ready"
REGISTRY_FILE = "registry.json"

# Identity-question detector and the fixed disclosure live in bot/persona_engine/identity.py (single source of truth).


# ── reply cleaning / filtering ───────────────────────────────────────────────
_RE_CONTACT = re.compile(
    r"(?:https?://|www\.|t\.me/)\S+"
    r"|[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+"
    r"|(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"
    r"|\d{6,}"
    r"|@[A-Za-z0-9_]{3,}"
)
_RE_PLACEHOLDER = re.compile(r"<(?:url|user|num|email)>|@user\b")
_RE_MD = re.compile(r"(\*\*|__|`+)")
_RE_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
_RE_SPECIAL = re.compile(r"<\|[^|>]{1,40}\|>|</?s>|\[/?INST\]")


def clean_reply(raw: Optional[str], max_chars: int, blocked_tokens: Iterable[str] = ()) -> Optional[str]:
    """Normalise a model completion to one chat message, or None if it must not be sent."""
    t = _RE_SPECIAL.sub("", raw or "").strip()
    if not t:
        return None
    t = re.sub(rf"^\s*{re.escape(fmt.SELF)}\s*[:：-]\s*", "", t, flags=re.IGNORECASE)
    t = _RE_MD.sub("", t).strip()
    if len(t) >= 2 and t[0] in "\"'“‘" and t[-1] in "\"'”’":
        t = t[1:-1].strip()
    if not t:
        return None
    if _RE_CONTACT.search(t) or _RE_PLACEHOLDER.search(t):
        return None
    blocked = {b.lower() for b in blocked_tokens}
    if blocked and any(tok.lower() in blocked for tok in _RE_TOKEN.findall(t)):
        return None
    if len(t) > max_chars:
        cut = t[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: stop + 1] if stop >= max_chars // 3 else (cut.rsplit(" ", 1)[0] if " " in cut else cut)
    return t.strip() or None


# ── adapter registry ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AdapterEntry:
    person: str
    model: str
    format_version: int
    dataset_sha256: str = ""
    base_model: str = ""
    chat_kinds: tuple = ()  # chat kinds present in the training data; the runtime never prompts with an unseen one


class AdapterRegistry:
    """``registry.json`` written by training/train_lora.py and training/promote.py (re-read when it changes)."""

    def __init__(self, directory: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(directory) / REGISTRY_FILE
        self._clock = clock
        self._mtime: Optional[float] = None
        self._next_check = 0.0
        self._entries: Dict[str, AdapterEntry] = {}

    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._entries:
                log.info("persona adapter registry disappeared; adapters disabled")
            self._entries, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            entries: Dict[str, AdapterEntry] = {}
            for pid, rec in (doc.get("persons") or {}).items():
                if rec.get("status") != STATUS_READY or not rec.get("model"):
                    continue
                entries[pid] = AdapterEntry(
                    person=pid, model=str(rec["model"]), format_version=int(rec.get("format_version", 0)),
                    dataset_sha256=str(rec.get("dataset_sha256", "")), base_model=str(rec.get("base_model", "")),
                    chat_kinds=tuple(k for k in (rec.get("chat_kinds") or ()) if k in fmt.CHAT_KINDS),
                )
            self._entries, self._mtime = entries, mtime
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("persona adapter registry unreadable (%s); adapters disabled until it is fixed", exc)
            self._entries, self._mtime = {}, mtime

    def ready(self, person_id: str) -> Optional[AdapterEntry]:
        now = self._clock()
        if now >= self._next_check:  # at most one stat() per 5 s
            self._next_check = now + 5.0
            self._reload()
        entry = self._entries.get(person_id)
        if entry is None:
            return None
        if entry.format_version != fmt.FORMAT_VERSION:
            log.warning("adapter for %s was trained with prompt format %s, code is %s; not used",
                        person_id, entry.format_version, fmt.FORMAT_VERSION)
            return None
        return entry


# ── HTTP client (OpenAI-compatible chat completions) ─────────────────────────
class PersonaModelClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, temperature: float = 0.7, top_p: float = 0.9, max_tokens: int = 120) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key, self.temperature, self.top_p, self.max_tokens = api_key, temperature, top_p, max_tokens
        self._http: Any = None

    async def complete(self, model: str, messages: Sequence[Dict[str, str]], timeout: float) -> str:
        import httpx  # lazy: keeps the module importable without the bot's dependency stack
        if self._http is None:
            self._http = httpx.AsyncClient()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = await self._http.post(
            self.url, headers=headers, timeout=timeout,
            json={"model": model, "messages": list(messages), "temperature": self.temperature,
                  "top_p": self.top_p, "max_tokens": self.max_tokens, "n": 1},
        )
        resp.raise_for_status()
        data = resp.json()
        return str((((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or "")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ── router ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RouterResult:
    text: str
    source: str  # "adapter" | "identity_probe"


class PersonaModelRouter:
    def __init__(
        self, registry: AdapterRegistry, client: Any, timeout: float = 12.0, marker: str = "",
        breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.registry, self.client, self.timeout, self.marker = registry, client, timeout, marker
        self.breaker = breaker or CircuitBreaker()
        self.stats: Dict[str, int] = {
            "adapter_ok": 0, "identity_probe": 0, "not_ready": 0, "breaker_open": 0,
            "rejected": 0, "error": 0,
        }

    async def reply(
        self, *, person_id: str, real_name: str, user_message: str, account_index: Optional[int],
        chat_kind: str, max_chars: int, blocked_tokens: Iterable[str] = (), reply_to_text: Optional[str] = None,
        recent: Iterable[Sequence[str]] = (), names: Iterable[str] = (),
    ) -> Optional[RouterResult]:
        """A RouterResult to send, or None meaning "use the v2.9 path"."""
        if is_identity_probe(user_message, [real_name, *names]):
            self.stats["identity_probe"] += 1
            return RouterResult(disclosure_reply(real_name), "identity_probe")
        entry = self.registry.ready(person_id)
        if entry is None:
            self.stats["not_ready"] += 1
            return None
        if not self.breaker.allow():
            self.stats["breaker_open"] += 1
            return None
        acct = fmt.account_tag(account_index) if account_index is not None else None
        if entry.chat_kinds and chat_kind not in entry.chat_kinds:
            chat_kind = entry.chat_kinds[0]  # e.g. a DM when the person's data is group-only: use the context the model knows
        messages = fmt.runtime_messages(user_message, reply_to_text, chat_kind, acct, recent)
        try:
            raw = await self.client.complete(entry.model, messages, self.timeout)
        except Exception as exc:  # network, timeout, HTTP status, bad JSON: never break a reply
            self.breaker.fail()
            self.stats["error"] += 1
            log.warning("persona adapter call failed for %s: %s: %s", person_id, type(exc).__name__, exc)
            return None
        self.breaker.ok()
        text = clean_reply(raw, max_chars, blocked_tokens)
        if text is None:
            self.stats["rejected"] += 1
            log.info("persona adapter reply for %s rejected by the output filter", person_id)
            return None
        self.stats["adapter_ok"] += 1
        if self.marker:
            text = f"{text} {self.marker}"
        return RouterResult(text, "adapter")


_router: Optional[PersonaModelRouter] = None


def get_router(settings: Any) -> Optional[PersonaModelRouter]:
    """Process-wide router built from bot settings; None when the feature is off or not configured."""
    global _router
    if not getattr(settings, "persona_model_enabled", False):
        return None
    if _router is None:
        base_url = str(getattr(settings, "persona_model_base_url", "") or "").strip()
        if not base_url:
            log.warning("PERSONA_MODEL_ENABLED is true but PERSONA_MODEL_BASE_URL is empty; adapters disabled")
            return None
        secret = getattr(settings, "persona_model_api_key", None)
        key = secret.get_secret_value() if secret is not None and hasattr(secret, "get_secret_value") else (secret or None)
        client = PersonaModelClient(
            base_url, key, float(settings.persona_model_temperature), float(settings.persona_model_top_p),
        )
        _router = PersonaModelRouter(
            AdapterRegistry(Path(settings.persona_adapter_dir)), client,
            float(settings.persona_model_timeout_seconds), str(settings.persona_model_marker or ""),
        )
    return _router


def reset_router() -> None:
    global _router
    _router = None
