"""
Font styling plugin — ported from font_bot_ultimate.py.

Changes from the original standalone bot:
  - Per-user prefs (default font, auto-delete, preview) now live on the
    shared `User` row instead of a separate JSON file, so they survive
    alongside the rest of NovaBot's data.
  - The old /settings, /clear, and /stats commands are renamed to
    /fontsettings, /clearfont, and /botstats — NovaBot already has its
    own /settings and /stats with different meanings.
  - The forced "join our channel to use this bot" gate is OFF by default
    (see FONT_GATE_ENABLED in config) instead of blocking every command —
    it was a font-bot-specific growth tactic, not something the rest of
    NovaBot's users should be surprised by.
  - Inline keyboard callback_data is namespaced with a "font:" prefix.
    The original used bare "page:"/"close" callback_data, which collides
    with the main /menu system in plugins/start.py (same prefixes, same
    default handler group) — without the prefix, whichever plugin loaded
    first would silently swallow the other's button taps.
  - Added 8 more substitution fonts (f11-f18: squares, circled, regional/
    "special", runic, bold, italic, sans-bold — see catalog.py for the
    sourcing of each), a /flip command for upside-down text, and
    /fontfx + 11 named shortcuts (/stinky, /bubbles, /underline, /rays,
    /birds, /slash, /stop, /skyline, /arrows, /strike, /frozen) — a
    combining-diacritic overlay mechanism, distinct from the substitution
    fonts, that works on arbitrary text rather than a fixed alphabet.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.identity import bot_name
from bot.core.database import User, async_session
from bot.fonts.catalog import EFFECTS, FONT_MAPS, FONT_NAMES, apply_effect, best_effort_reverse, translate_text, upside_down
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler

FONTS_PER_PAGE = 5

# Lightweight in-memory usage counters for /botstats — intentionally not
# persisted (a "since last restart" vanity metric, not user data).
_stats = {"total_messages": 0, "font_usage": {k: 0 for k in FONT_MAPS}, "started_at": datetime.utcnow()}

_rate_limit_store: Dict[int, List[float]] = {}


def _is_rate_limited(user_id: int) -> bool:
    now = time.time()
    window = _rate_limit_store.get(user_id, [])
    window = [t for t in window if now - t < settings.font_rate_limit_window]
    _rate_limit_store[user_id] = window
    if len(window) >= settings.font_rate_limit_max:
        return True
    window.append(now)
    return False


async def _get_or_create_user(session, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        session.add(user)
        await session.flush()
    return user


async def _require_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Optional forced-membership gate. Disabled by default — see FONT_GATE_ENABLED."""
    if not settings.font_gate_enabled or not settings.font_gate_channel:
        return True
    user = update.effective_user
    if settings.is_admin_id(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(settings.font_gate_channel, user.id)
        ok = member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        ok = False
    if not ok:
        await update.message.reply_text(
            f"🚫 Please join {settings.font_gate_channel} to use font commands, then try again.",
        )
    return ok


def _font_keyboard(page: int = 0, action: str = "select") -> InlineKeyboardMarkup:
    keys = list(FONT_MAPS.keys())
    total_pages = (len(keys) + FONTS_PER_PAGE - 1) // FONTS_PER_PAGE
    start_i = page * FONTS_PER_PAGE
    page_keys = keys[start_i:start_i + FONTS_PER_PAGE]

    buttons = []
    for k in page_keys:
        label = FONT_NAMES.get(k, k.upper())
        sample = translate_text("Ab", k)
        buttons.append([InlineKeyboardButton(f"{label}  {sample}", callback_data=f"font:select:{k}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"font:page:{action}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"font:page:{action}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("❌ Close", callback_data="font:close")])
    return InlineKeyboardMarkup(buttons)


# ---------- COMMANDS ----------

@safe_handler
async def fonts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    sample = "The Quick Brown Fox"
    lines = ["🎨 <b>Font Preview</b>\n"]
    for k in FONT_MAPS:
        name = FONT_NAMES.get(k, k.upper())
        styled = translate_text(sample, k)
        lines.append(f"<b>{escape_html(name)}</b>\n<code>{escape_html(styled)}</code>\n")
    fx_names = ", ".join(f"/{k}" for k in EFFECTS)
    lines.append(f"<b>🌀 Also:</b> /flip (upside-down), and effects — {fx_names}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_font_keyboard(0, "select")
    )


@safe_handler
async def font_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /f1 .. /f10 — instant styling if text follows, else sets default."""
    user = update.effective_user
    if not await _require_gate(update, context):
        return
    if _is_rate_limited(user.id):
        await update.message.reply_text("⏳ Please slow down.")
        return

    cmd = update.message.text.split()[0][1:].split("@")[0].lower()
    if cmd not in FONT_MAPS:
        return

    parts = update.message.text.split(maxsplit=1)
    if len(parts) > 1:
        styled = translate_text(parts[1], cmd)
        _stats["total_messages"] += 1
        _stats["font_usage"][cmd] = _stats["font_usage"].get(cmd, 0) + 1
        await update.message.reply_text(styled)

        async with async_session() as session:
            u = await _get_or_create_user(session, user.id)
            if u.font_auto_delete:
                try:
                    await update.message.delete()
                except Exception:
                    pass
            await session.commit()
        return

    async with async_session() as session:
        u = await _get_or_create_user(session, user.id)
        u.default_font = cmd
        await session.commit()

    sample = translate_text("Hello", cmd)
    await update.message.reply_text(
        f"✅ <b>{escape_html(FONT_NAMES.get(cmd, cmd.upper()))}</b> set as your default\n"
        f"Sample: <code>{escape_html(sample)}</code>\n\n"
        f"Send any text and I'll auto-style it. Use /clearfont to remove.",
        parse_mode="HTML",
    )


@safe_handler
async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random as _random
    user = update.effective_user
    if not await _require_gate(update, context):
        return
    if _is_rate_limited(user.id):
        await update.message.reply_text("⏳ Please slow down.")
        return

    parts = update.message.text.split(maxsplit=1)
    font_key = _random.choice(list(FONT_MAPS.keys()))
    if len(parts) > 1:
        styled = translate_text(parts[1], font_key)
        _stats["total_messages"] += 1
        _stats["font_usage"][font_key] = _stats["font_usage"].get(font_key, 0) + 1
        await update.message.reply_text(
            f"{styled}\n\n🎲 <i>Font:</i> {escape_html(FONT_NAMES.get(font_key, font_key))}",
            parse_mode="HTML",
        )
    else:
        async with async_session() as session:
            u = await _get_or_create_user(session, user.id)
            u.default_font = font_key
            await session.commit()
        await update.message.reply_text(
            f"🎲 Random default set to <b>{escape_html(FONT_NAMES.get(font_key, font_key))}</b>.",
            parse_mode="HTML",
        )


@safe_handler
async def mix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random as _random
    user = update.effective_user
    if not await _require_gate(update, context):
        return
    if _is_rate_limited(user.id):
        await update.message.reply_text("⏳ Please slow down.")
        return

    parts = update.message.text.split(maxsplit=1)
    if len(parts) <= 1:
        await update.message.reply_text("✏ Usage: <code>/mix your text here</code>", parse_mode="HTML")
        return

    result = []
    for ch in parts[1]:
        if ch.isalpha():
            fk = _random.choice(list(FONT_MAPS.keys()))
            result.append(translate_text(ch, fk))
        else:
            result.append(ch)
    _stats["total_messages"] += 1
    await update.message.reply_text("".join(result))


@safe_handler
async def reverse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) <= 1:
        await update.message.reply_text("✏ Usage: <code>/reverse &lt;styled text&gt;</code>", parse_mode="HTML")
        return

    best = best_effort_reverse(parts[1])
    if best == parts[1]:
        await update.message.reply_text("❌ Could not detect a known font to reverse.")
    else:
        await update.message.reply_text(f"🔓 <i>Reversed:</i>\n<code>{escape_html(best)}</code>", parse_mode="HTML")


@safe_handler
async def flip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) <= 1:
        await update.message.reply_text("✏ Usage: <code>/flip your text here</code>", parse_mode="HTML")
        return
    _stats["total_messages"] += 1
    await update.message.reply_text(upside_down(parts[1]))


# Reverse of fonts.py's register()-time SHORTCUT_OVERRIDES — lets
# fontfx_cmd map a renamed command (e.g. /prohibit) back to its real
# effect key ("stop") instead of silently finding nothing.
_SHORTCUT_TO_EFFECT = {"prohibit": "stop"}


@safe_handler
async def fontfx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic entry point: /fontfx <style> <text>. Each style also has
    its own direct shortcut command (/stinky, /bubbles, ...) registered
    below, reusing this same function with the style pre-filled."""
    if not await _require_gate(update, context):
        return
    parts = update.message.text.split(maxsplit=2)
    # parts[0] is either "/fontfx" (style comes from parts[1]) or one of
    # the direct shortcuts like "/stinky" (style is implied by the command).
    cmd_name = parts[0][1:].split("@")[0].lower()

    if cmd_name == "fontfx":
        if len(parts) < 3 or parts[1].lower() not in EFFECTS:
            names = ", ".join(EFFECTS)
            await update.message.reply_text(
                f"✏ Usage: <code>/fontfx &lt;style&gt; text</code>\nStyles: {names}", parse_mode="HTML"
            )
            return
        style, text = parts[1].lower(), parts[2]
    else:
        style = _SHORTCUT_TO_EFFECT.get(cmd_name, cmd_name)
        text_parts = update.message.text.split(maxsplit=1)
        if len(text_parts) <= 1:
            emoji, name = EFFECTS[style][1], EFFECTS[style][2]
            await update.message.reply_text(f"✏ Usage: <code>/{cmd_name} your text here</code> {emoji} {name}", parse_mode="HTML")
            return
        text = text_parts[1]

    _stats["total_messages"] += 1
    await update.message.reply_text(apply_effect(text, style))


@safe_handler
async def clearfont_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    async with async_session() as session:
        u = await _get_or_create_user(session, update.effective_user.id)
        u.default_font = None
        await session.commit()
    await update.message.reply_text("🗑 Default font cleared — I'll stop auto-styling your messages.")


@safe_handler
async def fontsettings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    async with async_session() as session:
        u = await _get_or_create_user(session, update.effective_user.id)
        auto_del = "✅" if u.font_auto_delete else "❌"
        preview = "✅" if u.font_show_preview else "❌"
        default = FONT_NAMES.get(u.default_font, u.default_font) if u.default_font else "None"
        await session.commit()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{auto_del} Auto-delete original", callback_data="font:toggle:font_auto_delete")],
        [InlineKeyboardButton(f"{preview} Show font preview", callback_data="font:toggle:font_show_preview")],
        [InlineKeyboardButton("❌ Close", callback_data="font:close")],
    ])
    await update.message.reply_text(
        f"⚙ <b>Font Settings</b>\n\n<i>Default font:</i> <code>{escape_html(default)}</code>\n"
        f"Tap below to toggle options.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@safe_handler
async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_gate(update, context):
        return
    uptime = datetime.utcnow() - _stats["started_at"]
    await update.message.reply_text(
        f"🤖 <b>{bot_name()} — Font Module</b>\n"
        f"• Fonts available: <b>{len(FONT_MAPS)}</b>\n"
        f"• Messages styled this run: <b>{_stats['total_messages']:,}</b>\n"
        f"• Module uptime: <b>{uptime.days}d {uptime.seconds // 3600}h</b>",
        parse_mode="HTML",
    )


@safe_handler
async def botstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only font usage overview (renamed from the original /stats to
    avoid colliding with NovaBot's own chat-level /stats)."""
    if not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    uptime = datetime.utcnow() - _stats["started_at"]
    usage_lines = [f"• {FONT_NAMES.get(k, k)}: {v}" for k, v in _stats["font_usage"].items()]
    await update.message.reply_text(
        f"📊 <b>Font Usage (since last restart)</b>\n\n"
        f"<i>Messages styled:</i> {_stats['total_messages']:,}\n"
        f"<i>Uptime:</i> {uptime.days}d {uptime.seconds // 3600}h {(uptime.seconds // 60) % 60}m\n\n"
        + "\n".join(usage_lines),
        parse_mode="HTML",
    )


@safe_handler
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only broadcast to every known user. Reuses the shared users table."""
    from sqlalchemy import select

    if not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) <= 1:
        await update.message.reply_text("✏ Usage: <code>/broadcast your message here</code>", parse_mode="HTML")
        return

    message = parts[1]
    async with async_session() as session:
        result = await session.execute(select(User.id))
        user_ids = [row[0] for row in result.all()]

    status_msg = await update.message.reply_text(f"📤 Broadcasting to {len(user_ids)} users...")
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📢 <b>Broadcast</b>\n\n{escape_html(message)}", parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"✅ Broadcast complete: {sent} sent, {failed} failed.")


# ---------- AUTO-STYLE MESSAGE HANDLER ----------

@safe_handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-styles plain messages for users who set a default font."""
    user = update.effective_user
    if not user or _is_rate_limited(user.id):
        return

    async with async_session() as session:
        u = await session.get(User, user.id)
        default = u.default_font if u else None
        auto_delete = u.font_auto_delete if u else False

    if not default:
        return

    styled = translate_text(update.message.text, default)
    _stats["total_messages"] += 1
    _stats["font_usage"][default] = _stats["font_usage"].get(default, 0) + 1
    await update.message.reply_text(styled)

    if auto_delete:
        try:
            await update.message.delete()
        except Exception:
            pass


# ---------- CALLBACK QUERY HANDLER ----------

@safe_handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles every `font:*` callback_data — namespaced so it never
    intercepts the main /menu system's page:/close callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data[len("font:"):]
    user = query.from_user

    if data == "close":
        await query.delete_message()
        return

    if data.startswith("page:"):
        _, action, page = data.split(":")
        await query.edit_message_reply_markup(reply_markup=_font_keyboard(int(page), action))
        return

    if data.startswith("toggle:"):
        field = data.split(":", 1)[1]
        async with async_session() as session:
            u = await _get_or_create_user(session, user.id)
            setattr(u, field, not getattr(u, field))
            await session.commit()
        await fontsettings_cmd(update, context)
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if data.startswith("select:"):
        font_key = data.split(":", 1)[1]
        replied = query.message.reply_to_message
        if replied and replied.text:
            styled = translate_text(replied.text, font_key)
            _stats["font_usage"][font_key] = _stats["font_usage"].get(font_key, 0) + 1
            await query.edit_message_text(
                f"{styled}\n\n🎨 <i>Font:</i> {escape_html(FONT_NAMES.get(font_key, font_key))}",
                parse_mode="HTML",
            )
        else:
            async with async_session() as session:
                u = await _get_or_create_user(session, user.id)
                u.default_font = font_key
                await session.commit()
            sample = translate_text("Hello", font_key)
            await query.edit_message_text(
                f"✅ <b>{escape_html(FONT_NAMES.get(font_key, font_key.upper()))}</b> set as default\n"
                f"Sample: <code>{escape_html(sample)}</code>",
                parse_mode="HTML",
            )
        return


def register(app):
    if not settings.enable_fonts:
        return

    for fk in FONT_MAPS:
        app.add_handler(CommandHandler(fk, font_command))

    app.add_handler(CommandHandler("fonts", fonts_cmd))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("mix", mix_cmd))
    # "/reverse" is registered by bot.plugins.anime (dispatches to reverse_cmd here for text input)
    app.add_handler(CommandHandler("flip", flip_cmd))
    app.add_handler(CommandHandler("fontfx", fontfx_cmd))
    # "stop" collides with music_live.py's /stop (stop playback) — that
    # command wins the name; this effect stays reachable via
    # /fontfx stop <text> and its own dedicated shortcut, /prohibit.
    SHORTCUT_OVERRIDES = {"stop": "prohibit"}
    for effect_key in EFFECTS:
        app.add_handler(CommandHandler(SHORTCUT_OVERRIDES.get(effect_key, effect_key), fontfx_cmd))
    app.add_handler(CommandHandler("clearfont", clearfont_cmd))
    app.add_handler(CommandHandler("fontsettings", fontsettings_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("botstats", botstats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^font:"))

    # Distinct group — see the comment in group_mgmt.py's register() for why
    # every "reply to any text message" handler needs its own group.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message), group=5)
