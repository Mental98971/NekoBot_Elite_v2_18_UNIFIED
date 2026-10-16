"""Model providers behind one interface, so the persona system never depends on a particular model.

  OpenAICompatProvider  any OpenAI-compatible /chat/completions endpoint: a local llama.cpp / Ollama / vLLM server,
                        or a hosted free tier that speaks that protocol.  Supports ``n`` candidates in one call.
  ProjectProvider       the bot's own AI plugin (its free-first provider chain: Gemini, Groq, free endpoint, OpenRouter ...).
                        One sample per call, so ``n`` candidates cost ``n`` calls.
  CallableProvider      wraps any async function (tests, offline evaluation replays).

Nothing here needs a GPU.  ``httpx`` / the AI plugin are imported lazily.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence

Messages = Sequence[Dict[str, str]]


class LLMProvider(Protocol):
    name: str

    async def generate(self, messages: Messages, *, n: int, temperature: float, max_tokens: int, timeout: float) -> List[str]: ...


class OpenAICompatProvider:
    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None, top_p: float = 0.95) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.top_p = model, api_key, top_p
        self.name = f"openai_compat:{model}"
        self._http: Any = None
        self._n_supported = True

    async def _post(self, messages: Messages, n: int, temperature: float, max_tokens: int, timeout: float) -> List[str]:
        import httpx  # lazy
        if self._http is None:
            self._http = httpx.AsyncClient()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = await self._http.post(
            self.url, headers=headers, timeout=timeout,
            json={"model": self.model, "messages": list(messages), "temperature": temperature, "top_p": self.top_p,
                  "max_tokens": max_tokens, "n": n},
        )
        resp.raise_for_status()
        data = resp.json()
        return [str(((c or {}).get("message") or {}).get("content") or "") for c in (data.get("choices") or [])]

    async def generate(self, messages: Messages, *, n: int, temperature: float, max_tokens: int, timeout: float) -> List[str]:
        if n > 1 and self._n_supported:
            try:
                out = await self._post(messages, n, temperature, max_tokens, timeout)
                if len(out) >= 2:
                    return out
                self._n_supported = False  # server ignored n: sample separately from now on
            except Exception:
                self._n_supported = False
        results = await asyncio.gather(*(self._post(messages, 1, temperature, max_tokens, timeout) for _ in range(max(1, n))), return_exceptions=True)
        flat = [t for r in results if isinstance(r, list) for t in r]
        if not flat:
            errs = [r for r in results if isinstance(r, BaseException)]
            raise errs[0] if errs else RuntimeError("provider returned no text")
        return flat

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class ProjectProvider:
    """Uses ``bot.plugins.ai.generate_persona_reply`` (the project's configured providers and fallbacks)."""

    name = "project_ai"

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model

    async def generate(self, messages: Messages, *, n: int, temperature: float, max_tokens: int, timeout: float) -> List[str]:
        from bot.plugins.ai import generate_persona_reply  # lazy: heavy and optional
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        calls = [asyncio.wait_for(generate_persona_reply(system, user, self.model), timeout=timeout) for _ in range(max(1, n))]
        results = await asyncio.gather(*calls, return_exceptions=True)
        flat = [r for r in results if isinstance(r, str) and r.strip()]
        if not flat:
            errs = [r for r in results if isinstance(r, BaseException)]
            raise errs[0] if errs else RuntimeError("no AI provider configured or all failed")
        return flat


class CallableProvider:
    def __init__(self, fn: Callable[..., Awaitable[List[str]]], name: str = "callable") -> None:
        self.fn, self.name = fn, name

    async def generate(self, messages: Messages, *, n: int, temperature: float, max_tokens: int, timeout: float) -> List[str]:
        return list(await asyncio.wait_for(self.fn(messages, n=n, temperature=temperature, max_tokens=max_tokens), timeout=timeout))


def choose_provider(settings: Any) -> Optional[LLMProvider]:
    """Pick a provider from settings; never raises.  Order: explicit endpoint -> the bot's AI plugin -> none.

    ``PERSONA_ENGINE_PROVIDER``: ``auto`` (default), ``openai_compat``, ``project``, ``none``.
    """
    mode = str(getattr(settings, "persona_engine_provider", "auto") or "auto").lower()
    if mode == "none":
        return None
    url = str(getattr(settings, "persona_engine_base_url", "") or "").strip()
    model = str(getattr(settings, "persona_engine_model", "") or "").strip()
    if mode in ("auto", "openai_compat") and url and model:
        secret = getattr(settings, "persona_engine_api_key", None)
        key = secret.get_secret_value() if secret is not None and hasattr(secret, "get_secret_value") else (secret or None)
        return OpenAICompatProvider(url, model, key)
    if mode == "openai_compat":
        return None  # asked for an endpoint but URL/model missing: do not silently use another provider
    if mode in ("auto", "project"):
        try:
            from bot.plugins import ai as ai_mod
            if ai_mod._provider_order(getattr(settings, "default_ai_model", "") or ""):  # at least one configured provider
                return ProjectProvider(None)
        except Exception:
            return None
    return None


def describe_provider(p: Optional[LLMProvider]) -> str:
    return "none (v2.9 retrieval persona answers)" if p is None else p.name
