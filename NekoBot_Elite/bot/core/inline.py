"""One inline-mode entry point shared by every plugin that answers ``@bot <query>``.

python-telegram-bot runs only the first matching handler of a group, and Telegram allows one
answer per inline query, so two plugins that each registered an ``InlineQueryHandler`` would
silently starve one another (the collector's character cards and the whisper plugin both want
inline mode). Plugins instead register a *provider*; :func:`dispatch` asks each of them and sends
a single combined answer.

A provider is ``async (update, context, query_text) -> InlineContribution | None``. A provider that
raises is logged and skipped; it can never take the others down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import InlineQueryResultsButton, Update
from telegram.ext import Application, ContextTypes, InlineQueryHandler

from bot.utils.logger import get_logger

logger = get_logger(__name__)

MAX_RESULTS = 50  # Telegram's limit per answer
DEFAULT_CACHE_SECONDS = 30  # what the collector used before the router existed
_INSTALLED_FLAG = "inline_router_installed"
_fallback_flags: Dict[int, Dict[str, bool]] = {}  # applications without bot_data (test doubles)

BEFORE, NORMAL, AFTER = "before", "normal", "after"


@dataclass
class InlineContribution:
    """What one provider adds to the answer."""

    results: List[Any] = field(default_factory=list)
    # Where the results go relative to other providers': BEFORE / NORMAL / AFTER.
    placement: str = NORMAL
    # The "start the bot" pill above the results; the first provider that sets one wins.
    button: Optional[InlineQueryResultsButton] = None
    # True: the query is unmistakably this provider's (e.g. "@user hello" for whispers), so
    # every other provider's results are dropped.
    exclusive: bool = False
    # True: results depend on who is asking, so Telegram must not share them between users.
    personal: bool = False
    # Preferred cache time in seconds (the smallest requested wins); None = no preference.
    cache_time: Optional[int] = None


Provider = Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Optional[InlineContribution]]]

# name -> (priority, provider); lower priority runs (and, within a placement, lists) first.
_providers: Dict[str, Tuple[int, Provider]] = {}


def register_provider(name: str, provider: Provider, priority: int = 100) -> None:
    """Register (or replace) a provider. Idempotent per ``name``."""
    _providers[name] = (priority, provider)


def unregister_provider(name: str) -> None:
    _providers.pop(name, None)


def providers() -> List[str]:
    return [n for n, _ in sorted(_providers.items(), key=lambda kv: (kv[1][0], kv[0]))]


def merge(contributions: List[InlineContribution]) -> Tuple[List[Any], Optional[InlineQueryResultsButton], bool, int]:
    """Combine contributions into ``(results, button, is_personal, cache_time)``. Pure; unit-tested."""
    exclusive = next((c for c in contributions if c.exclusive), None)
    chosen = [exclusive] if exclusive is not None else contributions
    ordered = (
        [c for c in chosen if c.placement == BEFORE]
        + [c for c in chosen if c.placement not in (BEFORE, AFTER)]
        + [c for c in chosen if c.placement == AFTER]
    )
    results: List[Any] = []
    for c in ordered:
        results.extend(c.results)
    button = next((c.button for c in chosen if c.button is not None), None)
    personal = any(c.personal for c in chosen)
    times = [c.cache_time for c in chosen if c.cache_time is not None]
    cache_time = min(times) if times else (0 if personal else DEFAULT_CACHE_SECONDS)
    return results[:MAX_RESULTS], button, personal, cache_time


async def dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    query_text = (inline_query.query or "").strip()

    contributions: List[InlineContribution] = []
    for name, (_prio, provider) in sorted(_providers.items(), key=lambda kv: (kv[1][0], kv[0])):
        try:
            contribution = await provider(update, context, query_text)
        except Exception:
            logger.exception("inline_provider_failed", provider=name)
            continue
        if contribution is not None:
            contributions.append(contribution)

    results, button, personal, cache_time = merge(contributions)
    kwargs: Dict[str, Any] = {"cache_time": cache_time, "is_personal": personal}
    if button is not None:
        kwargs["button"] = button
    try:
        await inline_query.answer(results, **kwargs)
    except Exception:
        logger.exception("inline_query_answer_failed")


def install(app: Application) -> None:
    """Attach the single InlineQueryHandler. Safe to call from every plugin that registers a provider."""
    flags = getattr(app, "bot_data", None)
    if flags is None:
        flags = _fallback_flags.setdefault(id(app), {})
    if flags.get(_INSTALLED_FLAG):
        return
    app.add_handler(InlineQueryHandler(dispatch))
    flags[_INSTALLED_FLAG] = True
