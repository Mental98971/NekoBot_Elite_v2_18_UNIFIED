"""
Unicode font catalog and translation helpers.

Extracted from font_bot_ultimate.py's FONT_DEFS/FONT_MAPS globals into
their own module, separate from the Telegram command handlers (see
bot/plugins/fonts.py) — the mapping data has nothing to do with Telegram
and is easier to test and reuse on its own.
"""
from __future__ import annotations

from typing import Dict

FONT_DEFS: Dict[str, tuple] = {
    "f1": ("αв¢∂єƒgнιנкℓмησρqяѕтυνωχуz", "ΑВ¢∂ЄƑGНΙנКℓМΗΟΡQЯЅΤΥѴωΧΥZ"),
    "f2": ("αвƈԃҽƒɢԋιʝƙʅɱɳσρϙɾʂƚυʋωxყȥ", "ΑBƇԃΕϜƓΗΙʝƘʟΜΝΟΡϘƦЅƬƱƔΜΧყȤ"),
    "f3": ("ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘqʀsᴛᴜᴠᴡxʏz", "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭"),
    "f4": ("𝒶𝒷𝒸𝒹ℯ𝒻ℊ𝒽𝒾𝒿𝓀𝓁𝓂𝓃ℴ𝓅𝓆𝓇𝓈𝓉𝓊𝓋𝓌𝓍𝓎𝓏", "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵"),
    "f5": ("卂乃匚ᗪ乇千Ꮆ卄丨ﾌҜㄥ爪几ㄖ卩Ɋ尺丂ㄒㄩᐯ山乂ㄚ乙", "卂乃匚ᗪ乇千Ꮆ卄丨ﾌҜㄥ爪几ㄖ卩Ɋ尺丂ㄒㄩᐯ山乂ㄚ乙"),
    "f6": ("𝖆𝖇𝖈𝖉𝖊𝖋𝖌𝖍𝖎𝖏𝖐𝖑𝖒𝖓𝖔𝖕𝖖𝖗𝖘𝖙𝖚𝖛𝖜𝖝𝖞𝖟", "𝕬𝕭𝕮𝕯𝕰𝕱𝕲𝕳𝕴𝕵𝕶𝕷𝕸𝕹𝕺𝕻𝕼𝕽𝕾𝕿𝖀𝖁𝖂𝖃𝖄𝖅"),
    "f7": ("𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ"),
    "f8": ("ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ", "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"),
    "f9": ("𝙖𝙗𝙘𝙙𝙚𝙛𝙜𝙝𝙞𝙟𝙠𝙡𝙢𝙣𝙤𝙥𝙦𝙧𝙨𝙩𝙪𝙫𝙬𝙭𝙮𝙯", "𝘼𝘽𝘾𝘿𝙀𝙁𝙂𝙃𝙄𝙅𝙆𝙇𝙈𝙉𝙊𝙋𝙌𝙍𝙎𝙏𝙐𝙑𝙒𝙓𝙔𝙕"),
    "f10": ("𝓪𝓫𝓬𝓭𝓮𝓯𝓰𝓱𝓲𝓳𝓴𝓵𝓶𝓷𝓸𝓹𝓺𝓻𝓼𝓽𝓾𝓿𝔀𝔁𝔂𝔃", "𝓐𝓑𝓒𝓓𝓔𝓕𝓖𝓗𝓘𝓙𝓚𝓛𝓜𝓝𝓞𝓟𝓠𝓡𝓢𝓣𝓤𝓥𝓦𝓧𝓨𝓩"),
    # ── Added from a broader web/Unicode-reference pass ──────────────
    # Squares (outline) — U+1F130-1F149 "SQUARED LATIN CAPITAL LETTER A-Z",
    # a contiguous block; no separate lowercase glyphs exist so both cases
    # map to the same square.
    "f11": (
        "".join(chr(0x1F130 + i) for i in range(26)),
        "".join(chr(0x1F130 + i) for i in range(26)),
    ),
    # Squares (filled) — U+1F170-1F189 "NEGATIVE SQUARED LATIN CAPITAL
    # LETTER A-Z" (white-on-black/colour square), contiguous, verified.
    "f12": (
        "".join(chr(0x1F170 + i) for i in range(26)),
        "".join(chr(0x1F170 + i) for i in range(26)),
    ),
    # Circled — U+24B6-24CF (upper), U+24D0-24E9 (lower), both contiguous.
    "f13": (
        "".join(chr(0x24D0 + i) for i in range(26)),
        "".join(chr(0x24B6 + i) for i in range(26)),
    ),
    # "Special" — regional-indicator letters (the flag-emoji building
    # blocks), U+1F1E6-1F1FF. Renders as bold boxed letters in most fonts.
    "f14": (
        "".join(chr(0x1F1E6 + i) for i in range(26)),
        "".join(chr(0x1F1E6 + i) for i in range(26)),
    ),
    # Runic — decorative only. Unicode's Runic block (U+16A0+) is a
    # distinct historical script, not a Latin cipher, so there's no
    # "correct" transliteration — like every novelty rune generator, this
    # is an arbitrary but consistent one-glyph-per-letter mapping for
    # visual style, not a linguistic transcription.
    "f15": (
        "ᚨᛒᚲᛞᛖᚠᚷᚺᛁᛃᚲᛚᛗᚾᛟᛈᚹᚱᛊᛏᚢᚡᚹᚴᛃᛉ",
        "ᚨᛒᚲᛞᛖᚠᚷᚺᛁᛃᚲᛚᛗᚾᛟᛈᚹᚱᛊᛏᚢᚡᚹᚴᛃᛉ",
    ),
    # Bold — U+1D400+ "MATHEMATICAL BOLD", fully contiguous (no
    # letterlike-symbol collisions, unlike Italic/Script).
    "f16": (
        "".join(chr(0x1D41A + i) for i in range(26)),
        "".join(chr(0x1D400 + i) for i in range(26)),
    ),
    # Italic — U+1D434+ "MATHEMATICAL ITALIC". One documented exception:
    # italic small h isn't encoded there (Unicode points it at the
    # pre-existing ℎ U+210E PLANCK CONSTANT instead) — hardcoded below.
    "f17": (
        "".join(("ℎ" if c == "h" else chr(0x1D44E + i)) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")),
        "".join(chr(0x1D434 + i) for i in range(26)),
    ),
    # Sans-Serif Bold — U+1D5EE+ / U+1D5D4+, fully contiguous.
    "f18": (
        "".join(chr(0x1D5EE + i) for i in range(26)),
        "".join(chr(0x1D5D4 + i) for i in range(26)),
    ),
}

FONT_NAMES: Dict[str, str] = {
    "f1": "🔮 Bubble",
    "f2": "🖋 Script",
    "f3": "🔤 Small Caps",
    "f4": "✒ Cursive",
    "f5": "🌏 Asian",
    "f6": "🦇 Gothic",
    "f7": "⭐ Double-Struck",
    "f8": "↔ Wide",
    "f9": "💻 Mono Bold",
    "f10": "🎭 Script Bold",
    "f11": "🔲 Squares",
    "f12": "⬛ Squares Filled",
    "f13": "⭕ Circled",
    "f14": "🏳 Special",
    "f15": "ᚱ Runic",
    "f16": "𝐁 Bold",
    "f17": "𝐼 Italic",
    "f18": "𝗦 Sans Bold",
}

NORMAL = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

FONT_MAPS: Dict[str, dict] = {}
REVERSE_MAPS: Dict[str, dict] = {}

for _key, (_low, _upp) in FONT_DEFS.items():
    _combined = _low + _upp
    FONT_MAPS[_key] = str.maketrans(NORMAL, _combined)
    # Best-effort reverse map — may not be perfect if a font reuses glyphs.
    REVERSE_MAPS[_key] = str.maketrans(_combined, NORMAL)


def translate_text(text: str, font_key: str) -> str:
    return text.translate(FONT_MAPS.get(font_key, {}))


def reverse_text(text: str, font_key: str) -> str:
    return text.translate(REVERSE_MAPS.get(font_key, {}))


def best_effort_reverse(text: str) -> str:
    """Try every known font's reverse map and return whichever changes the
    most characters — used by /reverse when the source font isn't specified."""
    best, best_score = text, 0
    for table in REVERSE_MAPS.values():
        attempt = text.translate(table)
        score = sum(1 for a, b in zip(attempt, text, strict=False) if a != b)
        if score > best_score:
            best_score, best = score, attempt
    return best


# ═══════════════════════════════════════════════════════════════════
# Upside-down / flip text
#
# A different mechanism from FONT_MAPS above: flipping needs both a
# per-character substitution AND reversing the whole string, which
# str.maketrans can't do on its own. Letter mapping is the same one
# used essentially everywhere "upside down text" is offered — it's
# built from real (if visually-approximate) rotated Unicode letters,
# mostly IPA/phonetic-extension characters, not a font-specific choice.
# ═══════════════════════════════════════════════════════════════════
_FLIP_LETTERS = {
    "a": "ɐ", "b": "q", "c": "ɔ", "d": "p", "e": "ǝ", "f": "ɟ", "g": "ƃ",
    "h": "ɥ", "i": "ᴉ", "j": "ɾ", "k": "ʞ", "l": "l", "m": "ɯ", "n": "u",
    "o": "o", "p": "d", "q": "b", "r": "ɹ", "s": "s", "t": "ʇ", "u": "n",
    "v": "ʌ", "w": "ʍ", "x": "x", "y": "ʎ", "z": "z",
}
_FLIP_PUNCT = {
    ".": "˙", ",": "'", "'": ",", '"': ",,", "!": "¡", "?": "¿",
    "(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{",
    "<": ">", ">": "<", "_": "‾", ";": "؛",
}
FLIP_MAP = str.maketrans({
    **{k: v for k, v in _FLIP_LETTERS.items()},
    **{k.upper(): v for k, v in _FLIP_LETTERS.items()},  # capitals reuse the flipped-lowercase glyphs
    **_FLIP_PUNCT,
})


def upside_down(text: str) -> str:
    return text.translate(FLIP_MAP)[::-1]


# ═══════════════════════════════════════════════════════════════════
# Combining-diacritic effects (Stinky, Bubbles, Underline, Rays, Birds,
# Slash, Stop, Skyline, Arrows, Strike, Frozen)
#
# A third mechanism, distinct from both of the above: these overlay one
# Unicode combining character onto EVERY character of arbitrary input
# text, rather than mapping to a fixed replacement alphabet — which is
# exactly why they work on any text (including ones already run through
# a FONT_MAP) rather than needing their own alphabet.
# ═══════════════════════════════════════════════════════════════════
EFFECTS: Dict[str, tuple] = {
    # key: (combining mark(s), emoji, display name)
    "stinky": ("\u0307", "💧", "Stinky"),          # combining dot above
    "bubbles": ("\u030A", "🫧", "Bubbles"),         # combining ring above
    "underline": ("\u0332", "_", "Underline"),      # combining low line
    "rays": ("\u20F0", "✨", "Rays"),                # combining asterisk above
    "birds": ("\u0311", "🐦", "Birds"),              # combining inverted breve above
    "slash": ("\u0335", "╱", "Slash"),               # combining short stroke overlay
    "stop": ("\u20E0", "🚫", "Stop"),                # combining enclosing circle-backslash
    "skyline": ("\u035B", "🏙️", "Skyline"),          # combining zigzag above
    "arrows": ("\u20D7", "➡️", "Arrows"),             # combining right arrow above
    "strike": ("\u0336", "✂️", "Strike"),            # combining long stroke overlay
    "frozen": ("\u0330", "❄️", "Frozen"),            # combining tilde below
}


def apply_effect(text: str, key: str) -> str:
    """Overlay a combining-mark effect onto every non-whitespace
    character of arbitrary text. Multiple marks in the tuple all get
    applied (none of the current effects use more than one, but the
    mechanism supports stacking)."""
    marks = EFFECTS.get(key, ("",))[0]
    return "".join(ch if ch.isspace() else ch + marks for ch in text)
