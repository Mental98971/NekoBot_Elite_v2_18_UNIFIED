# NekoBot Elite v2.18

One Telegram bot that combines group moderation, AI chat, a personality layer, live voice-chat
music, text-font styling, an economy/games layer, a character collector, scheduling and bot-wide
access control — merged from several previously separate projects into a single codebase with one
database, one config file and no command collisions (lineage in [docs/HISTORY.md](docs/HISTORY.md)).

**597 commands · 45 plugins · 10 languages · 3 registered personas · SQLite by default, Postgres-ready.**

## What's new in v2.18

v2.14 had split into two lines that never met: **A** (v2.15 → v2.16 → v2.17: ChatFight, PsstRobot, Kugisaki/Saitama/Masha, SangMata, math, polls, welcome-mute, MyAnimeList) and
**B** (v2.15.1: the Sonal persona, merged Tanvi/Yashmeet data, authorization for all three, 10-language support). Neither contained the other, so v2.17 was missing everything in B and v2.15.1 everything in A.
v2.18 is the three-way merge of both onto v2.14 (only 8 files were changed on both sides; everything else was taken from whichever line changed it).

- **Everything from both lines is present:** 597 commands (593 + `/lang /language /languages /setlang`), 45 plugins, the Sonal/Tanvi/Yashmeet personas with their authorization records, 10 languages.
- **Whispers and Rankings are translated in `/help` and `/menu`** (4 new catalog keys per language, 113 keys each; the menu now has 53 category buttons). The longer federation line from v2.16 is in the translated `/help` too.
- **Command menu (Telegram allows 100 entries):** `/lang`, `/whisper`, `/rankings` and `/mytop` are all in it; `/slap /pat /table /shrug` left it and still work (they are in `/help`).
- **English cannot drift:** `tests/english_ui_fixture.json` now holds v2.17's English `/help`; `test_i18n` proves the catalog-built English text still equals it.
- **Zero-loss audit of all 15 supplied archives** - every file, function/class, command and persona-corpus row: [docs/ZERO_LOSS_AUDIT.md](docs/ZERO_LOSS_AUDIT.md). Superseded derived files and 98 old snapshot files from the v2.6/v2.7 wrapper archives are kept in `docs/provenance/superseded/`.

## Earlier: v2.16 and v2.17 (line A)

- **v2.17:** `/simplify /factor /derive /integrate /zeroes /tangent /area` and exact-value `/sin /cos /tan /asin /acos /atan /ln` (sympy, no external API); polls `/apoll /stoppoll /forgotpollid`; `/cleanblue` family;
  `/welcomemute off|soft|strong`; `/myanime /mymanga /mycharacter /myupcoming` (MyAnimeList through Jikan v4). New dependency: `sympy`.
- **v2.16:** gbans/fedbans are enforced when a banned user joins or speaks (`/banenforce`); federation tools (`/fban /unfban /myfeds /chatfed /fedchats /renamefed /delfed /subfed /unsubfed /fedsubs /frules /setfrules /fednotif /setfedlog /unsetfedlog /fbroadcast /fedstat /fbanlist /importfbans`);
  warn filters (`/addwarn /nowarn /stopwarn /warnlist /warnfilters /strongwarn`); sticker blacklist (`/addblsticker /unblsticker /rmblsticker /blsticker /blstickers /blstickermode`); name history lookup, `/namealert`, `/forgetnames` (SangMata);
  `/gbanlist /whois`, a utility toolbox (`/paste /wiki /github /country /covid /me /bio /encrypt /decrypt /rss ...`, `s/old/new`). Full tables: [docs/CHANGELOG.md](docs/CHANGELOG.md).

## Earlier: v2.15, ChatFight / Whispers line (line A)

Two more bots folded in as plugins (origins and the licence question: [NOTICE.md](NOTICE.md)):

- **Activity rankings** (from ChatFight): `/rankings /topusers /userstats /mytop /sysstats /fightinfo`, owner `/update /restart`. Counts group messages, leaderboard images, Overall/Today/Week. [docs/CHATFIGHT.md](docs/CHATFIGHT.md)
- **Whispers** (from PsstRobot, rewritten from Go): inline `@bot @user secret`, `/whisper /list /privacy /notifications`. Needs inline mode + inline feedback in @BotFather. [docs/WHISPERS.md](docs/WHISPERS.md)
- `bot/core/inline.py`: one shared inline-query router (collector cards + whispers); `bot/core/restart.py`: graceful restart.
- Menu: `/pat /table /shrug` left the 100-entry Telegram command menu (still work and are in `/help`) to make room for `/whisper /rankings /mytop`.

## Earlier: v2.15.1 / v2.15, Sonal / languages line (line B)

- **v2.15.1:** the `/menu` (header, buttons, panel titles) and `/settings` are translated too, and a fixture test proves the English text did not change. See [docs/LANGUAGES.md](docs/LANGUAGES.md).

- **Sonal added as a persona** (account 5190868757, @FromPlanet_007897814, real name Sonal): corpus (3,900 messages), skin, profiles, hash-only reference manifest and her authorization record
  (reply of 9 Oct 2026). **Tanvi and Yashmeet were updated by merge** - `scripts/build_person_personas.py --merge-existing` keeps every old row and adds the new ones (Tanvi +1,125 messages).
  Details and numbers: [docs/PERSON_PERSONAS.md](docs/PERSON_PERSONAS.md#sonal-and-the-dataset-update-v215).
- **Authorization:** all three are `granted` in `training/authorizations.json` - training needs no further confirmation for anyone in the registry, and a new person is one JSON block. The gate itself stays
  functional (it was not needed to remove: all three could be authorised), so a person *without* a record is still refused.
- **Multi-language support:** `/lang` (per chat; admins in groups) with 10 languages - English, हिन्दी, Hinglish, Español, Русский, Bahasa Indonesia, العربية, 中文, Français, Português. `/start`, `/help`, `/settings`, the `/menu`
  buttons and panel titles, permission and cooldown messages are translated; `/ai` and the Neko personality are told to answer in the chat language (matching the user if they clearly write in another). Catalogs are plain JSON in
  `data/locales/`; adding a language is a file plus two name entries. Scope and limits: [docs/LANGUAGES.md](docs/LANGUAGES.md).
- **Fixed:** the v2.14 `persona_ft` preprocessing numbered synthetic message ids per person, so people sharing a chat overwrote each other's messages in multi-person runs (Yashmeet's split sizes were wrong); ids are now unique per person.

## Earlier: v2.14

- **Both lines in one tree.** v2.12/v2.13 (moderation core, approvals, blacklist, `/connect`, extras) and v2.11.1 (persona engine, evaluation, recorded authorizations) had grown apart from the same v2.10 build;
  v2.14 is their union plus the useful parts of the two overlay packs. Evidence that nothing was lost - 0 commands, 0 data paths, every definition accounted for:
  [docs/ZERO_LOSS_AUDIT.md](docs/ZERO_LOSS_AUDIT.md), [docs/SOURCE_MATRIX.md](docs/SOURCE_MATRIX.md).
- **v2.13 repaired.** Its new `extras` plugin could not import (`InputMediaGeo` does not exist), its command menu exceeded Telegram's limit of 100, its text `/slap` `/pat` shadowed the GIF commands (now `/slaptext` `/pattext`),
  and `/lyrics` posted lyric text by default. All fixed; the new commands `/truth /dare /carbon /wallpaper /webss /gps /gsearch` work with cooldowns, URL validation and fallbacks. Full-text lyrics are opt-in (`LYRICS_FULLTEXT_ENABLED`).
- **Persona engine** (off by default, `PERSONA_ENGINE_ENABLED`): [docs/PERSONA_ENGINE.md](docs/PERSONA_ENGINE.md), now also a two-step API (`prepare` / `finalize`) and the overlay-compatible
  `bot.personality.persona_engine.integration`. It tells people it is a bot when asked; no GPU, no training needed.
- **Authorization gate stays on.** Tanvi's and Yashmeet's records are filled in from the owner's evidence (`training/authorizations.json`), so weight training is not blocked for them - and still is for anyone else.
  The v2.11 `AUTH_BYPASS` variant was deliberately not carried. `scripts/persona_ft/01..03` are the overlay's commands, routed through the gated toolkit.
- **What was run:** see [docs/TEST_REPORT.md](docs/TEST_REPORT.md) - the stdlib-only test modules and static checks ran; the modules that need python-telegram-bot / SQLAlchemy / pydantic did **not** run in the build environment
  and need one `python -m pytest tests/ -q`.

## Earlier: v2.12

- **One moderation core** (`bot/utils/modtools.py`) under every moderation command: targets by reply / numeric id / tapped mention / known `@username`,
  the caller's *specific* Telegram admin right and the bot's own right checked first, admins/owner/bot protected, durations validated, anonymous admins supported.
  Full guide: [docs/MODERATION.md](docs/MODERATION.md).
- **New:** approved users (`/approve`), a whole-word **word blacklist** with actions, **per-chat anti-flood and slow-mode settings**, a **log channel** that
  receives bans/warns/reports, a real `/report` that pings the admins, **`/connect`** (manage a group from a private chat), `/promote /demote /title /admins`,
  silent (`/sban`) and message-deleting (`/dban`) variants, `/clear` for notes, `/editchar`, and the WAIFU-HUSBANDO-CATCHER / Fallen / Gojo / tgbot command names as aliases.
- **Fixed (each with a regression test):** time-zone-dependent `/tmute`/`/tban` durations, `/unban <id>` that never worked, `/unmute` handing out admin-like
  rights, filters that matched inside words, anti-flood deleting other members' messages, `/slowmode` calling a Bot API method that does not exist, `/save` breaking `/get`,
  night mode, and more - the evidence for each is in [docs/CHANGELOG.md](docs/CHANGELOG.md) (raw results: [docs/V210_REPRODUCTIONS.txt](docs/V210_REPRODUCTIONS.txt)).
- **All 17 archives supplied that round were reconciled** ([docs/SOURCE_MATRIX.md](docs/SOURCE_MATRIX.md)): no v2.10 command was lost (369 → 452). The v2.11 persona *overlay packs* were not carried
  then (their prompt hid that the bot is an AI); v2.14 carries the full v2.11.1 engine instead, which discloses it.
- Upgrading from v2.10 needs no manual step; a few behaviours changed on purpose (see *Changed* in the changelog).

## Earlier: v2.11

- **Persona engine** (`bot/persona_engine/`, [docs/PERSONA_ENGINE.md](docs/PERSONA_ENGINE.md)): a free-first replacement for "retrieve old messages and imitate them" - situation analysis, a measured behaviour profile,
  a sampled response plan, diverse evidence retrieval, LLM generation with gates, reranking, anti-repetition and memory. Works with any OpenAI-compatible endpoint or the bot's own AI chain; no GPU, no fine-tuning. Off by default
  (`PERSONA_ENGINE_ENABLED`). **No real model was available while building it: generation quality is unmeasured** - see [docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md) for what was and was not run.
- Audit reports: [SOURCE_MATRIX](docs/SOURCE_MATRIX.md), [DATASET_REPORT](docs/DATASET_REPORT.md), [PERSONA_REPORT](docs/PERSONA_REPORT.md), [EVALUATION_REPORT](docs/EVALUATION_REPORT.md), [TEST_REPORT](docs/TEST_REPORT.md).

## Earlier: v2.10

- **Persona fine-tuning pipeline** for the real-person personas (`training/`, [docs/FINE_TUNING.md](docs/FINE_TUNING.md)): data preparation
  from the raw exports, LoRA/QLoRA training with one adapter per person, held-out evaluation with promotion gates, and a runtime router
  (`PERSONA_MODEL_ENABLED`, off by default). **No adapter ships and none has been trained yet** (no GPU here). Both persons' agreement
  is recorded as `granted` in `training/authorizations.json` (their replies of 7 Oct 2026), so training only needs a GPU box.
- **Zero-loss audit** of every earlier build against v2.9 - [docs/SOURCE_MATRIX.md](docs/SOURCE_MATRIX.md); recovered regression tests,
  `/q` and `/health` made discoverable in `/menu`.

### Earlier: v2.9


- **Real-person personas — Tanvi and Yashmeet** (Sonal joined in v2.15.1's line). New skins (`tanvi`, `tanvi_iziiee`, `tanvi_ichiiren`, `yashmeet`, `sonal`; alias
  `galaxy_anhilator`) that imitate people's chat style from the supplied exports (20,572 messages as of v2.15), with deterministic
  account→person identity resolution (Tanvi: ids 7883544685 and 7187486402; Yashmeet: 5553376334; Sonal: 5190868757), retrieval-based replies, optional
  LLM conditioning, a disclosure notice, a per-person opt-out (`/pconsent`) and a kill switch (`PERSON_PERSONAS_ENABLED`).
  Full design, measured traits and privacy rules: [docs/PERSON_PERSONAS.md](docs/PERSON_PERSONAS.md).
- Fixed a crash in `/nnr_test` and a skin-switch gap (see [CHANGELOG](docs/CHANGELOG.md)).

### Earlier: v2.8

A synthesis of every earlier build (v2 → v2.7 FINAL and the neko-bot-* lines) with a full audit. Highlights —
the complete list is in [docs/CHANGELOG.md](docs/CHANGELOG.md):

- **No more head-of-line blocking.** Updates are processed concurrently across chats (in order within each
  chat), so one slow download or LLM call no longer freezes every other chat. `CONCURRENT_UPDATES=0` restores
  the old behaviour.
- **Welcome messages actually work** — `/setwelcome` text was stored but never sent. New: `/welcome`,
  goodbye messages, `/locks`, `/news`. (They were all advertised in `/help` and the command menu without a handler.)
- **Secrets stay out of logs and images.** Bot tokens/API keys are scrubbed from every log line (httpx logged the
  token on each poll), `.dockerignore` keeps `.env`/sessions/databases out of image layers, and the dashboard can
  require a password and now binds to localhost by default.
- **Dashboard container fixed** — it could not start (a module was missing from its image).
- **CI is green-able again** — 13 lint findings fixed; new contract tests stop help/menu/command drift.
- **27.8 MB → 8.7 MB** of tracked data: byte-identical duplicate archives removed (nothing unique lost).

## Quick start

```bash
cp .env.example .env
# edit .env — at minimum set BOT_TOKEN and OWNER_ID
pip install -r requirements.txt
python -m bot
```

SQLite is the default database, so there is nothing else to stand up. Live voice-chat music additionally needs
`MUSIC_API_ID` / `MUSIC_API_HASH` (free, from https://my.telegram.org); without them every other feature works and
the music-streaming commands explain that they need configuring.

### Docker

```bash
docker compose up -d --build     # bot + dashboard (dashboard on http://127.0.0.1:8080)
```

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # what ran where: docs/TEST_REPORT.md
python scripts/gen_command_reference.py --check   # docs/COMMANDS.md is generated from the code
```

## Configuration essentials

Every setting lives in `bot/config.py` and is documented in `.env.example` (a test enforces that the two never drift).

| Variable | Purpose |
|---|---|
| `BOT_TOKEN`, `OWNER_ID` | Required |
| `DATABASE_URL` | Default `sqlite+aiosqlite:///./data/novabot.db`; use a Postgres URL to switch |
| `ADMIN_IDS` | Extra bot-wide admins (comma-separated) |
| `CONCURRENT_UPDATES` | Updates handled at once (default 64; `0` = strictly sequential) |
| `DASHBOARD_TOKEN` | Password for the dashboard (HTTP Basic, any username); blank = open |
| `NEWS_API_KEY` | Enables `/news` |
| `MUSIC_API_ID`, `MUSIC_API_HASH` | Enable live voice-chat music |
| `FORCE_SUB_ENABLED`, `FONT_GATE_ENABLED` | Optional channel gates (both off by default) |
| `DEBUG` | Verbose logging (also re-enables httpx request logging) |

## Operations

**Concurrency.** python-telegram-bot handles updates one at a time by default, so a single slow handler (a yt-dlp
download, an LLM call) stalls every chat. `bot/core/concurrency.py` installs a per-chat-ordered, cross-chat-parallel
update processor: updates from one chat still run in arrival order, one at a time; different chats run in parallel,
bounded by `CONCURRENT_UPDATES`. Queued updates of a busy chat wait for their chat's turn *before* taking one of the
global slots, so a flooded chat cannot starve the others.

**Dashboard.** A separate read-only FastAPI service (`dashboard/`, own Dockerfile and requirements) that reads the
same database: chat/user counts, active voice-chat sessions, an XP leaderboard, recent warns/bans.

```bash
docker compose up -d --build   # starts the bot and the dashboard
# or standalone:
cd dashboard && pip install -r requirements.txt
PYTHONPATH=.. DATABASE_URL=... BOT_TOKEN=x OWNER_ID=1 uvicorn main:app --port 8080
```

It issues SELECT-only queries. Set `DASHBOARD_TOKEN` to require HTTP Basic credentials (any username, the token as
password); `/health` stays open for container health checks. `docker-compose.yml` publishes it on `127.0.0.1` only —
put a TLS reverse proxy in front before exposing it further, because user/chat ids and moderation reasons are personal data.

**Logging and secrets.** Logs are structured (structlog). `bot/utils/redact.py` scrubs bot tokens, bearer tokens and
common API-key shapes from every structlog event *and* every stdlib log line/traceback, and the httpx/httpcore loggers
are held at WARNING (unless `DEBUG`) because they print Bot API URLs, which embed the token.

**Images and data.** `.dockerignore` keeps `.env`, Pyrogram sessions, databases and pickles out of image layers
(compose injects `.env` at runtime). Personality data lives under `data/personality/` — see its README for what is
loaded at runtime versus kept as import source material.

**Upgrading from v2.7.** No schema changes and no required `.env` changes. Behaviour changes to be aware of:
welcome text saved with `/setwelcome` (and `/setgoodbye` text) will now actually be posted; `lock_url` now also
catches links in photo/video captions; `/q i` sends the quote as a PNG *file*; the dashboard port is published on
localhost only.

## Feature map

| Area | Commands | From |
|---|---|---|
| **Activity rankings** | `/rankings /topusers /userstats /mytop /sysstats /fightinfo /update /restart` | ChatFight - **v2.15** |
| **Whispers** | inline `@bot @user text` · `/whisper /list /privacy /notifications /whisperhelp /whisperban /whisperunban` | PsstRobot - **v2.15** |
| Moderation | `/ban /tban /mute /tmute /kick /unban /unmute /warn /warns /rmwarn /resetwarns /purge /del` + silent `s…` and deleting `d…` variants · `/notes /save /clear /filters /welcome /goodbye /rules /locks /captcha /nightmode /disable /zombies /unbanall /setwelcome /setgoodbye` | nova_guard_bot + FallenRobot/YaeMiko, rebuilt in v2.12 |
| **Admin tools** | `/promote /fullpromote /demote /title /admins /pin /unpin /unpinall /pinned /invitelink /setgtitle /setdesc` | FallenRobot / Gojo_Satoru / tgbot — **v2.12** |
| **Approvals & blacklist** | `/approve /unapprove /approved /approval /unapproveall` · `/addblacklist /unblacklist /blacklist /blacklistmode` | FallenRobot — **v2.12** |
| **Moderation settings** | `/modsettings /antiflood /setflood /setfloodmode /slowmode /antispam /setwarnlimit /setwarnaction /setlog /unsetlog /logchannel /report /reports` | **v2.12** |
| **Connect** | `/connect /disconnect /connection /allowconnect` — manage a group from a private chat | FallenRobot / Gojo_Satoru — **v2.12** |
| **Extras** | `/truth /dare /carbon /wallpaper /webss /gps /gsearch` (outside services, cooldowns, fallbacks) | FallenRobot / YukkiMusicBot — **v2.13, repaired in v2.14** |
| **Fun (text)** | `/roll /toss /decide /table /shrug /shout /runs /bluetext /slaptext /pattext` | FallenRobot — **v2.13** |
| **Persona engine** | `/pengine /premember /pmemory /pforget` (+ `/pconsent`, `/pwho`, `/ppersons`); `scripts/persona_ft/01..03` | **v2.11.1**, gated training toolkit |
| **Federations** | `/newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote /fedadmins` | FallenRobot/YaeMiko — previously advertised in `/help` but never implemented |
| AI chat | `/ai /chat /imagine /summarize /code /persona /see /transcribe` | nova_guard_bot + expanded |
| Anime / fun / utilities | `/anime /couples /karma /afk /weather /qr /tr /ud /imdb /news` | nova_guard_bot |
| Download & send | `/music /yt /insta /tts /qr` | nova_guard_bot |
| Live voice-chat music | `/play /skip /pause /resume /stop /queue /nowplaying /volume /shuffle /repeat /effects /loop /seek /seekback /playlist /lyrics /channelplay /videomode /toptracks /resetqueue /auth` | harmony + YukkiMusicBot + AnonXMusic |
| Personality mode | `/personality on\|off`, `/joke`, `/mystats`, + auto-replies | nanora_bot |
| Personality skins/presets | `/cpersonality skin <name>` — 113 skins (4 of them real-person style skins — see docs/PERSON_PERSONAS.md; pirate, tsundere, yandere, cyberpunk, robot, and more), 1,609 instruction presets | nanora_bot + personality pack — **expanded** |
| Fonts | `/f1`–`/f18`, `/flip`, `/fontfx` (+11 shortcuts), `/random`, `/mix`, `/reverse`, `/fonts`, `/fontsettings`, `/about`, `/botstats`, `/broadcast` | font_bot_ultimate.py + expanded |
| **Economy & leveling** | `/level /rank /leaderboard /daily /balance /pay /shop /buy /inventory` | **new** |
| **Games** | `/trivia /rps /coinflip /slots /guess /tictactoe` | **new** |
| **Scheduling** | `/poll /remind /reminders /announce /schedule` | **new** |
| **Auto-moderation** | `/antiraid /lockdown /mutes /bans` (+ flood, spam and blacklist above) | **new** |
| **Access control** | `/blacklistchat /authorize /privatemode /maintenance /globalstats /activevc /speedtest /cleanmode /gban /block /autoend` | YukkiMusicBot + AnonXMusic |
| **Character Collector** | `/grab /collection /characters /fav /myprofile /topcatchers /trade /gift /upload /editchar /delchar /addmod /setspawnrate /giveaway /claim` (+ `/harem /protecc /collect /hunt /ctop /delete /changetime`) | anime_collector_bot family + WAIFU-HUSBANDO-CATCHER |
| **Reaction GIFs** | `/reactions on\|off`, plus `/hug /pat /kiss /cuddle /slap /bite /poke /tickle /wave /highfive /handhold /wink /bully /glomp /spank /lick /nom /awoo /neko /waifu /shinobu /megumin /cry /dance /smile /blush /cringe /bonk /yeet /smug` | Mikobot/Team-ProjectCodeX (`nekomode.py`) — **new**, ported from Telethon |
| **Status dashboard** | separate read-only web service — see below | **new** |

Full list: `/help` in the running bot, or [docs/COMMANDS.md](docs/COMMANDS.md) — generated from the code, so it cannot drift.

### Earlier rounds' additions (economy, games, scheduling, fonts...)

- **Economy & leveling** — per-chat XP/levels from activity, a coin
  wallet, `/daily` rewards, `/pay`, and a small shop with an inventory.
  Scoped per-chat on purpose (stored on `ChatMember`, not a global user
  column) so grinding in one group doesn't leak into another.
- **Games** — trivia (20 questions across 5 categories, inline-button
  answers), rock-paper-scissors, coinflip and slots (betting, wired
  through the same balance checks as everything else), a number-guessing
  game, and two-player tic-tac-toe over inline keyboards.
- **Scheduling** — native Telegram polls, personal reminders, and
  admin announcements/scheduled messages, all on python-telegram-bot's
  JobQueue. Reminders and scheduled messages are persisted to the DB and
  re-armed on startup, so a restart doesn't lose them.
- **Auto-moderation** — the warn system's hardcoded "3 warnings = ban"
  is now `/setwarnlimit` + `/setwarnaction (mute|kick|ban)`, per chat.
  Anti-raid detects a join spike and auto-locks the chat (new joiners
  get restricted until an admin lifts it); `/mutes` and `/bans` list
  what's currently active — something Telegram's own API doesn't expose
  even though it enforces the expiry itself.
- **AI vision & voice** — `/persona` sets a custom system prompt per
  user (wires up a DB column, `ai_persona`, that existed in the schema
  but was never actually used by anything). `/see` answers questions
  about a photo; `/transcribe` runs a voice note through Whisper.
- **Playlists & lyrics** — `/playlist save/load` persist the live
  queue by name. `/lyrics` searches Genius and returns title/artist/link
  — deliberately not the lyric text itself; Genius's API doesn't expose
  that either, by design, and this project doesn't reproduce copyrighted
  text it fetches on a user's behalf regardless.
- **Status dashboard** — a small separate FastAPI service (`dashboard/`)
  that reads the same database read-only: chat/user counts, active
  voice-chat sessions, an XP leaderboard, recent warns/bans. This is
  what the original `docker-compose.yml`'s broken `dashboard` service
  reference should have pointed at — see below.

## Architecture

**Two (or more) Telegram connections, one process.** Everything except
live music runs on `python-telegram-bot` (the HTTP Bot API). Joining
and streaming audio *into* a voice chat isn't possible over the Bot
API — it needs an MTProto session, which is what Pyrogram + PyTgCalls
provide (`bot/core/music_client.py`). All of them — the primary
assistant plus any extras from `MUSIC_EXTRA_BOT_TOKENS` — are started
from the same `post_init` hook in `bot/core/bot.py` and share the same
event loop, so there's no manual threading or subprocess management. If
`MUSIC_API_ID`/`MUSIC_API_HASH` aren't set, none of them start; nothing
else depends on it.

**One database.** All four originals used different persistence:
nova_guard_bot used Postgres/SQLAlchemy, nanora_bot used its own SQLite
file, font_bot_ultimate.py used a JSON file, and harmony-music-bot used
MongoDB with a Redis cache in front. Everything now lives in one
SQLAlchemy database (`bot/core/database.py`), defaulting to a local
SQLite file — set `DATABASE_URL` to switch to Postgres. The live-music
queue is stored as a JSON column per chat rather than requiring a
separate MongoDB instance.

**Command collisions were real and are resolved.** All four bots
independently defined `/start`, `/help`, and (three of four) `/stats` —
only one handler per command name can ever fire, so a naive merge would
have silently dropped features. Resolved as:

| Original | Renamed to | Why |
|---|---|---|
| font_bot `/stats` (admin usage) | `/botstats` | nova_guard's `/stats` = chat-level stats |
| nanora `/stats` (personal) | `/mystats` | same reason |
| font_bot `/settings` | `/fontsettings` | nova_guard's `/settings` now actually does something (see below) |
| font_bot `/clear` | `/clearfont` | avoid ambiguity with queue/chat "clear" |

The font picker's inline-keyboard buttons also used bare `page:`/`close`
callback data — identical to the main `/menu` system's callback data.
Same problem, same fix: font buttons are now namespaced `font:page:...`
/ `font:close`.

## Project layout

```
bot/
├── __main__.py, config.py, identity.py
├── core/          bot.py (plugin discovery, middleware, startup/shutdown, jobs), concurrency.py, menu.py (command-menu validation),
│                  database.py (models + auto-migration), decorators.py, errors.py, music_client.py
├── middleware/    access_control, antiflood (+ slow mode), antispam (+ word blacklist enforcement), force_sub, logging_middleware
├── services/      audio_engine, economy_service, platforms, queue_manager
├── models/        queue, track
├── personality/   personality, custom_instructions, examples, jokes, memory, triggers, subplugins/, persons, person_consent,
│                  persona_model (adapter routing), persona_text, persona_sft_format, adapter_loader (optional PEFT),
│                  persona_engine/ (overlay-compatible API on the engine below)
├── persona_engine/ free-first persona engine: situation, profile, retrieval, prompt, identity (bot disclosure), safety, providers, memory, runtime
├── fonts/         catalog (font tables + translation helpers)
├── plugins/       one module per feature area — access_control, admin, adminutils, ai, anime, approvals, automod, blacklist,
│                  cleanmode, collector, connection, cosplay, economy, extras, federations, fonts, fun, games, group_mgmt,
│                  health, language, modsettings, music, music_live, name_history, personality, playlists, quotly, reactions,
│                  reference, scheduling, start, stickers, utilities, chatfight (rankings), whisper
├── assets/        chatfight/ (leaderboard background, logo, font)
└── utils/         blacklist, cache, chatcache, guards, helpers, logger, metrics, modtools, ratelimit, redact,
                   retry, sysstats, telegram_safe, textutil, unogame, validation, webtools, whisper_store, whisper_text, wordgames,
                   i18n (catalogs), langctx (chat language)
training/          persona fine-tuning toolkit (prepare_sft, train_lora, generate, evaluate, baselines, promote, revoke, persona_eval) + authorizations.json
dashboard/         separate read-only status service (main.py, requirements.txt, Dockerfile)
data/              SQLite default location, jokes.json, locales/ (translation catalogs), personality/ (skins, presets, packs, persons/, import inputs)
docs/              COMMANDS.md (generated), LANGUAGES.md, MODERATION.md, CHANGELOG.md, SOURCE_MATRIX.md, ZERO_LOSS_AUDIT.md, TEST_REPORT.md, PERSONA_ENGINE.md,
                   FINE_TUNING.md, PERSON_PERSONAS.md, EVALUATION_REPORT.md, DATASET_REPORT.md, PERSONA_REPORT.md, HISTORY.md, FEATURE_INVENTORY.md, FEATURE_MATRIX.md, provenance/
scripts/           import_personas.py, gen_command_reference.py, build_person_personas.py, build_behavior_profiles.py, persona_ft/ (01_preprocess, 02_train_lora, 03_evaluate)
tests/             unit, contract and static-analysis tests (stdlib-only modules run anywhere; the rest need requirements-dev.txt)
```

## Deliberate simplifications (disclosed, not hidden)

- **The forced channel-membership gate** from font_bot_ultimate.py
  (block every command until the user joins a specific channel) is off
  by default — `FONT_GATE_ENABLED=true` restores it. Defaulting it on
  would have meant every existing Neko user suddenly getting
  blocked from moderation commands by an unrelated promo gate.
- **MongoDB is gone.** The live-music queue now stores as a JSON column
  in the shared database instead. One fewer service to run.
- **Spotify source resolution and synced lyrics** (`lyricsgenius`,
  `spotipy`, `mutagen` in harmony's original `requirements.txt`) had no
  corresponding implementation anywhere in the uploaded code — only
  YouTube search/URL playback via `yt-dlp` was actually built out, so
  that's what shipped. `Track.source` still has a `SPOTIFY` enum value
  ready for when a resolver exists.
- **Sentry and encrypted-credential-storage packages** (`sentry-sdk`,
  `cryptography`, `argon2-cffi`) were listed in nova_guard's/harmony's
  original requirements but never imported by any code in the uploads.
  Left out of `requirements.txt` rather than installed for nothing; add
  back if you build out that functionality. (`tenacity` *is* used now —
  `bot/utils/retry.py` — and is a normal requirement.)
- **The `dashboard` service** in nova_guard's original
  `docker-compose.yml` built from `dashboard/Dockerfile`, which didn't
  exist anywhere in the upload — `docker compose up` would have failed
  outright. It was removed from the merged compose file at the time; a
  real `dashboard/` service with its own Dockerfile has since been added
  (see *Operations* above).
- **Deep per-message coordination** between the moderation listeners,
  font auto-styling, and the personality auto-reply isn't implemented —
  a message that a lock/filter listener deletes can still get an
  auto-styled or sarcastic reply afterward, since python-telegram-bot
  doesn't automatically stop later handler groups from processing an
  update just because an earlier one deleted the message. Cosmetic, not
  a moderation bypass; flagging it rather than quietly leaving it out
  of scope.

## Verifying live-music streaming

This bot was built and syntax-checked in a sandboxed environment with no
network access — the code is complete and internally consistent, but it
hasn't been run against real Telegram/Postgres credentials or an actual
voice chat. PyTgCalls in particular has had breaking API changes across
versions; the code here targets the 1.x line (`py-tgcalls>=1.2.0`,
matching what harmony-music-bot's own requirements pinned). If you
upgrade that dependency, double-check `bot/core/music_client.py`'s
`on_stream_end` wiring and `bot/services/audio_engine.py`'s
`join_group_call`/`AudioPiped` calls against PyTgCalls' current docs —
and, newer to this codebase and correspondingly less battle-tested here,
`change_stream()` (used by `/seek`, cross-referenced against
AnonXMusic's own working usage rather than assumed) and
`get_participants()` (used by `/autoend`, wrapped defensively — a
failure there skips that chat's check rather than raising).

## Known trade-offs

- **XP/coins are per-chat**, not global — a deliberate choice (see
  above), but worth knowing if you expected a single wallet everywhere.
- **Game state (trivia/tic-tac-toe/number-guessing) lives in memory**,
  not the database — a restart mid-game simply ends it. Reminders,
  scheduled messages, and temp-mute/ban records are DB-backed and
  survive a restart; ephemeral chat games aren't valuable enough to
  persist for the added complexity.
- **The dashboard and the bot don't coordinate in real time** — the
  dashboard reads whatever the bot last wrote via its heartbeat job
  (every `DASHBOARD_HEARTBEAT_INTERVAL` seconds, default 30s), not a
  live push.
- **Updates run concurrently across chats** (since v2.8). Order is preserved
  *within* a chat, so per-chat state behaves exactly as before; state shared
  *across* chats (coin balances) relies on atomic database updates, which the
  economy tests exercise with real concurrent callers. If you ever suspect a
  concurrency problem, `CONCURRENT_UPDATES=0` restores strictly sequential
  processing.
- **Quote cards quote one message.** The Bot API cannot read chat history, so
  `/q <n>` can only ever quote the replied-to message (the bot now says so).
- **Flood, slow-mode and report-cooldown counters are in memory**: per process, reset on restart. Slow mode is bot-enforced (early messages are deleted), not Telegram's native slow mode, which the Bot API cannot set.
- **`/news` needs a NewsAPI key** (`NEWS_API_KEY`); without one it explains how
  to enable itself rather than failing silently.

## Further reading

- [docs/COMMANDS.md](docs/COMMANDS.md) — every command, generated from the code
- [docs/CHANGELOG.md](docs/CHANGELOG.md) — what changed in v2.8 and how it was verified
- [docs/HISTORY.md](docs/HISTORY.md) — the full narrative of earlier rounds
- [docs/FEATURE_INVENTORY.md](docs/FEATURE_INVENTORY.md), [docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md) — per-feature status
- [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md) — architecture decisions and quality gates
- [data/personality/README.md](data/personality/README.md) — what personality data is loaded versus import-only
