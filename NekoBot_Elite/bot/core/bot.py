"""
Bot initialization, plugin auto-discovery, and middleware wiring.

Also owns the startup/shutdown of the optional live-music MTProto client
(bot/core/music_client.py). That client runs a second Telegram
connection (Pyrogram, not the Bot API) in the same asyncio event loop as
the main python-telegram-bot Application — PTB's post_init/post_shutdown
hooks are the intended place to start/stop long-lived resources like
this alongside the bot, so no manual event-loop juggling is needed.
"""
import logging

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from bot.config import settings
from bot.core.database import init_db
from bot.core.errors import NekoError, reply_safe
from bot.core.menu import sanitize_menu
from bot.identity import bot_name
from bot.middleware.access_control import access_control_middleware
from bot.middleware.antiflood import antiflood_middleware
from bot.middleware.antispam import antispam_middleware
from bot.middleware.force_sub import force_sub_check_callback, force_sub_middleware
from bot.middleware.logging_middleware import log_middleware

logger = structlog.get_logger()


def setup_logging():
    from bot.utils.redact import install_stdlib_redaction, redact_event

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            redact_event,
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
    # Scrub secrets (bot token, API keys) from every stdlib log line and traceback...
    install_stdlib_redaction(logging.getLogger())
    # ...and keep the HTTP client libraries quiet: httpx logs each Bot API request URL
    # (which embeds the bot token) at INFO on every long-poll cycle.
    if not settings.debug:
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)



async def global_error_handler(update: object, context) -> None:
    """Last-resort error handler registered on the Application.
    Logs full context and attempts a non-leaking reply.
    """
    err = context.error
    chat_id = user_id = None
    if isinstance(update, Update):
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
    logger.error(
        "unhandled_update_error",
        error=str(err),
        error_type=type(err).__name__ if err else None,
        chat_id=chat_id,
        user_id=user_id,
        exc_info=err,
    )
    if isinstance(update, Update):
        msg = (
            err.user_message
            if isinstance(err, NekoError)
            else f"Something went sideways inside {bot_name()}. The error is logged."
        )
        await reply_safe(update, msg)


def discover_plugins(application: Application) -> None:
    """Auto-discover and register all plugin modules under bot/plugins/."""
    import importlib
    import pkgutil

    import bot.plugins as plugins_pkg

    for _, name, ispkg in pkgutil.iter_modules(plugins_pkg.__path__):
        if ispkg:
            continue
        try:
            module = importlib.import_module(f"bot.plugins.{name}")
            if hasattr(module, "register"):
                module.register(application)
                logger.info("plugin_loaded", plugin=name)
        except Exception as e:
            logger.error("plugin_load_failed", plugin=name, error=str(e), exc_info=True)


async def _start_live_music(application: Application) -> None:
    """Start the optional MTProto voice-chat client(s). Failures here are
    logged and swallowed — every other plugin works fine without it."""
    if not settings.live_music_configured:
        logger.info("live_music_disabled", reason="MUSIC_API_ID/MUSIC_API_HASH not set")
        return

    try:
        from bot.core.music_client import get_assistant_pool
        import bot.plugins.music_live as music_live

        pool = get_assistant_pool()
        await pool.initialize()
        music_live.set_pool(pool)
        pool.on_stream_end(music_live.on_track_end)

        application.bot_data["assistant_pool"] = pool
        logger.info("live_music_started", assistants=len(pool.pairs))
    except Exception as e:
        logger.error("live_music_start_failed", error=str(e), exc_info=True)


async def _stop_live_music(application: Application) -> None:
    pool = application.bot_data.get("assistant_pool")
    if pool:
        try:
            await pool.shutdown()
        except Exception:
            logger.error("live_music_stop_failed", exc_info=True)


async def _try_import_and_call(module_path: str, func_name: str, *args):
    """Lazily import a plugin module and await one of its functions.
    Used for optional startup tasks (job re-arming, shop seeding) so
    core/bot.py doesn't need a hard import-time dependency on every
    plugin that happens to need startup work."""
    import importlib

    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    return await func(*args)


async def _heartbeat(context) -> None:
    """Periodically writes a status snapshot the separate dashboard
    service reads — the dashboard has no direct access into this
    process, only into the same database."""
    from datetime import datetime

    from sqlalchemy import func, select

    from bot.core.database import BotStats, Chat, User, async_session

    async with async_session() as session:
        total_chats = (await session.execute(select(func.count()).select_from(Chat))).scalar() or 0
        total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0

        active_calls = 0
        pool = context.application.bot_data.get("assistant_pool")
        if pool:
            active_calls = pool.total_active_calls

        row = await session.get(BotStats, 1)
        if row is None:
            row = BotStats(id=1, started_at=datetime.utcnow())
            session.add(row)
        row.last_heartbeat = datetime.utcnow()
        row.total_chats = total_chats
        row.total_users = total_users
        row.live_music_active_chats = active_calls
        await session.commit()


async def _sweep_idle_assistants(context) -> None:
    """Frees up assistant slots for chats that have gone idle longer
    than ASSISTANT_AUTO_LEAVE_SECONDS — see AssistantPool.sweep_idle."""
    pool = context.application.bot_data.get("assistant_pool")
    if pool:
        swept = await pool.sweep_idle()
        if swept:
            logger.info("idle_assistants_swept", count=swept)


async def _check_autoend(context) -> None:
    """When BotConfig.autoend_enabled is on, leaves any voice chat that
    has no real participants left (only the assistant itself) — from
    AnonXMusic's /autoend. Distinct from the idle-activity sweep above:
    this checks who's actually in the call, not how long since the last
    play/skip. PyTgCalls.get_participants() is a less battle-tested part
    of the API surface for this project than join/play/stop, so this is
    wrapped defensively — a lookup failure for one chat just skips it
    rather than raising."""
    pool = context.application.bot_data.get("assistant_pool")
    if not pool or not pool.pairs:
        return

    from bot.core.database import async_session, get_bot_config

    async with async_session() as session:
        cfg = await get_bot_config(session)
        enabled = cfg.autoend_enabled
        await session.commit()
    if not enabled:
        return

    import bot.plugins.music_live as music_live

    for chat_id, idx in list(pool.active_chats.items()):
        client, engine = pool.pairs[idx]
        try:
            participants = await client.pytgcalls.get_participants(chat_id)
            real_participants = [p for p in participants if not getattr(p, "is_self", False)]
            if not real_participants:
                await engine.stop(chat_id)
                pool.release(chat_id)
                music_live._clear_position(chat_id)
                logger.info("autoend_left_empty_chat", chat_id=chat_id)
        except Exception:
            logger.error("autoend_check_failed", chat_id=chat_id, exc_info=True)


async def _verify_data_assets() -> None:
    """Log once at startup if critical personality data files are missing.

    Does not abort boot — features degrade gracefully — but makes
    misconfigured DATA_DIR / CWD issues visible in logs immediately.
    """
    root = settings.data_dir
    required = [
        root / "personality" / "skins" / "neko.json",
        root / "personality" / "dataset" / "fewshot_bank.json",
        root / "jokes.json",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        logger.warning("startup_data_assets_missing", paths=missing, data_dir=str(root))
    else:
        skins = list((root / "personality" / "skins").glob("*.json")) if (root / "personality" / "skins").exists() else []
        presets = list((root / "personality" / "presets").glob("*.json")) if (root / "personality" / "presets").exists() else []
        logger.info(
            "startup_data_assets_ok",
            data_dir=str(root),
            skins=len(skins),
            presets=len(presets),
        )


async def post_init(application: Application) -> None:
    await init_db()
    await _start_live_music(application)

    try:
        await _verify_data_assets()
    except Exception as e:
        logger.warning("startup_data_assets_check_failed", error=str(e))

    # Idempotent startup tasks from the new plugins — safe to run every
    # boot. Each is independently guarded so one failing doesn't block
    # the others or bot startup itself.
    for label, coro in [
        ("shop_seed", _try_import_and_call("bot.plugins.economy", "seed_shop_items")),
        ("scheduling_rearm", _try_import_and_call("bot.plugins.scheduling", "rearm_jobs", application)),
        ("automod_rearm", _try_import_and_call("bot.plugins.automod", "rearm_jobs", application)),
        # Reloads persisted custom instructions/skins/configs from the DB
        # into personality.py's in-memory caches, and schedules the daily
        # personality-data cleanup job.
        ("personality_startup", _try_import_and_call("bot.plugins.personality", "on_startup", application)),
        # Drops whispers that expired while the bot was down.
        ("whisper_startup", _try_import_and_call("bot.plugins.whisper", "on_startup", application)),
    ]:
        try:
            await coro
        except Exception as e:
            logger.error("startup_task_failed", task=label, error=str(e), exc_info=True)

    application.job_queue.run_repeating(
        _heartbeat, interval=settings.dashboard_heartbeat_interval, first=5, name="dashboard_heartbeat"
    )

    if settings.live_music_configured and settings.assistant_auto_leave_seconds:
        application.job_queue.run_repeating(_sweep_idle_assistants, interval=60, first=60, name="idle_assistant_sweep")

    if settings.live_music_configured:
        application.job_queue.run_repeating(_check_autoend, interval=45, first=45, name="autoend_check")

    commands = [
        ("start", "Initialize bot & show menu"),
        ("help", "Show help menu"),
        ("settings", "Quick settings panel"),
        ("lang", "Change the bot's language"),
        ("menu", "Open inline control panel"),
        ("ai", "General AI assistant"),
        ("aiclear", "Clear your /ai history"),
        ("chat", "Talk to Neko (personality)"),
        ("persona", "Set a custom AI persona"),
        ("see", "Ask AI about a photo (vision)"),
        ("transcribe", "Transcribe a voice message"),
        ("anime", "Anime search & info"),
        ("music", "Download & send a track as a file"),
        ("play", "Play music live in the voice chat (YT/Spotify/Apple/SoundCloud/Resso)"),
        ("skip", "Skip the current track"),
        ("queue", "Show the music queue"),
        ("nowplaying", "Show the current track"),
        ("loop", "Repeat the current track N times"),
        ("seek", "Jump to a position in the current track"),
        ("toptracks", "Most-played tracks"),
        ("grab", "Catch the spawned character"),
        ("collection", "See your caught characters"),
        ("myprofile", "Your collector stats"),
        ("auth", "Let a non-admin control playback"),
        ("gban", "Ban a user across every chat (sudo)"),
        ("globalstats", "Bot-wide stats (sudo)"),
        ("playlist", "Save/load a music playlist"),
        ("lyrics", "Look up lyrics for a song"),
        ("joke", "Force a joke out of the personality layer"),
        ("personality", "Toggle sarcastic auto-replies: on|off"),
        ("nnr_settings", "Neko personality settings hub"),
        ("nnr_help", "List all Neko personality commands"),
        ("names", "See a user's recorded name/username history"),
        ("f1", "Preview font 1 (see /fonts for all 18)"),
        ("fonts", "Preview all fonts"),
        ("flip", "Upside-down text"),
        ("fontfx", "Text effects: stinky, bubbles, underline..."),
        ("level", "Your level & XP in this chat"),
        ("leaderboard", "Top XP in this chat"),
        ("daily", "Claim your daily coins"),
        ("balance", "Check your coin balance"),
        ("shop", "Browse the coin shop"),
        ("trivia", "Play a trivia round"),
        ("tictactoe", "Challenge someone to tic-tac-toe"),
        ("poll", "Create a poll"),
        ("remind", "Set a personal reminder"),
        ("warn", "Warn a user"),
        ("ban", "Ban a user"),
        ("mute", "Mute a user"),
        ("kick", "Kick a user"),
        ("notes", "Manage notes"),
        ("filters", "Manage filters"),
        ("welcome", "Set welcome message"),
        ("rules", "Show chat rules"),
        ("antiraid", "Toggle anti-raid protection"),
        ("lockdown", "Manually lock the chat down"),
        ("report", "Report a message"),
        ("newfed", "Start a federation"),
        ("fedban", "Ban a user across a whole federation"),
        ("disable", "Disable a command for non-admins here"),
        ("zombies", "Find deleted accounts in this chat"),
        ("id", "Get user/chat ID"),
        ("info", "Get user info"),
        ("afk", "Set AFK status"),
        ("karma", "Karma system"),
        ("purge", "Delete messages"),
        ("locks", "Manage content locks"),
        ("nightmode", "Toggle nightmode"),
        ("captcha", "Configure CAPTCHA"),
        ("log", "Set log channel"),
        ("stats", "Chat statistics"),
        ("tts", "Text to speech"),
        ("tr", "Translate text"),
        ("pokedex", "Pokedex lookup"),
        ("currency", "Convert currencies"),
        ("telegraph", "Publish text to Telegraph"),
        ("kang", "Add a sticker/image to your pack"),
        ("packinfo", "Show your sticker pack"),
        ("antichannel", "Block channel-identity spam (admin)"),
        ("ud", "Urban Dictionary"),
        ("imdb", "Movie/TV info"),
        ("weather", "Weather forecast"),
        ("news", "Latest news"),
        ("qr", "Generate QR code"),
        ("tagall", "Tag all members"),
        ("couples", "Couple of the day"),
        ("fun", "Fun commands"),
        ("truth", "Random truth question"),
        ("dare", "Random dare"),
        ("carbon", "Code/text carbon image"),
        ("wallpaper", "Search wallpapers"),
        ("webss", "Website screenshot"),
        ("gps", "Location pin + Maps link"),
        ("gsearch", "Web search summary"),
        ("roll", "Roll dice (NdM)"),
        ("toss", "Coin toss"),
        ("decide", "Yes/No decide"),
        ("whisper", "Send a private whisper (also inline: @bot @user text)"),
        ("rankings", "Group message leaderboard"),
        ("mytop", "Your most active groups"),
        # Telegram allows 100 menu entries. /slap /pat /table /shrug /shout /runs /bluetext /slaptext /pattext still work and are in /help.
    ]
    menu, dropped = sanitize_menu(commands)
    if dropped:
        logger.warning("command_menu_entries_dropped", dropped=dropped)
    try:
        await application.bot.set_my_commands(menu)
    except Exception as e:  # a rejected menu must never stop the bot from starting
        logger.warning("set_my_commands_failed", error=str(e))

    # Startup diagnostics for the most common "the bot seems dead" causes
    # — none of these can be detected reliably at runtime (Telegram's Bot
    # API doesn't expose a bot's own privacy-mode setting, and "no one
    # has said anything in group chat yet" isn't distinguishable from
    # "the bot can't see ambient messages"), so surface them every boot
    # instead of only when something goes wrong.
    logger.info(
        "startup_checklist",
        group_privacy_mode_reminder=(
            "If ambient (non-command) group replies — personality banter, "
            "AFK detection, XP, filters, the collector spawner — don't seem "
            "to work while commands do, check @BotFather -> /mypbot -> Bot "
            "Settings -> Group Privacy. Telegram only delivers commands, "
            "@mentions, and replies-to-the-bot while Privacy Mode is ON "
            "(the default for every new bot); ordinary group text is never "
            "even sent to the bot. Disable it there, or make the bot a "
            "group admin (which also disables the filter) — this can't be "
            "changed from bot code."
        ),
        force_sub=(
            f"enabled, channel={settings.force_sub_channel!r}"
            if settings.force_sub_enabled and settings.force_sub_channel
            else "disabled"
        ),
        personality_default_dm=settings.personality_default_dm,
        personality_default_group=settings.personality_default_group,
    )
    if settings.force_sub_enabled and not settings.force_sub_channel:
        logger.warning(
            "force_sub_misconfigured",
            note="FORCE_SUB_ENABLED=true but FORCE_SUB_CHANNEL is blank — "
                 "force_sub_middleware treats this as off (see its own guard), "
                 "so nothing is actually blocked, but this combination almost "
                 "certainly isn't what was intended. Set FORCE_SUB_CHANNEL or "
                 "set FORCE_SUB_ENABLED=false.",
        )

    logger.info("bot_initialized", mode="webhook" if settings.webhook_url else "polling")


async def post_shutdown(application: Application) -> None:
    await _stop_live_music(application)
    # Write the message counts still buffered in memory (one flush interval's worth at most).
    try:
        await _try_import_and_call("bot.plugins.chatfight", "flush_on_shutdown")
    except Exception:
        logger.error("activity_shutdown_flush_failed", exc_info=True)


def create_application() -> Application:
    setup_logging()

    persist_path = settings.data_dir / "bot_data.pkl"
    try:
        if persist_path.exists() and persist_path.stat().st_size > 0:
            backup_path = settings.data_dir / "bot_data.pkl.bak"
            backup_path.write_bytes(persist_path.read_bytes())
    except Exception as e:
        logger.warning("pickle_persistence_backup_failed", error=str(e))

    persistence = PicklePersistence(filepath=str(persist_path))
    builder = (
        Application.builder()
        .token(settings.bot_token)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if settings.concurrent_updates > 0:
        # Per-chat ordered, cross-chat concurrent — see bot/core/concurrency.py.
        from bot.core.concurrency import build_update_processor

        builder = builder.concurrent_updates(build_update_processor(settings.concurrent_updates))
    application = builder.build()

    # Global middleware — distinct negative groups so they always run
    # before any plugin handler, regardless of plugin load order.
    application.add_handler(MessageHandler(filters.ALL, access_control_middleware), group=-5)
    # Its own group, one step after access_control: PTB runs only the first
    # matching handler per group, so sharing a group would silently disable
    # this gate. Bans, blacklist, and maintenance mode still take priority
    # over a "please join our channel" prompt (a banned user shouldn't see
    # one at all) because group -5 runs first and stops the update.
    application.add_handler(MessageHandler(filters.ALL, force_sub_middleware), group=-4)
    application.add_handler(CallbackQueryHandler(force_sub_check_callback, pattern=r"^fsub:check$"))
    application.add_handler(MessageHandler(filters.ALL, log_middleware), group=-3)
    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, antiflood_middleware), group=-2)
    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, antispam_middleware), group=-1)

    discover_plugins(application)

    application.add_error_handler(global_error_handler)
    return application
