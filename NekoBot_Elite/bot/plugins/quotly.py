"""
Quotly — turn messages into quote stickers / images.

Commands:
  /q [options]          reply to a message
  /q <n>                accepted for compatibility; the Bot API cannot read chat
                        history, so only the replied-to message is quoted
  /q <color>            named or #hex background
  /q random             random color
  /q m|media            include media thumbnail when present
  /q r|reply            show parent reply context
  /q i|img|p|png        send as a lossless PNG file instead of a compressed photo

Combinations work: /q white rp  ·  /q i red  ·  /q #cbafff m
"""
from __future__ import annotations

import asyncio
import io
import random
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from telegram import InputFile, Update
from telegram.ext import CommandHandler, ContextTypes

from bot.core.errors import safe_handler
from bot.utils.logger import get_logger

logger = get_logger(__name__)

NAMED_COLORS = {
    "red": "#e74c3c",
    "blue": "#3498db",
    "green": "#27ae60",
    "yellow": "#f1c40f",
    "orange": "#e67e22",
    "purple": "#9b59b6",
    "pink": "#e91e63",
    "black": "#1a1a1a",
    "white": "#f5f5f5",
    "gray": "#7f8c8d",
    "grey": "#7f8c8d",
    "cyan": "#1abc9c",
    "navy": "#2c3e50",
    "brown": "#8d6e63",
}


def _load_font(size: int, bold: bool = False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _parse_args(args: list[str]) -> dict:
    opts = {
        "count": 1,
        "color": None,
        "media": False,
        "reply": False,
        "as_image": False,
    }
    for a in args:
        al = a.lower()
        if al.isdigit():
            opts["count"] = max(1, min(10, int(al)))
        elif al in ("m", "media"):
            opts["media"] = True
        elif al in ("r", "reply", "rp"):
            opts["reply"] = True
        elif al in ("i", "img", "p", "png", "image"):
            opts["as_image"] = True
        elif al == "random":
            opts["color"] = random.choice(list(NAMED_COLORS.values()))
        elif al in NAMED_COLORS:
            opts["color"] = NAMED_COLORS[al]
        elif re.fullmatch(r"#?[0-9a-fA-F]{6}", a):
            opts["color"] = a if a.startswith("#") else f"#{a}"
        elif re.fullmatch(r"#?[0-9a-fA-F]{3}", a):
            h = a.lstrip("#")
            opts["color"] = f"#{h[0]*2}{h[1]*2}{h[2]*2}"
    if not opts["color"]:
        opts["color"] = "#2c3e50"
    return opts


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb) -> float:
    r, g, b = [c / 255 for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


async def _download_avatar(context, user) -> Optional[Image.Image]:
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if not photos.total_count:
            return None
        file_id = photos.photos[0][-1].file_id
        f = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await f.download_to_memory(out=buf)
        return Image.open(io.BytesIO(buf.getvalue())).convert("RGBA")
    except Exception:
        return None


def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _render_quote(
    author: str,
    text: str,
    bg_hex: str,
    avatar: Optional[Image.Image] = None,
    reply_text: Optional[str] = None,
) -> bytes:
    bg = _hex_to_rgb(bg_hex)
    fg = (20, 20, 20) if _luminance(bg) > 0.55 else (245, 245, 245)
    muted = tuple(int((c + 128) / 2) for c in fg)

    width = 512
    pad = 28
    avatar_size = 48
    font_body = _load_font(22)
    font_name = _load_font(18, bold=True)
    font_reply = _load_font(16)

    # Measure
    probe = Image.new("RGB", (width, 100))
    d = ImageDraw.Draw(probe)
    max_text_w = width - pad * 2 - avatar_size - 16
    body_lines = _wrap(d, text, font_body, max_text_w)
    reply_lines = _wrap(d, reply_text, font_reply, max_text_w) if reply_text else []
    line_h = 28
    reply_h = (len(reply_lines) * 22 + 12) if reply_lines else 0
    text_h = len(body_lines) * line_h
    height = pad + max(avatar_size, 24) + 12 + reply_h + text_h + pad

    img = Image.new("RGBA", (width, height), bg + (255,))
    draw = ImageDraw.Draw(img)

    # Avatar
    ax, ay = pad, pad
    if avatar:
        av = _circle_avatar(avatar, avatar_size)
        img.paste(av, (ax, ay), av)
    else:
        draw.ellipse((ax, ay, ax + avatar_size, ay + avatar_size), fill=muted)

    # Author name
    name_x = ax + avatar_size + 14
    draw.text((name_x, ay + 8), author[:40], font=font_name, fill=fg)

    y = ay + avatar_size + 12
    if reply_lines:
        draw.rectangle((name_x - 4, y, width - pad, y + reply_h - 4), fill=tuple(max(0, c - 20) for c in bg) + (255,))
        ry = y + 4
        for line in reply_lines:
            draw.text((name_x, ry), line, font=font_reply, fill=muted)
            ry += 22
        y += reply_h

    for line in body_lines:
        draw.text((name_x, y), line, font=font_body, fill=fg)
        y += line_h

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", quality=95)
    return out.getvalue()


@safe_handler
async def q_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/q — quote a replied message as a stylized card."""
    msg = update.message
    if not msg:
        return
    reply = msg.reply_to_message
    if not reply:
        await msg.reply_text(
            "Reply to a message with /q\n\n"
            "Options: color name / #hex / random · m (media) · r (reply) · i (image) · number (multi)\n"
            "Examples: <code>/q red</code> · <code>/q #cbafff m</code> · <code>/q 3 white</code>",
            parse_mode="HTML",
        )
        return

    opts = _parse_args(list(context.args or []))

    author = reply.from_user.full_name if reply.from_user else "Anonymous"
    user_obj = reply.from_user

    # Bots cannot fetch chat history, so a multi-message count can only ever
    # quote the replied-to message; say so instead of silently ignoring it.
    joined = reply.text or reply.caption or ""
    if not joined and not (opts["media"] and (reply.photo or reply.sticker or reply.animation)):
        joined = "(media)"
    note = (
        "ℹ️ Bots can't read chat history, so only the replied message was quoted."
        if opts["count"] > 1
        else None
    )

    reply_ctx = None
    if opts["reply"] and reply.reply_to_message:
        parent = reply.reply_to_message
        who = parent.from_user.full_name if parent.from_user else "?"
        reply_ctx = f"{who}: {(parent.text or parent.caption or '')[:120]}"

    avatar = await _download_avatar(context, user_obj) if user_obj else None
    # Pillow rendering is CPU-bound; keep it off the event loop so other chats stay responsive.
    png = await asyncio.to_thread(
        _render_quote, author, joined[:800], opts["color"], avatar=avatar, reply_text=reply_ctx
    )

    bio = io.BytesIO(png)
    bio.name = "quote.png"
    if opts["as_image"]:
        await msg.reply_document(document=InputFile(bio, filename="quote.png"), caption=note)
    else:
        await msg.reply_photo(photo=InputFile(bio, filename="quote.png"), caption=note)


def register(app):
    app.add_handler(CommandHandler("q", q_cmd))
    app.add_handler(CommandHandler("quotly", q_cmd))
