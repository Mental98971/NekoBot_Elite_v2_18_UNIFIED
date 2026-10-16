"""
Bot identity & branding — single source of truth for the user-facing
name, username, and owner display.

To rebrand every user-facing surface at once (/start, /help, error
messages, personality responses, collector messages, music messages,
group-management messages, ...), change BOT_DISPLAY_NAME /
BOT_DISPLAY_USERNAME / BOT_OWNER_NAME in .env — see bot/config.py.
Nothing here needs editing for a routine rebrand; this module just
exposes those settings as small helpers so call sites don't reach into
`settings` directly for display strings, and documents the history.

Historical note — internal only, not shown to users
----------------------------------------------------
This codebase, its Python package name (`bot`), database tables,
Docker image, environment variable names, internal log fields, and
command-prefix conventions (e.g. the personality layer's `/nnr_*`
commands, short for its original "Nanora" codename) all still say
"novabot" / "nanora" internally. That's intentional, not an oversight:
renaming the underlying package, imports, class names, or database
schema would break every existing deployment, .env file, and database
for zero user-facing benefit — see the project's own README for why
`_auto_migrate()` and similar exist specifically to avoid destructive
renames. Only the *displayed* identity changed. See BOT_HISTORY below.

Do not "clean up" the internal names to match the current display name;
do not let the internal names leak into anything a Telegram user reads.
"""
from __future__ import annotations

from bot.config import settings

BOT_HISTORY = {
    "current_name": None,       # filled in below, from settings
    "current_username": None,
    "owner": None,
    "previous_names": ["NovaBot", "NovaGuard"],
    "history_note": (
        "This bot was previously developed and released under the "
        "NovaBot name (itself a merge of nova_guard_bot, "
        "harmony-music-bot, nanora_bot, and font_bot_ultimate.py — see "
        "README.md for the full history). The underlying Python "
        "package name, database tables, Docker image, and environment "
        "variable names remain 'novabot' internally for backward "
        "compatibility with existing deployments and databases; only "
        "the user-facing display name changed. The personality layer's "
        "'nnr_' command prefix and 'nanora'-named skin/config internals "
        "are the same kind of internal holdover from its original "
        "'Nanora' codename. None of this should ever appear in text a "
        "user can read — use bot_name()/bot_username()/owner_name() "
        "from this module for anything user-facing."
    ),
}


def bot_name() -> str:
    """The user-facing bot name (default: 'Neko'). Override with
    BOT_DISPLAY_NAME in .env.

    Never imply or state a gender for the bot in user-facing text;
    gender is intentionally unspecified and must not be disclosed.
    """
    return settings.bot_display_name


def bot_username() -> str:
    """The user-facing @username, without a leading '@' — callers add
    one where needed (default: 'Nekooooooobot'). Override with
    BOT_DISPLAY_USERNAME in .env."""
    return settings.bot_display_username.lstrip("@")


def owner_name() -> str:
    """The user-facing owner display name (default: 'Star'). Purely
    cosmetic — distinct from settings.owner_id, the numeric Telegram ID
    actually used for permission checks. Override with BOT_OWNER_NAME
    in .env.

    Never imply or state a gender for the owner in user-facing text.
    """
    return settings.bot_owner_name


BOT_HISTORY["current_name"] = bot_name()
BOT_HISTORY["current_username"] = f"@{bot_username()}"
BOT_HISTORY["owner"] = owner_name()
