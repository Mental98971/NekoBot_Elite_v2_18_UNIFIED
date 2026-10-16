"""
Sticker suite — kang, copypack, getsticker, getvidsticker, stickerid, packinfo, mmf.

Real Telegram Bot API sticker-set operations. Personal packs are named
deterministically from the user ID so no extra storage is required.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw, ImageFont
from telegram import InputFile, InputSticker, Update
from telegram.constants import StickerFormat
from telegram.error import BadRequest
from telegram.ext import CommandHandler, ContextTypes

from bot.core.errors import safe_handler, user_facing_error
from bot.utils.logger import get_logger

logger = get_logger(__name__)

STICKER_EMOJI_DEFAULT = "🙂"
MAX_VIDEO_STICKER_BYTES = 4 * 1024 * 1024


def _pack_name(user_id: int, bot_username: str) -> str:
    return f"a{user_id}_by_{bot_username}"


def _pack_title(user) -> str:
    name = (user.first_name or "User")[:40]
    return f"{name}'s pack"


async def _resize_for_sticker(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    scale = 512 / max(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _download_file_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    file_obj = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await file_obj.download_to_memory(out=buf)
    return buf.getvalue()


async def _get_reply_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return (kind, file_id, bytes|None, sticker_or_none)."""
    reply = update.message.reply_to_message if update.message else None
    if not reply:
        return None
    if reply.sticker:
        st = reply.sticker
        if st.is_video:
            data = await _download_file_bytes(context, st.file_id)
            return ("video", st.file_id, data, st)
        if st.is_animated:
            return ("animated", st.file_id, None, st)
        data = await _download_file_bytes(context, st.file_id)
        return ("static", st.file_id, data, st)
    if reply.photo:
        data = await _download_file_bytes(context, reply.photo[-1].file_id)
        return ("photo", reply.photo[-1].file_id, data, None)
    if reply.animation:
        data = await _download_file_bytes(context, reply.animation.file_id)
        return ("video", reply.animation.file_id, data, None)
    if reply.video and (reply.video.file_size or 0) <= MAX_VIDEO_STICKER_BYTES:
        data = await _download_file_bytes(context, reply.video.file_id)
        return ("video", reply.video.file_id, data, None)
    if reply.document:
        mime = (reply.document.mime_type or "").lower()
        if mime.startswith("image/"):
            data = await _download_file_bytes(context, reply.document.file_id)
            return ("photo", reply.document.file_id, data, None)
        if mime in ("video/mp4", "video/webm") and (reply.document.file_size or 0) <= MAX_VIDEO_STICKER_BYTES:
            data = await _download_file_bytes(context, reply.document.file_id)
            return ("video", reply.document.file_id, data, None)
    return None


@safe_handler
async def kang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kang [emoji] — reply to photo/static sticker/image (video when supported)."""
    user = update.effective_user
    if not user:
        return
    media = await _get_reply_media(update, context)
    if not media:
        await update.message.reply_text(
            "Reply to a <b>photo</b>, <b>static sticker</b>, <b>image file</b>, "
            "or <b>video sticker/GIF</b> (≤4MB) with /kang [emoji]",
            parse_mode="HTML",
        )
        return

    kind, file_id, data, st = media
    emoji = (context.args[0] if context.args else None) or (
        (st.emoji if st and getattr(st, "emoji", None) else None) or STICKER_EMOJI_DEFAULT
    )
    bot_username = context.bot.username
    pack_name = _pack_name(user.id, bot_username)
    pack_title = _pack_title(user)
    msg = await update.message.reply_text("⏳ Adding to your pack…")

    try:
        if kind in ("static", "photo"):
            png = await _resize_for_sticker(data)
            sticker = InputSticker(
                sticker=png,
                emoji_list=[emoji],
                format=StickerFormat.STATIC,
            )
            fmt = StickerFormat.STATIC
        elif kind == "video":
            sticker = InputSticker(
                sticker=data,
                emoji_list=[emoji],
                format=StickerFormat.VIDEO,
            )
            fmt = StickerFormat.VIDEO
        else:
            await msg.edit_text(
                "❌ Animated (.tgs) stickers can't be re-packed by bots yet. "
                "Use a static or video sticker."
            )
            return

        pack_exists = False
        try:
            await context.bot.get_sticker_set(pack_name)
            pack_exists = True
        except BadRequest:
            pack_exists = False

        if pack_exists:
            await context.bot.add_sticker_to_set(user_id=user.id, name=pack_name, sticker=sticker)
        else:
            await context.bot.create_new_sticker_set(
                user_id=user.id,
                name=pack_name,
                title=pack_title[:64],
                stickers=[sticker],
                sticker_format=fmt,
            )
        await msg.edit_text(f"✅ Added! Pack: https://t.me/addstickers/{pack_name}")
    except BadRequest as e:
        await msg.edit_text(f"❌ Telegram rejected that: {e.message}")
    except Exception as e:
        logger.warning("kang_failed", error=str(e), exc_info=True)
        await msg.edit_text(user_facing_error(e, fallback="❌ Couldn't add that sticker."))


@safe_handler
async def copypack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/copypack — reply to any sticker from a pack to copy the whole set into yours."""
    user = update.effective_user
    reply = update.message.reply_to_message if update.message else None
    if not reply or not reply.sticker or not reply.sticker.set_name:
        await update.message.reply_text(
            "Reply to a sticker that belongs to a pack with /copypack"
        )
        return

    set_name = reply.sticker.set_name
    bot_username = context.bot.username
    pack_name = _pack_name(user.id, bot_username)
    pack_title = _pack_title(user)
    msg = await update.message.reply_text(f"⏳ Copying pack <code>{set_name}</code>…", parse_mode="HTML")

    try:
        source = await context.bot.get_sticker_set(set_name)
    except BadRequest:
        await msg.edit_text("❌ Couldn't read that sticker set.")
        return

    stickers = list(source.stickers or [])[:50]
    if not stickers:
        await msg.edit_text("That pack is empty.")
        return

    added = 0
    failed = 0

    try:
        try:
            await context.bot.get_sticker_set(pack_name)
            pack_exists = True
        except BadRequest:
            pack_exists = False

        for i, st in enumerate(stickers):
            try:
                data = await _download_file_bytes(context, st.file_id)
                if st.is_video:
                    fmt = StickerFormat.VIDEO
                    payload = data
                elif st.is_animated:
                    failed += 1
                    continue
                else:
                    fmt = StickerFormat.STATIC
                    payload = await _resize_for_sticker(data)
                emoji = st.emoji or STICKER_EMOJI_DEFAULT
                inp = InputSticker(sticker=payload, emoji_list=[emoji], format=fmt)
                if not pack_exists and added == 0:
                    await context.bot.create_new_sticker_set(
                        user_id=user.id,
                        name=pack_name,
                        title=pack_title[:64],
                        stickers=[inp],
                        sticker_format=fmt,
                    )
                    pack_exists = True
                    added += 1
                else:
                    await context.bot.add_sticker_to_set(user_id=user.id, name=pack_name, sticker=inp)
                    added += 1
            except Exception:
                failed += 1
                logger.debug("copypack_one_failed", index=i, exc_info=True)
            if i % 5 == 4:
                try:
                    await msg.edit_text(
                        f"⏳ Copying… {added + failed}/{len(stickers)} (ok {added}, skip {failed})"
                    )
                except Exception:
                    pass

        await msg.edit_text(
            f"✅ Copied {added} sticker(s)"
            + (f" ({failed} skipped)" if failed else "")
            + f"\nPack: https://t.me/addstickers/{pack_name}"
        )
    except Exception as e:
        logger.warning("copypack_failed", error=str(e), exc_info=True)
        await msg.edit_text(user_facing_error(e, fallback="❌ Copy failed partway. Try again."))


@safe_handler
async def getsticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/getsticker — reply to a static sticker to get it as a PNG image."""
    reply = update.message.reply_to_message if update.message else None
    if not reply or not reply.sticker:
        await update.message.reply_text("Reply to a static sticker with /getsticker")
        return
    st = reply.sticker
    if st.is_animated or st.is_video:
        await update.message.reply_text("That isn't a static sticker. Use /getvidsticker for video stickers.")
        return
    data = await _download_file_bytes(context, st.file_id)
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        await update.message.reply_document(
            document=InputFile(out, filename="sticker.png"),
            caption="🖼 Static sticker as PNG",
        )
    except Exception:
        await update.message.reply_document(
            document=InputFile(io.BytesIO(data), filename="sticker.webp"),
            caption="🖼 Sticker file",
        )


@safe_handler
async def getvidsticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/getvidsticker — reply to a video sticker to download as a file."""
    reply = update.message.reply_to_message if update.message else None
    if not reply or not reply.sticker or not reply.sticker.is_video:
        await update.message.reply_text("Reply to a video sticker with /getvidsticker")
        return
    data = await _download_file_bytes(context, reply.sticker.file_id)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(data), filename="sticker.webm"),
        caption="🎬 Video sticker",
    )


@safe_handler
async def stickerid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stickerid — reply to a sticker to get its file_id and set name."""
    reply = update.message.reply_to_message if update.message else None
    if not reply or not reply.sticker:
        await update.message.reply_text("Reply to a sticker with /stickerid")
        return
    st = reply.sticker
    kind = "video" if st.is_video else ("animated" if st.is_animated else "static")
    lines = [
        "<b>Sticker ID</b>",
        f"Type: {kind}",
        f"<code>{st.file_id}</code>",
    ]
    if st.set_name:
        lines.append(f"Pack: <code>{st.set_name}</code>")
        lines.append(f"https://t.me/addstickers/{st.set_name}")
    if st.emoji:
        lines.append(f"Emoji: {st.emoji}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def packinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/packinfo — link to your personal /kang pack."""
    user = update.effective_user
    pack_name = _pack_name(user.id, context.bot.username)
    try:
        pack = await context.bot.get_sticker_set(pack_name)
        await update.message.reply_text(
            f"🏷 Your pack: https://t.me/addstickers/{pack_name}\n"
            f"{len(pack.stickers)} sticker(s) so far.\n"
            f"Title: {pack.title}"
        )
    except BadRequest:
        await update.message.reply_text(
            "You don't have a pack yet — reply to an image with /kang to start one."
        )


def _load_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list:
    words = text.split()
    if not words:
        return [""]
    lines = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


@safe_handler
async def mmf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mmf text — top text on sticker
    /mmf top ; bottom — top and bottom
    /mmf -c text — centered
    Alias: /memefi
    """
    reply = update.message.reply_to_message if update.message else None
    if not reply or not (
        reply.sticker
        or reply.photo
        or (reply.document and (reply.document.mime_type or "").startswith("image/"))
    ):
        await update.message.reply_text(
            "Reply to a sticker/image with:\n"
            "<code>/mmf Top text</code>\n"
            "<code>/mmf Top ; Bottom</code>\n"
            "<code>/mmf -c Center text</code>",
            parse_mode="HTML",
        )
        return
    if not context.args:
        await update.message.reply_text("Provide text after /mmf")
        return

    raw = " ".join(context.args)
    center = None
    top = None
    bottom = None
    if "-c" in raw.lower():
        parts = re.split(r"\s*-c\s*", raw, maxsplit=1, flags=re.I)
        before = parts[0].strip()
        after = parts[1].strip() if len(parts) > 1 else ""
        center = after or None
        raw = before
    if ";" in raw:
        a, b = raw.split(";", 1)
        top = a.strip() or None
        bottom = b.strip() or None
    else:
        if raw.strip():
            top = raw.strip()

    if not any([top, bottom, center]):
        await update.message.reply_text("No text to render.")
        return

    if reply.sticker and not reply.sticker.is_animated and not reply.sticker.is_video:
        data = await _download_file_bytes(context, reply.sticker.file_id)
    elif reply.photo:
        data = await _download_file_bytes(context, reply.photo[-1].file_id)
    elif reply.document:
        data = await _download_file_bytes(context, reply.document.file_id)
    else:
        await update.message.reply_text("Need a static sticker or image.")
        return

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    if max(img.size) < 512:
        scale = 512 / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font_size = max(18, h // 10)
    font = _load_font(font_size)
    stroke = max(1, font_size // 12)
    margin = max(8, w // 30)

    def draw_block(text: str, y_mode: str):
        lines = _wrap_text(draw, text.upper(), font, w - 2 * margin)
        line_h = font_size + 4
        block_h = line_h * len(lines)
        if y_mode == "top":
            y0 = margin
        elif y_mode == "bottom":
            y0 = h - margin - block_h
        else:
            y0 = (h - block_h) // 2
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=font)
            x = (w - tw) / 2
            y = y0 + i * line_h
            draw.text(
                (x, y),
                line,
                font=font,
                fill="white",
                stroke_width=stroke,
                stroke_fill="black",
            )

    if top:
        draw_block(top, "top")
    if bottom:
        draw_block(bottom, "bottom")
    if center:
        draw_block(center, "center")

    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    await update.message.reply_photo(photo=InputFile(out, filename="mmf.png"), caption="🖼 MemeFi")


def register(app):
    app.add_handler(CommandHandler("kang", kang_cmd))
    app.add_handler(CommandHandler("copypack", copypack_cmd))
    app.add_handler(CommandHandler("getsticker", getsticker_cmd))
    app.add_handler(CommandHandler("getvidsticker", getvidsticker_cmd))
    app.add_handler(CommandHandler("stickerid", stickerid_cmd))
    app.add_handler(CommandHandler("packinfo", packinfo_cmd))
    app.add_handler(CommandHandler("mmf", mmf_cmd))
    app.add_handler(CommandHandler("memefi", mmf_cmd))
