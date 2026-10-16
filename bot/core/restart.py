"""Graceful in-place restart, used by the owner's /restart and /update.

ChatFight called ``os.execv`` straight from the command handler, which replaces the process without
running ``post_shutdown`` (live-music calls are left dangling, buffered activity counts are lost).
Here the handler only *requests* a restart and stops the event loop; ``bot/__main__.py`` re-executes
the interpreter after ``run_polling`` / ``run_webhook`` has returned, i.e. after a normal shutdown.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)

_requested = False


def is_requested() -> bool:
    return _requested


def request(application: Any) -> bool:
    """Ask the running application to stop so ``exec_if_requested`` can start a fresh process.

    Returns False (and requests nothing) if the application cannot be stopped from here."""
    global _requested
    stop = getattr(application, "stop_running", None)
    if stop is None:
        return False
    _requested = True
    stop()
    return True


def exec_if_requested() -> None:
    """Replace this process with a fresh ``python -m bot`` if a restart was requested. Never returns then."""
    if not _requested:
        return
    logger.info("restarting_process")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # pragma: no cover - flushing a closed stream must not block the restart
        pass
    os.execv(sys.executable, [sys.executable, "-m", "bot"])


def reset_for_tests() -> None:
    global _requested
    _requested = False
