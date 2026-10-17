"""Expanded SFW reaction media commands for NekoBot.

Commands cover affection, comfort, playful gestures, greetings, emotions,
comedy, everyday gestures, social interactions, and anime/fantasy reactions.
Each command attempts its matching public image category and sensible fallbacks.
"""
from __future__ import annotations

from io import BytesIO
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils.anime_media import fetch_anime_media, is_animation_url
from bot.utils.helpers import mention_html, resolve_target_user
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Slugs in these groups become Telegram commands. They intentionally avoid
# punctuation/spaces so they are easy to type (e.g. /foreheadkiss).
_MEDIA_GROUPS: tuple[tuple[str, str], ...] = (
    # Affection, love and closeness
    ("hug", "hug cuddle snuggle embrace kiss foreheadkiss cheekkiss handkiss eskimokiss headpat hairruffle handhold armlink leanon shoulderlean shoulderhug sidehug bearhug squeeze gentletouch backrub comfortingpat nuzzle boop peck piggyback carry bridalcarry lapsit lappillow playfulpoke pinkypromise fistbump highfive secrethandshake cheekkiss blowakiss airkiss"),
    # Comfort, care and support
    ("comfort", "comfort reassure console patontheback patonthehead holdhands holdclose wipetears offertissue sitsidebyside sitinsilence shouldertocryon gentlesqueeze rubback strokehair blanket tuckin offerhand helpup staybeside listenattentively nodencouragingly thumbsup reassuringsmile smileencouragement checkon shield protect reassuringgesture whisperencouragement"),
    # Playful, silly and physical comedy
    ("bonk", "bonk nudge tease tickle playfullyshove playfullyslap gentleheadbutt noseboop fingerguns sillyface funnypose photobomb prank startle surprise sneakup peekaboo wigglefingers wiggleeyebrows raspberry fakepout dramaticgasp mockglare playfulgrowl pretendfaint dramaticfall bonkwithpillow pillowfight thumbwar rockpaperscissors pokecheek squishcheeks boopforehead shoulderbump"),
    # Greetings and farewells
    ("wave", "wave greet salute nod bow handshake hughello huggoodbye sayhello saygoodbye welcome welcomeback acknowledgmentwave tiphat enthusiasticwave littlewave raisehand peacesign twofingersalute elbowbump farewellbow"),
    # Anger and cartoon conflict reactions
    ("slap", "slap smack punch kick push shove hit swat backhand grab block dodge parry glare scowl frown snarl growl gritteeth teethclench clenchfists stompfoot slamdoor slamtable pointaccusingly angrycrossarms eyeroll scoff sneer snap barkorder yell shout argue protest threaten warn challenge angrilyturnaway stormoff walkaway silenttreatment"),
    # Embarrassment and shyness
    ("blush", "blush lookaway hideface coverface buryface hidebehind fidget stammer stutter nervousgiggle awkwardlaugh avoideyecontact glancesideways scratchhead rubneck tugsleeves twirlhair covermouth nervousbite shrinkback curlup pullhood blankethide mumble whisper freeze flinch shysmile sheepishsmile fiddlefingers duckhead bashfullook"),
    # Happiness and laughter
    ("laugh", "smile grin beam giggle chuckle laugh snicker snort cackle guffaw burstoutlaugh wheeze clap cheer celebrate jumpforjoy bounceexcitedly dance happywiggle pumpfists victoryspin squeal laughcry thumbsup excitednod enthusiasticclap handsup victorypose happygasp excitedhug"),
    # Sadness and crying
    ("cry", "frown tearup sob weep sniffle whimper wail tremble quiver wipeawaytears hideinsadness hanghead lookdown slumpshoulders curlintoball hugknees buryfaceinpillow sighsadly sadsmile reachforcomfort leanforcomfort shakehead sadstareblankly goquiet withdraw clingcomfortobject"),
    # Surprise and shock
    ("stare", "gasp blink doubletake freeze jump flinch wideneyes raiseeyebrows dropjaw covermouth stare stumbleback stepback shriek yelp exclaim gawk rubeyes pinchself lookaround tilthead leancloser pointindisbelief laughindisbelief staggerdramatically"),
    # Teasing, sarcasm and mischief
    ("smug", "smirk mischievousgrin sideeye rolleyes snicker scoff taunt mock mimic sticktongueout smuglook knowinglook nudgeknowingly playfulelbow fakeinnocence pretendnotnotice dramaticsigh sarcasticclap slowclap airquotes fingerwag innocentshrug playfulglare"),
    # Thinking and confusion
    ("think", "tilthead furrowbrows squint blinkrepeatedly raiseeyebrow narroweyes pause hesitate ponder thinkdeeply tapchin rubchin humthoughtfully mutter stareintospace puzzledshrug scratchcheek countfingers pace lookbetweenpeople askclarification confusedsmile inspectclosely"),
    # Fear and nervousness
    ("cry", "fear flinch tremble shiver recoil stepback duck cower hide cling grabsleeve coverears covereyes wince nervousswallow nervoussweat paceanxiously lookaroundfrantically peekcorner holdbreath backaway seekreassurance callforhelp panic worried nervous"),
    # Sleepy and tired
    ("sleep", "yawn stretch rubeyes sleepilyblink nodoff doze nap fallasleep sleepcurl snuggleblanket headonpillow sleepylean slump tiredsigh dragfeet sleepystumble sleepymumble stretcharms yawncover sleeptalk wakewithstart shakesleepoff"),
    # Disgust and disapproval
    ("cringe", "grimace crinklenose scrunchface gag wince recoil stepaway turnaway covernose covermouth disgustfrown suspicioussquint eyeroll disgustnoise sticktongueout brushaway thumbsdown unimpressed makeface"),
    # Flustered and affectionate reactions
    ("blush", "shygiggle softsmile bashfullook fidget hugpillow squeezehand leancloser gentlenudge smallwave shouldersbump lookbacksmiling coversmile foreheadonshoulder softlaugh fondlook pathead comfortinghug"),
    # Combat and defensive cartoon actions
    ("bonk", "combatpunch combatkick combatblock combatdodge combatduck combatparry sidestep feint counterattack release disarm retreat advance lunge leapaway brace shieldself raiseguard deflect trip tackle wrestle pin breakfree stumble recoverbalance surrender raisehands takecover"),
    # Respect and formal reactions
    ("wave", "respectfulbow respectfulsalute respectfulnod formalhandshake standattention offerseat stepaside makeway politegesture thank gratitude apologize acceptapology congratulations compliment praise acknowledge givecredit appreciation presentgift offerassistance standtogreet"),
    # Everyday gestures and body language
    ("wave", "shrug nodhead shakehead point clap snapfingers rubhands crossarms foldhands handsonhips handsinpockets leanforward leanback everydaystretch scratchcheek adjustglasses fixhair straightenclothes checkwatch lookaround pace sitdown standup turnaround sitcrosslegged kneel stretcharms"),
    # Cute, wholesome and animal-like interactions
    ("pat", "cheeksquish tinyhug bunnyhug tinywave shypeek peekbehind hidehoodie blanketcurl pawsleeve clingarm excitedwiggle cutetilt puffcheeks tinypout hidebehindhands fingerhearts heartgesture offerplushie stuffedanimal tinythumbsup peacesign purr tailwag headrub playfulpaw"),
    # Social and caring interactions
    ("comfort", "introduce askquestion answer agree disagree nodalong encourage thank forgive compliment congratulate checkin offerhelp accepthelp declinepolitely setboundary givepermission askpermission suggestion sharesecret confide reconcile makepeace compromise listenattentively taketurns"),
    # Dramatic, meme and reaction-face commands
    ("facepalm", "facepalm doublefacepalm dramaticfaint stareinsilence deadpan blankstare thousandyardstare internalscreaming fakecrying fakelaugh dramaticsob screamintopillow collapsecouch flopbed falltoknees throwhands slowturn pointdramatically stareatceiling lookatcamera slowmotionwalk exaggeratedthumbsup tinyvictorydance pretenddisappear vanish behinddoor accidentalwallbonk"),
    # Vocal reactions and sound-like expressions
    ("laugh", "sigh gasp groan grunt hum mutter murmur whisper shout scream shriek yelp chuckle giggle snicker snort cackle wheeze whimper sob squeal hiccup burp cough clearthroat clicktongue tsk hmph ahem pfft mwah hmm ugh oops whoa yay eek oof aww"),
    # Anime, fantasy and roleplay actions
    ("waifu", "castspell summon teleport vanish materialize transform glow levitate float fly hover conjure heal bless curse forcefield activateshield chargeattack powerup unleashenergy dash flashstep blinkbehind clone shapeshift growwings hiddenpower transformation heroicpose dramaticbow magicalgesture familiar fantasycreature tamecreature magicalheadpat petcreature"),
    # Extra emotional and social tags from the requested list
    ("smile", "relief guilt curiosity awe pride jealousy grateful confused annoyed curious bored happy sad angry shocked surprised sleepy disgusted flustered amazed impressed determined excited calm awkward relieved embarrassed lonely hopeful"),)

# Human-readable caption verbs for actions that normally target another member.
ACTION_VERBS: dict[str, str] = {
    "hug": "hugs", "cuddle": "cuddles", "snuggle": "snuggles", "embrace": "embraces",
    "kiss": "kisses", "foreheadkiss": "gives a forehead kiss to", "cheekkiss": "kisses",
    "handkiss": "kisses the hand of", "eskimokiss": "touches noses with",
    "headpat": "headpats", "hairruffle": "ruffles the hair of", "handhold": "holds hands with",
    "armlink": "links arms with", "leanon": "leans on", "shoulderlean": "leans on",
    "shoulderhug": "gives a shoulder hug to", "sidehug": "side-hugs", "bearhug": "bear-hugs",
    "backrub": "rubs the back of", "nuzzle": "nuzzles", "boop": "boops", "peck": "gives a peck to",
    "piggyback": "gives a piggyback ride to", "carry": "carries", "bridalcarry": "bridal-carries",
    "lapsit": "sits on the lap of", "lappillow": "offers a lap pillow to", "fistbump": "fist-bumps",
    "highfive": "high-fives", "secrethandshake": "does a secret handshake with", "comfort": "comforts",
    "reassure": "reassures", "console": "consoles", "patontheback": "pats the back of",
    "patonthehead": "pats the head of", "holdhands": "holds hands with", "holdclose": "holds close",
    "wipetears": "wipes the tears of", "offertissue": "offers a tissue to", "shouldertocryon": "offers a shoulder to",
    "gentlesqueeze": "gives a gentle squeeze to", "rubback": "rubs the back of", "strokehair": "strokes the hair of",
    "tuckin": "tucks in", "offerhand": "offers a hand to", "helpup": "helps up", "staybeside": "stays beside",
    "checkon": "checks on", "protect": "protects", "shield": "shields", "hughello": "hugs hello",
    "huggoodbye": "hugs goodbye", "welcome": "welcomes", "welcomeback": "welcomes back", "handshake": "shakes hands with",
    "elbowbump": "elbow-bumps", "playfullyshove": "playfully shoves", "playfullyslap": "playfully slaps",
    "noseboop": "nose-boops", "pokecheek": "pokes the cheek of", "squishcheeks": "squishes the cheeks of",
    "shoulderbump": "bumps shoulders with", "tickle": "tickles", "poke": "pokes", "nudge": "nudges",
    "tease": "teases", "slap": "slaps", "smack": "smacks", "punch": "punches", "kick": "kicks",
    "push": "pushes", "shove": "shoves", "hit": "hits", "swat": "swats", "backhand": "backhands",
    "grab": "grabs", "bonk": "bonks", "yeet": "yeets", "glomp": "glomps", "spank": "spanks",
    "lick": "licks", "nom": "noms on", "bite": "bites", "bully": "bullies", "feed": "feeds",
    "wave": "waves at", "greet": "greets", "salute": "salutes", "bow": "bows to",
    "sayhello": "says hello to", "saygoodbye": "says goodbye to", "blowakiss": "blows a kiss to",
    "airkiss": "air-kisses", "cheer": "cheers for", "celebrate": "celebrates with",
    "jumpforjoy": "jumps for joy with", "excitedhug": "excitedly hugs", "wink": "winks at",
    "highfive": "high-fives", "dance": "dances with", "laugh": "laughs with", "smile": "smiles at",
    "cry": "cries with", "comfortinghug": "gives a comforting hug to", "fondlook": "gives a fond look to",
    "pathead": "pats the head of", "clingsleeve": "clings to the sleeve of", "cling": "clings to",
    "encourage": "encourages", "thank": "thanks", "apologize": "apologizes to", "forgive": "forgives",
    "compliment": "compliments", "congratulate": "congratulates", "offerhelp": "offers help to",
    "accepthelp": "accepts help from", "sharesecret": "shares a secret with", "confide": "confides in",
    "reconcile": "reconciles with", "makepeace": "makes peace with", "presentgift": "gives a gift to",
    "offerassistance": "offers assistance to", "pet": "pets", "petcreature": "pets",
}

# Never shadow established moderation, utility, or group-management commands.
# Namespaced alternatives keep those reactions available without collisions.
_COMMAND_CONFLICTS = {
    "block", "grab", "kick", "pause", "pin", "roll", "shout", "shrug",
    "warn", "welcome", "whisper",
}
_COMMAND_ALIASES = {
    "reactionblock": "block",
    "reactiongrab": "grab",
    "reactionkick": "kick",
    "reactionpause": "pause",
    "reactionpin": "pin",
    "reactionroll": "roll",
    "reactionshout": "shout",
    "reactionshrug": "shrug",
    "reactionwarn": "warn",
    "reactionwelcome": "welcome",
    "reactionwhisper": "whisper",
}

# Map every command to a provider category that is likely to exist. The action
# itself is always tried first; these categories are fallback media, not claims
# that the provider has a dedicated image for every real-life gesture.
ACTION_FALLBACKS: dict[str, tuple[str, ...]] = {}
for _category, _slugs in _MEDIA_GROUPS:
    for _slug in _slugs.split():
        ACTION_FALLBACKS.setdefault(_slug, (_category,))

# Add common provider-native categories and legacy commands.
for _slug in ("neko", "waifu", "shinobu", "megumin", "blush", "cringe", "smug", "happy", "bored", "sleep", "think", "thumbsup", "pout", "facepalm", "stare", "pat", "kiss", "slap", "cuddle", "tickle", "poke", "feed", "cry", "smile", "wink", "yeet", "bonk", "highfive", "dance", "laugh", "hug", "bite", "wave", "comfort"):
    ACTION_FALLBACKS.setdefault(_slug, ())
for _alias, _original in _COMMAND_ALIASES.items():
    ACTION_FALLBACKS.setdefault(_alias, ACTION_FALLBACKS.get(_original, ("waifu", "neko")))

SOLO_ACTIONS = set(ACTION_FALLBACKS) - set(ACTION_VERBS)
ACTIONS = sorted(set(ACTION_VERBS) | SOLO_ACTIONS)

_REACTION_COMMANDS = sorted((set(ACTIONS) - _COMMAND_CONFLICTS) | set(_COMMAND_ALIASES))


def _fallbacks_for(action: str) -> tuple[str, ...]:
    fallback = ACTION_FALLBACKS.get(action, ())
    # Include category defaults, deduplicated; try action first in fetch helper.
    return tuple(dict.fromkeys((*fallback, "waifu", "neko")))


async def _fetch_reaction_url(action: str) -> str | None:
    """Try the requested category and sensible fallbacks in one API session."""
    try:
        url = await fetch_anime_media(
            action,
            extra_fallbacks=_fallbacks_for(action),
        )
        if url:
            logger.info("reaction_media_found action=%s", action)
            return url
    except Exception:
        logger.warning("reaction_media_lookup_failed action=%s", action, exc_info=True)
    logger.warning("reaction_media_unavailable action=%s", action)
    return None


@safe_handler
async def reactions_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_obj = update.effective_chat
    if not message or not chat_obj:
        return

    arg = context.args[0].lower() if context.args else None
    if arg not in ("on", "off"):
        await message.reply_text("Usage: /reactions on | off")
        return

    async with async_session() as session:
        chat = await session.get(Chat, chat_obj.id)
        if not chat:
            chat = Chat(id=chat_obj.id, title=chat_obj.title, type=chat_obj.type)
            session.add(chat)
        chat.reactions_enabled = arg == "on"
        await session.commit()

    await message.reply_text(f"Reaction GIFs {'enabled ✅' if arg == 'on' else 'disabled ❌'}.")


async def _reactions_enabled(chat_id: int) -> bool:
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        return bool(chat and chat.reactions_enabled)


def _build_caption(action: str, actor_name: str, actor_id: int, target_name: str | None, target_id: int | None) -> str | None:
    verb = ACTION_VERBS.get(action)
    if not verb or not target_name or target_id is None:
        return None
    actor = mention_html(actor_id, actor_name)
    if target_id == actor_id:
        return f"{actor} {verb} themselves. 🥲"
    return f"{actor} {verb} {mention_html(target_id, target_name)}!"


async def _send_media(message, url: str, caption: str | None = None) -> None:
    """Try URL delivery, then download and upload if Telegram rejects the URL."""
    kwargs = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    try:
        if is_animation_url(url):
            await message.reply_animation(animation=url, **kwargs)
        else:
            await message.reply_photo(photo=url, **kwargs)
        return
    except Exception:
        logger.info("reaction_url_delivery_failed; trying upload fallback", exc_info=True)

    timeout = httpx.Timeout(15.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if not content or len(content) > 45 * 1024 * 1024:
            raise ValueError("Reaction media is empty or exceeds 45 MB")
        content_type = (response.headers.get("Content-Type") or "").lower()
        path = urlparse(str(response.url)).path.lower()
        animated = "gif" in content_type or "video/mp4" in content_type or path.endswith((".gif", ".mp4"))
        filename = "reaction.mp4" if "mp4" in content_type or path.endswith(".mp4") else "reaction.gif" if animated else "reaction.jpg"
        stream = BytesIO(content)
        stream.name = filename
        if animated:
            await message.reply_animation(animation=stream, **kwargs)
        else:
            await message.reply_photo(photo=stream, **kwargs)


@safe_handler
async def reaction_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not message or not chat or not actor:
        return
    if chat.type != "private" and not await _reactions_enabled(chat.id):
        return
    raw_text = message.text or message.caption or ""
    if not raw_text.split():
        return
    command = raw_text.split()[0].split("@")[0].lstrip("/").lower()
    if command not in _REACTION_COMMANDS:
        return
    action = _COMMAND_ALIASES.get(command, command)
    url = await _fetch_reaction_url(action)
    if not url:
        await message.reply_text("😿 I couldn't find a reaction image right now. Please try again shortly.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    caption = _build_caption(action, actor.first_name, actor.id, target_name, target_id)
    try:
        await _send_media(message, url, caption)
    except Exception:
        logger.warning("reaction_send_failed action=%s url=%s", action, url[:160], exc_info=True)
        await message.reply_text("❌ I found a reaction, but Telegram couldn't send it. Please try again.")


@safe_handler
async def reaction_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List available reaction commands in Telegram-safe chunks."""
    message = update.effective_message
    if not message:
        return
    commands = sorted(_REACTION_COMMANDS)
    lines = ["✨ **NekoBot Reaction Commands**", "Use `/reactions on` in a group first.", ""]
    lines.extend(f"`/{command}`" for command in commands)
    text = "\n".join(lines)
    # Telegram message limit is 4096 chars; leave a safe margin.
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    for chunk in chunks:
        await message.reply_text(chunk, parse_mode="Markdown")


def register(app) -> None:
    if not settings.enable_reactions:
        logger.info("reaction_plugin_disabled")
        return
    app.add_handler(CommandHandler("reactions", admin_only(group_only(reactions_toggle_cmd))))
    app.add_handler(CommandHandler(_REACTION_COMMANDS, reaction_action_cmd))
    app.add_handler(CommandHandler(["reactionlist", "reactionhelp", "rxlist"], reaction_list_cmd))
    logger.info("reaction_plugin_registered actions=%s", len(_REACTION_COMMANDS))
