# Feature Inventory — Neko Integration Round

This documents every feature/function reviewed across this round's uploads,
what was found, what was integrated, what was intentionally deferred, and
why. Per the integration requirements for this round: no silent omissions —
anything not integrated is listed explicitly below with reasoning.

Sources reviewed this round:
- `novabot-with-nanora-v3.zip` — primary base
- `neko_v2_4.zip` (internally `nanora_v2.4`) — personality-layer revision
- `collector_enhanced.py` + its README — collector plugin revision
- `YaeMiko-main.zip` / `YaeMiko3_0-main.zip` / `YaeMiko-Miko.zip` (internally
  "Mikobot") — full alternative group-management bot
- `Marin-Music-main.zip` (internally "HydraMusic") — full alternative music bot
- `Anime_Character_Collector_Bot.zip`, `anime_collector_bot.zip`,
  `WAIFU-HUSBANDO-CATCHER-main.zip` — collector reference sources already
  cross-referenced in an earlier round (confirmed no new material beyond what
  `collector_enhanced.py` already synthesized from them)

---

## 1. Rebrand — NovaBot → Neko

| Item | Before | After | Where |
|---|---|---|---|
| Display name | "NovaBot" (hardcoded) | "Neko" (config-driven, `settings.bot_display_name`) | `bot/identity.py`, `bot/config.py` |
| Display username | none | "Nekooooooobot" (`settings.bot_display_username`) | same |
| Owner display name | none | "Star" (`settings.bot_owner_name`) — cosmetic only, distinct from `settings.owner_id` which still does real permission checks | same |
| Historical metadata | — | `BOT_HISTORY` dict in `bot/identity.py`, exact structure requested | `bot/identity.py` |
| Personality skin | `nanora` (self-identifies as "Nanora") | `neko` (self-identifies as "Neko") | `data/personality/skins/neko.json` (renamed from `nanora.json`), all Python defaults updated to match |

**User-facing strings replaced** (verified via full-codebase grep before and
after — nothing user-facing missed):

| File | String | Fixed via |
|---|---|---|
| `bot/plugins/start.py` | 4 headers ("NovaBot — moderation...", "NovaBot Control Panel", "NovaBot Help", "NovaBot Settings") | `bot_name()` |
| `bot/plugins/ai.py` | AI system prompt ("You are NovaBot AI...") + `/ai` reply header | `bot_name()` |
| `bot/plugins/fonts.py` | Font module header | `bot_name()` |
| `bot/plugins/music.py` | Audio file "performer" tag (visible in Telegram's player UI) | `bot_name()` |
| `bot/middleware/access_control.py` | Maintenance-mode message | `bot_name()` |
| `bot/plugins/access_control.py` | Blacklist message, block message | `bot_name()` |

**Deliberately NOT changed** (internal, per "don't blindly global-replace" —
classified below):

| Occurrence | Classification | Reasoning |
|---|---|---|
| Python package name (`bot`), `novabot.db`, Docker image name, `.env` variable names | Internal code identifier | Renaming breaks every existing deployment's imports, database file, and `.env` for zero user-facing benefit |
| Module docstrings/comments mentioning "NovaBot" (`config.py`, `database.py`, `access_control.py`, `collector.py`, `group_mgmt.py`, etc.) | Comments/reference | Developer-facing only; never rendered to a Telegram user |
| `/nnr_*` personality command prefix, `personality_*` config field names, `PersonalityLayer` class name | Internal code identifier | These are namespacing/abbreviation choices (short for the original "Nanora" codename), not identity a user reads — a user never sees the literal string "nnr" unless they specifically inspect command names; the *responses* those commands produce all say "Neko", which is what matters |
| README.md's historical narrative (YukkiMusicBot round, AnonXMusic round, etc.) | Historical documentation | Preserved as project history, matching this file's own existing convention |

**Not done, flagged rather than silently skipped:** `/nnr_*` command aliases
under a `/neko_*` name were considered and deliberately not added — see
§6 for reasoning.

---

## 2. Force-Subscribe

New feature, `@TheNexusX`, fully built and tested — no prior implementation
to compare against.

| Requirement | Status | Where |
|---|---|---|
| Correct membership check | ✅ | `bot/middleware/force_sub.py:_is_subscribed` |
| Already-authorized members pass through | ✅ | sudo/owner bypass via `settings.is_admin_id` |
| Clear join button/link | ✅ | `_join_keyboard()` — direct `t.me/<channel>` link, no extra bot permissions needed |
| "Check Again" button | ✅ | `force_sub_check_callback`, its own `CallbackQueryHandler` — never blocked by the same middleware it's checking, since PTB routes `CallbackQuery` updates separately from `Message` updates |
| Handles users who haven't joined | ✅ | Blocks + prompts with both buttons |
| Graceful Telegram API error handling | ✅ | Fails **open** (lets the user through) on any `TelegramError` or unexpected exception — a misconfigured channel can't lock out the whole bot; verified this is a deliberate, documented choice in the module docstring |
| Doesn't break private/group/channel contexts | ✅ | Skips `chat.type == "channel"`; DMs and groups both gated |
| Avoids unnecessary repeated API calls | ✅ | Per-user cache, `FORCE_SUB_CACHE_SECONDS` (default 300s) |
| Admin/owner bypass | ✅ | Bot-wide sudo only (not per-chat admins — see reasoning in code comment: a random chat admin of an unrelated group has no special relationship to this bot-wide gate) |
| Integrates cleanly with existing middleware | ✅ | Same handler group as `access_control_middleware` (-4), registered immediately after — bans/blacklist/maintenance still take priority, so a banned user never even sees a join prompt |
| Doesn't block the command needed to verify | ✅ | The check-again button is a callback query, a fundamentally different PTB update type from the `MessageHandler`-based gate — architecturally can't block itself |

Verified: full `Application` builds successfully with this registered;
config loads `force_sub_channel: TheNexusX` from `.env.example` with no
other configuration needed.

---

## 3. Collector — Full Integration

### 3a. Command-level union (nothing dropped)

Every command from both `novabot-with-nanora-v3`'s `collector.py` and
`collector_enhanced.py` is present in the rewritten file — verified by
diffing both files' handler-registration lists against the final one.

| Command | In v3? | In collector_enhanced.py? | Integrated | Notes |
|---|---|---|---|---|
| `/spawn` | ✅ | ✅ | ✅ | unchanged behavior |
| `/grab` | ✅ | ✅ | ✅ | **merged**: v3's proven no-await/lock-free safety + enhanced's per-chat `asyncio.Lock` (defense in depth) + enhanced's partial-name-match (4+ char substring) |
| `/collection` | ✅ | ✅ | ✅ | unchanged behavior |
| `/cview` | ❌ | ✅ | ✅ | new, from enhanced |
| `/crandom` | ❌ | ✅ | ✅ | new, from enhanced |
| `/characters` | ✅ | ✅ | ✅ | sort order changed to rarity-descending (enhanced's convention — see §3c) |
| `/owners` | ❌ | ✅ | ✅ | new, from enhanced; **bug fixed before integrating** (see §3b) |
| `/fav` | ✅ | ✅ | ✅ | v3's version already correctly cleared prior favorites — verified, not a bug, enhanced does the same thing |
| `/smelt` | ❌ | ✅ | ✅ | new, from enhanced |
| `/myprofile` | ✅ | ✅ | ✅ | v3's fields + enhanced's per-series completion tracking merged |
| `/cstats` | ❌ | ✅ | ✅ | new, from enhanced |
| `/cprivacy` | ❌ | ✅ | ✅ | new, from enhanced |
| `/topcatchers` | ✅ | ✅ | ✅ | **bug fixed** (see §3b) |
| `/chelp` | ❌ | ✅ | ✅ | new, from enhanced |
| `/trade` | ✅ | ✅ | ✅ | v3's version kept (enhanced's was materially equivalent) |
| `/gift` | ✅ | ✅ | ✅ | v3's version kept, extended with the User-row fix (§3b) |
| `/giveany` | ❌ | ✅ | ✅ | new, from enhanced; gating changed (see §3d) |
| `/takeany` | ❌ | ✅ | ✅ | new, from enhanced |
| `/transferall` | ❌ | ✅ | ✅ | new, from enhanced; confirmation-button flow preserved |
| `/giftall` | ❌ | ✅ | ✅ | new, from enhanced; confirmation-button flow preserved |
| `/upload` | ✅ | ✅ | ✅ | **rewritten**: stable `file_id` storage instead of `file_path` (§3b), duplicate-name check, rarity now required not defaulted, dual registration (explicit `/upload` command + caption-only path for mods) |
| `/delchar` | ✅ | ✅ | ✅ | gating changed (see §3d) |
| `/addmod` | ✅ | ✅ | ✅ | gating changed (see §3d) |
| `/removemod` | ✅ | ✅ | ✅ | gating changed (see §3d) |
| `/mods` | ✅ | ✅ | ✅ | unchanged |
| `/setspawnrate` | ✅ | ✅ | ✅ | **kept v3's `@admin_only` gate** — deliberately not changed, see §3d |
| `/giveaway` | ✅ | ✅ | ✅ | gating changed (§3d); character-giveaway variant added (`/giveaway char <id> <minutes>`) |
| `/endgiveaway` | ✅ | ✅ | ✅ | gating changed (§3d) |
| `/claim` | ✅ | ✅ | ✅ | extended to handle character-prize giveaways, not just coins |
| Inline query | ❌ | ✅ | ✅ | **rewritten** — enhanced's own version had a bug (§3b) |
| Ambient spawn listener | ✅ | ✅ | ✅ | unchanged |
| Periodic spawn job | ❌ | ✅ | ✅ | new, from enhanced |

### 3b. Bugs found and fixed

1. **`/topcatchers` and `/owners` silently excluded users without a `User`
   row.** Both use `INNER JOIN ... User`. `grab_cmd` never created a `User`
   row, so a user whose only interaction with the bot was ever catching a
   character was invisible on both leaderboards. **Reproduced the bug
   empirically, then fixed it** (`_ensure_user()` helper, called from
   `grab_cmd`, `gift_cmd`, `giveany_cmd`, and the `giftall` confirm path) and
   proved the fix with a real join query in `tests/test_collector.py`.

2. **`upload_cmd` stored a fetched `file_path` instead of the stable
   `file_id`.** Telegram's Bot API only guarantees a `file_path`-derived URL
   for a short window ("guaranteed... valid for at least 1 hour" per Bot API
   docs) — images uploaded today could silently break weeks later. Now
   stores `photo.file_id` directly, which stays valid indefinitely for
   re-sending.

3. **Fixing #2 broke inline query results** — found *while integrating*
   `collector_enhanced.py`'s own inline query code, before it was adopted.
   `InlineQueryResultPhoto`/`Video` require an actual fetchable URL in
   `photo_url`/`video_url`; passing a `file_id` there (which is what
   `collector_enhanced.py` did, since it also stored `file_id`) silently
   fails to render. Fixed by using `InlineQueryResultCachedPhoto`/
   `CachedVideo`, which take `photo_file_id`/`video_file_id` instead —
   verified with a test asserting the result types are never the URL-based
   variants.

### 3c. Rarity now actually affects spawn odds

v3's `_pick_character` was a uniform random pick over every character
regardless of rarity — "legendary"/"divine" were flavor text with zero
gameplay effect. Adopted `collector_enhanced.py`'s weighted-tier approach
(`RARITY_WEIGHTS`, dampened by how many characters exist in each tier).
Measured over 600 picks against a small seeded pool: common (10 characters,
weight 50 each) landed the large majority of picks, divine (1 character,
weight 1) landed a handful — rare but not impossible. Also adopted the
rarity-descending sort order (`/characters`, `/collection` now show
rarest-first) from `collector_enhanced.py`, a UX convention change from
v3's ascending order.

### 3d. Permission gating — changed, with reasoning

Both v3 and `collector_enhanced.py` were compared command-by-command against
this file's own stated design principle: *"a small collector moderator role
that's intentionally separate from being an admin of any one chat."* Two
real, distinct problems were found in **v3** (not introduced by this round):

- `/addmod`, `/removemod`, `/upload`, `/delchar` were gated with
  `@admin_only` (this-chat's-admins) **in addition to** an inline
  collector-mod check. `@admin_only` denies all private chats outright, so a
  legitimate collector mod curating via DM — the natural workflow for a
  bot-wide role — was blocked before the intended check even ran. Worse, any
  admin of *any* of potentially many mutually-unrelated group chats could
  unilaterally grant bot-wide collector-mod status to themselves or anyone
  else, since `@admin_only` alone was sufficient to reach `/addmod`.
- `/giveaway`, `/endgiveaway` were `@admin_only` with **no** collector-mod
  check and **no cap** on the coin amount — any admin of any chat could mint
  unlimited coins via giveaways.

**Fix:** `/addmod`/`/removemod` now require sudo (`_is_super_admin`) —
appointing a bot-wide trusted role should be stricter than holding it.
`/upload`/`/delchar`/`/giveaway`/`/endgiveaway`/`/giveany`/`/takeany` require
collector-mod status directly, with no `@admin_only` wrapper. `/transferall`
and `/giftall` (whole-collection operations) require sudo specifically, plus
a confirmation button, since their blast radius is larger still.

**`/setspawnrate` deliberately kept `@admin_only`** — unlike the above, this
is a genuinely per-chat setting (how often *this chat* spawns characters),
so gating it on that chat's own admins is the correct, consistent choice,
matching how every other per-chat setting in NovaBot works. This was a
considered decision, not an oversight — verified with a test that would fail
if `/upload` were ever re-gated with `@admin_only`.

### 3e. Nanora v2.4 personality-layer work — evaluated, mostly not adopted

`neko_v2_4` independently found the same four bugs already fixed in v3, but
with weaker implementations — verified empirically, not just by reading:

| v2.4 claim | Verification method | Result |
|---|---|---|
| "Atomic" `increment_and_check` via `SELECT...FOR UPDATE` | Load-tested 15 concurrent first-time calls | **14 of 15 raised an unhandled `IntegrityError`.** v3's `UPDATE...RETURNING` approach handles all 15 correctly (already verified in a prior round) — kept v3's version |
| `register()` wires `on_startup` via `app.post_init = _post_init` | Traced PTB's `Application.post_init` semantics | **This overwrites, not appends** — would silently destroy every other plugin's startup task (shop seeding, job re-arming). Kept v3's explicit task-list approach |
| Runtime DB-column patcher (`ensure_personality_columns()`) | Reproduced with a real SQLite table that already existed before the patch ran | **Never actually alters an existing table** — `Base.metadata.create_all()` only creates missing tables, doesn't migrate existing ones. Fails on exactly the "already-deployed database" case it claims to fix. Kept v3's direct-merge-into-real-models approach, which uses this project's own working `_auto_migrate()` |
| LLM hook: direct Gemini-only `httpx` call | Read both implementations | Duplicates logic v3's `generate_response()` already handles for five providers (Gemini, Groq, OpenRouter, OpenAI, Anthropic); also has an inconsistent precondition check. Not adopted |

**One idea adopted, implemented better than the source:** grounding the LLM
persona rewrite in a base identity so it doesn't drift generic when custom
instructions are minimal. v2.4 hardcoded one personality description
regardless of active skin — would have been wrong for any chat using a
non-default skin. Implemented instead using each skin's own `description`
field (already present in the skin JSON schema, previously unused, verified
via grep before use) — dynamically follows whichever skin is actually
active.

---

## 4. Mikobot Feature Review

Corrected a real regex-escaping bug in the initial pass (`\|` used inside a
`grep -E` extended-regex pattern, which is BRE-style syntax and silently
broke the alternation) and **re-verified every finding from scratch** before
concluding anything was absent.

| Mikobot feature | Initially assumed | Actually found | Integrated |
|---|---|---|---|
| `afk.py` | possibly missing | **Already exists**, fully — `bot/plugins/fun.py:afk_cmd` (set/clear/announce-on-mention) | No — genuinely redundant |
| `karma.py` | possibly missing | **Already exists**, fully — `bot/plugins/fun.py:karma_cmd` (+leaderboard) | No — genuinely redundant |
| `disable.py` | assumed missing (regex bug) | **Already exists**, fully — `Chat.disabled_commands`, enforced in `bot/middleware/access_control.py`, commands in `group_mgmt.py` | No — genuinely redundant |
| `zombies.py` (deleted-account cleanup) | not checked initially | **Already exists** — referenced directly in `start.py`'s help text (`/zombies /rmzombies`) | No — genuinely redundant |
| `sangmata.py` (name-change history) | assumed missing | **Confirmed genuinely absent** | **✅ Integrated** — `bot/plugins/name_history.py`, `/names` command, tested |
| `connection.py` (manage a group from DM) | assumed missing | **Confirmed genuinely absent** | ✅ Implemented in v2.12 (`bot/plugins/connection.py`; scope in docs/MODERATION.md) — was deferred, see §6 |
| `approve.py` (exempt users from locks/antiflood) | assumed missing | **Confirmed genuinely absent** | ✅ Implemented in v2.12 (`bot/plugins/approvals.py`, `modtools.is_exempt`) — was deferred, see §6 |
| Everything else (52 plugins total: `admin.py`, `ban.py`, `mute.py`, `warns.py`, `welcome.py`, `notes.py`, `blacklist.py`, `feds.py`, `flood.py`, `fsub.py`, `gban.py`, `purge.py`, `rules.py`, `locks.py`, `speedtest.py`, `pokedex.py`, `telegraph.py`, `quotely.py`, `reverse.py`, `instadl.py`, `stickers.py`, `couple.py`, `search.py`, `sports.py`, `tr.py`, `anime.py`, `imagegen.py`, `whispers.py`, `karma.py` — see §6) | not individually verified | — | ❌ Not individually audited — see §6 |

Security scan (before any integration work): checked for `eval`/`exec`,
`subprocess`, `pickle.loads`, session-string patterns across the whole
Mikobot codebase. Found: `ast.literal_eval` (safe, standard) in several DB
modules; `asyncio.create_subprocess_exec` with argument lists (not shell
strings, no injection risk) for image/sticker conversion; a `pickle.loads`
helper in `blacklistdb.py` that's defined but never actually called anywhere
in that file (dead code, not a live vulnerability, and not something this
round ports regardless). Nothing concerning found.

---

## 5. HydraMusic Feature Review

Structural review only — plugin/command list cross-referenced against
NovaBot's existing music engine, which has already absorbed two prior
music-bot codebases (YukkiMusicBot, AnonXMusic, per earlier rounds
documented in README.md). Security-scanned with the same method as Mikobot:
found only the standard self-update (`os.system("git pull")` against an
operator-configured remote, not user input) and owner-gated debug-eval
patterns common across this entire bot family — nothing concerning.

No standout missing feature was identified at the structural level — the
plugin surface (play/pause/skip/loop/seek/shuffle/queue/playlist/
live-voice-chat/channel-play/autoleave/auth/blacklist-chat/video-limit/
toptracks/inline/speedtest) overlaps heavily with what's already been
absorbed. **This was not a feature-by-feature code-level comparison** the
way Mikobot's AFK/karma/disable claims were individually verified — see §6
for why, and what would be needed to do this properly.

---

## 6. Explicitly Deferred — What, Why, and What Exists Instead

> **Update (v2.12):** the two largest entries below, `connection.py` and `approve.py`, are now implemented. `/connect` works only for the commands that
> were rebuilt on the shared moderation core (listed in docs/MODERATION.md) - exactly the half-implemented risk noted below is avoided by making every
> such command go through one resolver, and by leaving the rest (commands that need a replied-to message, welcome/goodbye text) explicitly group-only.
> The text below is kept as the original record.

Per the "no silent omissions" requirement, everything below was considered
and not integrated this round, with reasoning:

- **Mikobot `connection.py`** (manage a group's settings from DM without
  being active in the group). What it is: lets an admin run
  `/connect <chat>` in DM, after which subsequent admin commands operate on
  that chat instead of requiring them to type in the group itself. Why not
  integrated: doing this correctly requires touching the internals of every
  existing admin command (locks, warns, welcome, automod settings, etc.) to
  check "is there an active DM connection redirecting this" — a
  half-implemented version (working for only some commands) would be more
  confusing than not having it, and touching that many previously-hardened
  command handlers without individually re-reviewing each one risks
  regressing already-verified moderation behavior. No equivalent exists in
  NovaBot today; this is a genuine gap, not a redundancy.

- **Mikobot `approve.py`** (exempt specific users from locks/antiflood
  restrictions). What it is: a per-chat allowlist that skips automated
  enforcement for trusted users. Why not integrated: requires adding an
  "unless approved" branch inside `bot/middleware/antiflood.py` and
  `bot/middleware/antispam.py`, neither of which was read closely enough
  during this round to edit with the same confidence as the files that were
  (`access_control.py`, `force_sub.py`, `collector.py`). Editing enforcement
  logic without that review risks a silent regression in working
  anti-abuse code. No equivalent exists in NovaBot today; this is a genuine
  gap, not a redundancy.

- **Mikobot's remaining ~46 plugins** (ban/mute/warn/welcome/notes/
  blacklist/feds/flood/gban/purge/rules/locks/speedtest/pokedex/telegraph/
  quotely/reverse/instadl/stickers/couple/search/sports/tr/anime/imagegen/
  whispers/fsub, etc.). What exists instead: NovaBot's `group_mgmt.py`,
  `automod.py`, `federations.py`, `fun.py`, and `anime.py` already cover a
  large, overlapping set of this ground under different names/file
  boundaries (confirmed for AFK, karma, disable, zombies — see §4's table).
  Why not individually verified: a genuine one-by-one code-level equivalence
  check across 46 more plugins, each requiring the same rigor applied to
  AFK/karma (read both implementations, compare actual behavior, not just
  names) is a large, multi-session undertaking on its own. What would be
  needed to close this properly: the same file-by-file comparison already
  done for the 4 features in §4, extended to the rest of the list — a
  reasonable next round's scope, not something safely rushed alongside
  everything else in this one.

- **HydraMusic — full command-level equivalence table.** What exists
  instead: NovaBot's `music.py`/`music_live.py`/`playlists.py`, built up
  across two prior rounds absorbing YukkiMusicBot and AnonXMusic. Why not
  done: the same reasoning as Mikobot's remaining plugins — a genuine
  code-level comparison (not a structural/name-based one) across another
  ~30 files is more than this round's remaining scope could respect without
  shortcutting the rigor applied elsewhere.

- **`/nnr_*` → `/neko_*` command aliases.** Considered during the rebrand
  work. What exists instead: the personality layer's actual *responses* all
  say "Neko" now (skin rename, `bot_name()` everywhere applicable); only the
  command-prefix abbreviation itself still reads "nnr". Why not done: lower
  priority than the items above given the same time budget, and aliasing
  every `/nnr_*` command plus their callback-data prefixes touches several
  more call sites in `bot/plugins/personality.py` for a purely cosmetic
  command-name concern that most users interact with via buttons, not by
  typing the raw command. A reasonable candidate for a future round if
  wanted.

---

## 7. Testing Performed

Actually run, not just described:

- `tests/test_personality.py` — 30 tests, all passing (pre-existing +
  additions from the prior round).
- `tests/test_collector.py` — **12 new tests**, all passing: weighted rarity
  distribution, explicit rarity filtering, the User-row/topcatchers fix (both
  the helper directly and an end-to-end `grab_cmd` call), a 12-way concurrent
  grab race (exactly one winner, exactly one DB row), the sudo-vs-collector-mod
  gating split (both allow and reject paths, both commands), the
  favorite-is-exclusive behavior, and the inline-query Cached-type fix.
- `tests/test_name_history.py` — **5 new tests**, all passing: first-sighting
  seeding, real change detection across multiple fields, DB-hit throttling,
  private-chat/bot exclusion, and the `/names` command's rendering.
- Full-repository Python syntax scan (`ast.parse` on every `.py` file) — 0
  errors, run after every batch of edits, not just once at the end.
- Full `Application` object built end-to-end via
  `bot.core.bot.create_application()` with every new/changed piece wired in
  (identity, force-sub, name-history, rewritten collector) — succeeds with
  no errors.
- `Settings()` loaded from the actual shipped `.env.example` with only
  `BOT_TOKEN`/`OWNER_ID` set (matching the documented "only these two are
  required" claim) — confirmed `bot_display_name` resolves to "Neko" and
  `force_sub_channel` to "TheNexusX" with zero extra configuration.
- `init_db()` run against a real SQLite file to confirm the new columns
  (`Character`/`CollectorModerator`/`Giveaway` already existed;
  `User.name_history` is new) auto-migrate cleanly.

**Not tested — explicitly, not silently:** anything requiring a live
Telegram connection or real bot token (actual message delivery, actual
`getChatMember` calls against a real channel, actual inline query rendering
in a Telegram client, actual voice-chat audio). This has been true of every
round of this project per README.md's own disclosed limitations, and remains
true here — nothing in this environment can open a real connection to
Telegram's servers.

---

## Dataset Integration (this synthesis round)

- Full conversation dataset (1093 Q&A pairs) cleaned of old branding
  (Nanora → Neko, Sari → Star, @nanorabot → @Nekooooooobot) and stored at
  `data/personality/dataset/full_cleaned.jsonl`.
- Curated few-shot bank (180 high-signal examples, balanced across
  Identity, Humor, Programming, Philosophy, Emotional Support, Anime,
  Architecture, Casual) at `data/personality/dataset/fewshot_bank.json`.
- Runtime `ExampleBank` (`bot/personality/examples.py`) provides category
  and keyword-overlap retrieval + prompt formatting.
- When `PERSONALITY_LLM_ENABLED=true` and `PERSONALITY_FEWSHOT_ENABLED=true`,
  relevant examples are injected into the persona rewrite system prompt
  to lock tone and reduce drift.
- Regression tests in `tests/test_personality.py` assert bank load,
  retrieval quality, and absence of old branding.

## Error Handling Layer

- New `bot/core/errors.py` with typed exceptions (`NekoError`,
  `PermissionError`, `ValidationError`, `RateLimitError`, …) and a
  `@safe_handler` decorator that logs full context and replies with
  branded, non-leaking messages.
- Recommended for progressive adoption on all handlers.

## Config

- `CONFIG_VERSION=4`
- `PERSONALITY_FEWSHOT_ENABLED` / `PERSONALITY_FEWSHOT_MAX`

---

## Maximum Enhancement Round

### New modules
- `bot/utils/cache.py` — Redis-backed cache with in-memory LRU+TTL fallback
- `bot/utils/validation.py` — shared input validation helpers
- `bot/utils/retry.py` — tenacity-based retry decorator for transient failures
- `bot/core/errors.py` — typed errors + `@safe_handler` (from previous round, now wired globally)
- `bot/plugins/health.py` — `/health` + `/status` owner/admin diagnostics

### Core improvements
- Global Application error handler registered in `create_application()`
- `@safe_handler` on start + personality public handlers
- Few-shot bank preloaded at personality startup
- Config version 4 + few-shot settings
- Optional Redis fully integrated via unified Cache API
- Expanded unit tests (cache, validation, errors, example bank, identity)
- `tenacity` added to requirements for resilient retries

### Dataset
- 1093 cleaned conversation pairs + 180 curated few-shot examples
- Injected into LLM persona rewrite path when enabled

---

## Synthesis Round — MAXIMUM Consolidated, Verified, and Corrected

Inputs: `novabot-neko-v4.zip`, `production-ready`, `v5-enhanced`, and
`MAXIMUM` (each a strict superset of the last, confirmed by diffing file
lists and content byte-for-byte — nothing from any of them was lost),
plus `github-ready`, `neko_v2_4.zip`, and the base `novabot` archives
(confirmed strict *subsets* of v4, contributing nothing not already
carried forward — not re-merged). `MAXIMUM` was the base. Everything
below was found by actually running the code, not just reading it, per
this project's own standing rule.

### Fixed — would have broken on arrival
- **Personality plugin failed to load, silently, on every startup.**
  An import had been inserted before the module docstring, which pushed
  `from __future__ import annotations` past the position Python requires
  it — a `SyntaxError`. The plugin loader catches load errors per-plugin
  and logs them rather than crashing the bot, so this shipped without
  ever surfacing: the bot's flagship feature was completely inert.
  Confirmed fixed by actually building the `Application` and checking
  all 20 plugins loaded (they now do — 0 failures, 249 handlers).
- **`fun.py` crashed on AFK, wish, and dictionary lookups.** `escape_html`
  was called in 7 places but never imported — a guaranteed `NameError`
  on each of those commands. One-line import fix.
- **`try_spend()`/`add_coins()` (economy_service.py) claimed to be
  atomic in their own docstrings and weren't** — plain Python
  read-modify-write, the exact anti-pattern this project's own
  `learnings-and-workflow.md` already flags. Under concurrent requests
  this could drive a balance negative (an exploitable double-spend).
  `pay_cmd` had the same pattern inline, independently of these helpers,
  despite the module docstring claiming it went through them. Rewrote
  both as single conditional SQL UPDATEs, and added a `transfer_coins()`
  helper that does the debit+credit as one transaction (an earlier draft
  of this fix used two separately-committed calls, which traded the race
  condition for a partial-failure risk — coins deducted but never
  credited if the process died in between; caught before shipping).
  New `tests/test_economy.py` load-tests this for real: 50 concurrent
  `try_spend` calls against a 100-coin balance, asserting exactly 10
  succeed; concurrent bidirectional transfers asserted to conserve total
  coins. This is the verification this project's own principles call
  for — a docstring claiming atomicity is not evidence of it.
- **Leftover pre-rebrand content in the personality dataset.** One
  entry (id 152, "favorite anime character") still had the old
  creator-lore line ("Saria Nakano... she's the one who coded me"),
  and `category_index.json` still had a category keyed
  `"Nanora Personality Architecture"` after `full_cleaned.jsonl` had
  already been correctly renamed to `"Neko Personality Architecture"` —
  two derived artifacts had drifted out of sync with the source. Fixed
  both; `test_full_dataset_cleaned` now passes.
- **A test used a Python asyncio pattern (`get_event_loop().
  run_until_complete()`) that no longer works reliably** on this
  project's target Python version. Switched to `asyncio.run()`.
- Minor: a mutable-default-argument footgun on `PersonalityLayer.
  __init__` (harmless today only because nothing currently mutates the
  shared default in place); a redundant shadowed re-import in
  `admin.py`; a handful of unused imports/variables.
- **A flaky test, caught by running the full suite fresh from the
  packaged zip, not just in the working copy.** `test_collector.py`'s
  rarity-distribution test sampled only 600 random spawns; with this
  fixture set's effective weighting, "divine" lands roughly 1 draw in
  400-500, leaving a double-digit percent chance of zero hits by pure
  chance — not a code bug, an undersized sample. Raised to 3000 draws
  (below ~0.2% false-failure chance; confirmed with 6 consecutive
  passing runs) rather than touching the spawning logic itself.

### `bot/utils/retry.py` — was dead code, now actually used
The previous round's own documentation claimed tenacity-based retries
were wired in; empirically, `retry.py` was imported by nothing, and
`tenacity` wasn't even in `requirements.txt` (the requirements file had
a comment explicitly noting it *wasn't* used — which had gone stale the
moment `retry.py` started importing it). Fixed the dependency and gave
it real call sites: the three outbound HTTP calls in
`bot/services/platforms.py` (Spotify/Apple Music/Resso resolution), and
— higher-value — `init_db()`'s initial connection, which now retries
with backoff instead of crashing the bot outright if the database
container isn't accepting connections yet on first boot (the exact
"crash-on-startup" failure class this project treats as highest
priority). Verified with a standalone check that the decorator actually
retries (fails twice, succeeds on the third attempt) rather than just
trusting it compiles.

### SQLite reliability (surfaced by writing the concurrency tests)
Load-testing the economy fix surfaced two separate real SQLite issues
under genuine concurrent writes: `"database is locked"` errors (SQLite
serializes writers and doesn't queue by default), and a
`"bound to a different event loop"` error from a pooled aiosqlite
connection outliving the loop that created it. Fixed by enabling WAL
mode + a busy timeout, and switching the SQLite engine to `NullPool`
(open a fresh connection per checkout — cheap for a local file, unlike
a networked Postgres, and it sidesteps the cross-loop pooling issue
entirely). Postgres, the documented production target, was unaffected
by either problem and is unchanged by this fix.

### New: reaction GIFs (`bot/plugins/reactions.py`)
The Mikobot Feature Review (§4 above) audited AFK/karma/disable/zombies/
sangmata but didn't cover this one. The uploaded `nekomode.py` — the
same Mikobot/Team-ProjectCodeX lineage §4 already draws on — is a
Telethon + MongoDB plugin for anime-style reaction GIFs (`/hug`, `/pat`,
`/slap`, and 27 others via waifu.pics' SFW endpoint). Confirmed genuinely
absent from this codebase, and ported rather than dropped in, since
Telethon's event-decorator style and a Mongo toggle collection don't
exist in this bot's python-telegram-bot + SQLAlchemy architecture:
- Toggle renamed `/nekomode` → `/reactions` — this bot is itself named
  Neko and already has an unrelated `/personality` toggle; a second,
  different feature also called "nekomode" sitting next to it would be
  a real, everyday point of confusion, not just a naming nitpick.
- Per-chat state moved to `Chat.reactions_enabled` (this project's
  existing settings table, auto-migrated in) instead of a new Mongo
  collection; gated behind a matching global `ENABLE_REACTIONS` flag,
  consistent with every other plugin's `settings.enable_*` convention.
- Dropped rather than carried over: `/wallpaper` (tied to a different,
  unintegrated image API — not worth a new dependency for one command),
  and a stray `"hTojiy"` entry in the source's action list that was
  already unreachable there too (mixed-case, can never match Telegram's
  lowercased commands). Also dropped `/avatar` and `/feed`, which the
  source's own `__help__` text advertised but its code never actually
  registered — the port's docs now match its implementation exactly.
- Genuine improvement, not just a port: actions used as a reply or with
  a target mention now render as "Alice hugs Bob!" — completing what
  the source's own help text already promised ("hug: get hugged or hug
  a user") but its implementation never did; it only ever sent the raw
  file with no target logic at all.
- Outbound requests go through this project's established httpx +
  `with_retry` pattern instead of adding the source's `nekos` package
  as a new synchronous dependency for a single command.
- `tests/test_reactions.py`: caption-building logic, the action-list
  split, and the per-chat toggle (including default-off and
  bad-argument handling).

### Rebrand cleanup
`dashboard/main.py`'s page title, `<title>`, and `<h1>` still said
"NovaBot" — the one place the earlier rebrand pass missed something
actually user (admin) facing; `.env.example`'s header had the same
leftover. Both fixed. (Internal code comments elsewhere that mention
"NovaBot" as the bot's former engineering name — e.g. "the database
everything else in NovaBot already uses" — were left alone: they're
developer-facing history, not user-facing branding, and rewriting ~15
files of comments for a purely cosmetic sweep wasn't worth the risk of
mis-editing something unrelated in the process.)

### Known, deliberately not addressed this round
- **`datetime.utcnow()`** is used 44 times across 13 files. It's
  deprecated (not yet removed) on the Python version this project
  targets. A mechanical find-replace to `datetime.now(UTC)` was
  considered and rejected: it produces a timezone-*aware* value where
  the rest of the code (and the DB columns storing it) expect naive
  values, and fixing that properly means checking every comparison
  site, not just every call site — a bigger, separate piece of work.
- **`bot/utils/cache.py`** is well-built and tested, but is currently
  only exercised by `/health`'s status check — not wired into anything
  that would meaningfully benefit from caching (e.g. personality
  response caching, rate-limit counters). Left as available
  infrastructure rather than forced into a speculative integration.
- **~30 remaining `except Exception: pass` sites** (mostly `S110` on a
  linter pass) were sampled, not all individually rewritten. The
  security-relevant ones — `access_control.py`'s ban enforcement,
  `admin.py`'s bulk mod actions, `federations.py` — were read in full:
  all are best-effort per-item cleanup in bulk loops *after* the
  authoritative DB write already committed, not fail-open permission
  checks. The remainder follow the same shape by inspection but weren't
  each individually traced.
- **`bot/utils/validation.py`** is tested but, like `retry.py` was,
  not yet used by any plugin's actual command handlers — it was
  introduced alongside `@safe_handler` as a pattern piloted only in
  `personality.py`. Rolling it out to the other 19 plugins is a much
  larger, separate effort than this round's scope.

### Testing performed
`python -m compileall` clean across `bot/`, `dashboard/`, `tests/`.
`ruff --select=F,E9` clean. Full `Application` build verified to load
all 20 plugins with 0 failures (249 → 251 handlers after adding
reactions.py). Full test suite: 76 passed, 0 failed (was 54/56 on
`MAXIMUM` as uploaded), including two new files
(`test_economy.py`, `test_reactions.py`) and two pre-existing failures
fixed in place.

---

## Synthesis Round — Personality Pack, Broader `@safe_handler`, Rate Limiting

Four more uploads this round: `Neko-bot-ULTIMATE`, `ULTIMATE-PACKED`,
`v5-MAXIMUM-LAYERS`, and `v6-COMPLETE`, plus a standalone
`personality_pack_full.zip`. First finding: none of these four build on
the *previous* round's fixes — they're independent branches forked from
the old `MAXIMUM`, before the personality-plugin syntax error, the
`fun.py` crash, or the economy race condition were fixed. Confirmed this
concretely (all three bugs are still present, unfixed, in every one of
them) before deciding how to treat them: kept last round's verified build
as the base and cherry-picked new value from `v6-COMPLETE` (a strict
superset of the other two) rather than restarting from any of them.

Also uploaded: a small unrelated zip containing an HTML homework
assignment, unconnected to this project. Left out of the build; flagged
to the user rather than silently dropped or forced in.

### Adopted: a real personality skins/presets library
- 22 new skins (`data/personality/skins/`) and 14 new presets
  (`data/personality/presets/`) — pirate, tsundere, yandere, cyberpunk,
  and more. Verified both slot into infrastructure that already existed
  (`SkinLoader` and `PresetLoader` both glob `*.json` from their
  directory — no code changes needed to make new files selectable).
- Caught a mistake of our own mid-round: a first attempt blanket-copied
  the *entire* new presets folder, silently overwriting 10 of the
  original presets with this branch's (older, pre-fix) versions of the
  same names. A test (`test_medieval_preset_with_themed_metaphors`)
  caught it — that preset lost a `themed_metaphors` field the test
  depended on. Restored the 10 originals; kept only the 14 genuinely new
  names, which don't collide with the originals at all.
- The personality dataset grew from 1093 → 3923 entries so each new skin
  has its own grounding examples, and `bot/personality/examples.py` now
  supports per-skin retrieval (`get_relevant(..., personality=X)`,
  `get_by_personality(X)`), with a defined fallback to the "neko" pool
  for any skin without curated examples yet. Wired this into
  `personality.py`'s LLM-rewrite few-shot injection, which previously
  always grounded on generic examples regardless of the active skin.
- The *same* leftover pre-rebrand line ("Saria Nakano... coded me") that
  was fixed in the previous round's dataset had reappeared in this
  branch's independently-expanded copy — same id (152), same fix
  applied again, this time in the 3923-line file.
- `nanora.json` in `data/personality/skins/` was previously an empty
  `{}` stub (present so `SkinLoader` wouldn't need special-casing it,
  but broken if anyone actually selected it) — replaced with a real,
  explicitly-labeled "legacy alias of neko" skin from this round's
  upload, so `/cpersonality skin nanora` now actually works instead of
  silently degrading to an empty config.
- Not shipped: the `personality_pack/generator.py` templating tool and
  its supporting `personalities/` / `banks/` / `samples/` source
  directories. The tool doesn't run without those directories, and
  their content is already redundant with what's now properly
  integrated into the bot's actual `data/personality/` files — carrying
  a full duplicate copy for a tool that isn't wired to anything would be
  bulk without use. Can be added back as a dev-only tool on request.

### `@safe_handler` rolled out from 1 plugin to 15
Deferred explicitly at the end of the previous round as "a much larger,
separate effort." This round's uploads had independently done real work
on exactly this — but on the unfixed base, so it couldn't be merged
wholesale. Instead: scripted a pass that found every function this
upload had decorated with `@safe_handler`, then applied the same
decoration to the same-named functions in the current (fixed) codebase —
mirroring their targeting exactly rather than guessing which functions
"look like" handlers. 105 handlers across 14 files gained the decorator;
verified with a full compile + lint + test pass before and after, plus a
full `Application` build (still 0 plugin load failures).

Explicitly *not* carried over from the same diffs: a handful of
unrelated lines in `admin.py`, `games.py`, and `scheduling.py` that would
have reintroduced small bugs the previous round already fixed (an
unused-variable cleanup, a redundant re-import) — these three files were
on the unfixed branch too, and the diff included their old, since-fixed
versions of those specific lines alongside the genuinely new
`@safe_handler` additions.

### New: rate limiting, via `bot/utils/ratelimit.py`
This module existed in the upload but depends on `bot/utils/cache.py` —
which the previous round noted was "well-built and tested, but ... only
exercised by `/health`'s status check." This round gives it two real
call sites:
- **AI commands** (`/ai`, `/imagine`, `/summarize`, `/code`): a shared
  per-user hourly cap (default 20, `AI_RATE_LIMIT_PER_HOUR`), since all
  four hit paid provider APIs. The uploaded version only added this to
  `/ai`, and had a copy-paste bug — the rate-limit block was pasted
  twice in a row inside the same function. Wrote it once as a shared
  helper instead, and applied it consistently to all four commands, not
  just one.
- **Collector `/grab`**: a second, looser check (40 per 60s) alongside
  the existing in-memory per-process cooldown — confirmed these are
  complementary, not redundant: the existing cooldown is tight (1.5s)
  but resets on restart and wouldn't hold across multiple worker
  processes; this one goes through the shared cache, so it does.
- `/health` now also reports top metric counters (via the new
  `bot/utils/metrics.py`, previously unused) and the on-disk
  preset/skin counts.
- New `bot/utils/textutil.py`, `guards.py`, `telegram_safe.py` adopted
  as-is (small, self-contained, genuinely useful). One conflict
  resolved: `textutil.py` defined its own `escape_html` that differed
  slightly (quote-escaping) from the one already established in
  `bot/utils/helpers.py`. Rather than ship two slightly different
  functions with the same name, `textutil.safe_html_message` now
  delegates to the canonical `helpers.escape_html`.

### CI
The uploaded `.github/workflows/bot.yml` runs `python -m bot` as a
manually-triggered Action — useful as a one-off smoke test, but GitHub
Actions jobs are capped at 6 hours and aren't a substitute for the
Dockerfile-based deployment this project documents elsewhere. Relabeled
it clearly as a manual smoke test (and fixed its own leftover "NovaBot"
branding) rather than let it read as a deployment method. Added a
separate `ci.yml` that actually does what most people mean by
"CI" — compile, lint, and the full test suite on every push/PR.

### Testing performed
Full suite: **87 passed, 0 failed** (was 76 at the start of this round).
11 new tests (`test_personality_pack.py`) cover every shipped skin and
preset loading through the real loaders (not just JSON-parsing them),
the legacy-skin fallback behavior, and the rate limiter's pass/block
arithmetic — including that a cache failure fails *open* (allows the
request) rather than blocking all traffic, which matters more for a
rate limiter than for most other cache uses. `python -m compileall` and
`ruff --select=F,E9` clean across `bot/`, `dashboard/`, `tests/`. Full
`Application` build: 251 handlers, 0 plugin load failures.

---

## Debugging Round — Personality Auto-Reply "Seemed Dead"

No new uploads this round — a direct bug report instead: personality
banter wasn't replying in normal use, with commands "may or may not"
working and ambient replies "often" not. Investigated the full path
end to end (config defaults, middleware chain, handler groups, per-chat
flags, JobQueue, Telegram platform behavior) rather than assuming the
cause was in `personality.py` itself, since the report's own asymmetry
— commands vs. ambient — was a strong hint the cause was upstream of it.

### Root cause: force-subscribe, on by default, pointed at nobody's channel
`force_sub_enabled` defaulted to `True` with `force_sub_channel` defaulting
to a placeholder (`TheNexusX`) — not the operator's own channel. This
middleware runs in handler group `-4`, before every command and before
personality's handler ever sees an update, and raises
`ApplicationHandlerStop` for any user Telegram reports isn't a member of
that channel. The bot owner's account bypasses this via the existing
admin check, so testing as the owner looks fine while every other user
gets silently blocked from *everything* — the exact "commands sometimes
work, ambient often doesn't" split in the report. `.env.example`'s own
comment said "Blank channel = feature off"; the shipped default value
wasn't blank, so the code didn't match its documented intent. Confirmed
via this project's own history: a prior round explicitly tested that
config loading and `Application` construction succeeded, and explicitly
noted live-Telegram behavior was *not* tested — precisely the gap that
let this ship. Fixed: both `bot/config.py` and `.env.example` now default
force-sub fully off; an operator who wants it opts in by setting both
`FORCE_SUB_CHANNEL` (their own) and `FORCE_SUB_ENABLED=true`.

### Contributing/related findings
- **Telegram Group Privacy Mode** — a BotFather setting, not fixable in
  code, and previously undocumented anywhere in this repo. While it's
  on (the default for any new bot), Telegram never delivers ordinary
  group text to a bot at all, only commands/@mentions/replies — which by
  itself would produce the reported symptom. Added a startup log
  reminder (can't be auto-detected, so it's surfaced every boot rather
  than documented once and forgotten) and README guidance.
- `personality_default_group=False` (groups opt-in, DMs on by default)
  is deliberate, documented anti-spam behavior, not a bug — verified the
  `/personality on|off` toggle's DB write and its cache invalidation are
  both correct. Left the default alone; a real user being blocked by the
  force-sub bug before they could even run the toggle command was
  plausibly compounding the perception, though.
- `handle_message`'s `MessageHandler` had no explicit handler group,
  landing in PTB's default of group 0 — outside the deliberate 1-9
  sequence every other plugin's ambient-text listener uses specifically
  so none of them shadow each other (group_mgmt 1/2/4, fun 3, fonts 5,
  economy 6, collector 7, name_history 9). Not proven to be actively
  firing (nothing else was contesting group 0), but a latent collision
  risk and worth fixing on its own. Assigned group 8.
- Checked and ruled out: the in-memory per-chat enabled-cache (30s TTL,
  correctly invalidated by the toggle command already), every
  `access_control_middleware` stop condition (all default off/empty),
  JobQueue installation and startup (`[job-queue]` extra is correctly
  declared, `run_polling`/`run_webhook` both start it automatically),
  and `PluginManager.route()`'s fallback logic.

### Changes
- `bot/config.py`, `.env.example`: force-sub defaults fixed (above),
  with an expanded comment on the risk of this specific misconfiguration.
- `bot/plugins/personality.py`: debug-level logging at every point
  `handle_message` returns early (globally disabled, cooldown, disabled
  for this chat, over the message limit), plus logging for delayed vs.
  immediate send and a JobQueue-unavailable edge case — direct request
  from the bug report ("clear logging on skip reasons"). Explicit
  `group=8` on the ambient handler's registration.
- `bot/core/bot.py`: a startup diagnostic block — the Group Privacy Mode
  reminder, current force-sub state, and personality's DM/group
  defaults — plus a warning for the specific "enabled but no channel"
  misconfiguration.
- New `tests/test_personality_e2e_reply.py` — the actual missing piece.
  Every existing personality test exercised components in isolation
  (`PersonalityLayer.rewrite()`, the example bank, trigger matching);
  none drove `handle_message()` itself or the middleware chain in front
  of it, which is how a bot with 87 passing tests could still never
  send an ambient reply in practice. Now covers: force-sub and
  access-control not blocking a normal non-admin user under the fixed
  defaults; force-sub still correctly blocking when an operator
  deliberately enables it (the fix didn't just delete the feature); an
  enabled chat actually producing a `reply_text` call; a
  never-configured chat correctly staying silent; and the full
  middleware-then-handler chain succeeding for a realistic case.

### Testing performed
Full suite: **93 passed, 0 failed** (was 87 going into this round) — 6
new end-to-end tests. `compileall` and `ruff --select=F,E9` clean.
`Application` build confirmed: 251 handlers, group 8 holds personality's
ambient listener as intended.

### Manual steps only the operator can do
- **BotFather → Bot Settings → Group Privacy → disable** (or make the
  bot a group admin, which also disables the filter). Required for
  ambient personality replies — and AFK detection, XP tracking, the
  collector spawner, filters — to work in groups at all; cannot be set
  from bot code.
- If force-subscribe is wanted: set `FORCE_SUB_CHANNEL` to a channel the
  operator actually administers, and `FORCE_SUB_ENABLED=true`. Off by
  default as of this round.
- If personality should reply by default in every group rather than
  needing `/personality on` per chat: set `PERSONALITY_DEFAULT_GROUP=true`.

---

## Debugging Round — Personality Auto-Reply Wasn't Firing

No new uploads this round — a direct bug report: personality/banter
replies weren't firing in groups or DMs. Investigated the full path
end to end (config defaults, middleware, handler groups, per-chat
flags, JobQueue, Telegram-side privacy) rather than guessing at one
cause.

### First pass: `force_sub` defaulted on, pointed at a placeholder
`force_sub_enabled` defaulted to `true` with `force_sub_channel`
defaulting to a placeholder value, in middleware that runs before
every command and every ambient handler for every non-admin user. This
was a real, confirmed default-configuration bug for a fresh clone of
this repo — but the operator clarified that channel is their own, they
administer it, and they *want* force-subscribe on. The safe-by-default
fix (off unless both `FORCE_SUB_ENABLED=true` and `FORCE_SUB_CHANNEL`
are explicitly set) stayed, since a placeholder default is still wrong
for anyone else who clones this repo — but it means anyone updating to
this build who *does* want force-subscribe (like this operator) must
now set both explicitly in their own `.env` rather than relying on
defaults, or it silently turns off. Flagged prominently rather than
left to be discovered the hard way.

### Confirmed not the cause, for this operator
Telegram Group Privacy Mode was already disabled (operator confirmed).
The operator's own account is a real member of their own force-sub
channel, so force_sub wasn't blocking their own testing either —
ruling out both of the two leading hypotheses for *this specific*
deployment, though the defaults fix above still stands as a real bug
for the general case.

### The actual bug: `/chat` was a silent alias for `/ai`
`bot/plugins/ai.py` registered `CommandHandler("chat", ai_chat)` —
`/chat` and `/ai` were the exact same handler. `/persona`'s own
confirmation message even said so ("🎭 Persona set. /ai and /chat will
now respond as..."). There was no way to reach the personality/nanora
layer via `/chat` at all; it always went to the general AI assistant.
Fixed: `/chat` now belongs to `bot/plugins/personality.py`, as an
explicit, always-on entry point into personality (works even in a chat
that hasn't opted into ambient banter — the same way `/ai` always
works regardless of any ambient setting). Updated `/persona`'s
confirmation text and the `/help` output, which both still referenced
the old aliasing.

### Real gap: mentions and replies didn't bypass the ambient toggle
The operator's stated intent — a direct @mention or a reply to the
bot's own message should reliably reach personality, independent of
whether ambient (untargeted) banter is enabled for that chat — wasn't
implemented. `handle_message` treated a mention/reply exactly like any
other ambient message, subject to the same per-chat opt-in default
that exists specifically to stop the bot randomly banter-bombing a
chat that didn't ask for it. A *deliberate* address to the bot is a
different thing from ambient chat and shouldn't be gated by the same
switch. Added `_is_direct_address()` (checks reply-to-bot and
@mention-by-username via message entities) and wired it in: a direct
address now bypasses the per-chat toggle (still subject to the
cooldown, for spam protection); a plain ambient message still respects
it exactly as before.

### A bug introduced and caught in the same round
Refactoring `handle_message` into a shared `_generate_and_reply()` (so
both the ambient listener and the new `/chat` command could use the
same pipeline) left a leftover `@safe_handler` decorator sitting above
the wrong function — a pure boolean helper, not a handler — after a
str_replace edit. Since `safe_handler` wraps its target as an async
function, this silently turned `_is_direct_address()` into something
that returned an unawaited coroutine object instead of a bool. A
coroutine object is always truthy, so every message would have been
treated as a direct address, defeating the very toggle the fix was
meant to respect. Caught immediately by the existing test suite
(`test_disabled_chat_sends_nothing` failed) rather than shipping.
Moving the decorator back to `handle_message`, where it originally
belonged, fixed it — logged here because it's a good example of why
this project runs the full suite after every refactor, not just
compiles it.

### Debug logging (the other half of the original ask)
The previous debugging round added skip-reason logging
(`personality_skip: reason=...`) and a startup checklist covering the
Group Privacy Mode requirement. This round's fixes are additive to
that — the same logging now also covers the new direct-address bypass
(`personality_direct_address`) so an operator can distinguish "this
reached me because it was a mention/reply" from "this reached me
because the chat has ambient banter on."

### Testing performed
`tests/test_personality_e2e_reply.py` extended with 11 new cases:
`_is_direct_address()` against plain/mention/reply/wrong-user-reply/
wrong-mention inputs; a mention and a reply-to-bot each getting a
reply in a chat where the ambient default is off, and a plain message
in the same chat *not* getting one; `/chat` working the same way, with
no-args-and-no-reply showing usage instead of erroring; and a direct
check that `/ai` and `/chat` are no longer the same function object.
Full suite: **104 passed, 0 failed** (was 93 at the start of this
round). Full `Application` build: 251 handlers, 0 plugin load
failures.

## v2.18 - the two branches of v2.14 in one tree

Features introduced after v2.14 by **line A** (v2.15 ChatFight rankings and PsstRobot whispers; v2.16 Kugisaki/Saitama/Masha federation, warn-filter, sticker-blacklist and utility modules plus SangMata name history; v2.17 symbolic math, polls+,
clean blue text, welcome mute, MyAnimeList) and by **line B** (v2.15.1 Sonal persona, Tanvi/Yashmeet dataset merge, authorization for all three, 10-language support) are all present; none was removed or renamed. Per-feature tables:
[CHANGELOG.md](CHANGELOG.md); status by area: [FEATURE_MATRIX.md](FEATURE_MATRIX.md); the file / function / command / data audit of all 15 supplied archives: [ZERO_LOSS_AUDIT.md](ZERO_LOSS_AUDIT.md).

