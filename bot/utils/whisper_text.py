"""Pure parsing helpers for the whisper plugin (ported from PsstRobot's ``utils`` / ``wotoValues``).

No Telegram, database or network access, so everything here is unit-tested directly.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

MAX_QUERY_LENGTH = 253     # longer inline queries are redirected to the PM composer
MAX_TEXT_LENGTH = 199      # longest text that fits Telegram's 200-char callback alert
# Telegram user ids below this are service/staff accounts; treating small trailing numbers as
# recipients made "see you at 5" a whisper to user 5 in the original.
MIN_USER_ID = 10_000
MAX_USER_ID = 2 ** 52

ADVANCED_PREFIX = "-wh::"
ADVANCED_SUFFIX = "::"
HISTORY_RESULT_PREFIX = "wsh::"
PLAIN_RESULT_PREFIX = "wsn:"
ADVANCED_RESULT_PREFIX = "wsa:"
START_PREFIX = "w_"
START_CREATE = "create"
START_HELP_INLINE = "help-inline"
EVERYONE = "everyone"

_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


class WhisperType(IntEnum):
    UNKNOWN = 0
    PLAIN_TEXT = 1
    PHOTO = 2
    VIDEO = 3
    AUDIO = 4
    VOICE = 5
    STICKER = 6
    DOCUMENT = 7
    VIDEO_NOTE = 8
    ANIMATION = 9
    DICE = 10


ALBUM_TYPES = frozenset({WhisperType.PHOTO, WhisperType.VIDEO, WhisperType.AUDIO, WhisperType.DOCUMENT})


@dataclass
class Recipient:
    text: str = ""
    user_id: int = 0
    username: str = ""

    @property
    def is_everyone(self) -> bool:
        return self.user_id == 0 and not self.username


def is_valid_username(value: str) -> bool:
    return bool(_USERNAME_RE.match(value or ""))


def _as_user_id(token: str) -> int:
    if not token.isdigit():
        return 0
    value = int(token)
    return value if MIN_USER_ID <= value <= MAX_USER_ID else 0


def extract_recipient(value: str) -> Optional[Recipient]:
    """Split an inline query into recipient + text.

    Supported: ``@user text`` · ``text @user`` · ``12345678 text`` · ``text 12345678``; anything else
    is a whisper to everyone. Returns None for an empty query."""
    value = (value or "").strip()
    if not value:
        return None
    tokens = value.split()
    first, last = tokens[0], tokens[-1]
    if is_valid_username(first):
        return Recipient(text=value[len(first):].strip(), username=first)
    if len(tokens) > 1 and is_valid_username(last):
        return Recipient(text=value[: -len(last)].strip(), username=last)
    if _as_user_id(first):
        return Recipient(text=value[len(first):].strip(), user_id=int(first))
    if len(tokens) > 1 and _as_user_id(last):
        return Recipient(text=value[: -len(last)].strip(), user_id=int(last))
    return Recipient(text=value)


def is_everyone_word(text: str) -> bool:
    return (text or "").strip().lower() == EVERYONE


def is_advanced_inline(query: str) -> bool:
    return query.startswith(ADVANCED_PREFIX) and query.endswith(ADVANCED_SUFFIX) and len(query) > len(ADVANCED_PREFIX) + len(ADVANCED_SUFFIX)


def advanced_unique_id(query: str) -> str:
    unique_id = query[len(ADVANCED_PREFIX): -len(ADVANCED_SUFFIX)]
    return unique_id if _ID_RE.match(unique_id) else ""


def advanced_query(unique_id: str) -> str:
    return f"{ADVANCED_PREFIX}{unique_id}{ADVANCED_SUFFIX}"


def new_unique_id() -> str:
    """A URL-safe id that doubles as a /start deep-link payload (Telegram allows ``A-Za-z0-9_-`` only)."""
    return secrets.token_urlsafe(9)


def start_payload(unique_id: str) -> str:
    return START_PREFIX + unique_id


def parse_start_payload(payload: str) -> str:
    """``w_<id>`` -> ``<id>``; "" if it is not a whisper payload."""
    if payload.startswith(START_PREFIX) and _ID_RE.match(payload[len(START_PREFIX):]):
        return payload[len(START_PREFIX):]
    return ""


def parse_toggle(value: str) -> Optional[bool]:
    value = (value or "").strip().lower()
    if value in ("on", "true", "enable", "enabled", "yes", "1"):
        return True
    if value in ("off", "false", "disable", "disabled", "no", "0"):
        return False
    return None


def clip_name(name: str, limit: int = 34) -> str:
    return (name or "")[:limit]
