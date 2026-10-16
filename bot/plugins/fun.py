"""
Fun & Social Plugin
Couples, karma, AFK, memes, quotes, 8ball, dice, wish, urban dictionary.
"""
import hashlib
import random
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from bot.core.database import async_session, User, ChatMember
from bot.config import settings
from bot.core.errors import safe_handler
from bot.utils.helpers import mention_html, escape_html
from sqlalchemy import select, desc

from datetime import timezone

def _fmt_afk_duration(since) -> str:
    """Human-readable AFK duration (e.g. 11s, 2m 5s, 1h 3m)."""
    if not since:
        return "0s"
    now = datetime.now(timezone.utc)
    if getattr(since, "tzinfo", None) is None:
        since = since.replace(tzinfo=timezone.utc)
    s = max(0, int((now - since).total_seconds()))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h, m = s // 3600, (s % 3600) // 60
    if h < 24:
        return f"{h}h {m}m"
    return f"{h // 24}d {h % 24}h"



def _get_meme_url() -> str:
    subreddits = ["memes", "dankmemes", "me_irl", "ProgrammerHumor"]
    sub = random.choice(subreddits)
    return f"https://meme-api.com/gimme/{sub}"


@safe_handler
async def meme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # The feed is community-sourced: retry a couple of times to dodge NSFW/spoiler posts
        # rather than posting them into a group chat.
        for _ in range(3):
            async with session.get(_get_meme_url()) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            if data.get("nsfw") or data.get("spoiler"):
                continue
            await update.message.reply_photo(
                data["url"],
                caption=(
                    f"😂 <b>{escape_html(str(data.get('title', '')))}</b>\n"
                    f"👍 {data.get('ups', 0)} | 💬 {data.get('num_comments', 0)}"
                ),
                parse_mode="HTML",
            )
            return
    await update.message.reply_text("❌ Couldn't fetch a meme right now.")


@safe_handler
async def couples_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick two active members as Couple of the Day — polished UI with optional art.

    Deterministic per UTC day so the same couple is shown all day.
    Falls back gracefully when fewer than 2 members are known.
    """
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("📢 Use this in a group!")
        return

    async with async_session() as session:
        result = await session.execute(
            select(ChatMember.user_id, User.first_name, User.username)
            .join(User, User.id == ChatMember.user_id)
            .where(ChatMember.chat_id == chat.id)
            .order_by(desc(ChatMember.xp), desc(ChatMember.personality_msg_count))
            .limit(50)
        )
        rows = result.all()

    if len(rows) < 2:
        await update.message.reply_text(
            "💑 Need at least 2 active members in this chat first.\n"
            "Chat a bit more so the bot can learn who's here, then try again."
        )
        return

    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Stable across restarts and processes: builtin hash() of a str-containing
    # tuple is randomized per process (PYTHONHASHSEED), which would reshuffle
    # "today's couple" on every deploy despite the once-a-day promise.
    seed = int.from_bytes(hashlib.sha256(f"{chat.id}:{today}".encode()).digest()[:8], "big")
    rng = random.Random(seed)
    pair = rng.sample(rows, 2)
    (uid_a, name_a, user_a), (uid_b, name_b, user_b) = pair
    display_a = name_a or user_a or str(uid_a)
    display_b = name_b or user_b or str(uid_b)

    # Fancy caption matching the popular Couple of The Day style
    caption = (
        "🎀 <b>Couple of The Day</b> 🎀\n\n"
        f"  ➤ {mention_html(uid_a, display_a)}  +  {mention_html(uid_b, display_b)}  =  💕\n"
        f"     ───────────────\n"
        f"     ↳ ❤️  ship locked until midnight UTC"
    )

    # Optional art: only when the operator sets COUPLE_ART_URL. No network
    # probing on the hot path; any send failure falls back to plain text.
    art_url = getattr(settings, "couple_art_url", None)
    if art_url:
        try:
            await update.message.reply_photo(photo=art_url, caption=caption, parse_mode="HTML")
            return
        except Exception:
            pass

    await update.message.reply_text(caption, parse_mode="HTML")


@safe_handler
async def karma_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message

    if reply:
        target = reply.from_user
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == target.id))
            u = result.scalar_one_or_none()
            if not u:
                u = User(id=target.id, username=target.username, first_name=target.first_name)
                session.add(u)
            u.karma += 1
            await session.commit()
        await update.message.reply_text(
            f"☯️ {mention_html(target.id, target.first_name)} gained +1 karma! (Total: {u.karma})",
            parse_mode="HTML"
        )
    else:
        async with async_session() as session:
            result = await session.execute(
                select(User).order_by(desc(User.karma)).limit(10)
            )
            top = result.scalars().all()

        text = "<b>🏆 Karma Leaderboard</b>\n\n"
        for i, u in enumerate(top, 1):
            name = u.first_name or "Unknown"
            text += f"{i}. {mention_html(u.id, name)} — {u.karma}\n"
        await update.message.reply_text(text, parse_mode="HTML")


@safe_handler
async def afk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    reason = " ".join(context.args) or "AFK"

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            u = User(id=user.id, username=user.username, first_name=user.first_name)
            session.add(u)
        u.afk_reason = reason
        u.afk_since = datetime.utcnow()
        await session.commit()

    await update.message.reply_text(
        f"💤 {mention_html(user.id, user.first_name)} is now Away!\n"
        f"<i>{escape_html(reason)}</i>",
        parse_mode="HTML"
    )


@safe_handler
async def afk_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Check if anyone mentioned is AFK
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                username = update.message.text[entity.offset+1:entity.offset+entity.length]
                async with async_session() as session:
                    result = await session.execute(select(User).where(User.username == username))
                    u = result.scalar_one_or_none()
                    if u and u.afk_reason:
                        dur = _fmt_afk_duration(u.afk_since)
                        await update.message.reply_text(
                            f"💤 {username} is AFK!\n"
                            f"<i>{escape_html(u.afk_reason or 'AFK')}</i>\n"
                            f"Since: {dur}",
                            parse_mode="HTML"
                        )

    # Check reply
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == target.id))
            u = result.scalar_one_or_none()
            if u and u.afk_reason:
                dur = _fmt_afk_duration(u.afk_since)
                await update.message.reply_text(
                    f"💤 {mention_html(target.id, target.first_name)} is AFK!\n"
                    f"<i>{escape_html(u.afk_reason or 'AFK')}</i>\n"
                    f"Since: {dur}",
                    parse_mode="HTML"
                )

    # Remove AFK if user sends message
    user = update.effective_user
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if u and u.afk_reason:
            dur = _fmt_afk_duration(u.afk_since)
            u.afk_reason = None
            u.afk_since = None
            await session.commit()
            await update.message.reply_text(
                f"👋 Welcome back, {mention_html(user.id, user.first_name)}!\n"
                f"You were AFK for: <b>{dur}</b>",
                parse_mode="HTML"
            )


@safe_handler
async def eightball_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answers = [
        "🟢 It is certain.", "🟢 Without a doubt.", "🟡 Ask again later.",
        "🟡 Better not tell you now.", "🔴 Don't count on it.", "🔴 My sources say no.",
        "🟢 Yes definitely.", "🟡 Concentrate and ask again.", "🔴 Very doubtful."
    ]
    await update.message.reply_text(random.choice(answers))


@safe_handler
async def wish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wish = " ".join(context.args) or "nothing"
    chance = random.randint(1, 100)
    bar = "█" * (chance // 10) + "▒" * (10 - chance // 10)
    await update.message.reply_text(
        f"⭐ <b>Your Wish</b>: <i>{escape_html(wish)}</i>\n"
        f"<b>Likelihood</b>: {bar} {chance}%",
        parse_mode="HTML"
    )


@safe_handler
async def ud_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args)
    if not term:
        await update.message.reply_text("Usage: /ud <term>")
        return

    import aiohttp
    from urllib.parse import quote

    url = f"https://api.urbandictionary.com/v0/define?term={quote(term)}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await update.message.reply_text("❌ Urban Dictionary isn't responding right now.")
                return
            data = await resp.json()
            if data.get("list"):
                entry = data["list"][0]
                defi = entry["definition"][:800]
                example = entry.get("example", "")[:400]
                await update.message.reply_text(
                    f"<b>📖 {escape_html(entry['word'])}</b>\n"
                    f"{escape_html(defi)}\n\n"
                    f"<i>Example:</i> {escape_html(example)}\n"
                    f"👍 {entry['thumbs_up']} | 👎 {entry['thumbs_down']}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ No definition found.")


@safe_handler
async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quotes = [
        "The only way to do great work is to love what you do. — Steve Jobs",
        "Innovation distinguishes between a leader and a follower. — Steve Jobs",
        "Stay hungry, stay foolish. — Stewart Brand",
        "Code is like humor. When you have to explain it, it's bad. — Cory House",
        "First, solve the problem. Then, write the code. — John Johnson",
    ]
    await update.message.reply_text(f"💬 <i>{random.choice(quotes)}</i>", parse_mode="HTML")




# ── Extra fun commands absorbed from FallenRobot (full useful set) ──────────
# These were missing or weaker; templates are self-contained (standard Marie-style).

_SLAP_TEMPLATES = [
    "{user1} slaps {user2} around a bit with a large trout.",
    "{user1} slaps {user2} with a {item}.",
    "{user1} {hits} {user2} with a {item}.",
    "{user1} {hits} {user2} in the face with a {item}.",
    "{user1} {throws} a {item} at {user2}.",
    "{user1} grabs a {item} and {throws} it at {user2}'s face.",
    "{user1} launches a {item} in {user2}'s general direction.",
    "{user1} starts slapping {user2} silly with a {item}.",
    "{user1} pins {user2} down and repeatedly {hits} them with a {item}.",
    "{user1} grabs {user2}'s head and {hits} it against a wall.",
]
_ITEMS = ["frying pan", "baseball bat", "cricket bat", "wooden cane", "shovel",
          "toaster", "book", "laptop", "rubber chicken", "rainbow trout",
          "fried chicken leg", "banana peel", "pillow", "wet noodle"]
_HIT = ["hits", "whacks", "slaps", "smacks", "bashes"]
_THROW = ["throws", "flings", "chucks", "hurls"]
_PAT_TEMPLATES = [
    "{user1} pats {user2} on the head.",
    "*gently pats {user2}*",
    "{user1} gives {user2} a soft headpat.",
    "{user1} pats {user2}'s head affectionately.",
]
_TOSS = ["Heads", "Tails"]
_DECIDE = ["Yes.", "No.", "Maybe.", "Probably.", "I wouldn't.", "Absolutely!", "Ask again later."]
_TABLE = [
    "(╯°□°)╯︵ ┻━┻",
    "┬─┬ ノ( ゜-゜ノ)",
    "(ノ ゜Д゜)ノ ┻━┻",
    "┻━┻ ︵ヽ(`Д´)ﾉ︵ ┻━┻",
]
_RUNS = [
    "Runs to the nearest McDonalds.",
    "Runs away screaming.",
    "Runs into a wall.",
    "Runs like the Flash... then trips.",
]


@safe_handler
async def slap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/slaptext - text-only slap with a random item (reply or name); /slap sends a GIF."""
    user1 = escape_html(update.effective_user.first_name or "Someone")
    target = update.message.reply_to_message
    if target and target.from_user:
        user2 = escape_html(target.from_user.first_name or "them")
    elif context.args:
        user2 = escape_html(" ".join(context.args))
    else:
        user2 = user1
        user1 = "The bot"
    temp = random.choice(_SLAP_TEMPLATES)
    reply = temp.format(
        user1=user1, user2=user2,
        item=random.choice(_ITEMS),
        hits=random.choice(_HIT),
        throws=random.choice(_THROW),
    )
    await update.message.reply_text(reply, parse_mode="HTML")


@safe_handler
async def pat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pattext - text-only headpat (reply or name); /pat sends a GIF."""
    user1 = escape_html(update.effective_user.first_name or "Someone")
    target = update.message.reply_to_message
    if target and target.from_user:
        user2 = escape_html(target.from_user.first_name or "them")
    elif context.args:
        user2 = escape_html(" ".join(context.args))
    else:
        user2 = user1
    await update.message.reply_text(
        random.choice(_PAT_TEMPLATES).format(user1=user1, user2=user2),
        parse_mode="HTML",
    )


@safe_handler
async def roll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /roll [NdM] — dice roll (default 1d6). """
    expr = " ".join(context.args) if context.args else "1d6"
    import re as _re
    m = _re.fullmatch(r"(\d{1,2})d(\d{1,3})", expr.replace(" ", "").lower())
    if not m:
        await update.message.reply_text("Usage: /roll [NdM]  e.g. /roll 2d20")
        return
    n, sides = int(m.group(1)), int(m.group(2))
    if n < 1 or n > 20 or sides < 2 or sides > 1000:
        await update.message.reply_text("Keep it reasonable: 1–20 dice, 2–1000 sides.")
        return
    rolls = [random.randint(1, sides) for _ in range(n)]
    total = sum(rolls)
    detail = ", ".join(str(r) for r in rolls)
    await update.message.reply_text(f"🎲 {n}d{sides}: [{detail}] = <b>{total}</b>", parse_mode="HTML")


@safe_handler
async def toss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🪙 {random.choice(_TOSS)}")


@safe_handler
async def decide_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else ""
    ans = random.choice(_DECIDE)
    if q:
        await update.message.reply_text(f"❓ {escape_html(q)}\n→ <b>{ans}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text(ans)


@safe_handler
async def table_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(random.choice(_TABLE))


@safe_handler
async def shrug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¯\\_(ツ)_/¯")


@safe_handler
async def shout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /shout <text> — vertical shout. """
    if not context.args:
        await update.message.reply_text("Usage: /shout <text>")
        return
    text = " ".join(context.args)[:40]
    result = "\n".join(c for c in text.upper() if c.strip())
    await update.message.reply_text(f"<pre>{escape_html(result)}</pre>", parse_mode="HTML")


@safe_handler
async def runs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(random.choice(_RUNS))


@safe_handler
async def bluetext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /bluetext — classic blue-text prank message. """
    await update.message.reply_text(
        "⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤\n"
        "LOOK AT THIS BLUE TEXT!\n"
        "⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤⬤\n"
        "Now you have blue text on your screen."
    )



def register(app):
    app.add_handler(CommandHandler("meme", meme_cmd))
    app.add_handler(CommandHandler("couples", couples_cmd))
    app.add_handler(CommandHandler("karma", karma_cmd))
    app.add_handler(CommandHandler("k", karma_cmd))
    app.add_handler(CommandHandler("afk", afk_cmd))
    app.add_handler(CommandHandler("8ball", eightball_cmd))
    app.add_handler(CommandHandler("wish", wish_cmd))
    app.add_handler(CommandHandler("ud", ud_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    # Fallen-absorbed fun commands (full set, no removals)
    # /slap and /pat belong to plugins/reactions.py (GIF versions); the text-only Fallen versions keep their behaviour
    # under non-conflicting names so neither feature is lost (PTB runs only the first handler registered per command).
    app.add_handler(CommandHandler(["slaptext", "tslap"], slap_cmd))
    app.add_handler(CommandHandler(["pattext", "tpat"], pat_cmd))
    app.add_handler(CommandHandler("roll", roll_cmd))
    app.add_handler(CommandHandler("toss", toss_cmd))
    app.add_handler(CommandHandler("decide", decide_cmd))
    app.add_handler(CommandHandler("table", table_cmd))
    app.add_handler(CommandHandler("shrug", shrug_cmd))
    app.add_handler(CommandHandler("shout", shout_cmd))
    app.add_handler(CommandHandler("runs", runs_cmd))
    app.add_handler(CommandHandler("bluetext", bluetext_cmd))
    # Distinct group so this doesn't collide with group_mgmt.py's lock/filter
    # listeners or the personality plugin's catch-all reply handler — see the
    # comment in group_mgmt.py's register() for the full explanation.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, afk_listener), group=3)
