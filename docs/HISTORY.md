# History of earlier rounds

This file preserves, unchanged, the round-by-round narrative that used to fill the top of the README
(v2.1 – v2.7), plus the "bugs found", "what was fixed" and "fonts / performance" write-ups. The README now
carries only what an operator or contributor needs today; nothing from the old README was dropped — it
moved here. For the current release's changes see [CHANGELOG.md](CHANGELOG.md); for per-feature history
see [FEATURE_INVENTORY.md](FEATURE_INVENTORY.md).

---

### README preamble as of v2.6 (2026-09-30)

> Complete integration of every personality dataset + caption-free /cosplay.

**This build:**
- `/cosplay` `/cp` — pure photo, no caption (matches reference bot)
- **All** personality datasets from every attachment (v1–v5, packs, nanora, expansions, waves 1–41)
- AFK duration, polished couples, stronger IMDb fallback
- Ambient personality off in groups unless admin enables

See `data/personality/README.md` and `ENGINEERING_SUMMARY.md`.

---

font styling, an economy/games layer, scheduling, and bot-wide access
control. Merged and expanded from six previously separate projects —
**nova_guard_bot**, **harmony-music-bot**, **nanora_bot**,
**font_bot_ultimate.py**, and (this round) **YukkiMusicBot-3.0** — into
a single codebase with one database, one config file, and no command
collisions. (Formerly "NekoGuard" — renamed to Neko this round.)

---

## This round: YukkiMusicBot integration

[YukkiMusicBot](https://github.com/TeamYukki/YukkiMusicBot) is a real,
actively-maintained, GPL-3.0 open-source Telegram voice-chat bot —
checked the same way as every other upload (structure, dependencies, a
scan for the same red-flag patterns used throughout this project).
Nothing concerning; its `eval`/`exec` hits are a standard, openly-
credited owner-only debug console (`.eval`, gated to `SUDOERS`), the
same well-established pattern used across the Pyrogram bot ecosystem —
not something targeting anyone but the bot's own operator.

**Brought in, adapted to this project's architecture:**
- **Multi-platform search** — Spotify, Apple Music, and Resso links
  resolve to "title artist" metadata (their own public API/page
  metadata — never their actual audio) and stream the match from
  YouTube, same approach the engine already used for plain search.
  SoundCloud needs no special-casing — yt-dlp streams it directly. See
  `bot/services/platforms.py`.
- **Multi-assistant scaling** — one assistant can only be in a limited
  number of voice chats at once; `AssistantPool` (in
  `bot/core/music_client.py`) spreads chats across several,
  least-loaded first. **Deliberately not a port of Yukki's actual
  mechanism**: Yukki's extra assistants are full user accounts logged
  in via phone number (Pyrogram "string sessions"), a materially
  different, ToS-grayer pattern than the bot-token MTProto approach
  this project uses everywhere else. Extra assistants here are instead
  extra **bot accounts** — `MUSIC_EXTRA_BOT_TOKENS` — same account
  type, same scaling benefit, no new risk profile introduced.
- **Auth users** (`/auth`, `/unauth`, `/authusers`) — non-admins a chat
  admin explicitly allows to control playback. Off by default
  (`/restrictcontrols on` to require it) — existing chats see no
  behavior change until an admin opts in.
- **Access control** (`/blacklistchat`, `/authorize` + `/privatemode`,
  `/maintenance`) — a real `ApplicationHandlerStop`-based middleware
  pass (`bot/middleware/access_control.py`), not just a plugin-level
  check, so a blacklisted or unauthorized chat's messages don't reach
  *any* other plugin either. The BotConfig flags (maintenance/private
  mode) are DB-backed and toggle live, with a short in-memory cache
  since this middleware runs on every single update.
- **`/loop`** — repeat the current track exactly N times, distinct from
  `/repeat`'s indefinite modes.
- **`/channelplay`** — stream into a different chat's (typically a
  linked channel's) voice chat than the one the command was sent in.
- **Video streaming** — opt-in per chat (`/videomode on`), bot-wide
  concurrent cap (`VIDEO_STREAM_LIMIT`). Flagged as less certain than
  the audio path: the video-quality preset class name has moved around
  across pytgcalls versions more than what this project already relied
  on, so it fails loudly with a clear log reason rather than silently
  degrading to audio-only if your installed version doesn't match.
- **`/cleanmode`** — auto-delete bot replies after a delay. Scoped to
  the live-music plugin's highest-traffic replies (its original use
  case), not retrofitted onto all 160+ commands' reply calls — see
  `reply_with_cleanup()` in `bot/utils/helpers.py`.
- **`/globalstats`, `/activevc`, `/speedtest`** — bot-wide visibility.
  `/globalstats` rather than `/stats`: that name was already taken
  (chat-level moderation stats) before this round even started.

**Deliberately not carried over:**
- **Yukki's `/lyrics`** scrapes and sends full lyric text via
  `lyricsgenius` (and hardcodes a live API key in the source). This
  project's own `/lyrics` already searches Genius and links out instead
  of reproducing the text — kept as-is rather than degraded to match.
- Full inline-query search UI, Heroku-specific self-restart, the
  MongoDB backend, and the multi-language string system (10+ JSON
  language files) — each a substantial separate undertaking with
  limited marginal value given what's already here (Docker deployment,
  SQLAlchemy, English-only throughout). Flagging the scope decision
  rather than quietly shipping a partial version of any of them.
- The proactive "suggest commands in random chats" growth feature —
  messaging chats that didn't ask for it. Skipped on purpose, not an
  oversight.

## Also this round: AnonXMusic integration

[AnonXMusic](https://github.com/AnonyminHackz/AnonXMusic) turned out to
be a close sibling of YukkiMusicBot — same `platforms/` layout,
byte-for-byte, and enough shared structure elsewhere that most of what
it offers was already covered by the Yukki round. Same safety scan as
every upload (clean); efficiently focused the actual reading on the
handful of files that looked genuinely different rather than re-reading
ground already covered.

**Genuinely new, brought in:**
- **`/seek`, `/seekback`** — real seek, not a stub. The engine's `seek()`
  used to unconditionally return `False` with a "not implemented" log —
  AnonXMusic's `core/call.py` confirmed the actual mechanism (PyTgCalls
  has no true seek; you restart the stream with ffmpeg's `-ss`/`-to`
  flags via `change_stream()`, which swaps the stream on an
  already-joined call). Position tracking for both `/seek` and an
  accurate `/nowplaying` progress bar is computed from timestamps, not
  AnonXMusic's approach (a background task ticking every second for
  every playing chat) — same information, no polling loop.
- **`/gban`, `/ungban`, `/gbanned`** — removes a user from every chat
  Neko moderates. Deliberately kept **distinct** from the new
  **`/block`, `/unblock`, `/blocked`**: gban takes a real, visible
  action (actually kicks them); block just makes Neko stop
  responding everywhere, no effect on chat membership. Both check
  through the access-control middleware (same `ApplicationHandlerStop`
  mechanism as blacklist/maintenance), with the checked-user-ID set
  cached the same way BotConfig already was, since this also runs on
  every single update.
- **`/autoend`** — leaves a voice chat once no real participants are
  left, checked via `PyTgCalls.get_participants()`. Distinct from the
  existing `ASSISTANT_AUTO_LEAVE_SECONDS` (that's "no *playback*
  activity for N seconds"; this is "is anyone actually listening").
  Flagged the same way video was: participant-listing is a less
  battle-tested part of the pytgcalls surface than join/play/stop, so
  a lookup failure for one chat just skips it rather than raising.
- **`/toptracks`** — most-played tracks, chat-scoped by default,
  `/toptracks global` for bot-wide. New `TrackPlay` table, logged
  best-effort alongside every track start (a logging failure never
  interrupts actual playback).
- **`/resetqueue`** — force-clears a stuck chat's queue and playback
  state. The useful kernel of AnonXMusic's `restartbot`; the other half
  of what that command did (refresh a cached admin list) doesn't apply
  here — `admin_only` checks fresh via the Telegram API every time
  rather than caching, so there's no cache to go stale.
- Livestream detection (`Track.is_live`) — the existing generic yt-dlp
  resolver already played live YouTube streams fine; this just reads
  yt-dlp's own `is_live` flag to label them (🔴 in `/nowplaying`) and
  block `/seek` on them, rather than building a parallel live-specific
  playback path the way AnonXMusic's `live.py` does.

**Not carried over** (already covered, or out of scope for the same
reasons as the Yukki round): the platform resolvers (identical to
Yukki's — `bot/services/platforms.py` already handles it), the
quality-picker download flow in `songs.py` (richer than `/music`'s
"best available," but a multi-step callback UI for a smaller win than
the items above), and Heroku/i18n/inline-mode, unchanged from before.

## Also this round: a 10-file batch, and one of them was malware

Read every upload before touching any of them, same as every round.
Results varied a lot more than usual:

**Excluded — `GrabberAutoUserbot`:** this is malware, not a judgment
call. It's a real userbot (logs into a phone-verified personal Telegram
account via a saved session string, not a bot account) whose session is
literally named `"loli"`, built to auto-play a waifu-collecting game
bot faster than a human could. Its actual logic isn't readable in the
source — it's a giant base64 blob that gets *reversed*, decoded,
zlib-decompressed, and `exec()`'d at runtime. There's no legitimate
reason for honest code to hide what it does behind that many layers;
it's a textbook pattern for evading review. Nothing from this file was
read past confirming what it does structurally, nothing from it is in
this codebase, and it shouldn't be run. If you got it from wherever
these "auto grabber" tools circulate, that's worth knowing.

**Couldn't open — `Download.7z`:** genuinely just an environment
limitation, not a finding — no `.7z` extraction tool is available here
and there's no network access to install one. If you re-zip it as
`.zip` or extract it and re-upload the contents, it can be reviewed
next round.

**Clean, but noted — `WAIFU-HUSBANDO-CATCHER`:** a standard
"guess-the-character collector" game bot (à la Mudae) — the actual game
content is whatever an admin `/upload`s themselves, nothing objectionable
baked into the code. Its `config.py` does have what look like real, live
credentials (a bot token, a MongoDB password) committed in plaintext,
which is worth flagging as a lesson even though it changes nothing here
— this project has never hardcoded a secret anywhere; everything goes
through `.env`.

**Skipped as near-total duplicate — `Marin-Music` (internally
"HydraMusic"):** same `platforms/`, same `core/call.py` shape, same
plugin layout as YukkiMusicBot and AnonXMusic — a third sibling in that
family, not a fourth source of new material. Confirmed via the plugin
file listing rather than a full re-read.

**Actually integrated — from `FallenRobot` and the three `YaeMiko`
uploads (internally "Mikobot," three version snapshots of the same
project; compared them to find the most complete rather than reading
all three in full):**
- **Federations** (`/newfed /joinfed /leavefed /fedban /unfedban
  /fedinfo /fpromote /fdemote /fedadmins`) — this closes a real,
  self-inflicted gap: `/help` has listed `/newfed /joinfed /fedban
  /fedinfo` since the very first round, and the `Federation`/`FedBan`
  tables have existed just as long, but grepping the actual plugin
  files turned up zero handlers ever registered for them. New
  `plugins/federations.py`, reusing the same ban-propagation pattern
  already built for `/gban`.
- **`/disable`, `/enable`, `/disabled`** — quiet a specific command for
  non-admins in a chat. Implemented differently from the source
  projects' custom `CommandHandler` subclass: a check in the existing
  `access_control_middleware` (which already runs first, on every
  update) reading a new `Chat.disabled_commands` list, rather than
  wrapping all 188 command registrations individually.
- **`/zombies`, `/rmzombies`** — find and remove deleted/deactivated
  accounts from a chat. The Bot API has no way to list a group's full
  membership (that's an MTProto-only capability, for privacy/scale
  reasons) — this reuses the same assistant connection already set up
  for live music rather than standing up a separate userbot just for
  this, and says so plainly if `MUSIC_API_ID`/`MUSIC_API_HASH` aren't
  configured rather than pretending to work.
- **`/unbanall`** — lifts every ban this bot has issued in a chat.

**Not carried over:** `connection.py` (manage a group's settings
remotely from DM) would be a genuinely large change — every existing
admin command resolves its target chat from `update.effective_chat.id`
directly, and retrofitting a DM-override check into all of them at this
stage is a bigger, riskier change than its value justifies right now,
not a small addition like the four above. `antinsfw.py` turned out to
be filter plumbing rather than actual image classification, so there
was no well-defined capability to port. A long tail of smaller novelty
commands (`quotely`, `imagegen`, `cosplay`, `pkang`, `pokedex`,
`nekomode`, `currency_converter`, `truth_and_dare`, and others) were
reviewed but not integrated — each fine individually, but the batch
would meaningfully increase surface area for a smaller return than
federations/disable/zombies.

## Also this round: a real Character Collector game

Four more uploads: three independent single-file
`anime_collector_bot.py` variants (1,241 / 1,555 / 1,823 lines — not
versions of each other, genuinely different implementations of the
same idea) plus the more production-shaped `anime_catcher_bot` (proper
module layout, its own manual test suite), cross-referenced against
`WAIFU-HUSBANDO-CATCHER` from the previous round. Given the genre —
last round's malware was specifically a cheat tool targeting exactly
this kind of bot — every file here got the elevated scrutiny that
implies: the same obfuscation-pattern check that caught the malware
last time, plus a specific scan for self-bot/session-string patterns
and concerning content keywords. All four came back clean.

Rather than port the most complete single-file script wholesale, this
became one coherent `plugins/collector.py` that reuses what Neko
already has instead of duplicating it:
- **Catch rewards pay into the existing coin economy**
  (`bot/services/economy_service.py`) instead of standing up a second,
  parallel currency.
- **`/topcatchers`**, not `/leaderboard` — that name already means XP
  ranking (`plugins/economy.py`).
- **No separate `/daily`, `/broadcast`, `/stats`, `/botban`** — all
  four source projects have versions of these, and all four overlap
  with commands Neko already has that do the same job
  (`/daily`, `/broadcast`, `/stats`, `/gban`+`/block`).
- Trade proposals reuse the same "own-namespaced inline keyboard"
  pattern as `plugins/games.py` (`collector:` callback_data, checked
  against every other prefix in use before shipping — see the
  callback-namespace check in the validation section below).

**What's in:** ambient spawns based on chat activity (`/setspawnrate`
per chat), `/grab <name>` to catch, `/collection` / `/characters`
(paginated), `/fav`, `/myprofile`, `/topcatchers`, `/trade` (propose +
accept/decline) and `/gift`, mod tools (`/upload`, `/delchar`,
`/addmod`, `/removemod`, `/mods`), and coin-prize `/giveaway` +
`/claim` + `/endgiveaway`. Characters are bot-wide (upload once,
catchable in every chat) with their own moderator role, separate from
being an admin of any single chat — the content is shared across all
of them, so the curation permission is too.

## Also this round: the Nanora personality layer, finished

Eight uploads this round, all for one subsystem: two copies of the
full bot (one "github-ready" with CI config and the collector game
above already in it, confirmed to be a strict superset of the other),
carrying the personality layer as it already existed in
`bot/personality/` — same code, extracted standalone for reference as
`nanora_complete.txt` — plus two follow-up revisions of just that
subsystem (`nanora_v2`, `nanora_v2.2`), each responding to a code
review of the one before it (`nanora_improvements.md`,
`nanora_v2_2_improvements.md`), and a third revision (`nanora_v2.3`)
responding to the second review. Before touching anything, every joke,
metaphor, opener/closer, and subplugin response pool was diffed
programmatically from the original baseline through v2.3 to confirm
nothing had been silently dropped across three rounds of revision — it
hadn't (95 jokes, all subplugin data, fully accounted for). v2.3
became the base.

Both review docs' checklists were then checked against what v2.3
actually does, not just what it claims to — several fixes were written
but never actually took effect:
- **The #1 bug both reviews flagged — custom instructions lost on
  restart — was still live.** v2.3 added `on_startup()` to reload
  persisted settings and schedule daily cleanup, but nothing ever
  called it; `post_init()` in `core/bot.py` had no entry for it. Wired
  in, alongside the other idempotent startup tasks.
- **The database columns the personality layer writes to
  (`personality_skin`, `custom_instructions`, `personality_user_limit`,
  ...) only existed in a separate, never-imported
  `database_personality_additions.py`.** Writing to them was silently
  a no-op Python attribute, not a persisted column. Merged directly
  into `core/database.py`'s real `Chat`/`ChatMember` models — same
  fate for `config_personality_additions.py` into `config.py`.
- **`personality_llm_enabled` (opt-in, off by default) was defined
  but never checked**, and the function it was supposed to gate —
  `generate_response()` in `plugins/ai.py` — didn't exist at all, so
  the "LLM-backed persona rewriting" feature silently no-opped for
  every custom-instruction chat regardless of the setting. Built
  `generate_response()` (factored out of `_get_ai_response`'s provider
  chain rather than duplicating it), wired the flag to actually gate
  it, and added a timeout — `python-telegram-bot` processes updates
  sequentially by default, so an unbounded call here would have
  stalled the whole bot, not just one chat.
- **`increment_and_check`'s docstring claimed the per-user reply limit
  was race-free; it was only race-free on SQLite.** The increment
  itself was a Python read-then-write, which is safe under SQLite's
  single-writer model but a real lost-update risk on the Postgres this
  project also officially supports. Rewritten as a single atomic
  `count + 1` SQL expression (the same idiom `update_profile()` and
  `economy_service.py` already use elsewhere) with `RETURNING`;
  load-tested with 15 concurrent first-time callers racing to create
  the same row — exactly one row, exactly the right count, every time.
- **The programming/anime/gaming subplugins matched keywords with
  plain substring checks** — inherited unchanged from the original
  bot through every revision — so "gg" fired inside "stru**gg**ling"
  and "play" fired inside "dis**play**". Fixed with a shared
  word-boundary matcher.
- **`cachetools` is used throughout the personality layer's caching
  and was never in `requirements.txt`** — a fresh install would have
  had the entire plugin fail to load, silently, at the
  `discover_plugins()` try/except.
- Only 3 of the 10 personality-trait presets had actually been
  externalized to JSON; the other 7 silently fell back to Python-only
  defaults, so the "admins can edit presets without touching code"
  feature only half-worked. Generated the missing 7 from the code
  defaults they were already falling back to.

## This round: rebrand to Neko, force-subscribe, and a collector overhaul

Another large batch of uploads: this project's own previous output fed back
in as the new base, a fourth personality-layer revision (`nanora_v2.4`,
arrived as `neko_v2_4.zip`), a substantially rewritten
`collector_enhanced.py`, three snapshots of an unrelated full
group-management bot ("Mikobot"), one unrelated full music bot
("HydraMusic"), and the same collector reference sources already
cross-referenced in an earlier round (confirmed: nothing new there beyond
what's already in this file's collector plugin).

The full, itemized breakdown — every command compared, every bug's
reproduction steps, every deferred feature's reasoning — lives in
[`docs/FEATURE_INVENTORY.md`](FEATURE_INVENTORY.md). Summary:

- **Rebrand.** The bot is now Neko (@Nekooooooobot, owner "Star") in every
  user-facing surface — `bot/identity.py` is the single place that controls
  this, config-driven so it's a `.env` change, not a code change, for
  anyone who forks this again. The underlying package name, database
  tables, and Docker image stay `novabot` internally on purpose (see that
  file's docstring) — renaming those would break every existing deployment
  for zero user-facing benefit.
- **Force-subscribe** to `@TheNexusX`, new (`bot/middleware/force_sub.py`) —
  fails open on API errors so a misconfigured channel can't take the whole
  bot down, caches membership checks, and the "Check Again" button is
  architecturally incapable of being blocked by the same gate it's checking
  (callback queries and message-triggered middleware are different PTB
  update paths).
- **Collector plugin, substantially rewritten.** Rarity now actually
  affects spawn odds (it didn't before — "legendary" was flavor text with
  uniform-random odds under the hood). Found and fixed three real bugs in
  the process: `/topcatchers` and a new `/owners` command silently excluded
  any user who'd never triggered a `User` row before catching something;
  uploaded images were stored as a Telegram `file_path`, which the Bot API
  only guarantees for about an hour, not a stable `file_id`; and fixing
  that broke inline query results in a different way (they need an actual
  URL, not a file_id) until switched to the `Cached` result variants.
  Tightened four permission gates that were checking "admin of whichever
  chat this happened to run in" instead of the bot-wide collector-moderator
  role the code's own docstring says they should be — one of those gaps
  meant any admin of any group could've minted unlimited coins via
  `/giveaway`. All of this is now covered by 12 new tests in
  `tests/test_collector.py`, including a 12-way concurrent grab race.
- **`nanora_v2.4` reviewed and mostly not adopted** — it independently found
  the same four bugs already fixed here, but every fix was weaker under
  actual testing (its "atomic" counter failed 14 of 15 concurrent calls;
  its startup-hook fix silently destroys every other plugin's startup
  tasks; its runtime DB patch never touches an already-existing table). One
  idea was worth keeping — grounding LLM persona rewrites in a base
  identity — and it's implemented better than the source: using each skin's
  own (previously unused) `description` field instead of one hardcoded
  personality that would've been wrong for any non-default skin.
- **Mikobot, reviewed for gaps.** Corrected a real bug in the first pass —
  a broken regex meant several "missing" features weren't actually checked
  properly — then re-verified from scratch. AFK, karma, and per-chat
  command disabling all turned out to already exist under different
  names. Name/username change history ("sangmata") genuinely didn't, and
  is now `bot/plugins/name_history.py` + `/names`, tested. Two more
  genuine gaps (`connection.py`, `approve.py`) are deferred with specific
  reasoning in the inventory doc rather than bolted on carelessly — both
  need changes to already-hardened enforcement code that wasn't re-reviewed
  closely enough this round to edit with confidence.
- **HydraMusic, reviewed structurally, not deep-ported** — no standout gap
  found against a music engine that's already absorbed two prior bots
  across earlier rounds; a genuine file-by-file equivalence check the way
  Mikobot's AFK/karma claims were verified is flagged as a reasonable
  future round's scope rather than rushed here.

## This round: consolidated MAXIMUM, verified it by running it, and fixed what that found

Four more uploads arrived as one strict lineage — `v4` → `production-ready`
→ `v5-enhanced` → `MAXIMUM`, each confirmed a superset of the last by
diffing file lists *and* content, so nothing needed re-merging — plus
several older snapshots (`github-ready`, `neko_v2_4`, the base `novabot`
archives) confirmed to be strict subsets contributing nothing new. `MAXIMUM`
was the base. Full breakdown in
[`docs/FEATURE_INVENTORY.md`](FEATURE_INVENTORY.md); summary:

- **The personality plugin didn't actually load.** An import landed above
  the module docstring, pushing `from __future__ import annotations` past
  where Python allows it — a `SyntaxError` the plugin loader swallows and
  logs rather than crashes on, so the bot's flagship feature was silently
  inert on every startup. Fixed, and verified by actually building the
  `Application`: all 20 plugins now load, 0 failures.
- **A real double-spend bug.** `economy_service.try_spend()`'s own
  docstring called it atomic; it was a Python read-modify-write, the exact
  pattern this project's `learnings-and-workflow.md` already warns isn't
  safe on a real database. Rewritten as a single conditional SQL UPDATE,
  plus a proper single-transaction `transfer_coins()` for `/pay` (an
  earlier draft split it into two separately-committed calls, which fixed
  the race but opened a partial-failure gap instead — caught before
  shipping). `tests/test_economy.py` proves it under actual concurrent
  load: 50 simultaneous spends against a 100-coin balance, exactly 10
  succeed, balance never goes negative.
- **`bot/utils/retry.py` was dead code** — imported nowhere, and its only
  dependency wasn't even in `requirements.txt` despite the previous
  round's own notes claiming otherwise. Wired it into the three outbound
  API calls in `platforms.py` and, more importantly, into the database's
  initial connection at startup, so the bot retries with backoff instead
  of crashing outright if Postgres isn't accepting connections yet.
- **Reaction GIFs, ported in** — the uploaded `nekomode.py` (Mikobot
  lineage, same family already reviewed last round) turned out to be a
  genuine gap the earlier Mikobot audit didn't cover. It's Telethon +
  MongoDB; this bot is python-telegram-bot + SQLAlchemy, so it's a real
  port, not a drop-in — `/hug`, `/pat`, `/slap` and 27 others now live in
  `bot/plugins/reactions.py`, gated by a per-chat `/reactions on|off`
  (renamed from `/nekomode` — this bot is already *named* Neko and already
  has an unrelated `/personality` toggle, so reusing "nekomode" for a
  different feature would just confuse people). Targeted actions now
  actually render "Alice hugs Bob!" — the source's own help text promised
  that but its code never implemented it.
- **A data-cleaning miss.** One personality-dataset entry still had a
  pre-rebrand line about the bot's old "creator" character, and a derived
  index file still keyed a category under the old brand name after the
  main dataset had already been renamed. Both fixed; the dataset-cleanup
  test that catches exactly this now passes.
- **Two SQLite issues surfaced by writing the concurrency tests
  honestly** — `"database is locked"` and a pooled connection outliving
  its event loop — both fixed (WAL mode + busy timeout, `NullPool`).
  Doesn't affect Postgres, which was never susceptible to either.

Left alone on purpose, with reasoning: 44 uses of a soon-to-be-fully-removed
`datetime.utcnow()` (fixing it safely means auditing every comparison site,
not just every call site — bigger than this round); `bot/utils/cache.py`
and `bot/utils/validation.py`, both solid but currently under-integrated
rather than forced into a use they weren't built for; ~30 sampled (not all
individually rewritten) `except: pass` sites, none of them on a permission
check. Full test suite: **76 passed, 0 failed** (54/56 on `MAXIMUM` as
uploaded).

## This round: a real personality library, `@safe_handler` everywhere, rate limiting

This round's uploads (`ULTIMATE`, `ULTIMATE-PACKED`, `v5-MAXIMUM-LAYERS`,
`v6-COMPLETE`) turned out to be independent branches forked *before* last
round's fixes — confirmed by checking whether the personality-plugin
syntax error, the `fun.py` crash, and the economy race condition were
present (they were, in all four). So the verified build stayed the base,
and the genuinely new work got cherry-picked in rather than merged
wholesale. Full detail in
[`docs/FEATURE_INVENTORY.md`](FEATURE_INVENTORY.md); highlights:

- **22 new personality skins, 14 new presets** — pirate, tsundere,
  yandere, cyberpunk, and more, on top of the existing 3/10. They slot
  into loaders that already existed (both just glob their directory), so
  this was low-risk — until a first pass blanket-copied the whole
  presets folder and silently overwrote 10 originals with this branch's
  older versions of the same names. A test caught it (a lost
  `themed_metaphors` field); the 10 originals were restored, keeping
  only the 14 genuinely new ones.
- The personality dataset grew from 1093 to 3923 entries so each skin
  has its own grounding examples, with per-skin few-shot retrieval now
  wired into the LLM-rewrite pipeline. The *exact same* leftover
  pre-rebrand line fixed last round ("Saria Nakano... coded me") had
  reappeared in this branch's independently-expanded copy of the
  dataset — same entry, same fix, applied again.
- **`@safe_handler` rolled out from 1 plugin to 15** (105 handlers) —
  explicitly deferred last round as "a much larger, separate effort."
  This upload had done real work here, but on the unfixed base, so it
  couldn't be merged directly: scripted a pass that mirrored exactly
  which functions *they* decorated onto the *current, already-fixed*
  codebase, rather than merging diffs or guessing. A few unrelated lines
  in the same diffs — old, already-fixed bugs on their branch — were
  left out on purpose.
- **Rate limiting, for real** — `bot/utils/cache.py` (built last round,
  but only ever used by `/health`) finally has real call sites: a shared
  per-user hourly cap across all four AI commands (the upload only
  covered one of them, and had a copy-paste bug pasting the check twice
  into the same function — fixed and generalized instead of copied), and
  a second, distributed check on `/grab` alongside the existing
  in-memory cooldown (confirmed complementary: the existing one doesn't
  survive a restart or hold across worker processes, this one does).
- A proper `ci.yml` (compile + lint + test on every push/PR) added
  alongside the uploaded `bot.yml`, which runs the bot itself as a
  manually-triggered Action — fine as a one-off smoke test, but relabeled
  clearly as that rather than a deployment method, since GitHub Actions
  jobs cap out at 6 hours.

An unrelated file in this batch (an HTML homework assignment, nothing to
do with this project) was left out of the build rather than forced in.

Full test suite: **87 passed, 0 failed** (was 76 at the start of this
round) — 11 new tests cover every shipped skin/preset loading through
the real loaders, and the rate limiter's pass/block arithmetic,
including that a cache outage fails *open* rather than blocking
everyone.

## More real bugs found along the way

- **`admin_only` silently let anyone through in a DM.** The decorator
  treated "no group to check admin status in" as "nothing to check,
  let it through" — meaning all 20 commands it protects (ban, mute,
  kick, warn, filters, welcome, notes, locks, captcha, log-channel...)
  could be invoked by *any* user via a private message to the bot,
  admin check fully skipped. None of those commands have a legitimate
  DM use case, so private chats are now denied outright.
- **The warn system's "3 strikes" was hardcoded**, text and logic both
  (`if count >= 3`, literally printed as `"Count: {count}/3"`), with no
  way to change it. Replaced with per-chat `/setwarnlimit` and
  `/setwarnaction`, falling back to configurable defaults.
- **The AI plugin's docstring claimed OpenAI, Anthropic, *and* Gemini
  support; only OpenAI was wired up** — the other two branches didn't
  exist. Built out.
- **A fresh deployment following this README's own "only `BOT_TOKEN`
  and `OWNER_ID` are required" instructions would have crashed at
  startup**, caught by actually running the app instead of only
  syntax-checking it. Three separate causes stacked on the same
  handful of optional fields: pydantic-settings tries to JSON-decode
  any `.env` value for a `List[...]`-typed field (like `ADMIN_IDS`)
  before a custom validator gets a chance to run, so the blank default
  threw immediately; `python-dotenv` doesn't strip a trailing
  `# comment` when the value in front of it is blank, so
  `ADMIN_IDS=   # comma-separated...` was read as the literal comment
  text, not an empty string; and pydantic doesn't treat a
  present-but-empty env var as `None` for an `Optional[int]` field, so
  blank `MUSIC_API_ID=` failed validation too. Fixed all three
  (`NoDecode` annotations, moving `.env.example`'s inline comments on
  blank defaults to the line above instead, `env_parse_none_str=""`),
  then verified by actually loading `Settings()` with nothing but a
  fake token and owner ID set.

## What was actually fixed, not just moved

- **`bot/plugins/group_mgmt.py` and `fun.py`**: the blacklist-word
  filter, the content-lock listener, and the AFK listener were all
  registered as catch-all text handlers in the *same* default handler
  group. python-telegram-bot only invokes the first matching handler
  per group, so two of the three were silently dead code — the
  blacklist filter, in particular, never ran. Each now has its own
  handler group and all three work.
- **`__main__.py`**: used `Update.ALL_TYPES` without importing `Update`
  — this would crash with a `NameError` on every polling-mode startup
  (the default mode). Fixed.
- **`personality.py`'s message handler** (ported from nanora_bot's
  `bot.py`): called `random.random()` without importing `random`,
  ~10% chance of a `NameError` on any message that matched an intent.
  Fixed.
- **Auto-advancing the queue**: harmony's engine requested an
  end-of-stream notification from PyTgCalls (`StreamAudioEnded`) but
  nothing was ever registered to receive it, so playback would have
  stopped after one track. `music_client.py` now wires this to actually
  advance the queue.
- **Effects/volume resetting on skip**: `AudioEngine.play()` starts a
  brand-new, effect-less pipeline unless the caller passes the existing
  one back in — `/skip` and auto-advance now do, so `/effects` and
  `/volume` persist across a track change instead of silently resetting.
- **`/settings`** was listed in nova_guard's command menu but had no
  handler at all. It's now implemented.

## Fonts, expanded

Sourced from a screenshot of another bot's font picker, plus a pass
through actual Unicode references (not guessed) for accuracy:

- **8 more substitution fonts** (`/f11`–`/f18`, 18 total now): squares
  (outline `/f11` and filled `/f12` — U+1F130+ and U+1F170+), circled
  (`/f13`), regional-indicator "Special" (`/f14`), decorative Runic
  (`/f15` — a real Unicode script, but there's no actual Latin↔Runic
  correspondence, so like every novelty rune generator this is a
  consistent but stylistic mapping, not a transliteration), and the
  three most commonly-requested styles that were oddly missing before:
  Bold, Italic, and Sans-Bold (`/f16`–`/f18`).
- **`/flip`** — true upside-down text (character substitution *and*
  string reversal, which is why it isn't just another `/f#` slot).
- **11 combining-diacritic effects** — `/stinky /bubbles /underline
  /rays /birds /slash /stop /skyline /arrows /strike /frozen`, plus the
  generic `/fontfx <style> text`. Mechanically different from the
  substitution fonts above: each overlays a Unicode combining character
  onto arbitrary input rather than mapping to a fixed alphabet, which is
  also why they compose with anything (including already-styled text).
  `/stop`'s shortcut is `/prohibit`, not `/stop` — that name was already
  taken by music_live.py's stop-playback command; found by the same
  collision check described above, not missed by it.

Every mapping was verified programmatically before shipping — correct
length, valid code points, and (for Runic specifically) confirmed
in-range for Unicode's actual Runic block — not just eyeballed.

## Performance & indexing fixes

- **`settings.is_admin_id` was a `@property`** that rebuilt a set and
  allocated a new closure on *every single call* — every admin check,
  across every plugin. Real-world impact was minor (the set is tiny),
  but it's now a plain method with no rebuild, and every call site
  works unchanged (a bound method and a property-returning-callable
  have the same call syntax).
- **Added composite indexes** on the query patterns that actually run —
  `Warn(chat_id, user_id)` (warn-count lookups), `TempAction(chat_id,
  action, reversed, expires_at)` (the `/mutes` `/bans` listing), and
  `ChatMember(chat_id, xp)` (the leaderboard's per-chat ORDER BY).
- **Auto-migration now covers indexes, not just columns** — the first
  version only added missing columns to existing installs; new indexes
  on already-existing tables would've silently never been created on an
  upgrade. Fixed so both apply on every startup, safely (only ever
  adds, never drops or alters).

## Verification notes from the network-enabled round (v2.3 era)

_Moved out of the README's "Known trade-offs". It describes how that earlier round was verified, not the current build — see CHANGELOG.md for how v2.8 was verified._

- This round had network access, which previous rounds didn't: the
  personality layer's test suite (30 tests, including new ones added
  this round) was actually run, not just syntax-checked, in a real
  virtualenv with its real dependencies installed — including loading
  actual `Settings()` end to end, running `core/database.py`'s
  auto-migration against a live SQLite file, executing `on_startup()`
  against it, and load-testing `increment_and_check` with genuinely
  concurrent callers. Every `.py` file in the project was also parsed
  to confirm it's syntactically valid. What's *not* covered: anything
  needing a real Telegram connection, bot token, or voice-chat
  session — the multi-assistant pool, platform resolvers, and
  access-control middleware from earlier rounds are cross-referenced
  against the ORM models/engine methods they call, but haven't run
  against live credentials. Flag anything that misbehaves.
