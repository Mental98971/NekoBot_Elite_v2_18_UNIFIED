"""
Economy & Leveling — new plugin, not present in any of the original
four projects.

XP and coins are scoped per-chat (stored on ChatMember, the existing
chat_id+user_id junction table) rather than globally — a user's grind in
one community shouldn't silently follow them into another, which is
what a global column would have done. Balance/XP transactions go
through bot.services.economy_service, shared with plugins/games.py so
betting commands use the exact same deduct-and-check logic.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import desc, select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.core.database import ChatMember, ShopItem, User, UserInventory, async_session
from bot.core.errors import safe_handler
from bot.services.economy_service import get_or_create_member, level_from_xp, transfer_coins, xp_for_level
from bot.utils.helpers import escape_html, generate_progress_bar, resolve_target_user

DEFAULT_SHOP_ITEMS = [
    ("badge_star", "⭐ Star Badge", "A shiny star next to your profile.", 500, "⭐"),
    ("badge_crown", "👑 Crown Badge", "For those who have arrived.", 2000, "👑"),
    ("badge_ghost", "👻 Ghost Badge", "Spooky, mysterious, tasteful.", 750, "👻"),
    ("custom_title", "🏷️ Custom Chat Title", "Ask an admin to set your custom title.", 1000, "🏷️"),
]


async def seed_shop_items() -> None:
    """Idempotently ensure the default shop items exist. Called once from
    core/bot.py's post_init."""
    async with async_session() as session:
        result = await session.execute(select(ShopItem.key))
        existing = {row[0] for row in result.all()}
        for key, name, desc, price, emoji in DEFAULT_SHOP_ITEMS:
            if key not in existing:
                session.add(ShopItem(key=key, name=name, description=desc, price=price, emoji=emoji))
        await session.commit()


@safe_handler
async def xp_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Awards XP for activity, with a cooldown so spamming doesn't farm
    levels. Runs in its own handler group — see group_mgmt.py's register()
    for why every catch-all text handler needs one."""
    if not settings.enable_economy or update.effective_chat.type == "private":
        return
    user, chat = update.effective_user, update.effective_chat
    now = datetime.utcnow()

    async with async_session() as session:
        member = await get_or_create_member(session, chat.id, user.id)
        if member.last_xp_at and (now - member.last_xp_at).total_seconds() < settings.economy_xp_cooldown_seconds:
            return

        old_level = level_from_xp(member.xp or 0)
        member.xp = (member.xp or 0) + settings.economy_xp_per_message
        member.last_xp_at = now
        new_level = level_from_xp(member.xp)
        member.level = new_level
        await session.commit()

    if new_level > old_level:
        await update.effective_message.reply_text(
            f"🎉 {escape_html(user.first_name)} leveled up to <b>level {new_level}</b>!",
            parse_mode="HTML",
        )


@safe_handler
async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Leveling is a per-group feature — try this in a group chat.")
        return

    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        target_id, target_name = update.effective_user.id, update.effective_user.first_name

    async with async_session() as session:
        member = await get_or_create_member(session, update.effective_chat.id, target_id)
        xp, level, coins = member.xp or 0, member.level or 0, member.coins or 0
        await session.commit()

    cur_floor = xp_for_level(level)
    next_floor = xp_for_level(level + 1)
    progress = generate_progress_bar(xp - cur_floor, max(1, next_floor - cur_floor), length=12)

    await update.message.reply_text(
        f"📊 <b>{escape_html(target_name)}</b>\n"
        f"Level <b>{level}</b> · {xp:,} XP · {coins:,} 🪙\n"
        f"{progress} {xp - cur_floor}/{next_floor - cur_floor} to level {level + 1}",
        parse_mode="HTML",
    )


@safe_handler
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Leaderboards are per-group — try this in a group chat.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(ChatMember, User)
            .join(User, User.id == ChatMember.user_id)
            .where(ChatMember.chat_id == update.effective_chat.id)
            .order_by(desc(ChatMember.xp))
            .limit(10)
        )
        rows = result.all()

    if not rows:
        await update.message.reply_text("📊 No one has earned XP here yet.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Leaderboard</b>\n"]
    for i, (member, user) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = user.first_name or user.username or str(user.id)
        lines.append(f"{prefix} {escape_html(name)} — Level {member.level or 0} ({(member.xp or 0):,} XP)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("💰 Daily rewards are per-group — try this in a group chat.")
        return

    user, chat = update.effective_user, update.effective_chat
    now = datetime.utcnow()

    async with async_session() as session:
        member = await get_or_create_member(session, chat.id, user.id)
        if member.last_daily_at and (now - member.last_daily_at) < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - member.last_daily_at)
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            await session.commit()
            await update.message.reply_text(f"⏳ Already claimed. Next daily in {hours}h {minutes}m.")
            return

        reward = random.randint(settings.economy_daily_min, settings.economy_daily_max)
        member.coins = (member.coins or 0) + reward
        member.last_daily_at = now
        new_balance = member.coins
        await session.commit()

    await update.message.reply_text(f"💰 Daily claimed: +{reward:,} 🪙 (balance: {new_balance:,} 🪙)")


@safe_handler
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("💰 Balances are per-group — try this in a group chat.")
        return
    async with async_session() as session:
        member = await get_or_create_member(session, update.effective_chat.id, update.effective_user.id)
        coins = member.coins or 0
        await session.commit()
    await update.message.reply_text(f"💰 Balance: {coins:,} 🪙")


@safe_handler
async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("💸 Payments are per-group — try this in a group chat.")
        return

    target_id, target_name = await resolve_target_user(update, context)
    amount_args = [a for a in context.args if a.isdigit()]
    if not target_id or not amount_args:
        await update.message.reply_text("Usage: /pay <amount> (reply to someone, or /pay @username <amount>)")
        return
    amount = int(amount_args[0])
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be positive.")
        return
    if target_id == update.effective_user.id:
        await update.message.reply_text("❌ You can't pay yourself.")
        return

    chat_id = update.effective_chat.id
    # Single-transaction transfer (economy_service.transfer_coins) rather
    # than a Python read-then-write on two rows in one session — see that
    # function's docstring for why.
    if not await transfer_coins(chat_id, update.effective_user.id, target_id, amount):
        await update.message.reply_text("❌ You don't have enough coins.")
        return

    await update.message.reply_text(f"💸 Paid {amount:,} 🪙 to {escape_html(target_name)}.", parse_mode="HTML")


@safe_handler
async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(select(ShopItem).order_by(ShopItem.price))
        items = result.scalars().all()

    if not items:
        await update.message.reply_text("🏪 The shop is empty right now.")
        return

    lines = ["🏪 <b>Shop</b>  —  <code>/buy &lt;key&gt;</code>\n"]
    for item in items:
        lines.append(
            f"{item.emoji} <code>{item.key}</code> — {escape_html(item.name)} — {item.price:,} 🪙\n"
            f"<i>{escape_html(item.description)}</i>"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


@safe_handler
async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /buy <item key> — see /shop")
        return
    key = context.args[0].lower()
    chat_id = update.effective_chat.id if update.effective_chat.type != "private" else None

    async with async_session() as session:
        result = await session.execute(select(ShopItem).where(ShopItem.key == key))
        item = result.scalar_one_or_none()
        if not item:
            await update.message.reply_text("❌ No such item. See /shop")
            return

        if chat_id:
            member = await get_or_create_member(session, chat_id, update.effective_user.id)
            if (member.coins or 0) < item.price:
                await session.commit()
                await update.message.reply_text("❌ Not enough coins.")
                return
            member.coins -= item.price
        # (in DMs there's no per-chat wallet to charge — purchases there
        # are free; economy is a group feature, see the other commands.)

        inv_result = await session.execute(
            select(UserInventory).where(
                UserInventory.user_id == update.effective_user.id, UserInventory.item_key == key
            )
        )
        entry = inv_result.scalar_one_or_none()
        if entry:
            entry.quantity = (entry.quantity or 0) + 1
        else:
            session.add(UserInventory(user_id=update.effective_user.id, item_key=key, quantity=1))
        await session.commit()

    await update.message.reply_text(f"✅ Bought {escape_html(item.name)}!", parse_mode="HTML")


@safe_handler
async def inventory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(
            select(UserInventory, ShopItem)
            .join(ShopItem, ShopItem.key == UserInventory.item_key)
            .where(UserInventory.user_id == update.effective_user.id)
        )
        rows = result.all()

    if not rows:
        await update.message.reply_text("🎒 Your inventory is empty. Check /shop!")
        return

    lines = ["🎒 <b>Inventory</b>\n"]
    for inv, item in rows:
        lines.append(f"{item.emoji} {escape_html(item.name)} x{inv.quantity}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def register(app):
    if not settings.enable_economy:
        return
    app.add_handler(CommandHandler(["level", "rank"], level_cmd))
    app.add_handler(CommandHandler(["leaderboard", "lb"], leaderboard_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler(["balance", "bal"], balance_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))
    app.add_handler(CommandHandler("shop", shop_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler(["inventory", "inv"], inventory_cmd))

    # Distinct group — see group_mgmt.py's register() for why every
    # catch-all text handler needs its own.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, xp_listener), group=6)
