"""Structured logging middleware for all updates."""
import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


async def log_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_id = getattr(update, "update_id", None)
    if update.effective_message:
        msg = update.effective_message
        logger.info(
            "message_received",
            update_id=update_id,
            chat_id=msg.chat.id,
            chat_type=msg.chat.type,
            user_id=msg.from_user.id if msg.from_user else None,
            username=msg.from_user.username if msg.from_user else None,
            text=msg.text[:200] if msg.text else None,
            message_id=msg.message_id,
        )
    elif update.callback_query:
        cq = update.callback_query
        logger.info(
            "callback_query",
            update_id=update_id,
            user_id=cq.from_user.id,
            data=cq.data,
        )
