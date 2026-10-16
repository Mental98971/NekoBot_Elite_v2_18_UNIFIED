# Feature matrix — Neko

Status reflects this codebase as shipped, not a generic bot template.
See `docs/FEATURE_INVENTORY.md` for the full round-by-round history, `docs/COMMANDS.md` for the generated command list, and
`docs/CHANGELOG.md` for the latest changes.

| Area | Commands / entry | Flag / deps | Status |
|------|------------------|-------------|--------|
| Start / help / menu | `/start` `/help` `/menu` `/settings` | — | Works — menu buttons map to real command groups |
| AI assistant | `/ai` `/imagine` `/summarize` `/code` `/see` `/transcribe` `/persona` `/aiclear` | provider API key(s) | Works; soft in-character decline if no provider configured |
| AI diagnostics | `/aihealth` | owner only | Works — shows configured providers, no secrets |
| Personality | `/chat`, @mention/reply, `/personality on|off`, `/joke` | `enable_personality` | Works — 114 skins (incl. 5 real-person style skins), 1,609 instruction presets |
| Persona admin | `/plist` `/pshow` `/preload` `/padd` `/pdel` `/pmerge` `/pset` `/pdata` `/pmeta` `/pjson` `/padmin` | owner only (superowner) | Works |
| Person personas | `/cpersonality skin tanvi\|tanvi_iziiee\|tanvi_ichiiren\|yashmeet\|sonal`, `/pconsent`, `/ppersons`, `/pwho` | `PERSON_PERSONAS_ENABLED`; LLM optional | Works — retrieval replies from each person's own messages; LLM conditioning when `PERSONALITY_LLM_ENABLED`; opt-out + disclosure (added in v2.9, see docs/PERSON_PERSONAS.md) |
| Persona fine-tuning | `python -m training.prepare_sft / train_lora / generate / evaluate / promote / revoke` (offline); bot: `PERSONA_MODEL_*` | GPU box + `training/requirements-train.txt`; authorization per person | Pipeline complete and tested on synthetic + real-export data; **no adapter trained or shipped** - see docs/FINE_TUNING.md |
| Reactions | `/reactions on|off`, `/hug` `/pat` `/slap` + 22 more | `enable_reactions` | Works — target-mention captions |
| Anime | `/anime` `/manga` `/character` `/seasonal` | — | Works — AniList primary, MyAnimeList/Jikan fallback, 15-min cache |
| Music (file) | `/music` `/tts` | — | `/music` works; `/tts` is text-only (no audio TTS dependency bundled) |
| Live voice-chat music | `/play` `/skip` `/pause` `/resume` `/stop` `/queue` `/nowplaying` `/volume` `/shuffle` `/repeat` `/effects` `/loop` `/seek` `/seekback` `/playlist` `/lyrics` `/channelplay` `/videomode` `/toptracks` `/resetqueue` | `enable_live_music` + Telegram API id/hash + ffmpeg | Works when configured |
| Music diagnostics | `/musichealth` | owner only | Works |
| Fonts | `/f1`–`/f18` `/flip` `/fontfx` `/random` `/mix` `/reverse` `/fonts` | `enable_fonts` | Works |
| Collector | `/grab` `/collection` `/characters` `/listcharacters` `/fav` `/trade` `/gift` `/smelt` `/cstats` `/cprivacy` `/crandom` `/giveaway` `/owners` `/chelp` | `enable_collector` | Works — weighted rarity spawns, stable `file_id` storage |
| Economy | `/daily` `/balance` `/shop` `/level` `/leaderboard` `/pay` | `enable_economy` | Works — atomic balance updates, race-condition tested |
| Games | `/trivia` `/tictactoe` `/poll` | `enable_games` | Works |
| Moderation | `/ban` `/mute` `/kick` `/warn` `/purge` `/tmute` `/tban` `/unban` `/unmute` `/warns` `/rmwarn` + silent/deleting variants | — | Works (rebuilt on `modtools` in v2.12; see docs/MODERATION.md) |
| Approved users | `/approve` `/unapprove` `/approved` `/approval` `/unapproveall` | FallenRobot | Works (v2.12) |
| Word blacklist | `/addblacklist` `/unblacklist` `/blacklist` `/blacklistmode` | FallenRobot | Works (v2.12) |
| Moderation settings | `/modsettings` `/setflood` `/setfloodmode` `/slowmode` `/antispam` `/setlog` `/report` `/reports` | — | Works (v2.12; slow mode is bot-enforced) |
| Connect (manage a group from DM) | `/connect` `/disconnect` `/connection` `/allowconnect` | FallenRobot / Gojo_Satoru | Works for the commands listed in docs/MODERATION.md (v2.12) |
| Admin tools | `/promote` `/demote` `/title` `/admins` `/pin` `/unpin` `/invitelink` `/setgtitle` `/setdesc` | FallenRobot / Gojo_Satoru / tgbot | Works (v2.12) |
| Security | `/antispam` `/antiflood` `/captcha` `/lock` `/unlock` `/locks` `/nightmode` | — | Works — admin-status cached (45s TTL); `/locks` implemented in v2.8 |
| Chat management | `/notes` `/filters` `/welcome` `/rules` `/setwelcome` `/setgoodbye` `/setrules` | — | Works — welcome/goodbye messages post on join/leave (fixed in v2.8; they were stored but never sent) |
| News | `/news [topic]` | `NEWS_API_KEY` | Works when configured; explains itself otherwise (added in v2.8) |
| Name history | `/names` | — | Works — throttled ambient tracking |
| Federations | `/newfed` `/joinfed` `/leavefed` `/fedban` `/unfedban` `/fedinfo` `/fpromote` `/fedadmins` | `enable_federation` | Works |
| Admin ops | `/disable` `/enable` `/disabled` `/zombies` `/rmzombies` `/unbanall` | — | Works |
| Owner / sudo ops | `/globalstats` `/activevc` `/blacklistchat` `/authorize` `/maintenance` `/privatemode` `/cleanmode` `/autoend` `/speedtest` `/gban` `/block` | owner/sudo | Works |
| Scheduling / automod | various | flags | Works |
| Force-subscribe | middleware, no command | `FORCE_SUB_ENABLED` (off by default) | Works — off unless an operator opts in with their own channel |
| Dashboard | separate service | — | Read-only heartbeat |
| Extras (v2.13, repaired) | `/truth` `/dare` `/carbon` `/wallpaper` `/webss` `/gps` `/gsearch` | outside services; optional `UNSPLASH_ACCESS_KEY`, `WEBSS_API_URL` | Works by construction (helpers tested; **services not called from the build environment**); cooldowns, timeouts, fallbacks |
| Text fun (v2.13) | `/roll` `/toss` `/decide` `/table` `/shrug` `/shout` `/runs` `/bluetext` `/slaptext` `/pattext` | — | Works; `/slap` `/pat` remain the GIF commands |
| Lyrics | `/lyrics` | `GENIUS_API_KEY`; `LYRICS_FULLTEXT_ENABLED` + `lyricsgenius` for text | Official link by default; lyric text only on explicit opt-in |
| Persona engine | `/pengine` `/premember` `/pmemory` `/pforget` | `PERSON_PERSONAS_ENABLED`, `PERSONA_ENGINE_ENABLED`; any OpenAI-compatible endpoint or the bot's AI chain | Works with a scripted model in tests; **quality with a real model unmeasured** |
| Persona fine-tuning entry points | `scripts/persona_ft/01_preprocess.py` `02_train_lora.py` `03_evaluate.py`, `bot.personality.adapter_loader` | GPU for training only | Offline chain (preprocess -> baselines -> evaluate) run on the shipped corpora; training needs a GPU box; gate enforced |
| Language | `/lang` `/language` `/languages` `/setlang` | `DEFAULT_LANGUAGE`, `SUPPORTED_LANGUAGES` | Implemented (v2.15.1 line) — 10 languages for `/start` `/help` `/menu` `/settings` and permission/cooldown messages; other plugin output stays English; helpers tested, handlers need a `pytest` run |
| Activity rankings | `/rankings` `/topusers` `/userstats` `/mytop` `/sysstats` `/fightinfo`, owner `/update` `/restart` | `ACTIVITY_*`, `UPSTREAM_*` | Implemented (v2.15, `tests/test_chatfight.py`) — per-group message counts, leaderboard images; not executed in the v2.18 build environment |
| Whispers | inline `@bot @user text`, `/whisper` `/list` `/privacy` `/notifications` `/whisperhelp` `/whisperban` `/whisperunban` | inline mode + inline feedback in @BotFather | Implemented (v2.15, `tests/test_whisper.py`) — one-shot, only the recipient reads; not executed in the v2.18 build environment |
| Federation tools | `/fban` `/unfban` `/myfeds` `/chatfed` `/fedchats` `/renamefed` `/delfed` `/subfed` `/unsubfed` `/fedsubs` `/frules` `/setfrules` `/fednotif` `/setfedlog` `/unsetfedlog` `/fbroadcast` `/fedstat` `/fbanlist` `/importfbans` `/banenforce` `/gbanlist` | `enable_federation` | Implemented (v2.16, `tests/test_fedtools.py`) — gbans/fedbans enforced when a banned user joins or speaks |
| Warn filters, sticker blacklist | `/addwarn` `/nowarn` `/stopwarn` `/warnlist` `/warnfilters` `/strongwarn` · `/addblsticker` `/unblsticker` `/rmblsticker` `/blsticker` `/blstickers` `/blstickermode` | — | Implemented (v2.16, `tests/test_warnfilters.py`) |
| Name history (SangMata) | `/history` `/namealert` `/forgetnames` | — | Implemented (v2.16, `tests/test_name_history.py`) — alerts are opt-in per chat; users can erase their own history |
| Toolbox and Kugisaki extras | `/paste` `/wiki` `/github` `/country` `/covid` `/setme` `/me` `/setbio` `/bio` `/encrypt` `/decrypt` `/addrss` `/removerss` `/rss`, `s/old/new`, `/whois` `/fbanstat` `/weebify` | — | Implemented (v2.16, `tests/test_toolbox.py`, `tests/test_kugisaki_extras.py`) |
| Symbolic math | `/simplify` `/factor` `/derive` `/integrate` `/zeroes` `/tangent` `/area`, exact-value `/sin` `/cos` `/tan` `/asin` `/acos` `/atan` `/ln` | `sympy` | Implemented (v2.17, `tests/test_symbolic.py`) |
| Polls+ | `/apoll` `/stoppoll` `/forgotpollid` | — | Implemented (v2.17, `tests/test_pollsplus.py`) |
| Clean blue text | `/cleanblue` `/ignoreblue` `/unignoreblue` `/listblue` `/gignoreblue` `/ungignoreblue` | — | Implemented (v2.17, `tests/test_cleanblue.py`) |
| Welcome mute | `/welcomemute off\|soft\|strong` `/welcomemutehelp` | — | Implemented (v2.17, `tests/test_welcomemute.py`) |
| MyAnimeList | `/myanime` `/mymanga` `/mycharacter` `/myupcoming` | — | Implemented (v2.17, `tests/test_myanimelist.py`) — Jikan v4, cached, `sfw` filter |

**Not implemented** (do not advertise): Cricket, Cosplay, Pokedex, OCR, Upscale, Telegraph, Imposter, Logo designer, Sports live, AntiBanAll, real audio TTS.

**Deliberately deferred** (see `learnings-and-workflow` notes): full `datetime.utcnow()` → timezone-aware migration, wider `bot/utils/validation.py` rollout.

(`connection.py` and `approve.py`, deferred before v2.12, were implemented in v2.12 as `bot/plugins/connection.py` and `bot/plugins/approvals.py`.)
