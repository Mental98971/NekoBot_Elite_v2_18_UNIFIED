"""Track model definitions."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class TrackSource(str, Enum):
    """Supported audio sources."""
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    APPLE_MUSIC = "apple_music"
    SOUNDCLOUD = "soundcloud"
    DEEZER = "deezer"
    JIOSAAVN = "jiosaavn"
    GAANA = "gaana"
    AUDIOMACK = "audiomack"
    BANDCAMP = "bandcamp"
    RADIO = "radio"
    DIRECT_URL = "direct_url"
    TELEGRAM_AUDIO = "telegram_audio"
    TELEGRAM_VIDEO = "telegram_video"
    LOCAL_FILE = "local_file"
    HTTP_STREAM = "http_stream"
    HLS = "hls"
    DASH = "dash"


class AudioFormat(str, Enum):
    """Audio format types."""
    MP3 = "mp3"
    AAC = "aac"
    FLAC = "flac"
    OGG = "ogg"
    OPUS = "opus"
    WAV = "wav"
    M4A = "m4a"
    WEBM = "webm"


class Track(BaseModel):
    """Represents a single music track."""

    id: str = Field(..., description="Unique track identifier (UUID)")
    source: TrackSource
    title: str = Field(..., min_length=1, max_length=500)
    artist: str = Field(default="Unknown Artist", max_length=500)
    album: str = Field(default="", max_length=500)
    duration: int = Field(default=0, ge=0, description="Duration in seconds")
    thumbnail: HttpUrl | None = Field(default=None)
    audio_url: str | None = Field(default=None, description="Direct audio URL or file path")
    source_url: str | None = Field(default=None, description="Original platform URL")
    stream_url: str | None = Field(default=None, description="RTMP/HTTP stream URL")
    format: AudioFormat = Field(default=AudioFormat.OPUS)
    bitrate: int = Field(default=128, ge=8, le=320, description="Bitrate in kbps")
    file_size: int = Field(default=0, ge=0, description="File size in bytes")
    lyrics: str | None = Field(default=None)
    synced_lyrics: list[dict[str, Any]] | None = Field(default=None)
    added_at: datetime = Field(default_factory=datetime.utcnow)
    added_by: int = Field(..., description="Telegram user ID")
    requester_name: str = Field(default="Anonymous")
    is_live: bool = Field(default=False, description="True for an active YouTube livestream, not a VOD")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_formatted(self) -> str:
        """Return human-readable duration (MM:SS or HH:MM:SS)."""
        if self.duration == 0:
            return "Live"
        hours, remainder = divmod(self.duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @property
    def display_name(self) -> str:
        """Return formatted track name."""
        return f"{self.artist} - {self.title}"

    def model_dump_json_safe(self) -> dict[str, Any]:
        """Serialize for JSON storage (excludes large fields)."""
        data = self.model_dump(mode="json")
        data.pop("lyrics", None)
        data.pop("synced_lyrics", None)
        return data
