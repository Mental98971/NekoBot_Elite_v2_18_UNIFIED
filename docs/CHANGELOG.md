# Changelog

## v2.18 - The two v2.14 branches in one tree: ChatFight/Kugisaki line + Sonal/languages line (2026-10-16)

**Inputs:** 15 distinct archives - v2.6 FINAL, v2.7 FINAL, v2.7 FINAL_SYNTH, v2.8 UNIFIED, v2.9 PERSONAS, v2.10 FINAL, v2.11 FINAL, v2.11.1 FINAL, v2.12 UNIFIED, v2.13 STRENGTHENED (a six-file overlay on v2.12), v2.14 UNIFIED,
v2.15 UNIFIED, v2.15.1 UNIFIED, v2.16 UNIFIED, v2.17 UNIFIED (the upload list named v2.10, v2.15 and v2.16 more than once; the copies are byte-identical).
**What was found:** after v2.14 the work forked and the branches never met. **Line A** = v2.15 -> v2.16 -> v2.17 (ChatFight, PsstRobot, Kugisaki/Saitama/Masha, SangMata, sympy math, polls, cleanblue, welcomemute, MyAnimeList; 593 commands, 44 plugins).
**Line B** = v2.15.1 (Sonal, Tanvi/Yashmeet dataset merge, authorization for all three, 10 languages; 483 commands, 34 plugins). The newest archive (v2.17) lacked all of line B and the "highest-numbered" v2.15.1 lacked all of line A; no archive was a superset.

| Step | Result |
|---|---|
| Base | the v2.17 tree (line A) |
| Changed only by line B (50 files) | taken from v2.15.1 unchanged: `bot/utils/i18n.py`, `bot/utils/langctx.py`, `bot/plugins/language.py`, `data/locales/*.json`, Sonal's corpus/skin/profiles/manifest, `scripts/build_person_personas.py --merge-existing`, the `persona_ft` id fix, `bot/personality/personality.py`, `bot/plugins/ai.py`, `bot/plugins/extras.py`, `bot/core/decorators.py`, tests and docs |
| Changed by both lines (8 files) | `.env.example`, `bot/config.py`, `bot/core/database.py`: three-way merge (base v2.14), no conflicts. `bot/core/bot.py` and `bot/plugins/start.py`: resolved by hand (below). `README.md`, `docs/CHANGELOG.md`: merged by hand. `docs/COMMANDS.md`: regenerated (597 commands, 45 plugins) |
| Command menu | Telegram allows 100 entries. Line A had traded `/pat /table /shrug` for `/whisper /rankings /mytop`; line B had traded `/shrug` for `/lang`. All four new entries are kept; `/slap` also left the menu (still works, still in `/help`) |
| `/help` | line B's translated structure, with line A's longer federation line and the Whispers and Rankings lines; keys `help.cat.whispers`, `help.cat.rankings` |
| `/menu` | the two new buttons (Whisper, Rankings) and their panel titles are translated (`menu.btn.whisper`, `menu.btn.rankings`); 53 buttons; every catalog now has 113 keys. New strings are AI-written, like the existing ones (no native review) |
| English fixture | `tests/english_ui_fixture.json` `help` refreshed to v2.17's English `/help` (the pre-i18n text of line A); `test_i18n` still compares byte for byte |
| Kept for provenance | `docs/provenance/superseded/derived/`: the persona-derived files that line B regenerated (profiles, behaviour profiles, skins, reference manifests, authorization record) as they were in v2.17/v2.12, and `legacy_snapshots_v2_6_v2_7.zip`: the 98 files from the six v2.6/v2.7 wrapper archives that exist nowhere else |
| Audit tools | `docs/provenance/audit_tools/`: the scripts behind the zero-loss audit and the static checks (set `NEKO_SRC` / `NEKO_BUILD`); not part of the bot |

**Nothing from any input was dropped:** every file, every function/class, every command and every persona corpus row of all 15 archives is accounted for - see the v2.18 addendum in [ZERO_LOSS_AUDIT.md](ZERO_LOSS_AUDIT.md).
**Verified here:** `test_i18n` (34), `test_static_contracts` (21), the persona suites, 215 Python files statically (syntax, undefined names, project imports), command registry (597, no duplicates, nothing missing from any input), file/definition/command/data audits (numbers in the addendum).
**Not verified here:** the test modules that import `python-telegram-bot`, SQLAlchemy, pydantic, aiohttp, httpx or cachetools (not installable in this environment) - run `python -m pytest tests -q` once; translation quality of the new strings; a live Telegram run.

## v2.17 - the four leftover groups from v2.16 (2026-10-12)

| Part | From | How it was merged |
|---|---|---|
| `/simplify /factor /derive /integrate /zeroes /tangent /area` and exact-value `/sin /cos /tan /asin /acos /atan /arcsin /arccos /arctan /abs /sqrt /ln` | Masha `math.py` (external Newton API) | `plugins/symbolic_math.py` + `utils/symbolic.py`: local **sympy** in a sandboxed worker process (token whitelist, restricted namespace, 8 s hard timeout, 1 GB cap, one job at a time, per-user cooldown). `/log` is already the chat-log command, so natural log is `/ln` |
| `/apoll <id> \| <question> \| <True@n/False> <True/False> <True/False> <options>`, `/stoppoll <id>`, `/forgotpollid` (also `/poll <5 digits> \| ...`) | Masha `__polling.py` (Telethon + Mongo) | `plugins/pollsplus.py` on the Bot API; ids persisted in `poll_records`; the simple `/poll Question \| a \| b` is unchanged |
| `/cleanblue /ignoreblue /unignoreblue /listblue /gignoreblue /ungignoreblue` | Saitama/Masha `cleaner.py` | `plugins/cleanblue.py`, listener group 17. Fixes: global list is sudo-only (was any admin), `/listblue` actually lists, commands addressed to other bots are never deleted |
| `/welcomemute off\|soft\|strong`, `/welcomemutehelp` | Saitama/Masha `welcome.py` | `plugins/welcomemute.py`, listener group -7: soft = 24 h media restriction, strong = muted until the "I'm not a bot" button (120 s, then kicked); persisted deadlines swept every 30 s so restarts strand nobody; the welcome message is held back until verification. **`/captcha on` now works** (it only stored a flag before) as "strong" for chats with no `/welcomemute` choice |
| `/myanime /mymanga /mycharacter /myupcoming` | Saitama `myanimelist.py` (Jikan v3) | `plugins/myanimelist.py` on Jikan v4 with httpx: escaped output, cached, rate-limit aware, `sfw` filter |

**Schema (auto-migrated):** `chats.clean_blue`, `chats.welcome_mute`; tables `poll_records`, `blue_ignore`, `verified_members`, `pending_verifications`. New dependency: `sympy`.
**Handler groups added:** -7 (welcomemute), 17 (cleanblue).

## v2.16 - Kugisaki (Saitama/Masha) + SangMata gaps closed (2026-10-11)

**Inputs:** NekoBot_Elite v2.15 (base), KugisakiNobara-master (SaitamaRobot), NobaraKugisaki-prince (MashaRoBot), SangMataBot-main; ChatFight and PsstRobot re-checked against v2.15 (already folded in, nothing missing).

| Part | From | How it was merged |
|---|---|---|
| Gbans and fedbans enforced when a banned user joins or speaks; `/banenforce` | Saitama/Masha behaviour (v2.15 only applied bans to chats existing at ban time) | `plugins/fedtools.py`, listener at group -6, 60 s cache invalidated by every ban change |
| `/fban /unfban` aliases, `/myfeds /chatfed /fedchats /renamefed /delfed /subfed /unsubfed /fedsubs /frules /setfrules /fednotif /setfedlog /unsetfedlog /fbroadcast /fedstat /fbanlist /importfbans`, `/fedhelp` family | Saitama/Masha `feds.py` | `plugins/fedtools.py`; CSV/JSON export + import (formula-injection safe, 50k row cap) |
| `/addwarn /nowarn /stopwarn /warnlist /warnfilters /strongwarn` | Saitama/Masha `warns.py` | `plugins/warnfilters.py` on the existing `add_warn` pipeline |
| `/addblsticker /unblsticker /rmblsticker /blsticker /blstickers /blstickermode` | Saitama/Masha `blacklist_stickers.py` | `plugins/warnfilters.py`, glob triggers, per-chat action |
| `/history` lookup by id/@username/reply, `/namealert`, `/forgetnames` | SangMataBot | extends `plugins/name_history.py`; alerts are opt-in per chat, users can erase their own history |
| `/gbanlist`, `/whois` (= /info), `/fbanstat` (= /fedstat), `/sin /cos /tan /asin /acos /atan /arcsin /arccos /arctan /abs /sqrt /log`, `/weebify` | Saitama/Masha | `plugins/kugisaki_extras.py` |
| `/paste /wiki /github /country /covid /setme /me /setbio /bio /encrypt /decrypt /addrss /removerss /rss`, `s/old/new` | Saitama/Masha utility modules | `plugins/toolbox.py`; literal (not regex) substitution, Fernet + PBKDF2 encryption, SSRF-guarded feeds |

**Fixes:** `tests/__init__.py` stops an installed `tests` package shadowing the suite; the persona consent fail-closed test no longer depends on test order.
**Schema (auto-migrated, all nullable/new):** `federations.rules/notify/subscribed`, `chats.ban_enforce/name_alerts/blsticker_action/blsticker_duration`, tables `warn_filters`, `sticker_blacklist`, `rss_feeds`. New dependency: `cryptography`.
**Not carried over (see docs/ZERO_LOSS_AUDIT.md addendum):** Telethon/Pyrogram-only and third-party-API modules whose services are gone or need keys (anime lists, gps/json/logo makers, cricket, speech-to-text, MAL, Spotify-era extras).

## v2.15 (line A) - ChatFight + PsstRobot folded in (2026-10-11)

**Inputs:** NekoBot_Elite v2.14 (base), ChatFight (Python / MongoDB, 43 files), PsstRobot (Go / gotgbot / GORM, 78 files).

| Part | From | How it was merged |
|---|---|---|
| Message counting, `/rankings /topusers /userstats /mytop`, leaderboard + system-stats images, `/update /restart`, flood-guarded counter | ChatFight | re-implemented as `plugins/chatfight.py` on the SQLAlchemy database ([CHATFIGHT.md](CHATFIGHT.md)) |
| Inline whispers, advanced PM whispers, media/albums, privacy mode, recent recipients | PsstRobot | re-implemented in Python as `plugins/whisper.py` (+ `utils/whisper_text.py`, `utils/whisper_store.py`) ([WHISPERS.md](WHISPERS.md)) |
| `/list`, `/notifications`, Privacy Policy panel, `/whisperban` | gaps in PsstRobot (advertised, not implemented) | added |
| One inline-query entry point | new | `core/inline.py`; the collector's cards became a provider so both features coexist |
| Graceful restart | new | `core/restart.py`, re-exec in `__main__` after shutdown hooks |

Command collisions resolved without dropping either side: ChatFight `/ping` -> `/sysstats` (`/system`), `/start` -> `/fightinfo` (`/chatfight`); PsstRobot `/help` -> `/whisperhelp`, `/start <payload>` routed by the main `/start`.

Other changes: new settings (`ACTIVITY_*`, `UPSTREAM_*`, `GIT_TOKEN`, `WHISPER_*`), `tzdata` requirement, handler group 14 and a second disjoint group-0 message handler (documented in `group_mgmt.register()`), 100-entry command menu re-balanced, CI `OWNER_ID` aligned with the tests (123456), 4 new test modules, `NOTICE.md`.

Not carried: ChatFight `debug.py`, `resource/` image dump, empty `permissions.py`, Heroku/Render files, author promo buttons; PsstRobot Windows `.bat` scripts, `.vscode`, sharded SQLite, `dev_users`. MongoDB / old SQLite data are not migrated.

## v2.15.1 (line B) - Translated /menu and /settings, English proven unchanged (2026-10-11)

- **`/menu` navigation in 10 languages:** page header, *Menu*/*Close*, all 51 category buttons, every panel title (`menu.btn.<key>`). `MAIN_MENU`/`MENU_PANELS` keep their English literals (callback data and the contract tests depend on them);
  translation happens at render time, falls back to the English label, and never changes a callback. Panel bodies (command lists) deliberately stay as written.
- **`/settings` translated** (12 `prefs.*` keys; the state words and the live-music status).
- **English cannot drift:** `tests/english_ui_fixture.json` holds the pre-i18n English text of `/start`, `/help`, `/settings` and the menu header; `test_i18n` fails if the catalog-built English output stops matching it (only the `/lang` hint lines
  that were added on purpose differ). Verified against the original v2.12 text.
- **Translations keep what must not change:** a test checks that every `/command`, `ENV_VAR` and option word of the English text is still present in every language; another that non-Latin catalogs actually use their script;
  `menu.btn.*` must match the real menu one-to-one. `test_i18n`: 20 -> 34 tests.
- Not done (and why): the ~950 individual messages of the moderation, music, economy and game plugins. They sit in modules whose tests cannot run without the dependency stack, and an automated rewrite of that many call sites was not worth the
  regression risk without being able to execute them; see docs/LANGUAGES.md for the plan.

## v2.15 (line B) - Sonal, merged dataset updates, authorization for all three, multi-language support (2026-10-11)

**Base:** v2.14 (the uploaded `NekoBot_Elite_v2_14_UNIFIED.zip` is byte-identical to the delivered one). The other uploads were checked for anything not already contained: `v2_13_STRENGTHENED` and
`v2_11_1_FINAL` are the inputs v2.14 already merged; `v2_13_UNIFIED` is an earlier, simpler variant of the same `extras` plugin (same 7 commands, no `/webss ... full`, fewer related topics, fewer menu entries) - nothing to port.

| Request | What was done |
|---|---|
| Add Sonal (5190868757, @FromPlanet_007897814, 𝐼𝓂𝓈𝑒𝑜𝓅, real name Sonal) | registered (`registry.json`, aliases `sonal`, `imseop`); corpus (3,900 messages), skin, profile, behaviour profile, hash-only reference manifest, authorization record |
| Update Tanvi and Yashmeet **keeping all old data** | new `--merge-existing` mode of `scripts/build_person_personas.py`: union by row id, old rows never removed or rewritten, replay flags never newly granted. Tanvi 10,721 -> 11,846 rows (+1,125), Yashmeet 4,826 -> 4,826 (every message in the new exports was already there; provenance refreshed), verified: 0 old rows lost, 0 core fields changed |
| Authorise; no repeated authorization for anyone | records for Tanvi, Yashmeet **and Sonal** are `granted` (scope incl. `any_use_including_future`, no expiry) with the evidence you supplied (Sonal's reply of 09/10/26 8:49 pm). Nothing is asked at run time. Because authorisation succeeded, the "remove the gate if it fails" branch was not taken: the gate stays functional and refuses only a person who has no record (add one block to add a person) |
| Multi-language support | `/lang`, 10 catalogs, translated `/start` `/help` and permission/cooldown messages, reply-language instruction for the AI paths; `Chat.language` column ([docs/LANGUAGES.md](LANGUAGES.md)) |

Details worth knowing:

- Hash-only reference manifests (`training/reference_manifests/`) were regenerated from the new exports for all three people; a rebuild from them reproduces the manifests (`--reference-manifests` exits 0).
  Split sizes from the raw exports: Tanvi 6,925 / 910 / 951, Yashmeet 2,052 / 261 / 262, Sonal 2,306 / 337 / 315 (train / val / test; conversation-level split).
- `extracted_dataset.json` is a raw group export containing other people's messages. Only the registered accounts' own messages became rows; other participants appear only as the same scrubbed reply context the corpus
  always held. Raw exports are not shipped.
- **Bug fixed (introduced in v2.14):** `scripts/persona_ft/_bridge.py` numbered synthetic message ids per person, but people share chats and `training.prepare_sft` keys messages by (chat, id): in multi-person runs messages
  overwrote each other (the v2.14 preprocess numbers for Yashmeet were wrong). Ids are now unique per person (`ID_BLOCK`); two regression tests; with the fix every shipped message survives (3,900 / 11,846 / 4,826).
- `.gitignore` ignores everything under `data/` except named paths, so the new `data/locales/` would have been left out of a git-based deploy (and `/start` would have printed raw keys). `!data/locales/` added; `test_i18n` runs `git check-ignore` against the project's own `.gitignore` and checks `.dockerignore`.
- Behaviour profiles were rebuilt (with reply-latency from the corrected splits) for all three; a test fails if a profile no longer matches its corpus.
- Command menu: `/lang` added; `/shrug` (still works, still in `/help`) left the menu to stay at Telegram's limit of 100. 483 commands, 34 plugins, 0 duplicates.
- Tests added: `test_i18n` (20), persona completeness / size-floor tests, id-collision tests; see [TEST_REPORT](TEST_REPORT.md).
- **Not verified:** translation quality (AI-written, no native review); `/lang` against a real database and Telegram (the dependency stack is not installable here: those test modules need one `pytest` run on your side);
  persona quality for Sonal (no real model).

## v2.14 - One tree from both lines: v2.12/v2.13 moderation + v2.11.1 persona engine (2026-10-07)

**Inputs:** 17 archives (v2.4 unified, v2.5 FIXED, v2.6 FINAL / final, v2.7 FINAL / FINAL_SYNTH, v2.8 UNIFIED, v2.10 FINAL, v2.11 FINAL /
AUTHENTICATED / AUTH_BYPASS / 1_FINAL / PERSONA_ENGINE_FINAL / PERSONA_FT_FINAL, v2.12 UNIFIED, v2.13 STRENGTHENED, neko-bot-merged) plus the
older bot snapshots nested inside the personality archives. Lineage: the v2.10 fine-tuning build was continued twice, independently - **v2.12 -> v2.13**
(moderation core, approvals, blacklist, `/connect`, extras; persona engine deliberately not carried) and **v2.11 -> v2.11.1** (persona engine,
evaluation, authorization record) - and two small overlay packs (PERSONA_ENGINE / PERSONA_FT) were made against v2.10. Nothing of any line was lost
and nothing was duplicated: reconciliation evidence is in [SOURCE_MATRIX](SOURCE_MATRIX.md#v214-reconciliation-of-the-17-supplied-archives) and
[ZERO_LOSS_AUDIT](ZERO_LOSS_AUDIT.md).

**Base:** v2.12 (the larger tree). **Layered on it:** the whole v2.11.1 persona line, v2.13's additions (repaired), the useful parts of the overlays.

### What came from where

| Part | From | How it was merged |
|---|---|---|
| Moderation core, approvals, blacklist, `/connect`, per-chat flood/spam settings, log channel, 5 Fallen-style admin plugins | v2.12 | unchanged base |
| Persona engine (`bot/persona_engine/`, 15 modules), its prompt/identity/safety gates, `/pengine /premember /pmemory /pforget`, evaluation (`training/persona_eval.py`), behaviour profiles, 5 reports | v2.11 / v2.11.1 | added; `personality.py`, `persona_model.py`, `plugins/personality.py`, `config.py`, `.env.example`, `start.py` merged 3-way (no conflicts left) |
| Authorization records for Tanvi and Yashmeet (`granted`, evidence kept in the record) | v2.11.1 | the gate stays **enforced**; the records are the owner-supplied evidence of 7 Oct 2026 |
| `/truth /dare /carbon /wallpaper /webss /gps /gsearch`, `/roll /toss /decide /table /shrug /shout /runs /bluetext`, text slap/pat, optional full-text `/lyrics` | v2.13 | kept, **repaired** (below) |
| Two-step persona API (`PersonaEngine.prepare()` / `finalize()`), `bot.personality.persona_engine.integration` (`get_engine`, `prepare_persona_reply`, `list_available_persons`), `bot.personality.adapter_loader`, `scripts/persona_ft/01..03` | PERSONA_ENGINE / PERSONA_FT overlays | re-implemented on the main engine / the gated training toolkit (their own engine, prompt and ungated scripts are superseded) |
| Restored tests | neko-bot-merged | `PresetLoader` core-protection regression tests (5), identity/engine smoke tests (8) |

### Not carried, on purpose

- **v2.11 AUTH_BYPASS** replaced the authorization check by an unconditional `granted` for *any* person id and wrote a made-up `granted_by: bypass` record into every adapter.
  That is not needed: the two records are filled in (v2.11.1), so the gate passes for exactly those two people and still refuses anyone else. Removing it for everybody would also
  have removed the protection for the next person added to the registry.
- **v2.11 AUTHENTICATED** carried the same records but dated the replies 05/10/26; the owner's evidence (and v2.11.1) says the request was 6 Oct and the replies 7 Oct 2026.
- **The overlay's generator prompt** ("Do NOT claim to be an AI. Do NOT add disclaimers.") and its lack of an identity-question answer, contact-data filter and consent link. The prompt of the
  main engine is used behind the same function names; a bot that imitates a real person's chat style must say it is a bot when asked (`identity.py`, tested).
- **The overlay's derived `train/val/test.jsonl`** (message-id hash split: a conversation could land in both train and test) and `behavioral_profile.json`: regenerated on demand
  (`scripts/persona_ft/01_preprocess.py` -> `training/_private/`, leak-free split); raw-chat-derived splits are not shipped.

### Defects found in the inputs and fixed

Evidence: **R** = reproduced here on the supplied code (the project's own contract tests, run against v2.12 + the unmodified v2.13 files); **T** = covered by a new test that fails on the old code;
**C** = found by reading the code / documented API, not run.

| # | Defect (version) | Evidence | v2.14 |
|---|---|---|---|
| 1 | v2.13 `plugins/extras.py` imports `InputMediaGeo` from `telegram` - no such name in python-telegram-bot. The fault-tolerant plugin loader logs `plugin_load_failed` and carries on, so **all seven new commands were silently absent** | **C** (public API); guarded by new `test_ptb_names` | rewritten without it; every `telegram*` import in the project is checked |
| 2 | v2.13 command menu has 103 entries; Telegram accepts 100, and `set_my_commands` was un-guarded in `post_init` | **R** (`test_telegram_command_menu_is_valid_and_registered`: 103 > 100) | trimmed to 100 (`/shout /runs /bluetext /slaptext /pattext` stay usable and in `/help`); `bot/core/menu.py` validates and caps at runtime; a rejected menu is logged, never fatal |
| 3 | v2.13 `fun.py` registers `/slap` and `/pat`, which `reactions.py` already owns (GIFs). Plugins load alphabetically, so the text versions **shadowed the GIF commands** | **R** (`test_no_command_is_registered_twice...`) | text versions renamed `/slaptext` (`/tslap`) and `/pattext` (`/tpat`); both features kept |
| 4 | v2.13 `/lyrics` scraped and posted up to 3,500 characters of lyric text whenever `lyricsgenius` happened to be installed; it escaped *then* truncated (could cut `&amp;` in half -> Telegram parse error), used `asyncio.get_event_loop()` in a coroutine and echoed raw exception text | **C** | full text only with `LYRICS_FULLTEXT_ENABLED=true` (default off: title, artist and the official Genius link); truncates before escaping on line boundaries; `asyncio.to_thread`; searches by the resolved title/artist; no raw errors |
| 5 | v2.13 undeclared dependency `lyricsgenius`, undeclared setting `unsplash_access_key`, stale `docs/COMMANDS.md` | **R** (3 contract tests) | optional import via `importlib`, settings + `.env.example` entries, regenerated reference |
| 6 | v2.13 `/wallpaper` fell back to `picsum.photos/seed/{hash(query)}`: `hash()` is salted per process, and the result was unrelated to the query but presented as a search hit; the Unsplash URL used `aiohttp.helpers.quote` | **C** | Wallhaven (SFW, no key) -> Unsplash (key) -> Picsum labelled "random image"; `stable_seed()` (crc32); Telegram-safe preview + links |
| 7 | v2.13 `/webss` accepted any text starting with "http" and any host; `/gsearch` put unescaped, un-encoded user text into `href="..."` (a `"` broke the message) | **T** `test_webtools_menu` | `normalize_url` (public http(s) only, no credentials/local/private/numeric-host tricks), `safe_href`, `quote_plus`; per-user cooldowns on all outside calls; Nominatim limited to 1 request/second; built-in Truth/Dare lists when the API is down |
| 8 | v2.11.1 `plugins/personality._is_admin` raised on Telegram errors, crashed on updates without a user and refused admins posting anonymously | **T** (`test_is_admin_fails_closed_and_recognises_anonymous_admins`) | fails closed; anonymous admins recognised |
| 9 | v2.11.1 `clean_text` left a `Name:` label that sat inside quotation marks | **T** | stripped |
| 10 | v2.11.1 unused import / test variable that CI's `ruff --select=F` flags | measured | removed |

### Added
- `PersonaEngine.prepare()` / `finalize()` - run your own model on the engine's prompt, then apply the same cleaning, hard gates and repetition guard (returns `None` when a gate rejects the reply).
- `bot.personality.persona_engine.integration` - the overlay's documented API (`get_engine`, `prepare_persona_reply`, `list_available_persons`, `LegacyEngine.finalize/rebuild_profile`) on the main engine; `is_imitation_allowed()` for the opt-out check.
- `bot.personality.adapter_loader` - optional in-process PEFT loading; only for adapters the registry marks `ready`.
- `scripts/persona_ft/01_preprocess.py` (shipped corpora **or** raw exports -> leak-free splits), `02_train_lora.py` (-> `training.train_lora`, gate enforced), `03_evaluate.py` (baselines + evaluation).
- `bot.utils.webtools`, `bot.core.menu`; new tests: `test_webtools_menu`, `test_ptb_names`, `test_persona_two_step_api`, `test_persona_ft_wrappers`, `test_persona_registry_identity`, `test_preset_core_protection`.
- `training.revoke` reminds that live imitation is switched off per person with `/pconsent optout`.
- Settings: `LYRICS_FULLTEXT_ENABLED`, `UNSPLASH_ACCESS_KEY`, `WEBSS_API_URL`.

### Verified here, and not
See [TEST_REPORT](TEST_REPORT.md). This build environment had no network and none of python-telegram-bot, SQLAlchemy, pydantic, aiohttp: the test modules that need them were **not run here** and must be run
once with `python -m pytest tests/ -q` (CI does). Everything stdlib-only was run; a static scan covered all 181 Python files (syntax, undefined names, unresolved project imports, `telegram` import names).

## v2.12 - Unified moderation core, approvals, word blacklist, /connect (2026-10-06)

**Base:** v2.10 FINAL. All 17 supplied archives were inspected; what was carried, ported, deliberately not carried, and why is in
[SOURCE_MATRIX](SOURCE_MATRIX.md#v212-reconciliation-of-the-17-supplied-archives). Command-level audit against v2.10: **no command was lost**
(369 -> 452 registered command names, +83 incl. aliases); the only top-level definitions removed are three private duplicates
(`_is_chat_admin` x2, `_ADMIN_CACHE_TTL` x2) and `log_cmd` (replaced by `setlog_cmd`, `/log` kept as an alias).

### Defects fixed

Evidence: **R** = reproduced against the *unmodified v2.10 handlers* with the shared test fakes (raw results: [V210_REPRODUCTIONS.txt](V210_REPRODUCTIONS.txt));
**M** = modelled (the fake stands in for a Telegram answer I could not check); **C** = found by reading the code or the documented Bot API behaviour, not run.
Every fix has a regression test.

| # | Defect in v2.10 | Evidence | v2.12 |
|---|---|---|---|
| 1 | `/tmute`, `/tban`... built `until_date` from a *naive UTC* `datetime.utcnow().timestamp()`, which Python reads as local time. On a host in IST (UTC+5:30) `/tmute 30m` gave an `until_date` 5 h **in the past** (reported "muted for 30m"); Telegram treats that as permanent | **R** (TZ=Asia/Kolkata: -18001 s instead of +1800 s) | `modtools.epoch_after()` = `time.time() + n`; no time zone involved |
| 2 | Telegram treats restrictions < 30 s or > 366 d as permanent, so `/mute 10s` was a permanent mute | **C** (documented Bot API behaviour) | such durations are rejected with an explanation |
| 3 | `/unban <id>` never worked: it called `.get_member` on an un-awaited coroutine, the `except` swallowed it and the bot answered with the usage text | **R** (answered "Usage: /unban <user_id> or reply"; no unban call was made) | rewritten; also clears the ban record |
| 4 | Any admin could ban/mute/kick/warn regardless of the Telegram right they hold, and nothing stopped actions on admins, the owner or the bot | **R** (an admin without `can_restrict_members` banned a member; an admin and the bot itself were banned too) | caller's *specific* right and the bot's right are checked first; admins/owner/bot/anonymous & channel identities are protected |
| 5 | `/unmute` gave the user `can_change_info`, `can_pin_messages`, `can_invite_users` | **R** (all three `True`) | restores the chat's own default permissions |
| 6 | Anonymous admins (posting "as the group") were not recognised: `@admin_only` asked Telegram about `@GroupAnonymousBot`; locks/flood treated them as members | **M** for the decorator (the fake raises `BadRequest` for that account; real Telegram's answer not verified), **C** for locks/flood | `sender_chat == chat` is accepted; lookup failures give a clear message |
| 7 | `/save` on an existing name inserted a second row; `/get` then raised `MultipleResultsFound` | **R** | upsert + unique index `(chat_id, name)`; old duplicates are removed on upgrade (newest wins) |
| 8 | Filters matched substrings: a filter on `hi` fired on "this" | **R** | whole-word / whole-phrase match, longest trigger wins |
| 9 | Anti-flood deleted the id *range* `[latest-count+1 .. latest]`, i.e. other members' messages posted in between | **R** (burst with another member posting between each flooder message: 8 messages deleted, 4 of them other members') | the offender's own message ids are tracked and bulk-deleted |
| 10 | Anti-flood ignored `Chat.antiflood_*`, counted edited messages, never pruned its tracker, never stopped the update chain; `/antiflood`, `/antispam` were documented but not registered | **C** | per-chat settings, edits ignored, bounded memory, `ApplicationHandlerStop` |
| 11 | `/slowmode` called `Bot.set_chat_slow_mode_delay`, which does not exist in the Bot API or python-telegram-bot: it always answered "Couldn't set slow mode." | **R** (that reply; `hasattr(telegram.Bot, 'set_chat_slow_mode_delay')` is `False`) | bot-enforced slow mode (early messages are deleted); the setting is refused when the bot cannot delete |
| 12 | `/purge` slept 5 s inside the handler (blocking that chat's ordered update queue) and had no upper bound | **R** for the sleep (`asyncio.sleep(5)` awaited inside the handler), **C** for the missing bound | bulk delete, `PURGE_MAX_MESSAGES` (default 1000), confirmation removed by a job |
| 13 | `/unpin` removed *every* pin | **R** (called `unpin_all_chat_messages`) | `/unpin` = replied message or the latest pin; `/unpinall` asks first |
| 14 | Night mode rewrote chat permissions every minute, handed all members `can_change_info`/`can_pin_messages`/`can_invite_users` each morning, and left the chat muted if it was switched off at night | **C** | applied once, previous permissions saved and restored; disabling at night reopens; failed transitions back off |
| 15 | Bans were never recorded (`/unbanall` always said "No bans recorded"); the `reports` table and the `blacklist_*` columns existed but nothing used them; `/report` went nowhere unless a log channel was configured | **R** for `/unbanall` ("No bans recorded for this chat." right after a `/ban`), **C** for the rest | recorded / implemented (see Added) |
| 16 | `/delchar` said "existing copies are kept", but `user_characters` is `ON DELETE CASCADE`: by the schema Postgres wipes players' copies while SQLite (keys not enforced) leaves orphan rows | **C** (not run on Postgres) | refuses when players own it unless `force`, then removes copies explicitly (same on every database); blocked by a running giveaway |
| 17 | Reasons and names were not always HTML-escaped; raw Telegram error text was echoed to users | **C** | escaped everywhere; errors mapped to actionable text |
| 18 | CI lint (`ruff --select=F,E9`) failed on v2.10 itself (2 findings) | measured | fixed |
| 19 | `py-tgcalls>=1.2.0` now resolves to 3.0.0 (the docs say the code targets 1.x) | measured | bounded (`pyrogram<3`, `py-tgcalls<4`); see Limits |

### Added
- **Shared moderation core** `bot/utils/modtools.py`: target resolution (reply, numeric id, tapped mention, known `@username`; forum-topic replies handled),
  rights checks, target protection, durations, `punish`/`lift`/`add_warn`/`enforce_action`, bulk delete, log-channel events, `@mod_command`.
- **`bot/utils/chatcache.py`**: per-chat settings and approved users cached for 20 s / 60 s and invalidated at commit by a SQLAlchemy hook, so the
  new per-message checks add no database round trips (one query per chat per window instead of one per listener per message).
- **New plugins:** `approvals` (`/approve /unapprove /approved /approval /unapproveall`), `blacklist` (`/addblacklist /unblacklist /blacklist /blacklistmode`),
  `connection` (`/connect /disconnect /connection /allowconnect`), `modsettings` (`/modsettings /antiflood /setflood /setfloodmode /slowmode /antispam /setwarnlimit
  /setwarnaction /setlog /unsetlog /logchannel /report /reports`), `adminutils` (`/promote /fullpromote /demote /title /admins /pin /unpin /unpinall /pinned
  /invitelink /setgtitle /setdesc`).
- **Moderation variants:** `/sban /smute /skick /swarn` (silent), `/dban /dmute /dkick /dwarn /dtban /dtmute` (also delete the replied message), `/rmwarn`, `/kickme`, `/spurge`.
- **Notes:** `/clear`, `/clearall` (owner, confirmed), `#name` shortcut. **Group management:** `/goodbye`, `/resetwelcome`, `/resetgoodbye`, `/clearrules`,
  `/privaterules`, `/cleanwelcome`, `/cleanservice`, `/locktypes`. **Collector:** `/editchar` (from WAIFU-HUSBANDO-CATCHER `/update`).
- **Donor command names as aliases:** `/harem /protecc /collect /hunt /ctop /delete /changetime /updatechar` (WAIFU-HUSBANDO-CATCHER), `/saved /removeallfilters /flood
  /floodaction /warnlimit /tempban /tempmute /lowpromote /blacklistaction` (FallenRobot / Gojo_Satoru / tgbot).
- **Schema (additive, auto-migrated):** tables `approvals`, `connections`; 13 nullable `chats` columns (`NULL` = default); unique index `uq_notes_chat_name` created after a dedupe.
- Setting `PURGE_MAX_MESSAGES` (in `.env.example`).
- Tests: **594 total (403 inherited + 191 new)** - `test_modtools`, `test_admin_cmds`, `test_automod_v212`, `test_modsettings_connect_utils`, `test_group_mgmt_v212`,
  `test_collector_v212`, `test_migration_v212` (v2.10-schema database upgraded in place), `test_v212_wiring`; fakes in `tests/_fakes.py` expose only methods the real `telegram.Bot` has.

### Changed (behaviour admins will notice)
1. Moderation commands need the matching Telegram right (ban/mute/kick/warn/approve: *Ban users*; purge/del: *Delete messages*; pin: *Pin messages*; promote/demote/title: *Add new admins*;
   `/setgtitle` `/setdesc`: *Change group info*; `/invitelink`: *Invite users*). The new commands that *change* settings (`/setflood`, `/setfloodmode`, `/slowmode`, `/antispam`, `/reports`, blacklist
   edits, `/privaterules`, `/cleanwelcome`, `/cleanservice`, `/allowconnect`, `/unsetlog`) need *Change group info*; commands that existed before (locks, filters, notes, rules, captcha, night mode,
   `/setwarnlimit`, `/setwarnaction`, `/setlog`, `/disable`...) stay open to any admin.
2. `/tban` and `/tmute` require a time (they used to turn into permanent actions without one). `/ban`/`/mute` still accept an optional time.
3. When a user reaches the warn limit and the action succeeds, their warnings are cleared.
4. `/unpin` no longer removes all pins. `/pin` still notifies by default; add `silent` to suppress.
5. `/report` pings the chat's admins in the chat, stores the report, posts to the log channel if set, and has a 30 s per-user cooldown.
6. `/log` is now `/setlog` (alias kept) and refuses a channel the bot cannot post to.
7. `/warns` and `/resetwarns` moved from `utilities` to `admin`; `/setwarnlimit`/`/setwarnaction` from `automod` and `/log`/`/report` from `group_mgmt` to `modsettings` (same names).
8. Admins, approved users, bot owners and Telegram's own identities are exempt from locks, flood, spam, slow mode and the blacklist.
9. `/delchar` on a character players own needs `force`.

### Not carried from v2.11 (PERSONA_ENGINE / PERSONA_FT)
`PERSONA_FT` is a strict subset of `PERSONA_ENGINE`; both are an overlay on v2.10. Not carried, with reasons: v2.11's docs call both people's chat corpora "authorized" but no evidence of either
person's agreement is recorded (v2.10's `training/authorizations.json` says `pending` for both) and its **training scripts do not check that gate**; its **generation prompt tells the model "Do NOT claim to be an AI"** and
drops the disclosure that v2.10 enforces; its **retrieval skips the abusive-word filter**; the **engine is not wired into the bot** (implemented-but-disconnected) and duplicates
`persons.py` retrieval; the **train/val/test splits and behavioural profiles are derived from two real people's chats** and v2.10's rule is that such data never ships. Everything is still in
the original archive; nothing was deleted. To use any of it, record each person's agreement first (`docs/FINE_TUNING.md`).

### Verification
- **Executed, passed:** `pytest` 594 passed (1947 subtests); `ruff check bot dashboard tests scripts training --select=F,E9` clean; byte-compilation of all packages;
  `scripts/gen_command_reference.py --check`; v2.10-schema SQLite database upgraded in place and re-upgraded (idempotent) with duplicate notes removed.
- **Executed against unmodified v2.10:** the reproductions marked **R** above.
- **Not executed in this environment (needs resources that were not available):** PostgreSQL, the Docker image build, a real Telegram connection (so Telegram's actual replies to each
  call - e.g. what `getChatMember` returns for `@GroupAnonymousBot` - are inferred from the Bot API documentation), live voice-chat playback with py-tgcalls 3.0 (imports and the symbols the code
  uses were checked), GPU persona training, the dashboard.

### Limits worth knowing
- Anti-flood, slow-mode and report-cooldown state is in memory: it is per process and resets on restart.
- Slow mode is enforced by deleting early messages; it is not Telegram's native slow mode (the Bot API cannot set that).
- `/connect` cannot serve commands that need a replied-to message (`/purge`, `/del`, `/pin`, `/report`) and does not cover welcome/goodbye text.
- A message deleted by a *lock* can still reach later handler groups for the same update (cosmetic); the blacklist, spam filter, flood limit and slow mode stop the chain.
- `datetime.utcnow()` migration remains deferred (naive-UTC is the database convention); the new code does not use the deprecated call.

## v2.10 - Persona fine-tuning pipeline, zero-loss audit (2026-10-04)

**Base:** v2.9 PERSONAS. Every other archive was re-compared against it at byte, definition, nested-archive and record level
(method and results: [SOURCE_MATRIX](SOURCE_MATRIX.md)). Nothing from the older builds was found missing except what is listed here.

### Added
- **Weight-level persona adaptation** - `training/`: reproducible data preparation from the raw exports (identity by numeric id,
  conversation-aware examples, split by conversation, duplicate/leak flags, hash manifests), LoRA/QLoRA trainer (loss on the reply only,
  crash-safe adapter store, resume, recorded seeds/hashes/authorization), held-out generation, offline baselines, evaluation
  (distribution distance, memorization, persona separation, safety, noise floor) with promotion gates, `promote`, `revoke`.
  **No model has been trained** - this environment has no GPU, torch/transformers/peft, network or base weights. See [FINE_TUNING](FINE_TUNING.md).
- **Authorization gate** - `training/authorizations.json` ships with both persons `pending`; training refuses until the person's own
  agreement is recorded (weights cannot honour a later opt-out).
- **Runtime adapter router** - `bot/personality/persona_model.py` + `persona_sft_format.py` and a guarded hook in
  `Personality._person_reply`. Off by default (`PERSONA_MODEL_ENABLED=false` = exactly v2.9). Identity questions get a fixed disclosure and never
  reach a model; only adapters promoted after passing the gates are served; unsafe output is discarded; failures fall through to the
  v2.9 path; circuit breaker; reply marker (default 🤖).
- 8 settings: `PERSONA_MODEL_ENABLED/BASE_URL/API_KEY/TIMEOUT_SECONDS/TEMPERATURE/TOP_P/MARKER`, `PERSONA_ADAPTER_DIR`.
- `training/reference_manifests/` - hash-only manifests (no message text) to prove a rebuild reproduces the same datasets.
- Tests: `tests/test_persona_training.py` (89 tests on synthetic data + the real `Personality._person_reply` hook run with only heavy imports
  stubbed) and `tests/test_catalog_integrity.py` (recovered guards, below).

### Recovered from older archives
- Four test groups from neko-bot-merged guarding features v2.9 still ships (imported persona catalogue, `/plist` pagination, preset-loader
  core protection, no two catch-all listeners in one handler group) plus a static menu contract (every button has a panel, every panel a
  button) - `tests/test_catalog_integrity.py`.
- Display-name variants of the supplied identities (`Iziiee⋆𐙚`, `ichiiren`, `GALAXY ANNIHILATOR`) added to the registry as inert
  metadata. Display names still never identify anyone.
- The superseded presets / identity map / overlay README kept as inactive reference in `docs/provenance/superseded/`.

### Fixed
- `/q` (`/quotly`) and `/health` (`/status`) were registered but appeared in no menu panel or help text; they are now listed.
  `docs/COMMANDS.md` regenerated (175 commands with a description, was 174).

### Not carried over (with reasons)
`identity_resolver.py` (display-name/alias matching lets a copied name trigger a real person's persona); the overlay's trait presets
(unmeasured assertions; kept as reference only); raw chat exports and prepared datasets (other people's messages - never in a release);
`tests/test_menu_coverage.py` (drives the removed `CATEGORIES/_PAGES` menu API; statically replaced); the six wrapper zips under
`data/personality/archives/` (their 9,537 leaf files are all present in v2.9 apart from 89 older code/doc snapshots, see SOURCE_MATRIX).

### Verified / not verified
- **Verified here:** every `.py` compiles; 170 tests pass (4 skipped: they need the bot's dependency stack); the data pipeline ran on the real
  exports and a rebuild reproduces its reference manifests byte for byte; the record-level proof against the v2.9 corpus (15,547 / 15,547);
  offline baselines on the real held-out splits (numbers in FINE_TUNING).
- **Not run (needs a GPU box / services):** training, generation, evaluation of the base model or an adapter, serving through vLLM, and any live
  Telegram or LLM path. The full application test suite needs the dependency stack and was not run.

## v2.9 — Person personas: Tanvi and Yashmeet (2026-10-03)

**Base:** v2.8 UNIFIED. The other 17 uploaded archives (v2.3–v2.7 FINAL / SYNTH, neko-bot-merged) were compared against it; v2.8 was
already their verified superset (see the v2.8 report below), so nothing was re-merged and no older code was reintroduced.

### Added
- **Tanvi** (accounts 7883544685 @You_are_mine_143 / "Iziiee", 7187486402 @ichiiren) and **Yashmeet** (5553376334 @concyclicity /
  "GALAXY ANHILATOR") as persona skins built from the supplied datasets — 10,721 and 4,826 messages after merging 9 files in 4 schemas
  (`MASTER_*.jsonl/.db/_RICH`, `*_dataset.jsonl/.csv`, `*_MASTER_dataset.*`, `extracted_dataset.json`), de-duplicated by (chat, message id).
  Roster-shaped rows (shifted columns) are used as identity evidence only; 25 rows with unknown sender and 3 unparseable rows were counted and excluded.
- `bot/personality/persons.py` (registry, deterministic identity resolution, retrieval, prompt builder), `person_consent.py`, `persona_text.py`
  (shared scrub/classify code used by both the offline builder and the runtime), `scripts/build_person_personas.py`.
- Commands `/ppersons`, `/pwho`, `/pconsent`; settings `PERSON_PERSONAS_ENABLED`, `PERSON_PERSONA_DISCLOSURE`; table `persona_optouts` (additive migration).
- `generate_persona_reply` in the AI plugin (no Neko identity guard, own disclosure rules).
- 24 tests in `tests/test_person_personas.py` (stdlib only).

### Integration (no bypass)
Person skins pass through `/cpersonality skin`, per-chat persistence, `/personality`, DM/group/mention/reply handling, cooldown, per-user limits,
anti-repetition, LLM hook and logging. `SkinLoader` resolves aliases (`galaxy_anhilator` → `yashmeet`). One person = one cooldown/limit bucket across accounts.

### Fixed
- `/nnr_test` referenced an undefined `chat` variable (NameError on every use) → uses `chat_id`.
- `set_chat_skin`/`set_skin` accepted unknown names silently falling back to `neko` while reporting success; they now return False for unknown skins.

### Decisions worth knowing
- No fine-tuning: median message length is 2–3 words, so retrieval of the person's own replies + measured-fact prompting is both safer and better.
- No traits were invented. Everything in the profile is a computed statistic; unsupported attributes (humour, sarcasm, relationships, personal facts) are not asserted.
- Imitation of real people is opt-out-able, disclosed, flag-gated, and never replays contact data, third-party names or abusive tokens verbatim.
- Not run here: the full pre-existing suite needs the real dependency stack (no network in this environment); the stdlib-capable suites
  (static contracts 21, hardening 14, synthesis 4, personas 24) pass. Live Telegram and LLM paths were not exercised.

## v2.8 — Unified synthesis of v2 → v2.7 FINAL (2026-10-02)

### 1. Synthesis report

**Inputs.** 15 distinct archives were examined (the upload listed 19 names; `NekoBot_Elite_v2_7_FINAL` ×2 and
`neko-bot-merged` ×4 arrived under identical file names, so only one copy of each was readable): the
`NekoBot_Elite` line `Production → v2 → v2.1 → v2.2 → v2.3 → v2.4 → v2.5_FIXED → v2.6_final / v2.6_FINAL → v2.7_FINAL`,
`v2.7_FINAL_SYNTH`, and the earlier side lines `NekoBot_Production`, `neko-bot-merged`, `neko-bot-fixed`,
`neko-bot-consolidated`.

**Base chosen: v2.7 FINAL.** Not because it is newest, but because the evidence says it is a superset of everything else:

| Check | Result |
|---|---|
| Commands (static scan of literal registrations) | v2.7: 308; every other version ≤ 296; **no command in any older version is missing from v2.7** |
| Files | Every file path present in any older version exists in v2.7 (outside the personality data archives, handled below) |
| Modules / top-level symbols | The few symbols that disappeared were refactors (e.g. UNO/WordSeek moved into `bot/utils/`, `chess_join_handler` folded into `/chess`), the intentional removal of `/cosplay` captions, or a no-op stub (`configure_secret_redaction`) |
| Function bodies shared with the closest ancestors | Differences from v2.6_FINAL: 10 of 786 functions; v2.6_final: 11 of 766; v2.5_FIXED: 13 of 753. **Every one is an improvement in v2.7** (media-aware filters, video stickers, TMDB flow, …) — none is a regression |
| Command ownership | Only `/reverse` changed owner (fonts → anime). It is a deliberate dispatcher: replying to an image searches by image, otherwise it reverses styled text — both features survive |

**`v2.7_FINAL_SYNTH`** is code-identical to v2.7 FINAL: it differs only in `ENGINEERING_SUMMARY.md` and lacks
`.github/` and `.gitignore`. Its summary's statements were not carried over where they could not be verified or were wrong
(for example, the middleware order is access → force-sub → log → antiflood → antispam, not logging first; "no known
unresolved defects" was not true — see §2).

**Personality data archives.** The five cumulative `master_personality_datasets*.zip` files and the duplicate packs were
checked byte-for-byte (recursively through nested zips): every member is either present elsewhere in the tree, identical to a
file in one of the other inputs, or an older copy of project source that the codebase supersedes. Only their per-version
README notes were unique; those are preserved in `docs/provenance/personality_master_archives.md`.

**Recovered from an older snapshot:** five IMDb/TMDB tests that existed in the v2 line but had been dropped in later merges
(search buttons, Wikipedia fallback, poster/detail callback, malformed-callback guard).

### 2. Fixed

| Area | Defect | Fix |
|---|---|---|
| Welcome | `welcome_listener` read `update.chat_member` inside a *message* handler, so welcome text saved with `/setwelcome` was never sent | Reads `message.new_chat_members`; HTML-safe values; stray braces can't crash it (`{first}`-style tokens only, no `str.format`); at most 5 greetings per service message; plain-text fallback if Telegram rejects the markup; NULL flags from migrated schemas follow the column defaults |
| Goodbye | `/setgoodbye` text was stored but nothing ever sent it | New `goodbye_listener` (group 12) |
| Missing commands | `/welcome`, `/locks`, `/news` were listed in `/help`, the `/menu` panels and the Telegram command menu, but **no handler existed in any of the 15 versions** | Implemented (see §3) |
| Dashboard image | `bot/utils/retry.py` was not copied into the image and `tenacity` was missing from its requirements, so the container crashed on import; `pydantic-settings` floor differed from the bot's | Added both; floors aligned; HEALTHCHECK added |
| CI | `ruff --select=F,E9` would fail: 13 findings (unused imports/variables, placeholder-less f-strings) | All fixed; lint now also covers `scripts/` |
| `/watch` | Issued two redundant AniList requests per call (one built from a `"X" in dir()` expression that is always false) although the query already returns relations | Removed |
| `/ping` | Printed "AniList path: measured" and labelled the AniList round-trip "Bot handler" | Live, uncached probes of AniList and Jikan with ok/down and milliseconds |
| Locks | `lock_listener` called `get_chat_member` (a Telegram API round-trip) for **every message in every group**, locks or not; URL locks ignored caption links | Local rules first; admin lookup only on a violation; caption entities checked |
| HTML safety | Member-controlled text went into HTML-mode messages unescaped: antiflood warning (first name), ban/mute reasons, voice-chat auth list, note names, personality admin replies, `/meme` titles. A name like `<b` made the message fail *after* the action had succeeded | Escaped everywhere; `/rules` falls back to plain text if admin markup is invalid; a test now fails on any new unescaped `first_name`/`last_name`/`full_name`/`reason` |
| `/meme` | No timeout; NSFW/spoiler posts posted into groups | 12 s timeout; retries past NSFW/spoiler posts |
| `/ud` | Used a private aiohttp helper (`aiohttp.helpers.quote`), no timeout, crashed on a non-200 reply | `urllib.parse.quote`, timeout, clean error |
| Forwards | `forward_date` / `forward_from` are deprecated in PTB 21 and removed in later majors | `forward_origin` in antispam, locks and `/info`; a test forbids the old attributes |
| `/q` | Dead duplicate branch; `/q <n>` silently quoted only one message; `/q i` did nothing different; Pillow rendering blocked the event loop | Says why only one message is quoted; `i` sends a lossless PNG document; rendering runs in a worker thread |
| Logs | python-telegram-bot's httpx client logs every Bot API URL at INFO, and the URL contains the bot token — it was written to the logs on every poll | Token/key redaction on structlog **and** stdlib logging (messages and tracebacks); httpx/httpcore at WARNING unless `DEBUG` |
| Images | No `.dockerignore`: `COPY . .` baked `.env`, Pyrogram sessions and databases into image layers | `.dockerignore` added; a test guards it |
| Docs | README said v2.6, started mid-sentence, listed `/welcome` and `/locks` as working, claimed 25 skins / 24 presets (there are 109 / 1,609); FEATURE_MATRIX marked `/locks` and `/welcome` "Works"; the handler-group map in `group_mgmt.py` was incomplete | Rewritten from the code; map verified against the live registrations |

### 3. Added

- **Per-chat-ordered concurrent update processing** (`bot/core/concurrency.py`, `CONCURRENT_UPDATES`, default 64, `0` = legacy).
  PTB's default of one update at a time let a single yt-dlp download or LLM call stall every chat. Updates from the same chat still run
  in arrival order; different chats run in parallel; queued updates wait for their chat's turn *before* taking a global slot.
- **`/welcome [on|off]`**, **`/locks`**, **`/news [topic]`** (NewsAPI; key sent in a header, links validated and escaped; says how to
  enable itself when `NEWS_API_KEY` is unset).
- **Dashboard access control:** optional HTTP Basic via `DASHBOARD_TOKEN` (constant-time compare, `/health` stays open,
  `Cache-Control: no-store`); compose publishes it on `127.0.0.1` only.
- **`bot/utils/redact.py`** (stdlib-only secret scrubbing shared by structlog and logging), `render_template`, `html_or_plain`.
- **`docs/COMMANDS.md`** — all 366 commands, generated from the code by `scripts/gen_command_reference.py`; CI fails if it is stale.
- `init: true` on the bot container (reaps the ffmpeg / yt-dlp children it spawns); CI `permissions`, `concurrency` and timeout.
- `.env.example` now documents every setting (`DATA_DIR`, `DEFAULT_LANGUAGE`, `DOWNLOADS_DIR`, `SESSIONS_DIR` were missing).
- A contract test that every entry in `requirements.txt` is imported somewhere or is a documented implicit need (the inverse of the existing "imports ⊆ requirements" check).

### 4. Tests

194 → 272 test functions (17 → 21 modules). New: static contracts (command surface incl. dynamically registered fonts/effects/reactions,
help/menu vs. handlers, handler-group collisions, dead buttons, HTML escaping, settings ↔ `.env.example`, requirements coverage,
dashboard image contents, Python 3.11 syntax, generated docs), group management, redaction/templates, concurrency,
`/news`, and the recovered IMDb tests. The contract tests were run against the untouched v2.7 tree to confirm they fail on the
defects above: 11 of the 21 contract tests fail on the untouched v2.7 tree (9 assertion failures, plus 2 errors for files that did not exist there); all 21 pass here.

### 5. Removed

Six packages that `requirements.txt` declared but nothing imports — `alembic` (schema upgrades are the additive auto-migration), `aiofiles`, `aiohttp-socks`,
`lxml` (BeautifulSoup uses `html.parser`), `orjson`, `prometheus-client` (`bot/utils/metrics.py` is a small in-process counter module) — the same rule the file
already applied to `sentry-sdk`, `motor` and friends. Re-add any of them if you build on them.

Byte-identical duplicates in `data/personality/`: three copies of `all_personality_datasets.zip`, two of `personality_pack_full.zip`,
a second `nanora_dataset.jsonl`, a second identical `all_personality_pack/` tree, and the five cumulative `master_personality_datasets*.zip`
snapshots (tracked data 27.8 MB → 8.7 MB). Runtime data (skins, presets, dataset bank) and import inputs are untouched; see
`data/personality/README.md`.

### 6. Verification — what was and was not run

The build environment had no network access, so the real dependency stack (python-telegram-bot, SQLAlchemy, Pillow, …) could not be installed.

**Run and passing**

- Byte-compilation of every Python file; an offline linter replicating `ruff --select=F,E9` plus a Python 3.11 f-string check — clean.
- All 81 `bot.*` modules import (third-party packages replaced by stand-ins), all 27 plugins' `register()` run, and the **366 commands registered at
  runtime equal the 366 found statically**; there are no duplicate registrations.
- 78 new tests pass, including handler tests for welcome/goodbye/locks/`/news`/IMDb against simulated Telegram objects, the update processor against
  an emulation of python-telegram-bot's `BaseUpdateProcessor`, and the redaction filter against real log output. A mutation of the processor (global slot
  taken before the chat turn) makes its starvation test fail, as it should.
- Differential run of the existing suites under the same stand-ins: no test that passed on v2.7 fails on v2.8.

**Not run**

- The complete `pytest` suite with real dependencies (46 existing tests need real SQLAlchemy/PTB behaviour and fail identically on v2.7 under the stand-ins) —
  CI does this on the first push.
- Anything needing a live Telegram connection, a voice chat, or a real database server. The per-chat processor follows the documented
  `BaseUpdateProcessor` contract but was exercised against an emulation of it; `CONCURRENT_UPDATES=0` is the escape hatch.

### 7. Behaviour changes to be aware of

Saved welcome/goodbye text now posts; `lock_url` also catches caption links; `/q i` sends a PNG file; the dashboard port is localhost-only by default;
group chats no longer incur an admin lookup on every message; updates are processed concurrently across chats.

---

Earlier releases are described in [HISTORY.md](HISTORY.md) and [FEATURE_INVENTORY.md](FEATURE_INVENTORY.md).
