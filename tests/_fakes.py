"""Small Telegram fakes shared by the v2.12 moderation tests (no network, no PTB Application)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

from telegram.constants import ChatMemberStatus
from telegram import Bot as _RealBot
from telegram.error import BadRequest

_REAL_BOT_METHODS = frozenset(n for n in dir(_RealBot) if not n.startswith('_'))


def member(status=ChatMemberStatus.MEMBER, uid=1, first="User", **rights) -> SimpleNamespace:
    return SimpleNamespace(status=status, user=SimpleNamespace(id=uid, first_name=first, username=None, is_bot=False), **rights)


def admin(uid=1, first="Admin", **rights) -> SimpleNamespace:
    base = dict(can_restrict_members=True, can_delete_messages=True, can_promote_members=True,
                can_pin_messages=True, can_change_info=True, can_invite_users=True)
    base.update(rights)
    return member(ChatMemberStatus.ADMINISTRATOR, uid, first, **base)


def owner(uid=1, first="Owner") -> SimpleNamespace:
    return member(ChatMemberStatus.OWNER, uid, first)


class FakeBot:
    """Records every API call. ``members`` maps ``(chat_id, user_id)`` to a member object;
    ``fail`` maps a method name to an exception to raise; ``chat_permissions`` feeds ``get_chat``."""

    def __init__(self, bot_id: int = 999, members: Optional[Dict[Tuple[int, int], Any]] = None,
                 fail: Optional[Dict[str, Exception]] = None, chat_permissions: Any = None,
                 chat_info: Optional[Dict[str, Any]] = None):
        self.id = bot_id
        self.members = dict(members or {})
        self.fail = dict(fail or {})
        self.chat_permissions = chat_permissions
        self.chat_info = dict(chat_info or {})
        self.calls: List[Tuple[str, tuple, dict]] = []

    def called(self, name: str) -> List[Tuple[tuple, dict]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", (chat_id, user_id), {}))
        if "get_chat_member" in self.fail:
            raise self.fail["get_chat_member"]
        m = self.members.get((chat_id, user_id))
        if m is None:
            raise BadRequest("User not found")
        return m

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", (chat_id,), {}))
        if "get_chat" in self.fail:
            raise self.fail["get_chat"]
        info = dict(id=chat_id, type="supergroup", title="Fake", permissions=self.chat_permissions,
                    first_name="Someone", invite_link=None, username=None, pinned_message=None)
        info.update(self.chat_info)
        return SimpleNamespace(**info)

    async def get_chat_administrators(self, chat_id):
        self.calls.append(("get_chat_administrators", (chat_id,), {}))
        return [m for (cid, _uid), m in self.members.items()
                if cid == chat_id and m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)]

    async def send_message(self, *args, **kwargs):
        self.calls.append(("send_message", args, kwargs))
        if "send_message" in self.fail:
            raise self.fail["send_message"]
        return SimpleNamespace(message_id=9000 + len(self.calls), chat_id=args[0] if args else None)

    def __getattr__(self, name: str):
        # Only methods the real Bot class has: a fake that happily answers `set_chat_slow_mode_delay`
        # (which Telegram's Bot API does not have) hides exactly the bugs these tests are for.
        if name.startswith("_") or name not in _REAL_BOT_METHODS:
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.fail:
                raise self.fail[name]
            return True

        return call


def make_message(text="/cmd", *, user_id=5, chat_id=-1001, chat_type="supergroup", reply=None, entities=None,
                 sender_chat=None, first="Sender", message_id=100, edited=False, caption=None):
    chat = SimpleNamespace(id=chat_id, type=chat_type, title="Test Group", username=None)
    user = SimpleNamespace(id=user_id, first_name=first, last_name=None, username=None, is_bot=False)
    msg = SimpleNamespace(
        text=text, caption=caption, message_id=message_id, chat=chat, from_user=user, reply_to_message=reply,
        entities=entities or [], caption_entities=[], sender_chat=sender_chat, forward_origin=None,
        photo=None, video=None, sticker=None, animation=None, contact=None, location=None, venue=None,
        new_chat_members=None, left_chat_member=None, forum_topic_created=None,
        reply_text=AsyncMock(), delete=AsyncMock(), reply_html=AsyncMock(),
    )
    msg.parse_entity = lambda ent: getattr(ent, "_text", "")
    return msg, chat, user


def make_update(text="/cmd", **kw) -> SimpleNamespace:
    msg, chat, user = make_message(text, **kw)
    edited = kw.get("edited", False)
    return SimpleNamespace(effective_message=msg, effective_chat=chat, effective_user=user,
                           message=None if edited else msg, edited_message=msg if edited else None,
                           callback_query=None)


def make_context(bot: FakeBot, args=None, job_queue=None) -> SimpleNamespace:
    app = SimpleNamespace(job_queue=job_queue, bot_data={})
    return SimpleNamespace(bot=bot, args=list(args or []), application=app, job_queue=job_queue, bot_data=app.bot_data,
                           chat_data={}, user_data={})
