"""Small retry helpers for transient failures (DB, network)."""
from __future__ import annotations

from typing import Callable, TypeVar

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

T = TypeVar("T")

# Common transient classes — callers can pass more specific ones.
TRANSIENT = (ConnectionError, TimeoutError, OSError)


def with_retry(
    attempts: int = 3,
    min_wait: float = 0.2,
    max_wait: float = 2.0,
    exceptions: tuple = TRANSIENT,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
    )
