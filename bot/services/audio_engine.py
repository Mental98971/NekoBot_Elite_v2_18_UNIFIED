"""Audio engine with effects pipeline and streaming."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pytgcalls import PyTgCalls

# Types moved across pytgcalls versions — resolve at runtime in _make_stream().
try:
    from pytgcalls.types import AudioPiped, AudioVideoPiped, HighQualityAudio
except ImportError:  # pragma: no cover
    AudioPiped = AudioVideoPiped = HighQualityAudio = None  # type: ignore
try:
    from pytgcalls.types import MediaStream
except ImportError:  # pragma: no cover
    MediaStream = None  # type: ignore

from bot.models.track import Track, TrackSource
from bot.utils.logger import get_logger
from bot.config import get_settings

logger = get_logger(__name__)


class AudioEffect(str, Enum):
    """Available audio effects."""
    EQUALIZER = "equalizer"
    BASS_BOOST = "bass_boost"
    TREBLE_BOOST = "treble_boost"
    COMPRESSOR = "compressor"
    LIMITER = "limiter"
    STEREO_ENHANCER = "stereo_enhancer"
    MONO = "mono"
    NIGHTCORE = "nightcore"
    VAPORWAVE = "vaporwave"
    EIGHT_D = "8d"
    KARAOKE = "karaoke"
    ECHO = "echo"
    REVERB = "reverb"
    PITCH_SHIFT = "pitch_shift"
    SPEED = "speed"
    VOLUME_NORMALIZE = "volume_normalize"
    NOISE_REDUCTION = "noise_reduction"
    FADE_IN = "fade_in"
    FADE_OUT = "fade_out"


@dataclass
class EffectConfig:
    """Configuration for an audio effect."""
    enabled: bool = False
    intensity: float = 0.5  # 0.0 - 1.0
    params: dict[str, Any] | None = None


@dataclass
class AudioPipeline:
    """Complete audio processing pipeline."""
    volume: float = 1.0
    effects: dict[AudioEffect, EffectConfig] | None = None
    crossfade: float = 0.0
    gapless: bool = False

    def __post_init__(self) -> None:
        if self.effects is None:
            self.effects = {}


class AudioEngine:
    """Studio-quality audio engine with FFmpeg effects."""

    def __init__(self, pytgcalls: PyTgCalls) -> None:
        self.pytgcalls = pytgcalls
        self.settings = get_settings()
        self._active_chats: set[int] = set()
        self._video_chats: set[int] = set()
        self._pipelines: dict[int, AudioPipeline] = {}
        self._lock = asyncio.Lock()

    def _build_ffmpeg_filter(self, pipeline: AudioPipeline) -> str:
        """Build FFmpeg filter chain from pipeline config.

        Returns:
            FFmpeg af filter string.
        """
        filters: list[str] = []
        effects = pipeline.effects or {}

        # Volume
        if pipeline.volume != 1.0:
            filters.append(f"volume={pipeline.volume:.2f}")

        # Equalizer (10-band)
        if effects.get(AudioEffect.EQUALIZER, EffectConfig()).enabled:
            eq = effects[AudioEffect.EQUALIZER]
            bands = eq.params or {}
            eq_filters = []
            for freq, gain in bands.items():
                eq_filters.append(f"equalizer=f={freq}:t=h:width_type=o:width=1:g={gain}")
            if eq_filters:
                filters.extend(eq_filters)

        # Bass boost
        if effects.get(AudioEffect.BASS_BOOST, EffectConfig()).enabled:
            intensity = effects[AudioEffect.BASS_BOOST].intensity
            gain = 5 + (intensity * 15)  # 5-20dB
            filters.append(f"bass=g={gain:.1f}:f=100:w=0.3")

        # Treble boost
        if effects.get(AudioEffect.TREBLE_BOOST, EffectConfig()).enabled:
            intensity = effects[AudioEffect.TREBLE_BOOST].intensity
            gain = 3 + (intensity * 12)
            filters.append(f"treble=g={gain:.1f}:f=3000:w=0.5")

        # Compressor
        if effects.get(AudioEffect.COMPRESSOR, EffectConfig()).enabled:
            filters.append("acompressor=threshold=-20dB:ratio=4:attack=5:release=100")

        # Limiter
        if effects.get(AudioEffect.LIMITER, EffectConfig()).enabled:
            filters.append("alimiter=level=true:limit=-1dB")

        # Mono
        if effects.get(AudioEffect.MONO, EffectConfig()).enabled:
            filters.append("pan=mono|c0=0.5*c0+0.5*c1")

        # Stereo enhancer
        if effects.get(AudioEffect.STEREO_ENHANCER, EffectConfig()).enabled:
            intensity = effects[AudioEffect.STEREO_ENHANCER].intensity
            width = 0.5 + (intensity * 1.5)
            filters.append(f"stereowidth=width={width:.2f}")

        # Nightcore (speed + pitch)
        if effects.get(AudioEffect.NIGHTCORE, EffectConfig()).enabled:
            filters.append("asetrate=48000*1.25,aresample=48000")

        # Vaporwave (slow + pitch down)
        if effects.get(AudioEffect.VAPORWAVE, EffectConfig()).enabled:
            filters.append("asetrate=48000*0.85,aresample=48000")

        # 8D audio (auto-pan)
        if effects.get(AudioEffect.EIGHT_D, EffectConfig()).enabled:
            filters.append("apulsator=mode=sine:hz=0.5:offset_l=0:offset_r=0.5")

        # Karaoke (vocal removal)
        if effects.get(AudioEffect.KARAOKE, EffectConfig()).enabled:
            filters.append("pan=stereo|c0=c0-.5*c1|c1=c1-.5*c0")

        # Echo
        if effects.get(AudioEffect.ECHO, EffectConfig()).enabled:
            filters.append("aecho=0.8:0.9:500|1000:0.3|0.25")

        # Reverb
        if effects.get(AudioEffect.REVERB, EffectConfig()).enabled:
            filters.append("aecho=0.6:0.3:1000|1800:0.3|0.25")

        # Pitch shift
        if effects.get(AudioEffect.PITCH_SHIFT, EffectConfig()).enabled:
            intensity = effects[AudioEffect.PITCH_SHIFT].intensity
            semitones = int((intensity - 0.5) * 12)
            filters.append(f"rubberband=pitch={semitones}")

        # Speed
        if effects.get(AudioEffect.SPEED, EffectConfig()).enabled:
            intensity = effects[AudioEffect.SPEED].intensity
            speed = 0.5 + (intensity * 1.5)
            filters.append(f"atempo={speed:.2f}")

        # Noise reduction
        if effects.get(AudioEffect.NOISE_REDUCTION, EffectConfig()).enabled:
            filters.append("anlmdn=s=7:p=0.002:r=0.002")

        # Fade in
        if effects.get(AudioEffect.FADE_IN, EffectConfig()).enabled:
            filters.append("afade=t=in:d=3")

        # Fade out
        if effects.get(AudioEffect.FADE_OUT, EffectConfig()).enabled:
            filters.append("afade=t=out:d=3")

        # Volume normalization
        if effects.get(AudioEffect.VOLUME_NORMALIZE, EffectConfig()).enabled:
            filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

        return ",".join(filters) if filters else "anull"


    async def _fresh_audio_url(self, track: Track) -> str | None:
        """Re-extract a playable URL at play time.

        yt-dlp direct CDN links expire quickly; re-resolving from
        source_url (webpage) right before join avoids most silent stream failures.
        Falls back to the stored audio_url/stream_url if refresh fails.
        """
        import asyncio
        fallback = track.audio_url or track.stream_url
        page = track.source_url or fallback
        if not page:
            return None
        # Local files / non-http: use as-is
        if not str(page).startswith("http"):
            return fallback or page

        def _extract():
            try:
                import yt_dlp
                opts = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "skip_download": True,
                    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(page, download=False)
                    if info and "entries" in info and info["entries"]:
                        info = info["entries"][0]
                    return (info or {}).get("url") or fallback
            except Exception as e:
                logger.warning("stream_url_refresh_failed", error=str(e))
                return fallback

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _extract)

    async def play(
        self,
        chat_id: int,
        track: Track,
        pipeline: AudioPipeline | None = None,
        video: bool = False,
    ) -> bool:
        """Start playback in voice chat.

        Args:
            chat_id: Telegram chat ID.
            track: Track to play.
            pipeline: Optional audio processing pipeline.
            video: If True, stream video too (AudioVideoPiped instead of
                AudioPiped) — opt-in per chat via Chat.video_enabled, see
                plugins/music_live.py. Needs a source with an actual
                video stream (a YouTube video URL, not just audio).

        Returns:
            True if playback started successfully.
        """
        pipeline = pipeline or AudioPipeline()
        self._pipelines[chat_id] = pipeline

        try:
            # Determine input type
            if track.source in (TrackSource.YOUTUBE, TrackSource.DIRECT_URL, TrackSource.HTTP_STREAM):
                input_source = track.audio_url or track.stream_url
                if not input_source:
                    logger.error("no_audio_url", track_id=track.id)
                    return False
            elif track.source == TrackSource.LOCAL_FILE:
                input_source = track.audio_url
            else:
                # For other sources, stream URL should be pre-resolved
                input_source = track.stream_url or track.audio_url

            if not input_source:
                logger.error("no_input_source", track_id=track.id, source=track.source)
                return False

            # Refresh expiring CDN URLs (YouTube etc.) at play time
            if track.source in (TrackSource.YOUTUBE, TrackSource.DIRECT_URL, TrackSource.HTTP_STREAM):
                fresh = await self._fresh_audio_url(track)
                if fresh:
                    input_source = fresh

            # Build FFmpeg parameters
            ffmpeg_params = self._build_ffmpeg_filter(pipeline)

            if video:
                # -vn ("no video") is deliberately omitted here, unlike the
                # audio-only path below.
                if len(self._video_chats) >= self.settings.video_stream_limit and chat_id not in self._video_chats:
                    logger.info("video_stream_limit_reached", chat_id=chat_id, limit=self.settings.video_stream_limit)
                    return False
                ffmpeg_options = f"-af {ffmpeg_params}"
                try:
                    from pytgcalls.types import MediumQualityVideo
                    stream = AudioVideoPiped(
                        input_source,
                        HighQualityAudio(),
                        MediumQualityVideo(),
                        additional_ffmpeg_parameters=ffmpeg_options,
                    )
                except ImportError:
                    # Video quality preset class names have moved around
                    # across pytgcalls versions more than the audio-only
                    # path this project otherwise relies on — fail loudly
                    # with a clear reason rather than silently playing
                    # audio-only when video was explicitly requested.
                    logger.error(
                        "video_quality_class_not_found",
                        chat_id=chat_id,
                        hint="Check pytgcalls.types for the video quality preset in your installed version",
                    )
                    return False
            else:
                ffmpeg_options = f"-vn -af {ffmpeg_params}"
                stream = None
                if AudioPiped is not None:
                    try:
                        stream = AudioPiped(
                            input_source,
                            HighQualityAudio(),
                            ffmpeg_parameters=ffmpeg_options,
                        )
                    except TypeError:
                        stream = AudioPiped(input_source, HighQualityAudio())
                if stream is None and MediaStream is not None:
                    # Modern py-tgcalls (MediaStream API)
                    stream = MediaStream(input_source)
                if stream is None:
                    logger.error("no_stream_type_available", hint="Install a compatible py-tgcalls")
                    return False

            # If already in this call, swap the stream instead of re-joining.
            if chat_id in self._active_chats and hasattr(self.pytgcalls, "change_stream"):
                try:
                    await self.pytgcalls.change_stream(chat_id, stream)
                    logger.info(
                        "playback_changed_stream",
                        chat_id=chat_id,
                        track=track.display_name,
                        video=video,
                    )
                    return True
                except Exception as e:
                    logger.warning("change_stream_failed_will_rejoin", error=str(e))

            # py-tgcalls API differs by major version:
            #  - modern: play(chat_id, stream) / MediaStream
            #  - older:  join_group_call(chat_id, stream)  [no StreamAudioEnded]
            # StreamAudioEnded is an *event* type, never a valid stream_type —
            # passing it here was a hard failure on every /play.
            joined = False
            last_err: Exception | None = None
            if hasattr(self.pytgcalls, "play"):
                try:
                    await self.pytgcalls.play(chat_id, stream)
                    joined = True
                except Exception as e:
                    last_err = e
                    logger.warning("pytgcalls_play_failed_trying_join", error=str(e))
            if not joined and hasattr(self.pytgcalls, "join_group_call"):
                try:
                    await self.pytgcalls.join_group_call(chat_id, stream)
                    joined = True
                except TypeError:
                    # Some builds accepted stream_type=; try without first (already),
                    # then with a legacy enum if present.
                    try:
                        from pytgcalls.types import StreamType  # type: ignore
                        await self.pytgcalls.join_group_call(
                            chat_id, stream, stream_type=StreamType.pulse_stream
                        )
                        joined = True
                    except Exception as e:
                        last_err = e
                except Exception as e:
                    last_err = e
            if not joined:
                if last_err:
                    raise last_err
                raise RuntimeError("PyTgCalls has neither play() nor join_group_call()")

            self._active_chats.add(chat_id)
            if video:
                self._video_chats.add(chat_id)
            else:
                self._video_chats.discard(chat_id)
            logger.info(
                "playback_started",
                chat_id=chat_id,
                track=track.display_name,
                video=video,
                effects=list(pipeline.effects.keys()) if pipeline.effects else [],
            )
            return True

        except Exception as exc:
            logger.error("playback_error", chat_id=chat_id, error=str(exc), exc_info=True)
            return False

    async def pause(self, chat_id: int) -> bool:
        """Pause playback."""
        try:
            await self.pytgcalls.pause_stream(chat_id)
            logger.info("playback_paused", chat_id=chat_id)
            return True
        except Exception as exc:
            logger.error("pause_error", chat_id=chat_id, error=str(exc))
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume playback."""
        try:
            await self.pytgcalls.resume_stream(chat_id)
            logger.info("playback_resumed", chat_id=chat_id)
            return True
        except Exception as exc:
            logger.error("resume_error", chat_id=chat_id, error=str(exc))
            return False

    async def stop(self, chat_id: int) -> bool:
        """Stop playback and leave voice chat."""
        try:
            if hasattr(self.pytgcalls, "leave_call"):
                await self.pytgcalls.leave_call(chat_id)
            elif hasattr(self.pytgcalls, "leave_group_call"):
                await self.pytgcalls.leave_group_call(chat_id)
            else:
                raise RuntimeError("PyTgCalls has no leave_call/leave_group_call")
            self._active_chats.discard(chat_id)
            self._video_chats.discard(chat_id)
            self._pipelines.pop(chat_id, None)
            logger.info("playback_stopped", chat_id=chat_id)
            return True
        except Exception as exc:
            logger.error("stop_error", chat_id=chat_id, error=str(exc))
            return False

    async def change_volume(self, chat_id: int, volume: int) -> bool:
        """Change playback volume (0-200)."""
        try:
            # PyTgCalls volume is 1-200
            await self.pytgcalls.change_volume_call(chat_id, max(1, min(200, volume)))
            if chat_id in self._pipelines:
                self._pipelines[chat_id].volume = volume / 100.0
            logger.info("volume_changed", chat_id=chat_id, volume=volume)
            return True
        except Exception as exc:
            logger.error("volume_error", chat_id=chat_id, error=str(exc))
            return False

    async def seek(self, chat_id: int, track: Track, to_seconds: int) -> bool:
        """Seek to a position in the current track.

        Confirmed against AnonXMusic's core/call.py: PyTgCalls doesn't
        expose a true seek — this restarts the stream with ffmpeg's
        `-ss <offset> -to <duration>` flags via change_stream() (swaps
        the stream on an already-joined call, unlike join_group_call's
        initial join). Needs the Track itself (for its audio_url and
        duration), not just a chat_id, since the engine doesn't track
        "current track" on its own — bot/services/queue_manager.py
        already does via Queue.current_track.
        """
        if chat_id not in self._active_chats:
            return False
        if to_seconds < 0 or (track.duration and to_seconds >= track.duration):
            return False

        input_source = track.audio_url or track.stream_url
        if not input_source:
            return False

        pipeline = self._pipelines.get(chat_id) or AudioPipeline()
        ffmpeg_params = self._build_ffmpeg_filter(pipeline)
        duration_str = str(track.duration) if track.duration else "999999"
        ffmpeg_options = f"-vn -af {ffmpeg_params} -ss {to_seconds} -to {duration_str}"

        try:
            if AudioPiped is not None:
                try:
                    stream = AudioPiped(input_source, HighQualityAudio(), ffmpeg_parameters=ffmpeg_options)
                except TypeError:
                    stream = AudioPiped(input_source, HighQualityAudio())
            elif MediaStream is not None:
                stream = MediaStream(input_source)
            else:
                return False
            await self.pytgcalls.change_stream(chat_id, stream)
            logger.info("seek_ok", chat_id=chat_id, to_seconds=to_seconds)
            return True
        except Exception as exc:
            logger.error("seek_error", chat_id=chat_id, error=str(exc), exc_info=True)
            return False

    def is_active(self, chat_id: int) -> bool:
        """Check if chat has active playback."""
        return chat_id in self._active_chats

    def get_pipeline(self, chat_id: int) -> AudioPipeline | None:
        """Get current audio pipeline for chat."""
        return self._pipelines.get(chat_id)

    async def set_effect(
        self,
        chat_id: int,
        effect: AudioEffect,
        config: EffectConfig,
    ) -> None:
        """Enable/disable effect for chat."""
        if chat_id not in self._pipelines:
            self._pipelines[chat_id] = AudioPipeline()
        self._pipelines[chat_id].effects[effect] = config
        logger.info("effect_updated", chat_id=chat_id, effect=effect.value, enabled=config.enabled)
