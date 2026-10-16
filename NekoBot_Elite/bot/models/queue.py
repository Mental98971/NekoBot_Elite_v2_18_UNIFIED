"""Queue model definitions."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from bot.models.track import Track


class RepeatMode(str, Enum):
    """Queue repeat modes."""
    NONE = "none"
    ONE = "one"
    ALL = "all"


class QueueState(str, Enum):
    """Queue playback states."""
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"
    ERROR = "error"


class QueueEntry(BaseModel):
    """Single entry in the playback queue."""
    entry_id: str = Field(..., description="Unique entry ID")
    track: Track
    position: int = Field(default=0, ge=0)
    added_at: datetime = Field(default_factory=datetime.utcnow)
    priority: int = Field(default=0, ge=0, description="Higher = earlier playback")
    votes: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueueHistoryEntry(BaseModel):
    """Historical queue entry."""
    entry_id: str
    track_id: str
    played_at: datetime = Field(default_factory=datetime.utcnow)
    skipped: bool = Field(default=False)
    skip_votes: int = Field(default=0)
    duration_played: int = Field(default=0, ge=0)


class Queue(BaseModel):
    """Playback queue for a chat."""
    chat_id: int = Field(..., description="Telegram chat ID")
    entries: list[QueueEntry] = Field(default_factory=list)
    history: list[QueueHistoryEntry] = Field(default_factory=list)
    current_index: int = Field(default=-1, ge=-1)
    state: QueueState = Field(default=QueueState.IDLE)
    repeat_mode: RepeatMode = Field(default=RepeatMode.NONE)
    # /loop (from YukkiMusicBot) — repeat the *current* track exactly N
    # more times then resume normal advancement, distinct from
    # repeat_mode.ONE which loops indefinitely until manually changed.
    loop_remaining: int = Field(default=0, ge=0, le=10)
    shuffle_enabled: bool = Field(default=False)
    volume: int = Field(default=100, ge=0, le=200)
    crossfade_seconds: int = Field(default=0, ge=0, le=10)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    stats: dict[str, Any] = Field(default_factory=dict)

    @property
    def current_track(self) -> Track | None:
        """Return currently playing track."""
        if 0 <= self.current_index < len(self.entries):
            return self.entries[self.current_index].track
        return None

    @property
    def remaining_duration(self) -> int:
        """Total duration of remaining tracks in seconds."""
        if self.current_index < 0:
            return sum(e.track.duration for e in self.entries)
        return sum(
            e.track.duration
            for e in self.entries[self.current_index + 1:]
        )

    @property
    def eta_next(self) -> int:
        """Estimated seconds until next track."""
        current = self.current_track
        if not current or current.duration == 0:
            return 0
        return current.duration  # Simplified; actual requires position tracking

    @property
    def is_empty(self) -> bool:
        """Check if queue has no entries."""
        return len(self.entries) == 0
