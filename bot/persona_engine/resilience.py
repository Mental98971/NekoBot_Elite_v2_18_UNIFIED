"""Failure containment shared by the adapter router and the persona engine (standard library only)."""
from __future__ import annotations

import time
from typing import Callable


class CircuitBreaker:
    """Opens after ``failures`` consecutive errors and stays open for ``cooldown_seconds`` (no retry storms)."""

    def __init__(self, failures: int = 3, cooldown_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.failures, self.cooldown, self._clock = failures, cooldown_seconds, clock
        self._count = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self._open_until

    def ok(self) -> None:
        self._count = 0

    def fail(self) -> None:
        self._count += 1
        if self._count >= self.failures:
            self._open_until = self._clock() + self.cooldown
            self._count = 0

    @property
    def is_open(self) -> bool:
        return not self.allow()
