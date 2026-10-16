"""
Media & Download Plugin
YouTube/generic audio downloads (yt-dlp), Instagram downloads (yt-dlp,
public posts/reels only — see honest error handling below), real TTS
(edge-tts, no API key required), and QR code generation.
"""
import os
import asyncio
import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from bot.config import settings
from bot.identity import bot_name
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler, user_facing_error
import yt_dlp

# settings.data_dir is an absolute path resolved once at startup (see
# bot/config.py) — unlike a bare relative "data/downloads", this is safe
# regardless of the process's working directory (Docker/systemd/etc.).
DOWNLOAD_DIR = str(settings.data_dir / "downloads")

# Telegram Bot API hard limit for bot-uploaded files (as opposed to the
# larger limits available only via the Client/MTProto API or a local
# Bot API server). Checked before every send so a huge file degrades
# gracefully instead of raising a cryptic API error mid-send.
MAX_TELEGRAM_UPLOAD_BYTES = 50 * 1024 * 1024


def _ensure_download_dir() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)


async def _ytdlp_download(url: str, *, audio_only: bool = True) -> dict:
    """Download url (a direct link or a yt-dlp-supported search/URL) and
    return {"path", "title", "duration", "thumbnail"}. Raises on failure —
    callers turn that into a user-facing message via user_facing_error().
    """
    _ensure_download_dir()
    ydl_opts = {
        "format": "bestaudio/best" if audio_only else "best[filesize<50M]/best",
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s_%(epoch)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if audio_only:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    loop = asyncio.get_event_loop()

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            path = ydl.prepare_filename(info)
            if audio_only:
                path = os.path.splitext(path)[0] + ".mp3"
            return info, path

    info, path = await loop.run_in_executor(None, _run)
    if not os.path.exists(path):
        raise FileNotFoundError("yt-dlp reported success but the output file is missing")
    return {
        "path": path,
        "title": info.get("title", "Unknown"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
    }


@safe_handler
async def music_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/music <song name or URL> — search/download audio and send it as a file.
    For streaming straight into a group's live voice chat instead, use /play."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /music <song name or YouTube URL>")
        return

    msg = await update.message.reply_text("🎵 Searching & downloading...")
    url = query if query.startswith("http") else f"ytsearch1:{query}"
    file_path = None
    try:
        result = await _ytdlp_download(url, audio_only=True)
        file_path = result["path"]
        size = os.path.getsize(file_path)
        if size > MAX_TELEGRAM_UPLOAD_BYTES:
            await msg.edit_text(
                f"❌ That track is {size / 1024 / 1024:.0f}MB — over Telegram's 50MB bot upload "
                "limit. Try /play instead to stream it into a voice chat without downloading it."
            )
            return

        await msg.delete()
        with open(file_path, "rb") as audio:
            await update.message.reply_audio(
                audio,
                title=result["title"],
                duration=result["duration"],
                performer=bot_name(),
                thumbnail=result["thumbnail"],
                caption=f"🎵 <b>{escape_html(result['title'])}</b>",
                parse_mode="HTML",
            )
    except Exception as e:
        await msg.edit_text(user_facing_error(e, fallback="Couldn't download that. Check the link/query and try again."))
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


@safe_handler
async def tts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tts <text> — real text-to-speech via edge-tts (no API key needed)."""
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /tts <text>")
        return
    if len(text) > 800:
        await update.message.reply_text("❌ Keep it under 800 characters for /tts.")
        return

    import edge_tts

    _ensure_download_dir()
    out_path = os.path.join(DOWNLOAD_DIR, f"tts_{update.effective_message.message_id}.mp3")
    msg = await update.message.reply_text("🎤 Generating speech...")
    try:
        communicate = edge_tts.Communicate(text, voice="en-US-EmmaMultilingualNeural")
        await communicate.save(out_path)
        await msg.delete()
        with open(out_path, "rb") as audio:
            await update.message.reply_voice(audio, caption=f"🎤 {escape_html(text[:150])}")
    except Exception as e:
        await msg.edit_text(user_facing_error(e, fallback="❌ Text-to-speech failed. Try again shortly."))
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


@safe_handler
async def insta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/insta <Instagram URL> — download a public post/reel via yt-dlp.
    Private accounts, login-walled posts, and stories are not reachable
    without an authenticated session, and are reported honestly rather
    than silently failing or claiming to work."""
    url = context.args[0] if context.args else None
    if not url or "instagram.com" not in url:
        await update.message.reply_text("Usage: /insta <Instagram post/reel URL>")
        return

    msg = await update.message.reply_text("📸 Fetching...")
    file_path = None
    try:
        result = await _ytdlp_download(url, audio_only=False)
        file_path = result["path"]
        size = os.path.getsize(file_path)
        if size > MAX_TELEGRAM_UPLOAD_BYTES:
            await msg.edit_text(f"❌ That file is {size / 1024 / 1024:.0f}MB — over Telegram's 50MB bot upload limit.")
            return
        await msg.delete()
        with open(file_path, "rb") as media:
            await update.message.reply_video(
                media,
                caption=f"📸 <b>{escape_html(result['title'])}</b>",
                parse_mode="HTML",
            )
    except Exception as e:
        await msg.edit_text(
            user_facing_error(
                e,
                fallback=(
                    "❌ Couldn't fetch that post. It may be private, age-restricted, or "
                    "otherwise not reachable without a logged-in session — that's a real "
                    "limitation for public (non-authenticated) downloading, not a bug to retry."
                ),
            )
        )
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


@safe_handler
async def qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /qr <text or URL>")
        return

    # Use a public QR API
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={aiohttp.helpers.quote(text)}"
    await update.message.reply_photo(qr_url, caption=f"📲 QR for: <code>{escape_html(text[:100])}</code>", parse_mode="HTML")


@safe_handler
async def yt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url:
        await update.message.reply_text("Usage: /yt <YouTube (or other yt-dlp supported) URL>")
        return
    await music_cmd(update, context)  # Reuse music logic


def register(app):
    app.add_handler(CommandHandler("music", music_cmd))
    app.add_handler(CommandHandler("tts", tts_cmd))
    app.add_handler(CommandHandler("insta", insta_cmd))
    app.add_handler(CommandHandler("qr", qr_cmd))
    app.add_handler(CommandHandler("yt", yt_cmd))
