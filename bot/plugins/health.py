"""Owner/admin health & diagnostics command."""
from __future__ import annotations

import platform
import time
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.config import settings
from bot.core.errors import safe_handler, PermissionError
from bot.identity import bot_name, bot_username, owner_name
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_START = time.monotonic()


def _uptime() -> str:
    secs = int(time.monotonic() - _START)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _fmt_metrics() -> str:
    try:
        from bot.utils.metrics import snapshot
        s = snapshot()
        c = s.get("counters") or {}
        top = sorted(c.items(), key=lambda x: -x[1])[:5]
        return ", ".join(f"{k}={v}" for k, v in top) or "none"
    except Exception:
        return "n/a"


@safe_handler
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    allowed = {settings.owner_id, *settings.admin_ids}
    if user.id not in allowed:
        raise PermissionError("Health is owner/admin only.")

    # DB ping
    db_ok = "unknown"
    try:
        from bot.core.database import async_session
        from sqlalchemy import text
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        db_ok = "ok"
    except Exception as e:
        db_ok = f"fail ({type(e).__name__})"

    # Cache
    from bot.utils.cache import cache
    cache_backend = "memory"
    if getattr(settings, "redis_url", None):
        cache_backend = "redis (configured)"
    try:
        await cache.set("_health_ping", 1, ttl=5)
        v = await cache.get("_health_ping")
        cache_status = "ok" if v == 1 else "degraded"
    except Exception:
        cache_status = "fail"

    # Music
    pool = context.application.bot_data.get("assistant_pool")
    music = "disabled"
    if pool:
        try:
            music = f"active assistants={len(getattr(pool, 'pairs', []) or [])} calls={getattr(pool, 'total_active_calls', '?')}"
        except Exception:
            music = "error reading pool"

    # Personality / few-shot
    fewshot = "n/a"
    try:
        from bot.personality.examples import example_bank
        example_bank.load()
        fewshot = f"{example_bank.count} examples / {len(example_bank.personalities())} personalities"
    except Exception as e:
        fewshot = f"error ({e})"

    # Presets / skins on disk
    preset_count = skin_count = "?"
    try:
        preset_count = len(list((settings.data_dir / "personality" / "presets").glob("*.json")))
        skin_count = len(list((settings.data_dir / "personality" / "skins").glob("*.json")))
    except Exception:
        pass

    lines = [
        f"<b>{bot_name()}</b> (@{bot_username()}) health",
        f"Owner display: {owner_name()}",
        f"Uptime: {_uptime()}",
        f"Python: {platform.python_version()}",
        f"Config version: {getattr(settings, 'config_version', '?')}",
        f"DB: {db_ok}",
        f"Cache: {cache_status} ({cache_backend})",
        f"Live music: {music}",
        f"Few-shot bank: {fewshot}",
        f"Presets/skins: {preset_count}/{skin_count}",
        f"Metrics: {_fmt_metrics()}",
        f"Debug: {settings.debug}",
        f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("status", cmd_health))
