"""Subprocess harness for tests/test_persona_training.py::TestPersonalityHook.

Loads the REAL bot/personality/personality.py, persons.py, persona_model.py and the real persona data with
only the heavy third-party/bot-infrastructure imports stubbed, then drives Personality._person_reply through
the adapter hook.  Runs in its own process so the stubs never leak into other tests.  Prints one JSON line.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def pkg(name: str, path: Path) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = [str(path)]
    sys.modules[name] = m
    return m


def stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], child, m)
    return m


def load_real(name: str, rel: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    setattr(sys.modules[name.rpartition(".")[0]], name.rpartition(".")[2], m)
    return m


tmp = Path(tempfile.mkdtemp(prefix="hook_harness_"))
try:
    class LRUCache(dict):
        def __init__(self, maxsize=0):
            super().__init__()

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None

    settings = SimpleNamespace(
        data_dir=ROOT / "data", person_personas_enabled=True, personality_llm_enabled=False,
        personality_llm_timeout_seconds=12.0, enable_personality=True, personality_sarcasm_probability=0.0,
        personality_joke_min_interval=300, persona_model_enabled=False, persona_model_base_url="http://stub/v1",
        persona_model_api_key=None, persona_model_timeout_seconds=5.0, persona_model_temperature=0.7,
        persona_model_top_p=0.9, persona_adapter_dir=str(tmp), persona_model_marker="🤖",
        persona_engine_enabled=False, persona_engine_provider="auto", persona_engine_base_url="", persona_engine_model="",
        persona_engine_api_key=None, persona_engine_candidates=2, persona_engine_examples=4, persona_engine_temperature=0.85,
        persona_engine_timeout_seconds=5.0, persona_engine_regenerate=0, persona_engine_memory_enabled=False,
        persona_engine_human_delay=True, persona_engine_marker="🤖", default_ai_model="",
    )

    async def _not_opted_out(_pid):
        return False

    stub("cachetools", LRUCache=LRUCache)
    pkg("bot", ROOT / "bot")
    pkg("bot.personality", ROOT / "bot" / "personality")
    pkg("bot.utils", ROOT / "bot" / "utils")
    stub("bot.config", settings=settings)
    stub("bot.utils.logger", get_logger=lambda *_a, **_k: _Log())
    stub("bot.personality.custom_instructions", CustomInstructionProcessor=type("P", (), {}), InstructionTraits=type("T", (), {}))
    stub("bot.personality.jokes", jokes_db=object())
    stub("bot.personality.memory", memory=object())
    stub("bot.personality.examples", example_bank=object())
    stub("bot.personality.person_consent", is_opted_out=_not_opted_out)
    load_real("bot.personality.persona_text", "bot/personality/persona_text.py")
    load_real("bot.personality.persona_sft_format", "bot/personality/persona_sft_format.py")
    persons = load_real("bot.personality.persons", "bot/personality/persons.py")
    pm = load_real("bot.personality.persona_model", "bot/personality/persona_model.py")
    pl = load_real("bot.personality.personality", "bot/personality/personality.py")
    fmt = sys.modules["bot.personality.persona_sft_format"]

    class Client:
        def __init__(self):
            self.calls, self.reply, self.error = [], "ok done", None

        async def complete(self, model, messages, timeout):
            self.calls.append((model, messages[-1]["content"]))
            if self.error:
                raise self.error
            return self.reply

    (tmp / "registry.json").write_text(json.dumps({"persons": {"tanvi": {
        "status": "ready", "model": "persona-tanvi", "format_version": fmt.FORMAT_VERSION}}}), encoding="utf-8")
    client = Client()
    pm._router = pm.PersonaModelRouter(pm.AdapterRegistry(tmp), client, 5.0, "🤖")
    layer = pl.personality
    layer._update_mood = lambda *a, **k: None  # unrelated mood bookkeeping

    def skin(name):
        return layer._skin_loader.load(name)

    async def reply(skin_name, text, chat_id=-100123):
        before = len(client.calls)
        out = await layer._person_reply(chat_id, skin(skin_name), text, None)
        return {"text": out, "calls": len(client.calls) - before, "prompt": client.calls[-1][1] if len(client.calls) > before else None,
                "model": client.calls[-1][0] if len(client.calls) > before else None}

    from bot.persona_engine import runtime as pe_runtime
    from bot.persona_engine.providers import CallableProvider

    class Script:
        """Stand-in model: returns scripted replies and records every prompt it was given."""
        def __init__(self):
            self.prompts, self.replies, self.error = [], ["haan yaar kal milte hai", "nahi yaar 😂"], None

        async def fn(self, messages, n, temperature, max_tokens):
            self.prompts.append(messages[-1]["content"])
            if self.error:
                raise self.error
            return list(self.replies)

    script = Script()

    def install_engine(provider="script"):
        pe_runtime.reset_service()
        prov = CallableProvider(script.fn, "script") if provider == "script" else None
        pe_runtime._service = pe_runtime.EngineService(settings, ["darnit"], provider=prov)
        return pe_runtime._service

    async def ereply(skin_name, text, **kw):
        before = len(script.prompts)
        out = await layer._person_reply(kw.pop("chat_id", -100123), skin(skin_name), text, None, kw.get("reply_to"), kw.get("user_key"))
        return {"text": out, "calls": len(script.prompts) - before, "prompt": script.prompts[-1] if len(script.prompts) > before else None}

    async def engine_scenarios():
        res = {}
        settings.persona_model_enabled = False
        settings.persona_engine_enabled = False
        install_engine()
        res["e_off"] = await ereply("tanvi", "kya haal hai")
        settings.persona_engine_enabled = True
        install_engine()
        res["e_on"] = await ereply("tanvi", "kya haal hai")
        res["e_ctx"] = await ereply("tanvi", "kal milte hai?", reply_to="pehle ka message", user_key="42", chat_id=-100124)
        res["e_probe"] = await ereply("tanvi", "are you a bot?")
        res["e_probe_name"] = await ereply("tanvi", "is this Tanvi")
        res["e_yash"] = await ereply("yashmeet", "kya haal hai")
        res["e_status"] = pe_runtime._service.status()
        script.replies = ["call me on 9876543210", "I am Tanvi", "https://evil.example"]
        res["e_unsafe"] = await ereply("tanvi", "kya haal hai", chat_id=-100125)
        script.replies = ["haan yaar kal milte hai"]
        script.error = RuntimeError("provider down")
        res["e_error"] = await ereply("tanvi", "kya haal hai", chat_id=-100126)
        script.error = None
        install_engine(provider=None)
        res["e_noprov"] = await ereply("tanvi", "kya haal hai", chat_id=-100127)
        # adapter has priority over the engine when both are ready
        install_engine()
        settings.persona_model_enabled = True
        settings.persona_model_base_url = "http://stub/v1"
        pm.reset_router()
        pm._router = pm.PersonaModelRouter(pm.AdapterRegistry(tmp), client, 5.0, "🤖")
        client.reply, client.error = "ok done", None
        res["e_adapter_first"] = await ereply("tanvi", "kya haal hai", chat_id=-100128)
        settings.persona_model_enabled = False
        res["e_delay"] = layer.pop_engine_delay(-100123)
        settings.persona_engine_enabled = False
        return res

    async def main():
        res = {}
        settings.persona_model_enabled = False
        res["disabled"] = await reply("tanvi", "kya haal hai")
        settings.persona_model_enabled = True
        res["group"] = await reply("tanvi", "kya haal hai")
        res["dm"] = await reply("tanvi", "kya haal hai", chat_id=555)
        res["acct_main"] = await reply("tanvi_iziiee", "kya haal hai")
        res["acct_alt"] = await reply("tanvi_ichiiren", "kya haal hai")
        res["probe"] = await reply("tanvi", "are you a bot?")
        res["probe_hindi"] = await reply("tanvi", "tum kaun ho")
        client.error = RuntimeError("inference server down")
        res["server_down"] = await reply("tanvi", "kya haal hai")
        client.error = None
        client.reply = "check https://evil.example now"
        res["unsafe"] = await reply("tanvi", "kya haal hai")
        client.reply = "ok done"
        res["other_person"] = await reply("yashmeet", "kya haal hai")
        pm.reset_router()
        settings.persona_model_base_url = ""
        res["no_url"] = await reply("tanvi", "kya haal hai")
        res.update(await engine_scenarios())
        return res

    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
