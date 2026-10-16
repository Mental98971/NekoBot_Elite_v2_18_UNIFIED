"""Subprocess harness for tests/test_persona_training.py::TestPluginCommands.

Imports the REAL bot/plugins/personality.py (and the real personality layer, persons, persona engine) with only the Telegram
library and the bot's database/error/sub-plugin infrastructure stubbed, then drives /pengine, /premember, /pmemory and
/pforget with fake updates.  Runs in its own process; prints one JSON line.
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
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="plugin_harness_"))


def pkg(name, path):
    m = types.ModuleType(name)
    m.__path__ = [str(path)]
    sys.modules[name] = m
    return m


def stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], child, m)
    return m


def load_real(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    setattr(sys.modules[name.rpartition(".")[0]], name.rpartition(".")[2], m)
    return m


class Cache(dict):
    def __init__(self, maxsize=0, ttl=0):
        super().__init__()


class _Log:
    def __getattr__(self, _):
        return lambda *a, **k: None


class Settings(SimpleNamespace):
    """Explicit values for what the code under test reads; anything else the plugin touches at import time gets a harmless default."""
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return 30 if name.endswith("_seconds") or name.endswith("_interval") or name.endswith("_minutes") else (False if name.startswith(("enable_", "is_")) else 0)


try:
    settings = Settings(
        data_dir=ROOT / "data", person_personas_enabled=True, personality_llm_enabled=False, personality_llm_timeout_seconds=12.0,
        enable_personality=True, personality_sarcasm_probability=0.0, personality_joke_min_interval=300, persona_model_enabled=False,
        persona_model_base_url="", persona_model_api_key=None, persona_model_timeout_seconds=5.0, persona_model_temperature=0.7,
        persona_model_top_p=0.9, persona_adapter_dir=str(tmp / "adapters"), persona_model_marker="🤖",
        persona_engine_enabled=True, persona_engine_provider="auto", persona_engine_base_url="", persona_engine_model="", persona_engine_api_key=None,
        persona_engine_candidates=2, persona_engine_examples=4, persona_engine_temperature=0.85, persona_engine_timeout_seconds=5.0,
        persona_engine_regenerate=0, persona_engine_memory_enabled=True, persona_engine_human_delay=False, persona_engine_marker="🤖", default_ai_model="",
    )
    stub("cachetools", LRUCache=Cache, TTLCache=Cache)
    pkg("bot", ROOT / "bot")
    pkg("bot.personality", ROOT / "bot" / "personality")
    pkg("bot.utils", ROOT / "bot" / "utils")
    pkg("bot.core", ROOT / "bot" / "core")
    pkg("bot.plugins", ROOT / "bot" / "plugins")
    stub("bot.config", settings=settings)
    stub("bot.utils.logger", get_logger=lambda *_a, **_k: _Log())
    stub("bot.utils.helpers", escape_html=lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    load_real("bot.utils.textutil", "bot/utils/textutil.py")
    stub("bot.core.decorators", owner_only=lambda f: f)
    stub("bot.core.errors", safe_handler=lambda f: f)
    stub("bot.core.database", Chat=MagicMock(), async_session=MagicMock())
    for t in ("telegram", "telegram.constants", "telegram.ext"):
        sys.modules[t] = MagicMock()
    sys.modules["telegram"].Update = type("Update", (), {})
    stub("telegram.error", TelegramError=type("TelegramError", (Exception,), {}))  # a real exception class: the plugin catches it
    for name in ("bot.personality.custom_instructions", "bot.personality.jokes", "bot.personality.memory", "bot.personality.examples",
                 "bot.personality.subplugins", "bot.personality.triggers"):
        m = MagicMock()
        m.CustomInstructionProcessor, m.InstructionTraits = type("P", (), {}), type("T", (), {})
        sys.modules[name] = m
        setattr(sys.modules["bot.personality"], name.rsplit(".", 1)[1], m)
    stub("bot.personality.person_consent", is_opted_out=lambda _pid: asyncio.sleep(0, False))
    load_real("bot.personality.persona_text", "bot/personality/persona_text.py")
    load_real("bot.personality.persona_sft_format", "bot/personality/persona_sft_format.py")
    persons = load_real("bot.personality.persons", "bot/personality/persons.py")
    load_real("bot.personality.persona_model", "bot/personality/persona_model.py")
    pl = load_real("bot.personality.personality", "bot/personality/personality.py")
    plugin = load_real("bot.plugins.personality", "bot/plugins/personality.py")
    real_is_admin = plugin._is_admin

    from bot.persona_engine import runtime as pe_runtime
    from bot.persona_engine.providers import CallableProvider

    async def fn(messages, n, temperature, max_tokens):
        return ["kuch nahi yaar", "bas padh rhi"][:n]

    def fresh_service(memory=True):
        pe_runtime.reset_service()
        s = SimpleNamespace(**{**settings.__dict__, "data_dir": tmp, "persona_engine_memory_enabled": memory})
        pe_runtime._service = pe_runtime.EngineService(s, ["darnit"], provider=CallableProvider(fn, "script"))
        return pe_runtime._service

    layer = pl.personality
    layer._update_mood = lambda *a, **k: None

    class Msg:
        def __init__(self):
            self.out = []

        async def reply_text(self, text, **kw):
            self.out.append(str(text))

    def upd(chat=-100777):
        return SimpleNamespace(message=Msg(), effective_chat=SimpleNamespace(id=chat), effective_user=SimpleNamespace(id=42, username="u"))

    async def call(fn_, *args, chat=-100777, admin=True, skin="tanvi"):
        u = upd(chat)
        async def _admin(update, context):
            return admin
        async def _skin(chat_id):
            return layer._skin_loader.load(skin) if skin else {"persona_kind": "neko"}
        plugin._is_admin = _admin
        layer._resolve_active_skin = _skin
        await fn_(u, SimpleNamespace(args=list(args)))
        return " | ".join(u.message.out)

    async def admin_cases():
        """The real _is_admin (not the stub `call` installs): private chats, admins, members, API errors, anonymous admins."""
        status = plugin.ChatMemberStatus

        class Bot:
            def __init__(self, member_status=None, exc=None):
                self.member_status, self.exc = member_status, exc

            async def get_chat_member(self, chat_id, user_id):
                if self.exc:
                    raise self.exc
                return SimpleNamespace(status=self.member_status)

        def u(chat_type="supergroup", chat=-100500, user=SimpleNamespace(id=5), sender_chat=None, has_chat=True):
            return SimpleNamespace(effective_chat=SimpleNamespace(id=chat, type=chat_type) if has_chat else None, effective_user=user,
                                   effective_message=SimpleNamespace(sender_chat=sender_chat))

        ctx = lambda bot: SimpleNamespace(bot=bot)  # noqa: E731
        return {
            "private": await real_is_admin(u("private"), ctx(Bot(exc=RuntimeError("must not be asked")))),
            "admin": await real_is_admin(u(), ctx(Bot(status.ADMINISTRATOR))),
            "owner": await real_is_admin(u(), ctx(Bot(status.OWNER))),
            "member": await real_is_admin(u(), ctx(Bot(status.MEMBER))),
            "api_error": await real_is_admin(u(), ctx(Bot(exc=plugin.TelegramError("boom")))),
            "no_user": await real_is_admin(u(user=None), ctx(Bot(status.ADMINISTRATOR))),
            "no_chat": await real_is_admin(u(has_chat=False), ctx(Bot(status.ADMINISTRATOR))),
            "anonymous_admin": await real_is_admin(u(sender_chat=SimpleNamespace(id=-100500)), ctx(Bot(exc=RuntimeError("must not be asked")))),
            "channel_sender_is_not_admin": await real_is_admin(u(sender_chat=SimpleNamespace(id=-100999)), ctx(Bot(status.MEMBER))),
        }

    async def main():
        R = {}
        settings.persona_engine_enabled = False
        R["off"] = await call(plugin.cmd_pengine)
        settings.persona_engine_enabled = True
        fresh_service(memory=True)
        R["status_empty"] = await call(plugin.cmd_pengine)
        R["last_empty"] = await call(plugin.cmd_pengine, "last")
        R["debug_bad"] = await call(plugin.cmd_pengine, "debug")
        R["debug_on"] = await call(plugin.cmd_pengine, "debug", "on")
        R["debug_set"] = sorted(pe_runtime._service.debug_chats)
        R["debug_off"] = await call(plugin.cmd_pengine, "debug", "off")
        R["debug_set_after"] = sorted(pe_runtime._service.debug_chats)
        # memory commands
        settings.persona_engine_memory_enabled = False
        R["mem_off"] = await call(plugin.cmd_premember, "a", "fact")
        settings.persona_engine_memory_enabled = True
        R["non_admin"] = await call(plugin.cmd_premember, "a", "fact", admin=False)
        R["usage"] = await call(plugin.cmd_premember)
        R["no_person"] = await call(plugin.cmd_premember, "a", "fact", skin=None)
        R["remembered"] = await call(plugin.cmd_premember, "we", "are", "planning", "a", "goa", "trip")
        R["refused_contact"] = await call(plugin.cmd_premember, "call", "9876543210")
        R["listed"] = await call(plugin.cmd_pmemory)
        R["other_chat_listed"] = await call(plugin.cmd_pmemory, chat=-100888)
        R["forget_usage"] = await call(plugin.cmd_pforget, "banana")
        R["forget_one"] = await call(plugin.cmd_pforget, "1")
        R["after_forget"] = await call(plugin.cmd_pmemory)
        await call(plugin.cmd_premember, "another", "fact")
        R["forget_all"] = await call(plugin.cmd_pforget, "all")
        R["pmemory_non_admin"] = await call(plugin.cmd_pmemory, admin=False)
        # a real engine reply, then /pengine last and status
        R["reply"] = await layer._person_reply(-100777, layer._skin_loader.load("tanvi"), "kal milte hai?", None, None, "42")
        R["last"] = await call(plugin.cmd_pengine, "last")
        R["status"] = await call(plugin.cmd_pengine)
        # memory reaches the model through the plugin-managed store
        await call(plugin.cmd_premember, "the", "goa", "trip", "is", "in", "march")
        seen = []
        async def spy(messages, n, temperature, max_tokens):
            seen.append(messages[-1]["content"])
            return ["haan yaar"]
        pe_runtime._service._provider = CallableProvider(spy, "spy")   # engine_for() hands the current provider to the engine on every call
        await layer._person_reply(-100777, layer._skin_loader.load("tanvi"), "when is the goa trip", None, None, "42")
        R["memory_in_prompt"] = bool(seen) and "the goa trip is in march" in seen[0] and "[BOT MEMORY" in seen[0]
        R["is_admin"] = await admin_cases()
        return R

    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
