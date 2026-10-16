"""
Neko Unified Configuration — Pydantic Settings v2.

Merges configuration that used to live in four separate projects:
  - nova_guard_bot   (moderation core, AI plugin, anti-spam)
  - harmony-music-bot (live voice-chat music engine)
  - nanora_bot       (sarcastic personality / banter layer)
  - font_bot_ultimate.py (Unicode font styling)

Auto-loads from environment variables and a local .env file. Every field
has a sensible default except BOT_TOKEN and OWNER_ID, so the bot can run
locally with a minimal .env.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Every optional int/float/SecretStr field (MUSIC_API_ID,
        # GEMINI_API_KEY, ...) ships blank in .env.example. Without this,
        # a present-but-empty env var stays the string "" instead of
        # becoming None, which fails validation for anything typed
        # Optional[int] etc. — the exact fields most users are expected
        # to leave untouched.
        env_parse_none_str="",
    )

    # ── Core Bot ──────────────────────────────────────────────────────
    bot_token: str = Field(..., alias="BOT_TOKEN")
    owner_id: int = Field(..., alias="OWNER_ID")

    # ── Identity / Branding ─────────────────────────────────────────
    # User-facing display identity — shown in /start, /help, error
    # messages, personality responses, collector messages, music
    # messages, etc. Change these (or set via .env) to rebrand every
    # surface at once. Deliberately separate from the underlying
    # package name, database tables, Docker image, and internal log
    # fields, which stay as they are — see bot/identity.py's
    # BOT_HISTORY for why that's intentional, not an oversight.
    bot_display_name: str = Field("Neko", alias="BOT_DISPLAY_NAME")
    bot_display_username: str = Field("Nekooooooobot", alias="BOT_DISPLAY_USERNAME")
    bot_owner_name: str = Field("Star", alias="BOT_OWNER_NAME")

    # ── Force-subscribe ──────────────────────────────────────────────
    # Require joining a channel before using protected bot functionality.
    # Off by default — an operator opts in by setting BOTH
    # FORCE_SUB_CHANNEL (to their own channel) AND FORCE_SUB_ENABLED=true.
    # Blank channel = feature off regardless of the enabled flag, and the
    # flag itself now defaults off too: earlier defaults shipped this
    # pointed at a placeholder channel the operator doesn't own, with the
    # gate already on, which force_sub_middleware raising
    # ApplicationHandlerStop turns into "close to everything the bot does
    # is silently blocked for every non-admin user" the moment someone
    # runs this bot without editing this section first — including
    # ambient personality replies, since this middleware runs before
    # every plugin. Set FORCE_SUB_CHANNEL to an @username or a -100...
    # channel id (the bot must be an admin of that channel to check
    # membership).
    force_sub_channel: Optional[str] = Field(None, alias="FORCE_SUB_CHANNEL")
    force_sub_enabled: bool = Field(False, alias="FORCE_SUB_ENABLED")
    # How long a passed membership check is cached per user, so every
    # message doesn't re-hit Telegram's getChatMember API.
    force_sub_cache_seconds: int = Field(300, alias="FORCE_SUB_CACHE_SECONDS", ge=0)
    # NoDecode: without it, pydantic-settings tries to JSON-parse the raw
    # ".env" string (e.g. the empty string ADMIN_IDS= ships with by
    # default) *before* _parse_admin_ids ever runs, which throws on
    # anything that isn't valid JSON — including "" and "111,222" — and
    # crashes the whole app at startup. NoDecode hands the raw string
    # straight to the validator below instead.
    admin_ids: Annotated[List[int], NoDecode] = Field(default_factory=list, alias="ADMIN_IDS")
    debug: bool = Field(False, alias="DEBUG")
    # How many updates may be handled at the same time. Updates from one chat
    # are always processed in order (bot/core/concurrency.py), but different
    # chats run in parallel, so a slow download or LLM call in one chat no
    # longer stalls every other chat. 0 restores the legacy strictly
    # sequential behaviour of python-telegram-bot.
    concurrent_updates: int = Field(64, alias="CONCURRENT_UPDATES", ge=0, le=1024)

    # ── Server (webhook mode; omit WEBHOOK_URL to use polling) ─────────
    webhook_url: Optional[str] = Field(None, alias="WEBHOOK_URL")
    webhook_port: int = Field(8443, alias="WEBHOOK_PORT")

    # ── Database ─────────────────────────────────────────────────────
    # Defaults to a local SQLite file so the bot runs with zero external
    # services. Point this at postgresql+asyncpg://... for production.
    database_url: str = Field("sqlite+aiosqlite:///./data/novabot.db", alias="DATABASE_URL")
    redis_url: Optional[str] = Field(None, alias="REDIS_URL")  # optional cache layer, not required

    # ── AI Providers (the /ai /code /summarize /imagine plugin) ──
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    google_api_key: Optional[str] = Field(None, alias="GOOGLE_API_KEY")
    # Modern free/fallback providers. These are OpenAI-compatible APIs.
    gemini_api_key: Optional[str] = Field(None, alias="GEMINI_API_KEY")
    groq_api_key: Optional[str] = Field(None, alias="GROQ_API_KEY")
    openrouter_api_key: Optional[str] = Field(None, alias="OPENROUTER_API_KEY")
    # Optional OpenAI-compatible free/extra endpoint (Cerebras, Together, etc.)
    free_ai_api_key: Optional[str] = Field(None, alias="FREE_AI_API_KEY")
    free_ai_base_url: Optional[str] = Field(None, alias="FREE_AI_BASE_URL")
    free_ai_model: Optional[str] = Field(None, alias="FREE_AI_MODEL")
    default_ai_model: str = Field("gemini-2.5-flash", alias="DEFAULT_AI_MODEL")
    gemini_model: str = Field("gemini-2.5-flash", alias="GEMINI_MODEL")
    groq_model: str = Field("llama-3.3-70b-versatile", alias="GROQ_MODEL")
    openrouter_model: str = Field("openrouter/free", alias="OPENROUTER_MODEL")
    ai_rate_limit_per_hour: int = Field(20, alias="AI_RATE_LIMIT_PER_HOUR")

    # ── External APIs ────────────────────────────────────────────────
    youtube_api_key: Optional[str] = Field(None, alias="YOUTUBE_API_KEY")
    tmdb_api_key: Optional[str] = Field(None, alias="TMDB_API_KEY")
    weather_api_key: Optional[str] = Field(None, alias="WEATHER_API_KEY")
    news_api_key: Optional[str] = Field(None, alias="NEWS_API_KEY")
    genius_api_key: Optional[str] = Field(None, alias="GENIUS_API_KEY")
    omdb_api_key: Optional[str] = Field(None, alias="OMDB_API_KEY")  # /imdb; falls back to Wikipedia if unset
    couple_art_url: Optional[str] = Field(None, alias="COUPLE_ART_URL")  # optional image for /couples; text-only if unset
    unsplash_access_key: Optional[str] = Field(None, alias="UNSPLASH_ACCESS_KEY")  # /wallpaper second source (Wallhaven needs no key)
    webss_api_url: Optional[str] = Field(None, alias="WEBSS_API_URL")  # /webss screenshot endpoint override (default: public service in plugins/extras.py)
    lyrics_fulltext_enabled: bool = Field(False, alias="LYRICS_FULLTEXT_ENABLED")  # /lyrics may also scrape the full text (needs lyricsgenius); OFF = official Genius link only

    # ── Anti-Spam Heuristics ─────────────────────────────────────────
    antiflood_threshold: int = 5
    antiflood_interval: int = 6
    antiflood_ban_duration: int = 300
    spam_ml_threshold: float = 0.75

    # ── Live Music — voice-chat streaming (formerly harmony-music-bot) ─
    # Streaming audio INTO a voice chat needs an MTProto session on top
    # of the normal Bot API token — the HTTP Bot API alone cannot join
    # group calls. Get MUSIC_API_ID / MUSIC_API_HASH from my.telegram.org
    # (same bot account, just a second login method). Live music is
    # automatically disabled if these aren't set; /music and /yt (file
    # download + send) keep working either way.
    music_api_id: Optional[int] = Field(None, alias="MUSIC_API_ID")
    music_api_hash: Optional[SecretStr] = Field(None, alias="MUSIC_API_HASH")
    music_session_name: str = Field("novabot_music", alias="MUSIC_SESSION_NAME")
    ffmpeg_path: str = Field("/usr/bin/ffmpeg", alias="FFMPEG_PATH")
    music_workers: int = Field(8, alias="MUSIC_WORKERS", ge=1, le=64)
    music_max_connections: int = Field(100, alias="MUSIC_MAX_CONNECTIONS", ge=1)
    max_queue_size: int = Field(2000, alias="MAX_QUEUE_SIZE", ge=1)
    default_group_volume: int = Field(100, alias="DEFAULT_GROUP_VOLUME", ge=1, le=1000)
    max_group_volume: int = Field(200, alias="MAX_GROUP_VOLUME", ge=1, le=1000)

    # ── Multi-assistant scaling (from YukkiMusicBot, adapted) ────────
    # One assistant can only be in a limited number of voice chats at
    # once, so YukkiMusicBot spreads load across up to 5 assistants —
    # but its assistants are full user accounts logged in via phone
    # number (Pyrogram "string sessions"), which is a materially
    # different, ToS-grayer pattern than the bot-token MTProto approach
    # used everywhere else in this project. Extra assistants here are
    # instead extra BOT accounts (their own BOT_TOKEN from BotFather,
    # sharing MUSIC_API_ID/MUSIC_API_HASH) — same scaling benefit,
    # same account type as the primary one. Leave blank to run with a
    # single assistant.
    music_extra_bot_tokens: Annotated[List[str], NoDecode] = Field(
        default_factory=list, alias="MUSIC_EXTRA_BOT_TOKENS"
    )
    assistant_auto_leave_seconds: int = Field(0, alias="ASSISTANT_AUTO_LEAVE_SECONDS", ge=0)  # 0 = disabled

    # ── Multi-platform music sources (from YukkiMusicBot) ────────────
    # Spotify/Apple Music/Resso links resolve to title+artist metadata
    # (via their own public API/page metadata — never their audio, which
    # isn't legally redistributable this way), then search & stream the
    # match from YouTube, same as harmony's original approach. SoundCloud
    # needs no extra config — yt-dlp streams it directly.
    spotify_client_id: Optional[str] = Field(None, alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: Optional[SecretStr] = Field(None, alias="SPOTIFY_CLIENT_SECRET")

    # ── Playlists & downloads (from YukkiMusicBot) ───────────────────
    playlist_fetch_limit: int = Field(25, alias="PLAYLIST_FETCH_LIMIT", ge=1, le=200)
    server_playlist_limit: int = Field(30, alias="SERVER_PLAYLIST_LIMIT", ge=1)
    song_download_duration_limit_min: int = Field(180, alias="SONG_DOWNLOAD_DURATION_LIMIT", ge=1)

    # ── Video calls (from YukkiMusicBot) — opt-in per chat via Chat.
    # video_enabled; this just caps how many can run at once bot-wide. ─
    video_stream_limit: int = Field(3, alias="VIDEO_STREAM_LIMIT", ge=0)

    # ── Cleanmode default (per-chat toggle, see Chat.cleanmode_enabled) ─
    cleanmode_delete_minutes: int = Field(5, alias="CLEANMODE_DELETE_MINUTES", ge=1)

    # ── Access control (from YukkiMusicBot) ──────────────────────────
    # Global default for private-bot-mode; the live value is DB-backed
    # (BotConfig, toggleable at runtime via /privatemode without a
    # restart) and seeded from this on first run.
    private_bot_mode: bool = Field(False, alias="PRIVATE_BOT_MODE")

    # ── Personality layer (formerly nanora_bot) ─────────────────────
    personality_cooldown_seconds: float = Field(1.5, alias="PERSONALITY_COOLDOWN_SECONDS")
    personality_max_context: int = Field(20, alias="PERSONALITY_MAX_CONTEXT")
    personality_sarcasm_probability: float = Field(0.85, alias="PERSONALITY_SARCASM_PROBABILITY")
    # DMs default to personality-on (closest to nanora's original standalone
    # behaviour); existing moderation groups default to OFF so Neko
    # doesn't suddenly start bantering in a community that didn't ask for
    # it — admins opt in with /personality on.
    personality_default_dm: bool = Field(True, alias="PERSONALITY_DEFAULT_DM")
    personality_default_group: bool = Field(False, alias="PERSONALITY_DEFAULT_GROUP")
    personality_joke_min_interval: int = Field(300, alias="PERSONALITY_JOKE_MIN_INTERVAL", ge=0)
    personality_joke_cooldown_enabled: bool = Field(True, alias="PERSONALITY_JOKE_COOLDOWN_ENABLED")
    personality_default_skin: str = Field("neko", alias="PERSONALITY_DEFAULT_SKIN")

    # Real-person personas (Tanvi, Yashmeet — see docs/PERSON_PERSONAS.md). These skins imitate the
    # chat style of real people from supplied chat exports; PERSON_PERSONAS_ENABLED=false hides and
    # disables all of them without touching any other skin.
    person_personas_enabled: bool = Field(True, alias="PERSON_PERSONAS_ENABLED")
    # Show the "this is a bot imitating <name>" notice when an admin selects such a skin.
    person_persona_disclosure: bool = Field(True, alias="PERSON_PERSONA_DISCLOSURE")

    # Per-user message limit — after this many personality replies to one
    # user in a chat within the reset window, Neko goes quiet for them
    # (see bot/personality/memory.py:increment_and_check).
    personality_user_limit_default: int = Field(20, alias="PERSONALITY_USER_LIMIT_DEFAULT", ge=1)
    personality_user_limit_max: int = Field(100, alias="PERSONALITY_USER_LIMIT_MAX", ge=1)
    personality_msg_count_reset_hours: int = Field(24, alias="PERSONALITY_MSG_COUNT_RESET_HOURS", ge=1)

    # Artificial reply delay so Neko doesn't feel instantaneous/robotic.
    personality_response_delay_default: float = Field(3.0, alias="PERSONALITY_RESPONSE_DELAY_DEFAULT", ge=0)
    personality_response_delay_min: float = Field(0.0, alias="PERSONALITY_RESPONSE_DELAY_MIN", ge=0)

    # How old personality messages/callbacks must be before the daily
    # cleanup job deletes them (see memory.py:cleanup_old_data).
    personality_cleanup_days: int = Field(30, alias="PERSONALITY_CLEANUP_DAYS", ge=1)

    # Off by default: LLM-backed persona rewriting (bot/plugins/ai.py's
    # generate_response) costs real API calls/latency on every message in
    # a chat with custom instructions set, so it's an explicit opt-in even
    # when a provider key happens to already be configured for /ai.
    personality_llm_enabled: bool = Field(False, alias="PERSONALITY_LLM_ENABLED")
    # Upper bound on a single persona-rewrite LLM call. PTB processes
    # updates sequentially by default, so an unbounded call here would
    # stall the whole bot, not just this chat.
    personality_llm_timeout_seconds: float = Field(12.0, alias="PERSONALITY_LLM_TIMEOUT_SECONDS", ge=1)

    # Fine-tuned persona adapters (docs/FINE_TUNING.md).  Off by default: with PERSONA_MODEL_ENABLED=false
    # behaviour is exactly v2.9.  The adapters themselves are served by an OpenAI-compatible inference
    # server (e.g. vLLM with --enable-lora); the bot only calls it.  An adapter is used only after it
    # passed the evaluation gates (status "ready" in <PERSONA_ADAPTER_DIR>/registry.json).
    persona_model_enabled: bool = Field(False, alias="PERSONA_MODEL_ENABLED")
    persona_model_base_url: str = Field("", alias="PERSONA_MODEL_BASE_URL")
    persona_model_api_key: Optional[SecretStr] = Field(None, alias="PERSONA_MODEL_API_KEY")
    persona_model_timeout_seconds: float = Field(12.0, alias="PERSONA_MODEL_TIMEOUT_SECONDS", ge=1)
    persona_model_temperature: float = Field(0.7, alias="PERSONA_MODEL_TEMPERATURE", ge=0, le=2)
    persona_model_top_p: float = Field(0.9, alias="PERSONA_MODEL_TOP_P", gt=0, le=1)
    persona_adapter_dir: str = Field("./data/models/persona_adapters", alias="PERSONA_ADAPTER_DIR")
    # Appended to every fine-tuned reply so a convincing imitation is never mistaken for the real person.
    # Set to an empty value to switch it off.
    persona_model_marker: str = Field("🤖", alias="PERSONA_MODEL_MARKER")

    # Persona engine (docs/PERSONA_ENGINE.md): situation analysis + measured behaviour profile + diverse evidence
    # retrieval + LLM generation + scoring.  Off by default (= v2.9 behaviour).  No GPU and no fine-tuning needed.
    # Provider: "auto" uses the explicit endpoint below if set, otherwise the bot's AI plugin (its free-first chain).
    persona_engine_enabled: bool = Field(False, alias="PERSONA_ENGINE_ENABLED")
    persona_engine_provider: str = Field("auto", alias="PERSONA_ENGINE_PROVIDER")      # auto | openai_compat | project | none
    persona_engine_base_url: str = Field("", alias="PERSONA_ENGINE_BASE_URL")          # e.g. http://localhost:11434/v1 (Ollama)
    persona_engine_model: str = Field("", alias="PERSONA_ENGINE_MODEL")
    persona_engine_api_key: Optional[SecretStr] = Field(None, alias="PERSONA_ENGINE_API_KEY")
    persona_engine_candidates: int = Field(3, alias="PERSONA_ENGINE_CANDIDATES", ge=1, le=6)    # >1 enables reranking
    persona_engine_examples: int = Field(4, alias="PERSONA_ENGINE_EXAMPLES", ge=0, le=8)
    persona_engine_temperature: float = Field(0.85, alias="PERSONA_ENGINE_TEMPERATURE", ge=0, le=2)
    persona_engine_timeout_seconds: float = Field(14.0, alias="PERSONA_ENGINE_TIMEOUT_SECONDS", ge=1)
    persona_engine_regenerate: int = Field(1, alias="PERSONA_ENGINE_REGENERATE", ge=0, le=3)
    persona_engine_memory_enabled: bool = Field(False, alias="PERSONA_ENGINE_MEMORY_ENABLED")  # /premember facts (opt-in)
    persona_engine_human_delay: bool = Field(False, alias="PERSONA_ENGINE_HUMAN_DELAY")        # use the person's measured reply latency
    persona_engine_marker: str = Field("🤖", alias="PERSONA_ENGINE_MARKER")
    # Inject curated few-shot examples (from the conversation dataset) into
    # the LLM persona rewrite prompt. Off when LLM rewrite itself is off.
    personality_fewshot_enabled: bool = Field(True, alias="PERSONALITY_FEWSHOT_ENABLED")
    personality_fewshot_max: int = Field(3, alias="PERSONALITY_FEWSHOT_MAX", ge=0, le=8)

    # Config schema version — bump when making breaking settings changes.
    # Used by future migration helpers; currently informational.
    config_version: int = Field(4, alias="CONFIG_VERSION")

    # ── Font plugin (formerly font_bot_ultimate.py) ─────────────────
    # Off by default — the original bot gated every command behind
    # forced-channel-membership; that's a font-bot-specific growth tactic,
    # not something a moderation/AI/music bot's users should be surprised
    # by. Turn it on and set FONT_GATE_CHANNEL to restore that behaviour.
    font_gate_enabled: bool = Field(False, alias="FONT_GATE_ENABLED")
    font_gate_channel: Optional[str] = Field(None, alias="FONT_GATE_CHANNEL")
    font_rate_limit_window: int = Field(60, alias="FONT_RATE_LIMIT_WINDOW")
    font_rate_limit_max: int = Field(20, alias="FONT_RATE_LIMIT_MAX")

    # ── Feature toggles ──────────────────────────────────────────────
    enable_ai: bool = True
    enable_music_download: bool = True
    enable_live_music: bool = True
    enable_anime: bool = True
    enable_captcha: bool = True
    enable_federation: bool = True
    enable_personality: bool = True
    enable_fonts: bool = True
    enable_economy: bool = True
    enable_games: bool = True
    enable_scheduling: bool = True
    enable_automod: bool = True
    enable_playlists: bool = True
    enable_reactions: bool = True

    # ── Economy & Leveling ───────────────────────────────────────────
    economy_xp_per_message: int = Field(5, alias="ECONOMY_XP_PER_MESSAGE", ge=0)
    economy_xp_cooldown_seconds: int = Field(60, alias="ECONOMY_XP_COOLDOWN_SECONDS", ge=0)
    economy_starting_balance: int = Field(100, alias="ECONOMY_STARTING_BALANCE", ge=0)
    economy_daily_min: int = Field(50, alias="ECONOMY_DAILY_MIN", ge=0)
    economy_daily_max: int = Field(150, alias="ECONOMY_DAILY_MAX", ge=0)

    # ── Games ─────────────────────────────────────────────────────
    games_min_bet: int = Field(10, alias="GAMES_MIN_BET", ge=1)
    games_max_bet: int = Field(5000, alias="GAMES_MAX_BET", ge=1)

    # ── Character Collector (from anime_collector_bot / anime_catcher_bot
    # / WAIFU-HUSBANDO-CATCHER) ──────────────────────────────────────
    enable_collector: bool = True
    collector_default_spawn_rate: int = Field(50, alias="COLLECTOR_SPAWN_RATE", ge=5)  # messages between spawns
    collector_trade_expiry_minutes: int = Field(5, alias="COLLECTOR_TRADE_EXPIRY_MINUTES", ge=1)
    collector_catch_reward_coins: int = Field(20, alias="COLLECTOR_CATCH_REWARD_COINS", ge=0)

    # ── Auto-Moderation defaults (per-chat overridable) ─────────────
    default_warn_limit: int = Field(3, alias="DEFAULT_WARN_LIMIT", ge=1)
    default_warn_action: str = Field("mute", alias="DEFAULT_WARN_ACTION")  # mute|kick|ban
    purge_max_messages: int = Field(1000, alias="PURGE_MAX_MESSAGES", ge=1, le=10000)  # /purge upper bound per command
    antiraid_default_join_threshold: int = Field(5, alias="ANTIRAID_JOIN_THRESHOLD", ge=2)
    antiraid_default_join_window: int = Field(10, alias="ANTIRAID_JOIN_WINDOW", ge=1)

    # ── Dashboard heartbeat (the bot writes stats; the dashboard reads
    # them — they don't share a process, see dashboard/) ─────────────
    # Optional shared secret for the separate dashboard service. When set, the
    # dashboard asks for HTTP Basic credentials (any username, this value as the
    # password). /health stays open so container health checks keep working.
    dashboard_token: Optional[str] = Field(None, alias="DASHBOARD_TOKEN")
    dashboard_heartbeat_interval: int = Field(30, alias="DASHBOARD_HEARTBEAT_INTERVAL", ge=5)

    # ── Activity rankings (ported from ChatFight; see docs/CHATFIGHT.md) ──
    # Counts group messages per member and serves /rankings, /topusers,
    # /userstats and /mytop. Counts are buffered in memory and written in one
    # batch every ACTIVITY_FLUSH_INTERVAL seconds (and on shutdown).
    activity_enabled: bool = Field(True, alias="ACTIVITY_ENABLED")
    # IANA zone that decides when "today" and "this week" roll over.
    activity_timezone: str = Field("Asia/Kolkata", alias="ACTIVITY_TIMEZONE")
    activity_flush_interval: int = Field(180, alias="ACTIVITY_FLUSH_INTERVAL", ge=5)
    # A member who sends >= ACTIVITY_SPAM_THRESHOLD messages within
    # ACTIVITY_SPAM_WINDOW seconds stops counting for ACTIVITY_SPAM_BLOCK_SECONDS.
    # 0 disables the counting-only flood guard (the antiflood middleware is separate).
    activity_spam_threshold: int = Field(10, alias="ACTIVITY_SPAM_THRESHOLD", ge=0)
    activity_spam_window: int = Field(10, alias="ACTIVITY_SPAM_WINDOW", ge=1)
    activity_spam_block_seconds: int = Field(1200, alias="ACTIVITY_SPAM_BLOCK_SECONDS", ge=1)
    activity_count_bots: bool = Field(False, alias="ACTIVITY_COUNT_BOTS")

    # ── Owner self-update (/update, /restart; from ChatFight) ───────────
    # /update runs `git pull` in the checkout the bot runs from, then restarts.
    # It refuses to run when there is no .git directory (e.g. in a Docker image).
    upstream_repo: Optional[str] = Field(None, alias="UPSTREAM_REPO")
    upstream_branch: str = Field("main", alias="UPSTREAM_BRANCH")
    git_token: Optional[SecretStr] = Field(None, alias="GIT_TOKEN")

    # ── Whispers (ported from PsstRobot; see docs/WHISPERS.md) ───────────
    # Private inline messages only the chosen recipient can open. Needs inline
    # mode AND inline feedback (100%) enabled for the bot in @BotFather.
    whisper_enabled: bool = Field(True, alias="WHISPER_ENABLED")
    whisper_max_expiry_days: int = Field(14, alias="WHISPER_MAX_EXPIRY_DAYS", ge=1, le=365)
    whisper_cleanup_minutes: int = Field(15, alias="WHISPER_CLEANUP_MINUTES", ge=1)
    whisper_history_size: int = Field(3, alias="WHISPER_HISTORY_SIZE", ge=1, le=10)
    # Optional "Support" button on the whisper help page (blank = no button).
    whisper_support_url: Optional[str] = Field(None, alias="WHISPER_SUPPORT_URL")

    # ── Localization ─────────────────────────────────────────────────
    default_language: str = "en"
    supported_languages: Annotated[List[str], NoDecode] = Field(
        default=["en", "hi", "hl", "es", "ru", "id", "ar", "zh", "fr", "pt"], alias="SUPPORTED_LANGUAGES"
    )

    # ── Paths ────────────────────────────────────────────────────────
    data_dir: Path = Field(default=Path("./data"))
    sessions_dir: Path = Field(default=Path("./sessions"))
    downloads_dir: Path = Field(default=Path("./data/downloads"))

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        """Accept 'comma,separated,ids' from .env as well as a JSON list."""
        if v is None:
            return []
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [int(x.strip()) for x in stripped.split(",") if x.strip()]
        return v

    @field_validator("music_extra_bot_tokens", mode="before")
    @classmethod
    def _parse_extra_bot_tokens(cls, v):
        """Accept 'token1,token2' from .env as well as a JSON list."""
        if v is None:
            return []
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [t.strip() for t in stripped.split(",") if t.strip()]
        return v

    @field_validator("supported_languages", mode="before")
    @classmethod
    def _parse_supported_languages(cls, v):
        """Accept 'en,es,ru' from .env as well as a JSON list."""
        if v is None:
            return []
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [t.strip() for t in stripped.split(",") if t.strip()]
        return v

    @field_validator("data_dir", "sessions_dir", "downloads_dir", mode="before")
    @classmethod
    def _ensure_path(cls, v):
        # .resolve() is the fix: without it, a relative default/override
        # (e.g. the built-in Path("./data")) stays relative, and every
        # settings.data_dir / "..." call downstream is silently sensitive
        # to whatever the process's cwd happens to be at startup again —
        # the exact bug class this field exists to centralize and close.
        p = Path(v).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def live_music_configured(self) -> bool:
        """Whether enough credentials are present to start the voice-chat engine."""
        return bool(self.enable_live_music and self.music_api_id and self.music_api_hash)

    def is_admin_id(self, user_id: int) -> bool:
        """True if user_id is the owner or in ADMIN_IDS."""
        return user_id == self.owner_id or user_id in self.admin_ids


settings = Settings()


def get_settings() -> Settings:
    """Compatibility shim for modules ported from harmony-music-bot, which
    called `get_settings()` instead of importing the module-level singleton."""
    return settings
