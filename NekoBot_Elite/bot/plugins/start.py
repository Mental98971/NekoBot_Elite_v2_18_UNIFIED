"""
Start handler, help, and the functional inline menu system.

Each menu button opens a real command guide for features that exist in
this codebase — not empty placeholders.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from bot.identity import bot_name
from bot.utils.helpers import mention_html
from bot.utils import i18n
from bot.utils.langctx import chat_language
from bot.core.errors import safe_handler


MAIN_MENU = [
    ["🛡️ Admin", "🤖 AI", "🎭 Personality"],
    ["🎌 Anime", "🎵 Music", "🎧 Live Music"],
    ["🎯 Fun", "🔤 Fonts", "🤗 Reactions"],
    ["🔐 Locks", "📝 Notes", "🔍 Filters"],
    ["👋 Greetings", "⚠️ Warnings", "📊 Stats"],
    ["🎴 Collector", "💰 Economy", "🎮 Games"],
]

PAGE_2 = [
    ["🧹 Purge", "📜 Rules", "ℹ️ Info"],
    ["💤 AFK", "☯️ Karma", "💑 Couples"],
    ["🌐 Translate", "📖 Urban", "🎬 IMDb"],
    ["📰 News", "🌤 Weather", "📲 QR"],
    ["🎤 TTS", "⬇️ Download", "🛠 Tools"],
]

PAGE_3 = [
    ["🌙 Nightmode", "🚫 Anti-Spam", "🛡 Anti-Flood"],
    ["✅ Captcha", "📢 Tag All", "📋 Log Channel"],
    ["🌐 Federation", "🔧 Disable", "⚫ Blacklist"],
    ["✅ Approvals", "🔗 Connect", "🧰 Admin Tools"],
    ["🎧 Playlists", "📜 Lyrics", "🎛 Control"],
    ["⏰ Schedule", "🔎 Reference", "🏷 Stickers"],
    ["🤫 Whisper", "⚔️ Rankings"],
]

PAGES = {1: MAIN_MENU, 2: PAGE_2, 3: PAGE_3}
TOTAL_PAGES = 3

MENU_PANELS: dict[str, tuple[str, str]] = {
    "schedule": (
        "⏰ Scheduling & polls",
        "/remind &lt;time&gt; &lt;text&gt; · /reminders\n"
        "/schedule · /announce · /poll · /apoll · /stoppoll · /forgotpollid\n"
        "<i>Time: 30m 2h 1d</i>",
    ),
    "admin": (
        "🛡️ Admin & moderation",
        "/ban [time] · /tban [time] · /mute [time] · /tmute [time]\n"
        "/unban /unmute /kick /warn /warns /resetwarns\n"
        "/purge /del /slowmode /pin /unpin\n"
        "/zombies /rmzombies /unbanall · /report (reply — flags a message to admins)\n"
        "<i>Time: 30m 2h 1d — reply to target user</i>",
    ),
    "ai": (
        "🤖 AI assistant",
        "<b>/ai</b> &lt;question&gt; — general assistant\n"
        "/imagine /summarize /code /see /transcribe /persona\n"
        "<i>For Neko personality banter use /chat (not /ai).</i>",
    ),
    "personality": (
        "🎭 Neko personality",
        "<b>/chat</b> &lt;message&gt; — talk to Neko\n"
        "Or <b>@mention</b> / <b>reply</b> to the bot\n"
        "/personality on|off · /joke · /nnr_settings · /nnr_help\n"
        "Imitation personas (admins): /premember &lt;fact&gt; · /pmemory · /pforget &lt;id|all&gt; — owner: /pengine\n"
        "Ambient banter is on by default in DMs.",
    ),
    "anime": (
        "🎌 Anime",
        "/anime · /manga · /manhwa · /manhua · /novel · /movie &lt;title&gt;\n"
        "/character (/char) · /relations · /watch &lt;title&gt;\n"
        "/seasonal · /topanime · /topmanga · /topcharacters · /top &lt;genre&gt;\n"
        "/airing · /upcoming · /anischedule · /browse · /rec · /fillers\n"
        "/animeid · /malid · /reverse · /ping\n"
        "/user · /flex · /alauth · /logout · /activity · /favourites\n"
        "<i>Sources: AniList + MyAnimeList (Jikan)</i>",
    ),
    "music": (
        "🎵 Music files",
        "/music &lt;query&gt; — download &amp; send as audio file\n"
        "/tts &lt;text&gt; · /insta · /qr\n"
        "<i>For voice-chat streaming see Live Music.</i>",
    ),
    "live": (
        "🎧 Live voice-chat music",
        "<b>Requires:</b> active voice chat + MUSIC_API_ID/HASH in .env\n"
        "Bot account must be able to join the call (usually admin).\n\n"
        "/play &lt;song or URL&gt; · /skip · /pause · /resume · /stop\n"
        "/queue · /nowplaying · /volume · /shuffle · /repeat\n"
        "/loop · /seek · /seekback · /effects · /playlist · /lyrics\n"
        "/auth · /toptracks · /resetqueue · /channelplay · /videomode",
    ),
    "fun": (
        "🎯 Fun",
        "/fun · /meme · /cosplay · /quote · /wish · /couples · /dice\n"
        "/8ball · /joke · /karma · /coinflip · /rps · /slots\n"
        "/truth · /dare · /roll · /toss · /decide\n"
        "/slaptext · /pattext · /table · /shrug · /shout · /runs · /bluetext\n"
        "<i>GIF slaps and pats: /slap · /pat (🤗 Reactions).</i>",
    ),
    "fonts": (
        "🔤 Fonts",
        "/fonts · /f1 … /f18 · /flip · /fontfx\n"
        "/stinky /bubbles /underline /rays /strike /frozen\n"
        "/random /mix /reverse",
    ),
    "reactions": (
        "🤗 Reactions",
        "/reactions on|off\n"
        "Then: /hug /pat /kiss /cuddle /slap /poke /tickle /bonk …\n"
        "Reply to someone or @mention them as the target.",
    ),
    "locks": (
        "🔐 Locks",
        "/lock &lt;type&gt; · /unlock &lt;type&gt; · /locks\n"
        "Types: url forward photo video sticker gif contact location\n"
        "Admins and /approve'd users are never locked out.",
    ),
    "notes": (
        "📝 Notes",
        "/notes — list · /save &lt;name&gt; &lt;content&gt; · /get &lt;name&gt; (or just #name)\n"
        "/clear &lt;name&gt; — delete one · /clearall — delete all (owner)",
    ),
    "filters": (
        "🔍 Filters",
        "/filter &lt;trigger&gt; &lt;reply&gt; — add a keyword auto-reply (or reply to content)\n"
        "/filters — list · /stopfilter &lt;trigger&gt; · /stopallfilters\n"
        "Triggers match whole words or phrases, not parts of words.",
    ),
    "greetings": (
        "👋 Greetings",
        "/setwelcome &lt;text&gt; · /setgoodbye &lt;text&gt;\n"
        "Use {user} / {chat} placeholders. Fires on join/leave.",
    ),
    "warnings": (
        "⚠️ Warnings",
        "/warn · /dwarn (also deletes their message) · /swarn (silent)\n"
        "/warns [user] · /rmwarn (remove latest) · /resetwarns\n"
        "/setwarnlimit &lt;n&gt; · /setwarnaction mute|kick|ban — applied when the limit is reached.",
    ),
    "stats": (
        "📊 Stats",
        "/stats · /mystats · /botstats · /globalstats\n"
        "/level · /leaderboard · /balance · /topcatchers · /cstats\n"
        "/health (or /status) — bot diagnostics, owner/admin only",
    ),
    "collector": (
        "🎴 Character collector",
        "Characters spawn in groups — /grab to catch\n"
        "/collection · /characters · /listcharacters · /cview · /crandom\n"
        "/fav · /smelt · /myprofile · /cstats · /cprivacy · /topcatchers\n"
        "/trade · /gift — full list: /chelp",
    ),
    "economy": (
        "💰 Economy",
        "/daily · /balance · /pay · /shop · /buy · /inventory\n"
        "/level · /leaderboard — XP from chatting in groups",
    ),
    "games": (
        "🎮 Games",
        "/trivia · /tictactoe · /guess · /dice · /coinflip · /rps · /slots\n"
        "/wordseek · /wordgrid (/wordgridend) · /wordchain\n"
        "/chess · /ludo · /uno (hands arrive by DM)\n"
        "<i>/poll lives under ⏰ Schedule.</i>",
    ),
    "purge": (
        "🧹 Purge",
        "/purge — delete from replied message to here\n"
        "/del — delete the replied message",
    ),
    "rules": (
        "📜 Rules",
        "/rules · /setrules",
    ),
    "info": (
        "ℹ️ Info",
        "/id · /info [reply|id] · /names|/history [reply|id|@user] (name history) · /namealert · /forgetnames\n"
        "/me /setme /bio /setbio · /paste /wiki /github /country /covid · /encrypt /decrypt · /rss /addrss /removerss\n"
        "/myanime /mymanga /mycharacter /myupcoming (MyAnimeList)",
    ),
    "afk": (
        "💤 AFK",
        "/afk [reason] — mark yourself away",
    ),
    "karma": (
        "☯️ Karma",
        "Reply with +1 / -1 or /karma",
    ),
    "couples": (
        "💑 Couples",
        "/couples — couple of the day in this chat",
    ),
    "translate": (
        "🌐 Translation",
        "/tr &lt;lang&gt; &lt;text&gt; — e.g. /tr es Hello\n"
        "/tr &lt;text&gt; → English · reply + /tr &lt;lang&gt;",
    ),
    "urban": (
        "📖 Urban Dictionary",
        "/ud &lt;term&gt;",
    ),
    "imdb": (
        "🎬 IMDb / titles",
        "/imdb &lt;title&gt; — movie &amp; series lookup",
    ),
    "news": (
        "📰 News",
        "/news — latest headlines (when configured)\n"
        "<i>Also: anime seasonal via /seasonal</i>",
    ),
    "weather": (
        "🌤 Weather",
        "/weather &lt;city&gt; — live conditions (wttr.in)",
    ),
    "qr": (
        "📲 QR",
        "/qr &lt;text&gt;",
    ),
    "tts": (
        "🎤 TTS",
        "/tts &lt;text&gt;",
    ),
    "download": (
        "⬇️ Download",
        "/music &lt;query&gt; · /yt · /insta — media as files\n"
        "<i>Not the same as /play (live VC streaming).</i>",
    ),
    "tools": (
        "🛠 Tools",
        "/id · /info · /tr · /ud · /weather · /qr\n"
        "/calc · /imdb · /tagall · /names · /speedtest\n"
        "/simplify /factor /derive /integrate /zeroes /tangent /area · /sin /cos /tan /sqrt /ln (exact)\n"
        "/carbon · /wallpaper · /webss · /gps · /gsearch",
    ),
    "nightmode": (
        "🌙 Nightmode",
        "/nightmode — toggle restricted hours (admin)",
    ),
    "anti-spam": (
        "🚫 Anti-Spam",
        "Automatic heuristic spam scoring in groups. Admins and approved users are exempt.\n"
        "/antispam on|off — per chat · /approve &lt;user&gt; — exempt someone\n"
        "/antichannel — toggle deleting + banning posts made via a channel identity (admin)",
    ),
    "reference": (
        "🔎 Reference",
        "/pokedex &lt;name|number&gt; — Pokémon info\n"
        "/currency &lt;amount&gt; &lt;FROM&gt; &lt;TO&gt; · /convert — currency conversion\n"
        "/telegraph &lt;title&gt; | &lt;text&gt; — publish a Telegraph page",
    ),
    "stickers": (
        "🏷 Stickers",
        "/kang — reply to a sticker/image to add it to your personal pack\n"
        "/packinfo — show your sticker pack\n"
        "/q [color] (or /quotly) — reply to a message to turn it into a quote sticker",
    ),
    "anti-flood": (
        "🛡 Anti-Flood",
        "Automatic flood detection in groups. /antiflood — show settings\n"
        "/setflood &lt;3-100|off&gt; · /setfloodmode ban|mute|kick|tban|tmute [time]\n"
        "/slowmode &lt;seconds|off&gt; — bot-enforced gap between a member's messages\n"
        "/modsettings — every moderation setting at a glance.",
    ),
    "captcha": (
        "✅ Captcha",
        "/captcha on|off — join check (same as /welcomemute strong unless you pick a mode there)\n"
        "/welcomemute off|soft|strong · /welcomemutehelp\n"
        "Federations &amp; bans: /banenforce on|off (remove gbanned/fbanned users when they join or speak)",
    ),
    "all": (
        "📢 Tag all",
        "/tagall [message] · /all · /mentionall\n"
        "Mentions active members (admin only).",
    ),
    "channel": (
        "📋 Log channel",
        "/setlog &lt;channel id|@channel&gt; · /unsetlog · /logchannel — bans, warns, reports… are posted there\n"
        "/report (reply) pings the admins · /reports on|off",
    ),
    "approvals": (
        "✅ Approvals",
        "/approve &lt;user&gt; · /unapprove &lt;user&gt; · /approved · /approval · /unapproveall (owner)\n"
        "Approved users skip locks, flood, spam, slow mode and the word blacklist in this chat.",
    ),
    "connect": (
        "🔗 Connect",
        "/connect — in your group, or /connect &lt;chat id|@group&gt; here · /disconnect · /connection\n"
        "Run moderation, notes, filters, locks and settings commands from this private chat. "
        "/allowconnect on|off (admins) controls who may connect.",
    ),
    "admintools": (
        "🧰 Admin tools",
        "/promote · /fullpromote · /demote · /title &lt;text&gt; · /admins\n"
        "/pin [silent] · /unpin · /unpinall · /pinned · /invitelink\n"
        "/setgtitle · /setdesc · /purge [n] · /spurge · /del · /unbanall",
    ),
    "federation": (
        "🌐 Federation",
        "/newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote\n"
        "/myfeds /chatfed /fedstat /fbanlist /importfbans /subfed /unsubfed /fedsubs /fedchats /renamefed /delfed "
        "/frules /setfrules /fednotif /setfedlog /fbroadcast · /banenforce on|off · /fedhelp",
    ),
    "disable": (
        "🔧 Disable commands",
        "/disable &lt;cmd&gt; · /enable &lt;cmd&gt; · /disabled",
    ),
    "blacklist": (
        "⚫ Blacklist",
        "Word blacklist (admins): /addblacklist &lt;word&gt; · /unblacklist &lt;word&gt; · /blacklist\n"
        "/blacklistmode delete|warn|mute|kick|ban|tmute|tban [time]\n"
        "Whole-word match, * wildcard. Bot owners: /blacklistchat /authorize — chat access control\n"
        "Warn filters: /addwarn &lt;word&gt; &lt;reason&gt; · /nowarn · /warnlist · /strongwarn on|off\n"
        "Sticker packs: /addblsticker (reply) · /unblsticker · /blsticker · /blstickermode &lt;mode&gt; [time]\n"
        "Blue text: /cleanblue on|off · /ignoreblue &lt;cmd&gt; · /unignoreblue · /listblue (owners: /gignoreblue)",
    ),
    "playlists": (
        "🎧 Playlists",
        "/playlist save|load|list — save queues for later",
    ),
    "lyrics": (
        "📜 Lyrics",
        "/lyrics &lt;song&gt;",
    ),
    "whisper": (
        "🤫 Whispers",
        "Inline: <code>@bot @username your secret</code> — only they can open it\n"
        "/whisper (/create) — compose long or media whispers in PM · /list — pending whispers\n"
        "/privacy on|off · /notifications · /cancel · /whisperhelp\n"
        "<i>Owner: /whisperban · /whisperunban</i>",
    ),
    "rankings": (
        "⚔️ Rankings",
        "Group message leaderboards:\n"
        "/rankings (/ranking) · /topusers · /userstats &lt;reply|@user|id&gt; · /mytop\n"
        "/sysstats — host resources · /fightinfo — about\n"
        "<i>Owner: /update · /restart</i>",
    ),
    "control": (
        "🎛 Control panel",
        "You're already in /menu — use the pages or /settings.",
    ),
}


# Buttons normally map to the lower-cased last word of their label. Labels
# that would collide on that rule get an explicit key here ("🎵 Music" and
# "🎧 Live Music" both end in "music"; without this the live-music panel
# was unreachable and its button opened the file-download panel instead).
_KEY_OVERRIDES: dict[str, str] = {"🎧 Live Music": "live", "🧰 Admin Tools": "admintools"}


def _callback_key(label: str) -> str:
    return _KEY_OVERRIDES.get(label) or label.split()[-1].lower()


def _localized_label(label: str, lang: str, key: str | None = None) -> str:
    """Button/title text in ``lang``: the English label's leading emoji + the catalog's ``menu.btn.<key>`` text.

    ``key`` is the panel key; it defaults to the key derived from a button label.  Callback data always comes from the English label
    (``_callback_key``), so translating can never change what a button does.  English, or a missing translation, returns the label
    unchanged.
    """
    if lang == "en":
        return label
    emoji, sep, _rest = label.partition(" ")
    if not sep:
        return label
    catalog_key = f"menu.btn.{key or _callback_key(label)}"
    text = i18n.t(catalog_key, lang)
    return label if text == catalog_key else f"{emoji} {text}"


def _build_keyboard(page_data, page_num: int, total_pages: int, lang: str = "en") -> InlineKeyboardMarkup:
    buttons = []
    for row in page_data:
        buttons.append([
            InlineKeyboardButton(_localized_label(btn, lang), callback_data=f"menu:{_callback_key(btn)}")
            for btn in row
        ])
    nav = []
    if page_num > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page_num - 1}"))
    nav.append(InlineKeyboardButton("🗑", callback_data="close"))
    if page_num < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{page_num + 1}"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def _panel_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("menu.back", lang), callback_data="page:1")],
        [InlineKeyboardButton(i18n.t("menu.close", lang), callback_data="close")],
    ])


@safe_handler
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep links: /start w_<id> opens a whisper, /start create | help-inline are the whisper plugin's too.
    if context.args:
        try:
            from bot.plugins.whisper import handle_start_payload

            if await handle_start_payload(update, context):
                return
        except ImportError:
            pass
    user = update.effective_user
    lang = await chat_language(update)
    text = "\n".join([
        i18n.t("start.welcome", lang, name=mention_html(user.id, user.first_name)),
        "",
        i18n.t("start.tagline", lang, bot=bot_name()),
        i18n.t("start.commands", lang),
        i18n.t("start.tap", lang),
        i18n.t("start.language", lang),
    ])
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_build_keyboard(MAIN_MENU, 1, TOTAL_PAGES, lang),
    )


@safe_handler
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    lang = await chat_language(update)

    if data.startswith("page:"):
        page = int(data.split(":")[1])
        page_data = PAGES.get(page, MAIN_MENU)
        await query.edit_message_text(
            i18n.t("menu.header", lang, bot=bot_name(), page=page, total=TOTAL_PAGES),
            parse_mode="HTML",
            reply_markup=_build_keyboard(page_data, page, TOTAL_PAGES, lang),
        )
        return

    if data == "close":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if data.startswith("menu:"):
        key = data.split(":", 1)[1]
        panel = MENU_PANELS.get(key)
        if panel:
            title, body = panel
            await query.edit_message_text(
                f"<b>{_localized_label(title, lang, key)}</b>\n\n{body}",  # the panel body (commands and short English glosses) stays as written
                parse_mode="HTML",
                reply_markup=_panel_keyboard(lang),
            )
        else:
            await query.edit_message_text(
                f"<b>📋 {key.title()}</b>\n\n"
                f"No dedicated panel for this item. Try /help for the full command list.",
                parse_mode="HTML",
                reply_markup=_panel_keyboard(lang),
            )


@safe_handler
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await chat_language(update)

    def L(key: str) -> str:
        return i18n.t(key, lang)

    text = "\n".join([
        i18n.t("help.title", lang, bot=bot_name()),
        "",
        f"<b>👮 {L('help.cat.moderation')}:</b> /ban /mute /kick /warn /unban /unmute /tmute /tban /purge — also /dban /sban /dwarn variants",
        f"<b>🛡️ {L('help.cat.security')}:</b> antiflood &amp; antispam (automatic), /locks /nightmode /captcha /blacklist /approve /modsettings",
        f"<b>📝 {L('help.cat.management')}:</b> /notes /filters /welcome /rules /setwelcome /setrules /connect",
        f"<b>🤖 {L('help.cat.ai')}:</b> /ai /imagine /summarize /code /see /transcribe /persona",
        f"<b>🎭 {L('help.cat.personality')}:</b> /chat — or @mention / reply to the bot (ambient banter on by default in DMs; /personality on|off in groups)",
        f"<b>🎌 {L('help.cat.anime')}:</b> /anime /manga /manhwa /manhua /novel /movie /char /relations /watch /seasonal /topanime /topmanga /topcharacters /top /airing /upcoming /anischedule /browse /rec /fillers /user /flex /alauth /activity /favourites <i>(AniList + MyAnimeList)</i>",
        f"<b>🎵 {L('help.cat.media')}:</b> /music /yt /insta /tts /qr",
        f"<b>🎧 {L('help.cat.vcmusic')}:</b> /play /skip /pause /resume /stop /queue /nowplaying /volume /shuffle /repeat /effects /loop /seek /playlist /lyrics <i>(needs MUSIC_API_ID/HASH + active voice chat)</i>",
        f"<b>🎯 {L('help.cat.fun')}:</b> /fun /couples /meme /cosplay /quote /wish /joke",
        f"<b>🎴 {L('help.cat.collector')}:</b> /grab /collection /characters /fav /myprofile /topcatchers /trade /gift — see /chelp",
        f"<b>💰 {L('help.cat.economy')}:</b> /daily /balance /pay /shop /buy /level /leaderboard",
        f"<b>🔧 {L('help.cat.utilities')}:</b> /id /info /names /tr /ud /weather /qr /calc /imdb /tagall /dice",
        f"<b>🔎 {L('help.cat.reference')}:</b> /pokedex /currency /convert /telegraph · <b>🏷 {L('help.cat.stickers')}:</b> /kang /packinfo /q",
        f"<b>📊 {L('help.cat.stats')}:</b> /stats /mystats /botstats /top /leaderboard",
        f"<b>🌐 {L('help.cat.fed')}:</b> /newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote /myfeds /fedstat /fbanlist /subfed /fedhelp",
        f"<b>🛠 {L('help.cat.admin')}:</b> /disable /enable /disabled /zombies /rmzombies /unbanall /names",
        f"<b>🛡 {L('help.cat.ops')}:</b> /globalstats /activevc /blacklistchat /authorize /maintenance /privatemode /cleanmode /autoend /gban /block",
        f"<b>🎭 {L('help.cat.personality_extras')}:</b> /personality on|off, /joke, /nnr_settings, /nnr_help",
        f"<b>🔤 {L('help.cat.fonts')}:</b> /f1–/f18, /flip, /fontfx, /fonts",
        f"<b>🤗 {L('help.cat.reactions')}:</b> /reactions on|off, /hug /pat /kiss /cuddle /slap …",
        f"<b>🤫 {L('help.cat.whispers')}:</b> inline <code>@bot @user text</code>, /whisper /list /privacy /notifications /whisperhelp",
        f"<b>⚔️ {L('help.cat.rankings')}:</b> /rankings /topusers /userstats /mytop /sysstats /fightinfo — owner: /update /restart",
        "",
        i18n.t("help.footer", lang),
        i18n.t("help.language", lang),
    ])
    await update.message.reply_text(text, parse_mode="HTML")


@safe_handler
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.config import settings as cfg
    from bot.core.database import async_session, Chat

    lang = await chat_language(update)
    chat = update.effective_chat
    personality_state = i18n.t("prefs.state.default", lang)
    if chat.type != "private":
        async with async_session() as session:
            row = await session.get(Chat, chat.id)
            if row is not None and row.personality_enabled is not None:
                personality_state = i18n.t("prefs.state.on" if row.personality_enabled else "prefs.state.off", lang)
    else:
        personality_state = i18n.t("prefs.state.on_dm" if cfg.personality_default_dm else "prefs.state.off_dm", lang)

    live = i18n.t("prefs.live.configured" if cfg.live_music_configured else "prefs.live.missing", lang)
    text = "\n".join([
        i18n.t("prefs.title", lang, bot=bot_name()),
        "",
        i18n.t("prefs.personality", lang, state=personality_state),
        i18n.t("prefs.live", lang, state=live),
        i18n.t("prefs.ai", lang),
        i18n.t("prefs.more", lang),
    ])
    await update.message.reply_text(text, parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(page:|menu:|close)"))
