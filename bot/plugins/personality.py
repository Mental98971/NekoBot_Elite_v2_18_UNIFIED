"""
Personality / banter plugin v2.3 — all 28 improvements applied.

v2.3 changes:
  - Atomic increment_and_check() replaces race-prone check+increment (fix 1.2).
  - Response delay uses JobQueue instead of blocking asyncio.sleep (fix 1.7).
  - handle_message passes skin_data to subplugins via route() kwargs (fix 1.3).
  - Inline keyboard has Back buttons for navigation (fix 3.3).
  - /dpersonality and nnr_settings>Reset have aligned scope (fix 3.4).
  - /nnr_test previews personality response without sending to chat (feature 4.3).
  - /nnr_help unified command list (feature 4.8).
  - Graceful DB degradation: fallback to stateless mode on DB failure (feature 4.7).
  - hashlib imported at module level (quality 5.1-5.2).
  - _is_enabled_for cached for 30s (fix 2.2).
  - Startup loading via personality.load_persisted_settings() (fix 1.1/3.1).
  - Daily cleanup job for old personality data (feature 4.6).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import random
import time

from cachetools import TTLCache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters,
)

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.decorators import owner_only
from bot.core.errors import safe_handler
from bot.personality.custom_instructions import instruction_processor
from bot.personality.jokes import jokes_db, Joke
from bot.personality import person_consent
from bot.personality import persons as persons_mod
from bot.personality.memory import memory
from bot.personality.personality import personality
from bot.personality.subplugins import (
    AnimePlugin, GamingPlugin, GeneralPlugin, MetaBanterPlugin,
    PluginManager, ProgrammingPlugin,
)
from bot.personality.triggers import triggers
from bot.utils.logger import get_logger
from bot.utils.helpers import escape_html
from bot.utils.textutil import paginate_joined

logger = get_logger(__name__)

_cooldowns: TTLCache = TTLCache(
    maxsize=10_000,
    ttl=max(settings.personality_cooldown_seconds * 2, 10),
)

# v2.3: Cache for _is_enabled_for (30s TTL)
_enabled_cache: TTLCache = TTLCache(maxsize=500, ttl=30)

_plugin_manager = PluginManager()
_plugin_manager.register(ProgrammingPlugin())
_plugin_manager.register(AnimePlugin())
_plugin_manager.register(GamingPlugin())
_plugin_manager.register(MetaBanterPlugin())
_plugin_manager.register(GeneralPlugin())


def _on_cooldown(user_id: int, chat_id: int) -> bool:
    now = time.time()
    key = (user_id, chat_id)
    last = _cooldowns.get(key, 0)
    if now - last < settings.personality_cooldown_seconds:
        return True
    _cooldowns[key] = now
    return False


def _identity(user) -> "persons_mod.Identity | None":
    """Deterministic person lookup for a Telegram user (account id first, then username)."""
    if user is None:
        return None
    try:
        return persons_mod.get_service().registry.resolve(user.id, getattr(user, "username", None))
    except Exception as exc:  # identity must never break a reply
        logger.warning("identity_resolution_failed: %s", exc)
        return None


def _identity_key(user, ident: "persons_mod.Identity | None" = None) -> int:
    """One person = one cooldown / message-limit bucket, whichever of their accounts is talking."""
    ident = ident if ident is not None else _identity(user)
    return ident.person.primary_account_id if ident else user.id


async def _is_enabled_for(chat_id: int, chat_type: str) -> bool:
    # v2.3: Check cache first
    cache_key = (chat_id, chat_type)
    if cache_key in _enabled_cache:
        return _enabled_cache[cache_key]

    default = settings.personality_default_dm if chat_type == "private" else settings.personality_default_group
    try:
        async with async_session() as session:
            row = await session.get(Chat, chat_id)
            if row is not None and row.personality_enabled is not None:
                result = row.personality_enabled
            else:
                result = default
    except Exception as e:
        logger.warning("DB error in _is_enabled_for, using default: %s", e)
        result = default

    _enabled_cache[cache_key] = result
    return result


async def _get_response_delay(chat_id: int) -> float:
    """Get the per-chat response delay, falling back to global default."""
    try:
        async with async_session() as session:
            row = await session.get(Chat, chat_id)
            if row is not None:
                delay = row.personality_response_delay
                if delay is not None:
                    return max(settings.personality_response_delay_min, delay)
    except Exception as e:
        logger.warning("DB error in _get_response_delay, using default: %s", e)
    return settings.personality_response_delay_default


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True in private chats and for admins/owner of the current chat (including admins posting anonymously as the group).

    Fails closed: an update without a chat/user, or a Telegram API error while looking the member up, means "not an admin".
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if chat.type == "private":
        return True
    message = update.effective_message
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None and sender_chat.id == chat.id:  # admin writing "as the group"
        return True
    if user is None:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


# ==================== /nnr_settings COMMAND =========================

def _settings_keyboard():
    """Build the main settings keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Custom Instructions", callback_data="nnr_ci")],
        [InlineKeyboardButton("User Limit", callback_data="nnr_ul"),
         InlineKeyboardButton("Response Delay", callback_data="nnr_rd")],
        [InlineKeyboardButton("Skin", callback_data="nnr_skin"),
         InlineKeyboardButton("Sarcasm/Metaphors", callback_data="nnr_cfg")],
        [InlineKeyboardButton("Reset All", callback_data="nnr_reset")],
    ])


def _back_keyboard():
    """v2.3: Back button for sub-pages."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("<< Back", callback_data="nnr_main")],
    ])


def _format_settings_text(chat, chat_settings) -> str:
    """Render the settings-hub body text. Shared by the initial /nnr_settings
    command and the callback's "<< Back" handler so the two can't drift."""
    ci = chat_settings.custom_instructions
    ci_display = (ci[:60] + "...") if ci and len(ci) > 60 else (ci or "Not set")
    return (
        f"Neko Settings - {chat.title or 'this chat'}\n"
        f"{'=' * 35}\n"
        f"Custom Instructions: {ci_display}\n"
        f"Skin: {chat_settings.skin or 'neko'}\n"
        f"User Limit: {chat_settings.user_limit or settings.personality_user_limit_default}\n"
        f"Response Delay: {chat_settings.response_delay if chat_settings.response_delay is not None else settings.personality_response_delay_default}s\n"
        f"{'=' * 35}\n"
        f"Tap a button below to adjust:"
    )


@safe_handler
async def cmd_nnr_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main settings hub for Neko in group chats."""
    chat = update.effective_chat

    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    chat_settings = await memory.get_settings(chat.id)
    text = _format_settings_text(chat, chat_settings)
    await update.message.reply_text(text, reply_markup=_settings_keyboard())


@safe_handler
async def cmd_nnr_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks from /nnr_settings."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    data = query.data

    if not await _is_admin(update, context):
        await query.edit_message_text("Admin only.")
        return

    # v2.3: Back button returns to main menu
    if data == "nnr_main":
        chat_settings = await memory.get_settings(chat.id)
        text = _format_settings_text(chat, chat_settings)
        await query.edit_message_text(text, reply_markup=_settings_keyboard())
        return

    if data == "nnr_ci":
        await query.edit_message_text(
            "Custom Instructions\n\n"
            "Send a message starting with /nnr_ci followed by your instructions.\n"
            "Examples:\n"
            "  /nnr_ci be cute and supportive, no sarcasm\n"
            "  /nnr_ci speak like a medieval knight\n"
            "  /nnr_ci be serious and professional\n"
            "  /nnr_ci be chaotic and unhinged\n\n"
            "Available presets: " + ", ".join(instruction_processor.available_presets()) + "\n\n"
            "Use /nnr_ci clear to remove custom instructions.",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_ul":
        current = (await memory.get_settings(chat.id)).user_limit or settings.personality_user_limit_default
        await query.edit_message_text(
            f"User Limit\n\n"
            f"Current: {current} messages per user\n"
            f"Max: {settings.personality_user_limit_max}\n\n"
            f"Use: /nnr_ul <number> (1-{settings.personality_user_limit_max})\n"
            f"Example: /nnr_ul 50",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_rd":
        current_delay = (await memory.get_settings(chat.id)).response_delay
        current_val = current_delay if current_delay is not None else settings.personality_response_delay_default
        await query.edit_message_text(
            f"Response Delay\n\n"
            f"Current: {current_val}s\n"
            f"Min: {settings.personality_response_delay_min}s\n\n"
            f"Use: /nnr_rd <seconds>\n"
            f"Example: /nnr_rd 1.5  (or /nnr_rd 0 for instant)",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_skin":
        skins = personality.available_skins()
        current = personality.get_chat_skin(chat.id)
        await query.edit_message_text(
            f"Skin\n\n"
            f"Current: {current}\n"
            f"Available: {', '.join(skins)}\n\n"
            f"Use: /cpersonality skin <name>",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_cfg":
        cfg = personality.get_chat_config_dict(chat.id)
        await query.edit_message_text(
            f"Config\n\n"
            f"Sarcasm: {cfg['sarcasm_level']}\n"
            f"Metaphors: {cfg['metaphor_frequency']}\n"
            f"Deadpan: {'on' if cfg['deadpan'] else 'off'}\n"
            f"Max Length: {cfg['max_response_length']}\n\n"
            f"Use: /cpersonality sarcasm 0.3\n"
            f"     /cpersonality metaphors 0.6\n"
            f"     /cpersonality deadpan off\n"
            f"     /cpersonality max_length 600",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_reset":
        # v2.3: Aligned with /dpersonality scope (fix 3.4)
        personality.reset_chat_config(chat.id)
        personality.clear_custom_instructions(chat.id)
        await memory.clear_all_settings(chat.id)
        await query.edit_message_text(
            "All settings reset to defaults. Neko is back to her usual self.",
            reply_markup=_back_keyboard(),
        )


# ─── /nnr_ci — Set Custom Instructions ───────────────────────────────

@safe_handler
async def cmd_nnr_ci(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set or clear custom instructions for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    chat_id = update.effective_chat.id

    if not context.args:
        chat_settings = await memory.get_settings(chat_id)
        ci = chat_settings.custom_instructions
        text = (
            "Custom Instructions\n\n"
            f"Current: {ci or 'Not set'}\n\n"
            "Usage:\n"
            "  /nnr_ci be cute and supportive, no sarcasm\n"
            "  /nnr_ci speak like a medieval knight\n"
            "  /nnr_ci be serious and professional\n\n"
            "Available presets: " + ", ".join(instruction_processor.available_presets()) + "\n\n"
            "  /nnr_ci clear  - remove custom instructions"
        )
        await update.message.reply_text(text)
        return

    arg_text = " ".join(context.args)

    if arg_text.lower() == "clear":
        personality.clear_custom_instructions(chat_id)
        await memory.clear_custom_instructions(chat_id)
        await update.message.reply_text("Custom instructions cleared. Back to my usual charming self.")
        return

    # Parse and apply
    traits = personality.set_custom_instructions(chat_id, arg_text)
    await memory.set_custom_instructions(chat_id, arg_text)

    # Report what was detected
    if traits.tone:
        await update.message.reply_text(
            f"Custom instructions set. Detected tone: {traits.tone}.\n"
            f"Sarcasm: {traits.sarcasm_level}, Deadpan: {traits.deadpan}, "
            f"Metaphors: {traits.metaphor_frequency}.\n"
            f"I'll try to follow these. No promises though. I'm testing this too."
        )
    else:
        await update.message.reply_text(
            "Custom instructions set. I'll do my best to follow them.\n"
            "This is experimental - I might not perfectly match what you expect."
        )


# ─── /nnr_ul — Set User Limit ────────────────────────────────────────

@safe_handler
async def cmd_nnr_ul(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the per-user message limit for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        current = (await memory.get_settings(update.effective_chat.id)).user_limit
        current_val = current or settings.personality_user_limit_default
        await update.message.reply_text(
            f"User Limit: {current_val}\n"
            f"Usage: /nnr_ul <number> (1-{settings.personality_user_limit_max})"
        )
        return

    try:
        val = int(context.args[0])
        val = max(1, min(settings.personality_user_limit_max, val))
    except ValueError:
        await update.message.reply_text(f"Must be a number between 1 and {settings.personality_user_limit_max}.")
        return

    await memory.set_user_limit(update.effective_chat.id, val)
    await update.message.reply_text(
        f"User limit set to {val}. I'll 'get tired' of someone after {val} messages. "
        f"{'Generous.' if val > 50 else 'Reasonable.' if val > 20 else 'Strict. I like it.'}"
    )


# ─── /nnr_rd — Set Response Delay ────────────────────────────────────

@safe_handler
async def cmd_nnr_rd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the response delay for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        current = (await memory.get_settings(update.effective_chat.id)).response_delay
        current_val = current if current is not None else settings.personality_response_delay_default
        await update.message.reply_text(
            f"Response Delay: {current_val}s\n"
            f"Usage: /nnr_rd <seconds> (min {settings.personality_response_delay_min})"
        )
        return

    try:
        val = float(context.args[0])
        val = max(settings.personality_response_delay_min, val)
    except ValueError:
        await update.message.reply_text(f"Must be a number (min {settings.personality_response_delay_min}).")
        return

    await memory.set_response_delay(update.effective_chat.id, val)
    await update.message.reply_text(
        f"Response delay set to {val}s. "
        f"{'I will be zippy.' if val < 1 else 'I will take my time.' if val > 5 else 'Balanced pace.'}"
    )


# ==================== /nnr_test — Preview (v2.3) ====================

@safe_handler
async def cmd_nnr_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2.3: Preview what Neko would say without sending to the chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /nnr_test <message>\nExample: /nnr_test hello there")
        return

    test_message = " ".join(context.args)
    chat_id = update.effective_chat.id

    trigger_matches = triggers.analyze(test_message)
    intents = [m.intent for m in trigger_matches]

    # Get skin data for subplugins (fix 1.3)
    skin_data = await personality._resolve_active_skin(chat_id)

    plugin_result = await _plugin_manager.route(
        test_message, chat_id, [], intents=intents, skin_data=skin_data
    )

    if plugin_result and plugin_result.response:
        response = await personality.rewrite(chat_id, plugin_result.response, intents, test_message)
    else:
        response = await personality.generate_direct(chat_id, intents, test_message)

    await update.message.reply_text(
        f"Preview for: \"{test_message}\"\n"
        f"Intents: {intents or 'none'}\n"
        f"Skin: {personality.get_chat_skin(chat_id)}\n"
        f"CI: {'yes' if personality.has_custom_instructions(chat_id) else 'no'}\n"
        f"{'-' * 30}\n"
        f"{response}"
    )


# ==================== /nnr_help — Unified Help (v2.3) ===============

@safe_handler
async def cmd_nnr_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2.3: Unified command list."""
    lines = [
        "Neko Commands:\n",
        "/nnr_settings - Settings hub (admin)",
        "/nnr_ci [text] - Custom instructions (admin)",
        "/nnr_ul [n] - User message limit (admin)",
        "/nnr_rd [s] - Response delay (admin)",
        "/nnr_test [msg] - Preview personality response (admin)",
        "/cpersonality [key] [val] - Fine-tune personality (admin)",
        "/dpersonality - Reset to defaults (admin)",
        "/personality on|off - Toggle personality (admin)",
        "/personalitystatus - Show stats",
        "/joke [category] - Get a joke",
        "/mystats - Your stats",
        "/addjoke <cat> | <text> - Add joke (owner)",
        "/nnr_help - This message",
    ]
    await update.message.reply_text("\n".join(lines))


# ==================== EXISTING COMMANDS (v2.0/v2.1) ==================

@safe_handler
async def cmd_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await memory.save_message(chat_id, "user", "/joke", intent="joke_command")
    category = context.args[0].lower() if context.args else None
    recent_ids = await memory.get_recent_joke_ids(chat_id, limit=15)
    joke = jokes_db.get_random(category, exclude_ids=recent_ids)
    if joke:
        await memory.record_callback(chat_id, joke.id, "forced_joke")
        response = f"Fine. Here's your joke.\n\n{joke.text}\n\nHappy now?"
    else:
        response = "I don't have jokes about that. I'm not a circus."
    await update.message.reply_text(response)
    await memory.save_message(chat_id, "bot", response, intent="joke")


@safe_handler
async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    profile = await memory.get_user_profile(chat_id)
    if profile:
        text = (
            f"Your stats? Fine.\n\n"
            f"Interactions: {profile['interaction_count']}\n"
            f"Running gags: {len(profile.get('running_gags', []))}\n\n"
            f"Satisfied? I didn't think so."
        )
    else:
        text = "No stats. You barely exist to me yet."
    await update.message.reply_text(text)


@safe_handler
async def cmd_personality_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /personality on  or  /personality off")
        return
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            row = Chat(id=chat.id, type=chat.type)
            session.add(row)
        row.personality_enabled = arg == "on"
        await session.commit()
    _enabled_cache.pop((chat.id, chat.type), None)
    await update.message.reply_text(f"Personality mode is now {'ON' if arg == 'on' else 'OFF'} here.")


@safe_handler
async def cmd_cpersonality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customize the personality for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    chat = update.effective_chat
    if not context.args:
        skins = personality.available_skins()
        current_skin = personality.get_chat_skin(chat.id)
        cfg = personality.get_chat_config_dict(chat.id)
        ci_traits = personality.get_custom_instructions_traits(chat.id)
        ci_info = f"Custom Instructions: {ci_traits.tone or 'none'}" if ci_traits else "Custom Instructions: none"
        lines = [
            "Customize the personality:\n",
            "  /cpersonality skin <name>       - switch skin",
            "  /cpersonality sarcasm <0.0-1.0> - set sarcasm level",
            "  /cpersonality metaphors <0.0-1.0> - set metaphor frequency",
            "  /cpersonality deadpan <on|off>  - toggle deadpan filter",
            "  /cpersonality max_length <num>   - set max response length\n",
            f"Available skins: {', '.join(skins)}",
            f"Current skin: {current_skin}",
            f"{ci_info}",
            f"Config: sarcasm={cfg['sarcasm_level']}, metaphors={cfg['metaphor_frequency']}, deadpan={cfg['deadpan']}, max_len={cfg['max_response_length']}",
        ]
        await update.message.reply_text("\n".join(lines))
        return

    setting = context.args[0].lower()

    if setting == "skin":
        if len(context.args) < 2:
            await update.message.reply_text(f"Available skins: {', '.join(personality.available_skins())}")
            return
        skin_name = personality._skin_loader.canonical(context.args[1].lower())
        owner = persons_mod.get_service().registry.skin_owner(skin_name)
        if owner is not None and await person_consent.is_opted_out(owner[0].id):
            await update.message.reply_text(
                f"{owner[0].real_name} has opted out of being imitated, so that skin is unavailable."
            )
            return
        if personality.set_chat_skin(chat.id, skin_name):
            await memory.set_skin(chat.id, skin_name)
            note = ""
            if owner is not None and settings.person_persona_disclosure:
                note = "\n\n" + str((personality._skin_loader.get_raw(skin_name) or {}).get("disclosure") or "")
            await update.message.reply_text(
                f"Personality skin set to '{skin_name}'. I feel different already.{note}".rstrip()
            )
        else:
            await update.message.reply_text(f"Skin '{skin_name}' not found. Available: {', '.join(personality.available_skins())}")
        return

    if setting == "sarcasm":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality sarcasm <0.0-1.0>")
            return
        try:
            val = max(0.0, min(1.0, float(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number between 0.0 and 1.0.")
            return
        personality.set_chat_config(chat.id, sarcasm_level=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Sarcasm level set to {val}.")
        return

    if setting == "metaphors":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality metaphors <0.0-1.0>")
            return
        try:
            val = max(0.0, min(1.0, float(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number between 0.0 and 1.0.")
            return
        personality.set_chat_config(chat.id, metaphor_frequency=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Metaphor frequency set to {val}.")
        return

    if setting == "deadpan":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality deadpan <on|off>")
            return
        val = context.args[1].lower() == "on"
        personality.set_chat_config(chat.id, deadpan=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Deadpan filter {'enabled' if val else 'disabled'}.")
        return

    if setting == "max_length":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality max_length <num>")
            return
        try:
            val = max(50, min(4096, int(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number (50-4096).")
            return
        personality.set_chat_config(chat.id, max_response_length=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Max response length set to {val}.")
        return

    await update.message.reply_text("Unknown setting. Use /cpersonality for help.")


@safe_handler
async def cmd_dpersonality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset personality to default settings for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    chat_id = update.effective_chat.id
    # v2.3: Aligned with nnr_settings>Reset scope (fix 3.4)
    personality.reset_chat_config(chat_id)
    personality.clear_custom_instructions(chat_id)
    await memory.clear_all_settings(chat_id)
    await update.message.reply_text("Personality reset to default. Back to my charming self.")


@safe_handler
async def cmd_personality_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stats = await memory.get_stats(chat_id)
    if not stats:
        await update.message.reply_text("No personality data yet.")
        return
    cfg = personality.get_chat_config_dict(chat_id)
    ci = stats.get("custom_instructions")
    ci_display = (ci[:40] + "...") if ci and len(ci) > 40 else (ci or "none")
    lines = [
        f"Personality: {'ON' if stats['enabled'] else 'OFF'}",
        f"Skin: {stats['skin']}",
        f"Custom Instructions: {ci_display}",
        f"User Limit: {stats.get('user_limit') or settings.personality_user_limit_default}",
        f"Response Delay: {stats.get('response_delay') if stats.get('response_delay') is not None else settings.personality_response_delay_default}s",
        f"Config: sarcasm={cfg['sarcasm_level']}, metaphors={cfg['metaphor_frequency']}, deadpan={cfg['deadpan']}, max_len={cfg['max_response_length']}",
        f"Interactions: {stats['interactions']}",
        f"Jokes told: {stats['jokes_told']}",
        f"Running gags: {stats['running_gags']}",
        f"Messages tracked: {stats['messages']}",
    ]
    await update.message.reply_text("\n".join(lines))


@safe_handler
async def cmd_addjoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not settings.is_admin_id(user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /addjoke <category> | <joke text>\n"
            "Categories: " + ", ".join(jokes_db.get_categories())
        )
        return
    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("Format: /addjoke <category> | <joke text>")
        return
    parts = text.split("|", 1)
    category = parts[0].strip().lower()
    joke_text = parts[1].strip()
    if not category or not joke_text:
        await update.message.reply_text("Both category and joke text are required.")
        return
    joke_id = f"user_{hashlib.md5(joke_text.encode(), usedforsecurity=False).hexdigest()[:8]}"
    if jokes_db.has_id(joke_id):
        await update.message.reply_text("That joke already exists.")
        return
    joke = Joke(id=joke_id, category=category, text=joke_text)
    if jokes_db.add(joke):
        await update.message.reply_text(f"Joke added (id: {joke_id}, category: {category}). My suffering grows.")
    else:
        await update.message.reply_text("Failed to add joke.")


# ==================== MESSAGE PIPELINE ====================

def _is_direct_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if this message @mentions the bot or replies to one of the
    bot's own messages — a deliberate address to the bot, as opposed to
    ambient chat the bot may or may not be configured to jump into."""
    message = update.message
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and context.bot is not None:
        if reply.from_user.id == context.bot.id:
            return True
    bot_username = getattr(context.bot, "username", None)
    if bot_username and message.entities:
        for entity in message.entities:
            if entity.type != MessageEntity.MENTION:
                continue
            mention_text = message.text[entity.offset: entity.offset + entity.length]
            if mention_text.lstrip("@").lower() == bot_username.lower():
                return True
    return False


def _strip_bot_mention(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> str:
    """Remove a leading/embedded @mention of the bot from text before it
    goes into the generation pipeline, so replies aren't generated from
    (and don't echo back) "@Nekooooooobot" as if it were part of what
    the user actually said."""
    bot_username = getattr(context.bot, "username", None)
    if not bot_username or not update.message.entities:
        return text
    for entity in update.message.entities:
        if entity.type != MessageEntity.MENTION:
            continue
        mention_text = text[entity.offset: entity.offset + entity.length]
        if mention_text.lstrip("@").lower() == bot_username.lower():
            text = (text[:entity.offset] + text[entity.offset + entity.length:]).strip()
    return text


async def _generate_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat, user, text: str,
) -> None:
    """Steps 3-8 of the personality pipeline — shared by the ambient
    listener and /chat, once each has decided a reply should happen."""
    ident = _identity(user)
    # v2.3: Atomic limit check + increment (fixes race condition)
    try:
        within_limit = await memory.increment_and_check(chat.id, _identity_key(user, ident))
    except Exception as e:
        logger.error("DB error in increment_and_check: %s — falling back to allow", e)
        within_limit = True

    if not within_limit:
        # Neko is "tired" of this user
        logger.debug("personality_skip", reason="over_message_limit", chat_id=chat.id, user_id=user.id)
        if random.random() < 0.05:
            tired_responses = [
                "I'm tired. Give me a break.",
                "You talk a lot. I need coffee.",
                "My patience has a cooldown and you exceeded it.",
                "...can we pick this up later? I'm buffering.",
            ]
            await update.message.reply_text(random.choice(tired_responses))
        return

    # v2.3: Graceful DB degradation (feature 4.7)
    try:
        await memory.update_profile(chat.id, username=user.username, chat_type=chat.type)
    except Exception as e:
        logger.warning("DB error in update_profile, continuing: %s", e)

    trigger_matches = triggers.analyze(text)
    intents = [m.intent for m in trigger_matches]
    primary_intent = trigger_matches[0] if trigger_matches else None

    try:
        await memory.save_message(
            chat.id, "user", text,
            intent=primary_intent.intent if primary_intent else None,
        )
    except Exception as e:
        logger.warning("DB error in save_message, continuing: %s", e)

    # v2.3: Pass skin_data to subplugins (fix 1.3)
    skin_data = await personality._resolve_active_skin(chat.id)

    # A real-person skin speaks only from that person's own data: the sub-plugins' canned
    # (Neko-voiced) answers are skipped so no Neko text leaks into their voice.
    if skin_data.get("persona_kind") == "person":
        plugin_result = None
    else:
        plugin_result = await _plugin_manager.route(
            text, chat.id, [], intents=intents, skin_data=skin_data
        )

    replied = getattr(update.message, "reply_to_message", None)
    reply_to_text = ((getattr(replied, "text", None) or getattr(replied, "caption", None) or "")[:300] or None) if replied else None
    if plugin_result and plugin_result.response:
        response = await personality.rewrite(chat.id, plugin_result.response, intents, text, speaker=ident,
                                             reply_to_text=reply_to_text, user_key=str(user.id))
    else:
        response = await personality.generate_direct(chat.id, intents, text, speaker=ident,
                                                     reply_to_text=reply_to_text, user_key=str(user.id))

    # v2.3: Response delay via JobQueue instead of blocking sleep (fix 1.7)
    delay = await _get_response_delay(chat.id)
    delay = max(delay, personality.pop_engine_delay(chat.id))  # persona engine: the person's measured latency (opt-in, 0 otherwise)
    if delay > 0 and hasattr(context, 'job_queue') and context.job_queue:
        # Schedule the send for later
        chat_id = chat.id
        reply_to_id = update.message.message_id

        async def _delayed_send(ctx: ContextTypes.DEFAULT_TYPE):
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id, text=response,
                    reply_to_message_id=reply_to_id,
                )
                logger.debug("personality_reply_sent", chat_id=chat_id, mode="delayed", delay=delay)
            except Exception as e:
                logger.error(
                    "personality_delayed_send_failed", chat_id=chat_id, error=str(e), exc_info=True
                )

        context.job_queue.run_once(_delayed_send, when=delay)
        logger.debug("personality_reply_scheduled", chat_id=chat.id, delay=delay)
    else:
        # No delay or no job_queue — send immediately
        if delay > 0:
            logger.debug(
                "personality_delay_skipped_no_job_queue", chat_id=chat.id,
                note="JobQueue unavailable (python-telegram-bot[job-queue] extra not installed?)",
            )
        await update.message.reply_text(response)
        logger.debug("personality_reply_sent", chat_id=chat.id, mode="immediate")

    try:
        await memory.save_message(
            chat.id, "bot", response,
            intent=primary_intent.intent if primary_intent else "general",
        )
    except Exception as e:
        logger.warning("DB error in save_message (bot), continuing: %s", e)

    if primary_intent and random.random() < 0.1:
        try:
            await memory.update_profile(chat.id, add_gag=primary_intent.intent)
        except Exception as e:
            logger.warning("DB error in update_profile (gag), continuing: %s", e)

    logger.debug(
        "personality_fired",
        chat_id=chat.id, user_id=user.id,
        intent=primary_intent.intent if primary_intent else None,
        skin=personality.get_chat_skin(chat.id),
        person_id=ident.person.id if ident else None,
        custom_instructions=personality.has_custom_instructions(chat.id),
        response_length=len(response),
    )


@safe_handler
async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chat <message> — explicitly talk to the personality layer
    (nanora/neko), regardless of whether ambient auto-replies are
    enabled for this chat. Distinct from /ai (bot/plugins/ai.py), which
    is the general-purpose AI assistant using whatever persona /persona
    set — the two used to be accidental aliases of the same handler."""
    if not settings.enable_personality:
        await update.message.reply_text("Personality mode is disabled bot-wide.")
        return
    text = " ".join(context.args) if context.args else None
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text
    if not text:
        await update.message.reply_text("Usage: /chat <message> — or reply to a message with /chat")
        return

    chat = update.effective_chat
    user = update.effective_user
    if _on_cooldown(_identity_key(user), chat.id):
        logger.debug("personality_skip", reason="cooldown", chat_id=chat.id, user_id=user.id)
        return
    # An explicit /chat is a deliberate ask, unlike ambient banter — it
    # works even in a chat where the auto-reply toggle is off, the same
    # way /ai always works regardless of any ambient setting.
    await _generate_and_reply(update, context, chat, user, text)


@safe_handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main personality pipeline v2.3.

    1. Cooldown check (per-user-per-chat)
    2. Is personality mode active? (skipped for a direct @mention/reply —
       see _is_direct_address — since that's a deliberate address to the
       bot, not ambient banter the per-chat toggle is meant to gate)
    3. Atomic per-user message limit check + increment (v2.3: atomic)
    4. Analyze triggers/intents
    5. Route to a sub-plugin (with skin_data) or generate directly
    6. Rewrite with personality (including custom instructions)
    7. Wait for response delay (v2.3: via JobQueue, not blocking)
    8. Send + save to memory
    """
    if not settings.enable_personality:
        logger.debug("personality_skip", reason="enable_personality=false")
        return
    if not update.message or not update.message.text:
        return  # nothing to reply to — not a "skip", just not applicable

    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text

    if _on_cooldown(_identity_key(user), chat.id):
        logger.debug("personality_skip", reason="cooldown", chat_id=chat.id, user_id=user.id)
        return

    direct_address = _is_direct_address(update, context)
    if not direct_address and not await _is_enabled_for(chat.id, chat.type):
        logger.debug(
            "personality_skip", reason="disabled_for_chat",
            chat_id=chat.id, chat_type=chat.type,
        )
        return
    if direct_address:
        logger.debug("personality_direct_address", chat_id=chat.id, user_id=user.id)
        text = _strip_bot_mention(update, context, text)

    await _generate_and_reply(update, context, chat, user, text)


# ==================== v2.3: Startup + Cleanup Jobs ====================

async def on_startup(app):
    """v2.3+: Called on bot startup. Loads persisted settings, few-shot bank, and schedules cleanup."""
    try:
        await personality.load_persisted_settings()
    except Exception as e:
        logger.error("Failed to load persisted settings: %s", e)
    try:
        usable = await asyncio.to_thread(persons_mod.get_service().preload)
        logger.info("Person personas ready", usable=usable)
    except Exception as e:
        logger.warning("Person persona preload skipped: %s", e)
    try:
        from bot.personality.examples import example_bank
        example_bank.load()
        logger.info("Few-shot bank ready", count=example_bank.count)
    except Exception as e:
        logger.warning("Few-shot bank load skipped: %s", e)

    # Schedule daily cleanup job
    if hasattr(app, 'job_queue') and app.job_queue:
        async def _daily_cleanup(ctx: ContextTypes.DEFAULT_TYPE):
            try:
                deleted = await memory.cleanup_old_data(days=settings.personality_cleanup_days)
                logger.info("Daily cleanup: deleted %d rows", deleted)
            except Exception as e:
                logger.error("Daily cleanup failed: %s", e)

        app.job_queue.run_daily(_daily_cleanup, time=dt.time(hour=3, minute=0))
        logger.info("Scheduled daily personality data cleanup at 03:00")


# ═══════════════════════════════════════════════════════════════════
# SUPEROWNER persona management — skins, presets, response data
# OWNER_ID only (superowner). Supports single-line and multi-line
# (reply-to-message) bulk data injection.
# ═══════════════════════════════════════════════════════════════════

_PERSONA_ADMIN_HELP = """\
<b>👑 Persona admin</b> (superowner only)

<b>List / inspect</b>
/plist — list all skins + presets
/pshow skin|preset &lt;name&gt; — show JSON summary
/preload — reload skins &amp; presets from disk

<b>Skins</b>
/padd skin &lt;name&gt; [description…] — create minimal skin
/pdel skin &lt;name&gt; — delete (neko/nanora protected)
/pmerge skin &lt;source&gt; &lt;target&gt; — merge source → target
/pset skin &lt;name&gt; &lt;field&gt; &lt;value&gt; — set scalar field
  fields: description, persona_context, sarcasm_level, …

<b>Presets</b>
/padd preset &lt;name&gt; [description…]
/pdel preset &lt;name&gt;
/pmerge preset &lt;source&gt; &lt;target&gt;
/pset preset &lt;name&gt; &lt;field&gt; &lt;value&gt;

<b>Response data</b>
/pdata &lt;skin&gt; &lt;category&gt; &lt;line…&gt; — append one direct_response line
/pdata &lt;skin&gt; &lt;category&gt; — reply with multiple lines (one per line)
/pmeta &lt;skin&gt; &lt;category&gt; &lt;metaphor…&gt; — append metaphor
/pjson skin|preset &lt;name&gt; — reply with full JSON body to replace

Categories for /pdata: general, greeting, thanks, confusion, …

<b>Real-person personas</b> (see docs/PERSON_PERSONAS.md)
/ppersons — registered people, their accounts and skins
/pwho [id|@user] — which person (if any) an account belongs to
/pconsent [status|optout|optin] — a person's own opt-out of being imitated
"""


def _personality_layer():
    from bot.personality.personality import personality
    return personality


def _instruction_processor():
    from bot.personality.custom_instructions import instruction_processor
    return instruction_processor


def _parse_float(s: str):
    try:
        return float(s)
    except ValueError:
        return s


@owner_only
@safe_handler
async def cmd_plist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    layer = _personality_layer()
    proc = _instruction_processor()
    skins = layer.available_skins()
    presets = proc.available_presets()

    combined = (
        f"<b>🎭 Skins ({len(skins)})</b>\n<code>" + ", ".join(skins) + "</code>"
        f"\n\n<b>🎛️ Presets ({len(presets)})</b>\n<code>" + ", ".join(presets) + "</code>"
    )
    if len(combined) <= 4000:
        await update.message.reply_text(combined, parse_mode="HTML")
        return

    # Either list may be too long on its own once the catalog grows (a
    # persona pack import can add over a thousand presets) — paginate each
    # independently rather than a single join that silently breaks past
    # Telegram's message-length limit.
    async def _send_section(label: str, items: list) -> None:
        if not items:
            await update.message.reply_text(f"<b>{label} (0)</b>", parse_mode="HTML")
            return
        pages = paginate_joined(items, max_len=3800)
        for i, page in enumerate(pages, 1):
            header = f"<b>{label} ({len(items)})</b>" + (f" — page {i}/{len(pages)}" if len(pages) > 1 else "")
            await update.message.reply_text(f"{header}\n<code>{page}</code>", parse_mode="HTML")

    await _send_section("🎭 Skins", skins)
    await _send_section("🎛️ Presets", presets)


@owner_only
@safe_handler
async def cmd_pshow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pshow skin|preset <name>")
        return
    kind, name = args[0], args[1].lower()
    if kind == "skin":
        data = _personality_layer()._skin_loader.get_raw(name)
    else:
        data = _instruction_processor().get_preset(name)
    if not data:
        await update.message.reply_text(f"❌ {escape_html(kind)} <code>{escape_html(name)}</code> not found.", parse_mode="HTML")
        return
    import json as _json
    blob = _json.dumps(data, indent=2, ensure_ascii=False)
    if len(blob) > 3500:
        blob = blob[:3500] + "\n… (truncated)"
    await update.message.reply_text(f"<b>{escape_html(kind)} {escape_html(name)}</b>\n<pre>{escape_html(blob)}</pre>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_preload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    layer = _personality_layer()
    persons_mod.reset_service()
    person_consent.invalidate()
    layer.reload_skins()
    layer.reload_presets()
    _instruction_processor().reload_presets()
    await update.message.reply_text(
        f"✅ Reloaded. Skins: {len(layer.available_skins())} · "
        f"Presets: {len(_instruction_processor().available_presets())}"
    )


@owner_only
@safe_handler
async def cmd_padd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /padd skin|preset <name> [description…]")
        return
    kind, name = args[0], args[1].lower()
    desc = " ".join(args[2:]) or f"Custom {kind} '{name}'"
    if kind == "skin":
        data = {
            "name": name,
            "description": desc,
            "default_config": {
                "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
                "deadpan": False, "programmer_humor": 0.2, "anime_references": 0.2,
                "max_response_length": 400,
            },
            "metaphors": {"general": []},
            "direct_responses": {"general": ["Okay."], "greeting": ["Hi."]},
            "persona_context": desc,
        }
        _personality_layer()._skin_loader.save(name, data, overwrite=False)
        # matching preset for keyword detection
        _instruction_processor().save_preset(name, {
            "keywords": [name],
            "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
            "deadpan": False, "deadpan_safe": False, "programmer_humor": 0.2,
            "anime_references": 0.2, "max_response_length": 400,
            "persona_context": desc, "themed_openers": ["Okay."], "themed_closers": [],
            "themed_prefixes": [], "themed_metaphors": {},
        }, overwrite=True)
    else:
        _instruction_processor().save_preset(name, {
            "keywords": [name],
            "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
            "deadpan": False, "deadpan_safe": False, "programmer_humor": 0.2,
            "anime_references": 0.2, "max_response_length": 400,
            "persona_context": desc, "themed_openers": ["Okay."], "themed_closers": [],
            "themed_prefixes": [], "themed_metaphors": {},
        }, overwrite=False)
    await update.message.reply_text(f"✅ Created {escape_html(kind)} <code>{escape_html(name)}</code>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pdel skin|preset <name>")
        return
    kind, name = args[0], args[1].lower()
    try:
        if kind == "skin":
            ok = _personality_layer()._skin_loader.delete(name)
        else:
            ok = _instruction_processor().delete_preset(name)
    except PermissionError as e:
        await update.message.reply_text(f"🚫 {e}")
        return
    await update.message.reply_text("✅ Deleted." if ok else "❌ Not found.")


@owner_only
@safe_handler
async def cmd_pmerge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 3 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pmerge skin|preset <source> <target>")
        return
    kind, src, tgt = args[0], args[1].lower(), args[2].lower()
    try:
        if kind == "skin":
            _personality_layer()._skin_loader.merge(src, tgt)
        else:
            _instruction_processor().merge_presets(src, tgt)
    except FileNotFoundError as e:
        await update.message.reply_text(f"❌ Missing: {e}")
        return
    await update.message.reply_text(f"✅ Merged {escape_html(kind)} <code>{escape_html(src)}</code> → <code>{escape_html(tgt)}</code>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 4 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pset skin|preset <name> <field> <value…>")
        return
    kind, name, field = args[0], args[1].lower(), args[2]
    value: object = " ".join(args[3:])
    # numeric fields
    if field in {
        "sarcasm_level", "metaphor_frequency", "exaggeration_level",
        "programmer_humor", "anime_references", "max_response_length",
    }:
        try:
            value = float(value) if field != "max_response_length" else int(float(str(value)))
        except ValueError:
            await update.message.reply_text("❌ Expected a number for that field.")
            return
    if field in {"deadpan", "deadpan_safe"}:
        value = str(value).lower() in {"1", "true", "yes", "on"}

    if kind == "skin":
        data = _personality_layer()._skin_loader.get_raw(name)
        if not data:
            await update.message.reply_text("❌ Skin not found.")
            return
        if field in data.get("default_config", {}):
            data.setdefault("default_config", {})[field] = value
        else:
            data[field] = value
        _personality_layer()._skin_loader.save(name, data, overwrite=True)
    else:
        data = _instruction_processor().get_preset(name)
        if not data:
            await update.message.reply_text("❌ Preset not found.")
            return
        data[field] = value
        _instruction_processor().save_preset(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Set {escape_html(kind)} <code>{escape_html(name)}</code>.{escape_html(field)}", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Append direct_response lines to a skin. Bulk via reply body."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage:\n/pdata <skin> <category> <line…>\n"
            "Or reply to a multi-line message with: /pdata <skin> <category>"
        )
        return
    name, category = args[0].lower(), args[1].lower()
    lines = []
    if len(args) > 2:
        lines.append(" ".join(args[2:]))
    if update.message.reply_to_message and update.message.reply_to_message.text:
        lines.extend(
            ln.strip() for ln in update.message.reply_to_message.text.splitlines() if ln.strip()
        )
    if not lines:
        await update.message.reply_text("❌ Provide a line or reply to a multi-line message.")
        return
    loader = _personality_layer()._skin_loader
    data = loader.get_raw(name)
    if not data:
        await update.message.reply_text("❌ Skin not found.")
        return
    dr = data.setdefault("direct_responses", {})
    bucket = dr.setdefault(category, [])
    for ln in lines:
        if ln not in bucket:
            bucket.append(ln)
    loader.save(name, data, overwrite=True)
    await update.message.reply_text(
        f"✅ Added {len(lines)} line(s) to <code>{escape_html(name)}</code>.direct_responses.{escape_html(category)} "
        f"(now {len(bucket)})",
        parse_mode="HTML",
    )


@owner_only
@safe_handler
async def cmd_pmeta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Usage: /pmeta <skin> <category> <metaphor…>")
        return
    name, category = args[0].lower(), args[1].lower()
    metaphor = " ".join(args[2:])
    loader = _personality_layer()._skin_loader
    data = loader.get_raw(name)
    if not data:
        await update.message.reply_text("❌ Skin not found.")
        return
    metas = data.setdefault("metaphors", {})
    bucket = metas.setdefault(category, [])
    if metaphor not in bucket:
        bucket.append(metaphor)
    loader.save(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Metaphor added to <code>{escape_html(name)}</code>.{escape_html(category)}", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pjson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replace entire skin/preset JSON from a replied message body."""
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text(
            "Usage: reply to a JSON message with /pjson skin|preset <name>"
        )
        return
    kind, name = args[0], args[1].lower()
    body = update.message.reply_to_message.text if update.message.reply_to_message else None
    if not body:
        await update.message.reply_text("❌ Reply to a message that contains JSON.")
        return
    import json as _json
    try:
        data = _json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("root must be an object")
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid JSON: {e}")
        return
    if kind == "skin":
        data.setdefault("name", name)
        _personality_layer()._skin_loader.save(name, data, overwrite=True)
    else:
        _instruction_processor().save_preset(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Replaced {escape_html(kind)} <code>{escape_html(name)}</code> from JSON.", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_padmin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_PERSONA_ADMIN_HELP, parse_mode="HTML")



# ═══════════════════════════════════════════════════════════════════
# Real-person personas — registry inspection and each person's own opt-out
# ═══════════════════════════════════════════════════════════════════

@owner_only
@safe_handler
async def cmd_ppersons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List registered real-person personas, their accounts and skins."""
    svc = persons_mod.get_service()
    if not svc.registry:
        await update.message.reply_text("No person registry loaded (data/personality/persons/registry.json).")
        return
    lines = [f"<b>Person personas</b> ({'enabled' if settings.person_personas_enabled else 'DISABLED'})"]
    for p in svc.registry.persons():
        data = svc.data(p.id)
        n = (data.profile.get("records") or {}).get("total", 0) if data and data.available else 0
        out = await person_consent.is_opted_out(p.id)
        lines.append(f"\n<b>{escape_html(p.real_name)}</b> · skin <code>{escape_html(p.skin)}</code> · "
                     f"{n} msgs{' · <b>OPTED OUT</b>' if out else ''}")
        for a in p.accounts:
            tag = f" → <code>{escape_html(a.variant_skin)}</code>" if a.variant_skin else ""
            uname = f"@{escape_html(a.username)}" if a.username else "no username"
            lines.append(f"  • <code>{a.id}</code> {uname}{tag}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pwho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve an account (id, @username, or reply) to its registered person."""
    reg = persons_mod.get_service().registry
    uid, uname = None, None
    if context.args:
        arg = context.args[0]
        if arg.lstrip("-").isdigit():
            uid = int(arg)
        else:
            uname = arg
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        uid, uname = u.id, u.username
    else:
        u = update.effective_user
        uid, uname = u.id, u.username
    ident = reg.resolve(uid, uname)
    if not ident:
        await update.message.reply_text("Not a registered person.")
        return
    await update.message.reply_text(
        f"{ident.person.real_name} (person <code>{escape_html(ident.person.id)}</code>) — account "
        f"<code>{ident.account.id}</code> matched by {ident.via}.", parse_mode="HTML")


@safe_handler
async def cmd_pconsent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A registered person's own opt-out of being imitated (also usable by the owner)."""
    user = update.effective_user
    reg = persons_mod.get_service().registry
    arg = (context.args[0].lower() if context.args else "status")
    target = None
    if len(context.args or []) > 1 and settings.is_admin_id(user.id):
        target = reg.person(context.args[1])
        if target is None:
            await update.message.reply_text("Unknown person id. See /ppersons.")
            return
    else:
        ident = _identity(user)
        target = ident.person if ident else None
    if target is None:
        await update.message.reply_text(
            "This command is for the people whose chat style the bot can imitate. "
            "Your account isn't one of them.")
        return
    if arg == "status":
        out = await person_consent.is_opted_out(target.id)
        await update.message.reply_text(
            f"Imitation of {target.real_name} is {'OFF (opted out)' if out else 'ON'}. "
            "/pconsent optout · /pconsent optin")
    elif arg in ("optout", "optin"):
        own = _identity(user)
        if arg == "optin" and (own is None or own.person.id != target.id):
            await update.message.reply_text(
                f"Only {target.real_name} can switch imitation back on.")
            return
        await person_consent.set_opt_out(target.id, arg == "optout", user.id)
        await update.message.reply_text(
            f"Done — imitation of {target.real_name} is now {'OFF' if arg == 'optout' else 'ON'}.")
    else:
        await update.message.reply_text("Usage: /pconsent [status|optout|optin]")


# ── Persona engine: operator + memory commands (docs/PERSONA_ENGINE.md) ──────
async def _active_person(update: Update):
    """(PersonaData, account_id) of the person skin active in this chat, or None."""
    skin = await personality._resolve_active_skin(update.effective_chat.id)
    if skin.get("persona_kind") != "person":
        return None
    return persons_mod.get_service().data_for_skin(skin)


def _engine_service():
    from bot.persona_engine import runtime as pe_runtime
    return pe_runtime.get_service(settings, persons_mod.get_service().registry.abusive_tokens)


@owner_only
async def cmd_pengine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /pengine [status|debug on|off|last] - persona-engine diagnostics (counters and metadata only)."""
    msg = update.message
    arg = (context.args[0].lower() if context.args else "status")
    if not settings.persona_engine_enabled:
        await msg.reply_text("Persona engine is off (set PERSONA_ENGINE_ENABLED=true; see docs/PERSONA_ENGINE.md).")
        return
    svc = _engine_service()
    chat_id = update.effective_chat.id
    if arg == "debug":
        mode = (context.args[1].lower() if len(context.args) > 1 else "")
        if mode not in ("on", "off"):
            await msg.reply_text("Usage: /pengine debug on|off  (traces go to the bot log; /pengine last shows the latest here)")
            return
        (svc.debug_chats.add if mode == "on" else svc.debug_chats.discard)(chat_id)
        await msg.reply_text(f"Persona-engine debug logging {'on' if mode == 'on' else 'off'} for this chat.")
        return
    if arg == "last":
        tr = personality.last_engine_trace(chat_id)
        if not tr:
            await msg.reply_text("No engine reply recorded for this chat yet.")
            return
        lines = [f"stage={tr.get('stage')} situation={tr.get('situation')} composite={tr.get('composite')} ms={tr.get('ms')}",
                 f"evidence used: {len(tr.get('evidence', []))} | memory facts: {tr.get('memory_facts', 0)}"]
        for i, a in enumerate(tr.get("attempts", [])):
            lines.append(f"attempt {i + 1}: candidates={a.get('candidates')} kept={a.get('kept')} rejected={a.get('rejected')}")
        await msg.reply_text("\n".join(lines))
        return
    st = svc.status()
    lines = [f"provider: {st['provider']}", f"candidates: {st['candidates']} | examples: {st['examples']} | long-term memory: {'on' if st['long_term_memory'] else 'off'}"]
    for pid, e in st["engines"].items():
        lines.append(f"{pid}: pool {e['evidence_pool']} | requests {e['requests']} | llm_ok {e['llm_ok']} | identity {e['identity_probe']} | "
                     f"rejected {e['all_rejected']} | provider_errors {e['provider_error']} | breaker {'OPEN' if e['breaker_open'] else 'ok'}")
    if len(lines) == 2:
        lines.append("No engine built yet (it loads on the first persona reply).")
    await msg.reply_text("\n".join(lines))


async def cmd_premember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /premember <fact> - ask the persona to remember a fact in this chat (opt-in, stored as BOT_MEMORY)."""
    msg = update.message
    if not settings.persona_engine_memory_enabled:
        await msg.reply_text("Long-term memory is off (PERSONA_ENGINE_MEMORY_ENABLED).")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Admin only.")
        return
    fact = " ".join(context.args or []).strip()
    if not fact:
        await msg.reply_text("Usage: /premember <fact>  (max 200 characters; no links, handles or numbers). Remove with /pforget.")
        return
    active = await _active_person(update)
    if active is None:
        await msg.reply_text("No imitation persona is active in this chat.")
        return
    from bot.persona_engine.memory import MemoryError_
    try:
        mid = await asyncio.to_thread(_engine_service().long.add, update.effective_chat.id, active[0].person.id, fact)
    except MemoryError_ as exc:
        await msg.reply_text(f"Not stored: {exc}")
        return
    await msg.reply_text(f"Remembered (#{mid}). It is a note people gave the bot, not something {active[0].person.real_name} said.")


async def cmd_pmemory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /pmemory - list what the persona was asked to remember in this chat."""
    msg = update.message
    if not settings.persona_engine_memory_enabled:
        await msg.reply_text("Long-term memory is off (PERSONA_ENGINE_MEMORY_ENABLED).")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Admin only.")
        return
    active = await _active_person(update)
    if active is None:
        await msg.reply_text("No imitation persona is active in this chat.")
        return
    rows = await asyncio.to_thread(_engine_service().long.list, update.effective_chat.id, active[0].person.id)
    await msg.reply_text("\n".join(f"#{i}: {escape_html(t)}" for i, t in rows) if rows else "Nothing remembered here.")


async def cmd_pforget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /pforget <id|all> - delete remembered facts for this chat."""
    msg = update.message
    if not settings.persona_engine_memory_enabled:
        await msg.reply_text("Long-term memory is off (PERSONA_ENGINE_MEMORY_ENABLED).")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Admin only.")
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg != "all" and not arg.isdigit():
        await msg.reply_text("Usage: /pforget <id>  or  /pforget all")
        return
    n = await asyncio.to_thread(_engine_service().long.forget, update.effective_chat.id, None, None if arg == "all" else int(arg))
    await msg.reply_text(f"Forgot {n} fact(s).")


def register(app):
    """Register all personality handlers with the Telegram app."""
    if not settings.enable_personality:
        return

    # v2.3: Main settings hub
    app.add_handler(CommandHandler("nnr_settings", cmd_nnr_settings))
    app.add_handler(CallbackQueryHandler(cmd_nnr_settings_callback, pattern="^nnr_"))

    # v2.2: Direct setting commands
    app.add_handler(CommandHandler("nnr_ci", cmd_nnr_ci))
    app.add_handler(CommandHandler("nnr_ul", cmd_nnr_ul))
    app.add_handler(CommandHandler("nnr_rd", cmd_nnr_rd))

    # v2.3: New commands
    app.add_handler(CommandHandler("nnr_test", cmd_nnr_test))
    app.add_handler(CommandHandler("nnr_help", cmd_nnr_help))

    # v2.0/v2.1 commands
    app.add_handler(CommandHandler("joke", cmd_joke))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("personality", cmd_personality_toggle))
    app.add_handler(CommandHandler("cpersonality", cmd_cpersonality))
    app.add_handler(CommandHandler("dpersonality", cmd_dpersonality))
    app.add_handler(CommandHandler("personalitystatus", cmd_personality_status))
    app.add_handler(CommandHandler("addjoke", cmd_addjoke))
    app.add_handler(CommandHandler("chat", cmd_chat))

    # Superowner persona management
    app.add_handler(CommandHandler("plist", cmd_plist))
    app.add_handler(CommandHandler("pshow", cmd_pshow))
    app.add_handler(CommandHandler("preload", cmd_preload))
    app.add_handler(CommandHandler("padd", cmd_padd))
    app.add_handler(CommandHandler("pdel", cmd_pdel))
    app.add_handler(CommandHandler("pmerge", cmd_pmerge))
    app.add_handler(CommandHandler("pset", cmd_pset))
    app.add_handler(CommandHandler("pdata", cmd_pdata))
    app.add_handler(CommandHandler("pmeta", cmd_pmeta))
    app.add_handler(CommandHandler("pjson", cmd_pjson))
    app.add_handler(CommandHandler(["padmin", "personaadmin"], cmd_padmin_help))
    app.add_handler(CommandHandler("ppersons", cmd_ppersons))
    app.add_handler(CommandHandler("pengine", cmd_pengine))
    app.add_handler(CommandHandler("premember", cmd_premember))
    app.add_handler(CommandHandler("pmemory", cmd_pmemory))
    app.add_handler(CommandHandler("pforget", cmd_pforget))
    app.add_handler(CommandHandler("pwho", cmd_pwho))
    app.add_handler(CommandHandler("pconsent", cmd_pconsent))

    # Message handler.
    #
    # Explicit group=8: every plugin's ambient (non-command) text listener
    # gets its own handler group — group_mgmt's lock/filter listeners are
    # 1/2, fun's afk_listener is 3, group_mgmt's welcome_listener is 4,
    # fonts is 5, economy's xp_listener is 6, collector's spawn_listener
    # is 7, name_history is 9 — specifically so none of them can shadow
    # another (PTB runs at most one handler per group per update, then
    # moves on to the next group; every group still sees the update).
    # This handler previously had no explicit group, which put it in the
    # PTB default of group 0 — outside that deliberate sequence, and a
    # silent collision risk for anything else later registered there
    # without noticing personality.py already was. group=8 fills the one
    # open slot in the existing 1-9 sequence.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message), group=8)
