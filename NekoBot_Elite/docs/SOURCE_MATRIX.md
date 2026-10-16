# Source-to-feature/data matrix (v2.10)

v2.10 starts from the v2.9 PERSONAS build and was **proven** to contain everything worth keeping from the other 15 archives - not
assumed. This file is the evidence and the disposition of every difference. (19 files were uploaded; 16 are distinct archives: v2.9,
13 older builds, and 2 Tanvi/Yashmeet overlay archives. Duplicate uploads were byte-identical.)

## How it was checked

1. **Byte level, every file.** Each file of each older archive was hashed and looked up in v2.9. Files with the same path but different
   bytes are older revisions of a v2.9 file and go to step 2.
2. **Definition level, every older revision of a Python file.** AST diff of functions, classes, methods, registered commands and
   `UPPER_CASE` constants: anything that exists in an older revision and not in v2.9's revision of that file.
3. **Nested archives, recursively.** The six personality wrappers in `data/personality/archives/` (zips inside zips, depth ≤ 4) were
   opened and every leaf file hashed against everything in v2.9, *including the contents of v2.9's own zips*. (A first pass that did not
   open zips wrongly reported the nanora response banks as missing - they are inside `data/personality/packs/personality_pack_full.zip`.)
4. **Record level, the supplied chat exports.** Every message from the registered accounts, rebuilt from this round's raw exports, was
   compared with the v2.9 persona corpus.

| Archive (distinct) | Files | Byte-identical in v2.9 | Older revision of a v2.9 file | Path absent from v2.9 |
|---|---|---|---|---|
| Production · v2 | 269 · 272 | 230 · 233 | 39 · 39 | 0 · 0 |
| v2.1 · v2.2 · v2.3 · v2.4 | 408 · 464 · 461 · 467 | 362 · 418 · 417 · 423 | 40 · 40 · 38 · 38 | 6 each (wrapper zips) |
| v2.5 FIXED | 85 | 58 | 27 | 0 |
| v2.6 FINAL · v2.6 final | 499 · 1,974 | 463 · 1,934 | 30 · 34 | 6 each |
| v2.7 FINAL · v2.7 FINAL_SYNTH | 1,976 · 1,973 | 1,937 · 1,935 | 33 · 32 | 6 each |
| v2.8 UNIFIED | 1,937 | 1,925 | 12 | 0 |
| neko-bot-merged | 1,832 | 1,781 | 50 | 1 (`tests/test_menu_coverage.py`) |
| v2.7 TANVI_YASHMEET_PERSONALITY_LAYERS · v2.8 …FINAL_INTEGRATED (overlays) | 8 · 16 | 1 · 1 (`import_personas.py`) | 0 | 7 · 15 (data, presets, identity map, resolver) |

The "6 absent" are the same six wrapper zips in every v2.1–v2.7 build (step 3). Within them, 9,537 leaf-file entries were examined
(wrappers nest each other, so most files are counted several times); **89 distinct files** are not byte-identical to anything in v2.9 -
all are older snapshots of code (64 `.py`) or docs/config (25 `.md/.txt/.yml/.example`), none is data. Their definitions are covered in
step 2's table below. The `character_styles` packs hold 1,839 persona members: 1,754 are in v2.9's presets and the other 85 are listed
with a reason under `excluded`/`skipped` in `data/personality/catalog_manifest.json` (none unaccounted).

## Older-only definitions and their final treatment

| Older-only item (source) | Final treatment | Evidence |
|---|---|---|
| `logger._redact_event/_redact_value/configure_secret_redaction` (v2.1–v2.6 …) | **Replaced** by `bot/utils/redact.py` (structlog *and* stdlib logging) | `tests/test_text_and_redaction.py` |
| `personality._message_addresses_bot`, `_run_personality_pipeline` (Production…v2.6) | **Replaced** by `_is_direct_address` / `_strip_bot_mention` and the current pipeline | `bot/plugins/personality.py` |
| `games._make_wordseek_grid/_render_grid/_uno_deck/_UNO_*/_WORDSEEK_WORDS` | **Moved** to `bot/utils/wordgames.py` and `bot/utils/unogame.py` (testable without Telegram) | `from bot.utils import unogame, wordgames` in games.py |
| `games.chess_join_handler` | **Merged** into `chess_cmd` (`/chess join`) | `games.py` |
| `cosplay._CAPTIONS` | **Removed on purpose** (documented: photo only, as in the reference bot) | `cosplay.py` docstring |
| `reactions.WAIFU_PICS_SFW_URL` | **Moved** into the provider table (`waifu.pics` entry) | `reactions.py` |
| `start.CATEGORIES/_KEYS/_PAGES` | **Replaced** by `MAIN_MENU`/`PAGE_2`/`PAGE_3` + `MENU_PANELS`; 48 buttons ↔ 48 panels, no orphan either way | `tests/test_catalog_integrity.py::TestMenuPanels` |
| `stickers._get_source_image_bytes` | **Inlined** (`bot.get_file` + `_resize_for_sticker`) | `stickers.py` |
| `fonts` command `reverse` | **Preserved** - `/reverse` is a dispatcher in `anime.py` (image → scene search, text → un-style fancy fonts via fonts' `reverse_cmd`) | CHANGELOG v2.8 |
| tests `TestImportedPersonaCatalog`, `TestPaginateJoined`, `TestPresetLoaderCoreProtection`, `TestCatchAllListenerGroupsDontCollide`, `test_menu_labels_have_panel_keys`, `test_every_page_button_has_a_real_category` | **Recovered** (ported to the current API) - the features they guard are still shipped | `tests/test_catalog_integrity.py` |
| `tests/test_menu_coverage.py` (neko-bot-merged) | **Not carried**: it drives the removed `CATEGORIES/_PAGES` API through a Telegram stub; its static successor is `TestMenuPanels` | - |
| `identity_resolver.py` (overlay) | **Not carried** (display-name/alias matching impersonation risk) | `docs/provenance/superseded/README.md` |

## Chat exports (record level)

| Raw file (v2.8 FINAL_INTEGRATED `datasets/custom/`) | Size | Rows read |
|---|---|---|
| `extracted_group_dataset.json` | 5.61 MB | 11,383 messages |
| `tanvi_iziiee_dataset.csv` / `.jsonl` | 1.83 / 4.05 MB | 10,721 messages each |
| `tanvi_iziiee_master.csv` / `.jsonl` | 5.38 / 11.30 MB | 23,736 messages + 13,429 roster rows each |
| `yashmeet_galaxy_anhilator.db` / `_full.jsonl` | 0.71 / 1.41 MB | 4,826 messages each |
| `yashmeet_galaxy_anhilator.jsonl` / `_rich.jsonl` / `_rich.db` | 1.24 / 1.24 / 0.97 MB | 1,453 messages each |

The v2.7 PERSONALITY_LAYERS overlay's four datasets are byte-identical to files in the v2.8 overlay. After merging duplicate rows of the same
message across all ten files: **29,011 distinct messages**, of which **15,547** are from the registered accounts (Tanvi 10,721 · Yashmeet
4,826). Rebuilt with the v2.9 builder's own id formula, those 15,547 ids and their scrubbed texts are **identical** to the v2.9 corpus
(0 missing, 0 extra, 0 different) - nothing in the raw exports was lost by v2.9's preprocessing, and nothing in them is missing from the
corpus. The remaining 13,464 messages are other participants' and are used only as anonymised context in the training examples.

## Matrix

| Source | Feature / data | Unique? | Behavioural value | Security / privacy | Final treatment |
|---|---|---|---|---|---|
| Production … v2.5 | whole code base | No (every path exists in v2.9; older revisions) | none beyond v2.9 | - | Superseded |
| v2.1 – v2.7 wrappers | 6 personality zips (9,537 leaf entries) | No - all but 89 code/doc snapshots are byte-identical in v2.9 | - | - | Content preserved in v2.9 (loose files + `packs/*.zip`); wrapper zips redundant |
| v2.6 / v2.7 | 1,500 imported character styles | No | catalogue | 85 members excluded with reasons | Preserved (1,754 presets) + manifest |
| v2.8 UNIFIED | v2.8 behaviour | No | - | - | Superseded by v2.9 |
| neko-bot-merged | 4 test groups, menu-coverage test | Yes (tests) | regression protection | - | Recovered 4 groups + successor menu test; 1 not carried (above) |
| v2.7 overlay | Tanvi/Yashmeet raw datasets | Yes (raw) | **High** | other people's messages; contact data | Used as private training source; scrubbed corpus already in v2.9 (identical); never shipped |
| v2.8 overlay | `*.preset.json` | Yes | Low (unmeasured traits) | none | Kept inactive in `docs/provenance/superseded/` |
| v2.8 overlay | `identity_map.json` | Partly | identity metadata | alias/display-name matching is spoofable | ids/usernames already in registry; display-name variants merged as inert metadata; ranking rule rejected |
| v2.8 overlay | `identity_resolver.py` | Yes | none | spoofable | Not carried |
| v2.9 | persons, skins, consent, builder, tests | base | High | opt-out + disclosure | Kept unchanged; additions only |

## What v2.10 added to v2.9 (beyond recovering tests)

`training/` (data preparation, LoRA/QLoRA training, generation, baselines, evaluation, promotion, revocation, tests) · `bot/personality/persona_sft_format.py`
· `bot/personality/persona_model.py` + the guarded hook in `Personality._person_reply` · 8 `PERSONA_MODEL_*`/`PERSONA_ADAPTER_DIR` settings · `/q` and
`/health` added to `/menu` and `/help` text (both were registered but undiscoverable) · display-name variants in the registry. Details:
[CHANGELOG](CHANGELOG.md), [FINE_TUNING](FINE_TUNING.md).

---

# v2.12: reconciliation of the 17 supplied archives

This round supplied 17 distinct archives (two file names were listed twice in the upload; the upload folder holds one file per name): six NekoBot Elite
builds (v2.7 FINAL, v2.8 UNIFIED, v2.9 PERSONAS, v2.10 FINAL, v2.11 PERSONA_ENGINE, v2.11 PERSONA_FT) and eleven other bots. The tables above remain the
evidence for the older NekoBot lineage; this section records what v2.12 did with each archive.

## How it was checked

1. **Inventory** of every archive (structure, language/framework, size, tests, docs).
2. **Lineage:** v2.10's own audit (above) covers v2.7-v2.9; it was trusted and not re-run byte-for-byte. v2.11 was compared file-by-file with v2.10 (new / same / different) and read where it differs.
3. **Command-level gap analysis** per donor: command names were extracted by regular expression from each donor's handler registrations and compared with NekoBot's. This is
   *approximate* (it misses commands registered dynamically and over-counts internal names) and was used to find candidates, not to claim completeness.
4. **Zero-loss check of v2.12 against v2.10:** AST comparison of every top-level definition in `bot/` and comparison of the generated command reference. Result: **0 commands lost** (369 -> 452 names);
   top-level definitions 886 -> 1,120 (847 -> 1,075 distinct names) with 3 names removed (`_is_chat_admin`, `_ADMIN_CACHE_TTL`, both duplicated in the flood and spam middleware and replaced by `modtools.is_chat_admin`; `log_cmd`, replaced by `setlog_cmd` with `/log` kept as an alias).
5. **Baseline:** v2.10's 403 tests were run before changing anything (all passed) and again after (all passed; a handful of group-management tests had their harness updated because the handlers now read chat settings through `bot/utils/chatcache.py`).

## Disposition by archive

| Archive | Disposition |
|---|---|
| **NekoBot Elite v2.10 FINAL** | **Base.** Architecture, plugin loader, tests, persona consent gate kept. |
| NekoBot Elite v2.7 FINAL, v2.8 UNIFIED, v2.9 PERSONAS | Older lineage; fully covered by the v2.10 audit above. Not re-diffed this round. |
| NekoBot Elite v2.11 PERSONA_ENGINE / PERSONA_FT | **Nothing carried** (reasons in [CHANGELOG](CHANGELOG.md#not-carried-from-v211-persona_engine--persona_ft)): authorization gate bypassed by its scripts, impersonation prompt without disclosure, abusive-word filter skipped, engine not wired in, real-person data splits. FT is a strict subset of ENGINE. Still in the original archive. |
| **FallenRobot** | **Ported (rebuilt on the shared core):** approvals, word blacklist + modes, `/connect` family, promote/demote/title/admins/invite link/pinned/group title & description, report flow with admin ping, `/goodbye` `/resetwelcome` `/resetgoodbye` `/clearrules` `/locktypes` `/privaterules` `/cleanwelcome` `/cleanservice` `/kickme`, silent and delete variants, `/saved` `/removeallfilters` `/lowpromote`. **Not ported:** owner shell/eval/git/reboot/module-loading commands (remote code execution and host control are deliberately excluded), sticker blacklist, chatbot, carbon/github/paste/screenshot utilities, math and fun one-liners, `/bio` `/me`. |
| **Gojo_Satoru** | **Ported:** `/dtban` `/dtmute`, flood/blacklist action aliases (`/floodaction` `/blacklistaction` ...). **Not ported:** birthdays (`/mybday` `/bdays` ... needs a table and a daily job), join-request modes, GDPR export, private notes, captcha modes, image/Instagram/Google search, eval/exec, per-chat staff lists. |
| **tgbot (Marie lineage)** | **Ported:** `/tempban` `/tempmute` `/warnlimit` `/saved` `/goodbye` `/resetwelcome` `/resetgoodbye` `/clearrules` `/locktypes` ... **Not ported:** RSS subscriptions, warn *filters* (superseded by the blacklist's `warn` action), `/me` `/setme` `/bio` `/setbio`, `/ip` `/time`, import/export of chat data. |
| **WAIFU-HUSBANDO-CATCHER** | NekoBot's collector already covered 12 of its 24 command names (19 of 24 after this round, measured with the same approximate extraction). **Added:** `/harem` `/protecc` `/collect` `/hunt` `/ctop` `/delete` `/changetime` as aliases, `/editchar` (its `/update`). **Fixed on the way:** `/delchar` promised to keep players' copies although the schema cascades the delete (CHANGELOG #16; not run on Postgres). **Not ported:** `/groups` `/topgroups` `/list` and owner dev tooling. |
| **Fam-Tree-Game** | **Not ported - keep as a separate service.** About 45K lines on a different MTProto framework with its own database, ~19 MB of assets and roughly 160 command names by the regex extraction (family tree, economy, jobs, crime, gangs, farming). Merging it would replace NekoBot's runtime, not extend it. |
| **on9wordchainbot, WordSeek** | NekoBot's `games` plugin already has word games (name overlap 4/31 and 10/30). **Not ported:** WordSeek tournaments, tracking and leaderboards; on9wordchain's lobby, dictionary curation and statistics - each depends on its own database and word lists. |
| **quote-bot** | NekoBot's `/q` exists. **Not ported:** per-user quote settings, archive, top lists and emoji packs (separate storage). |
| **TgMusicBot 2.3 (Go)** | **Not ported** - different language and runtime; NekoBot's live voice-chat music (Pyrogram + PyTgCalls) already provides the playback features. |
| **YukkiMusicBot 3.0, Yukki Replit-ready** | Live-music and access-control features were merged in earlier NekoBot versions (see HISTORY / FEATURE_INVENTORY). Command extraction is meaningless here (the names live in a strings file); not re-audited this round. |

## Candidates for the next round (not done, not forgotten)
Birthdays (Gojo), RSS (tgbot), WordSeek leaderboards, `/me`-style profile text, optional `docker-compose` profile that runs Fam-Tree-Game next to the bot.

## v2.14: reconciliation of the 17 supplied archives

(18 paths were listed, 17 distinct files; the duplicate uploads were byte-identical.) Method and results: [ZERO_LOSS_AUDIT](ZERO_LOSS_AUDIT.md). Two independent lines grew from the v2.10 fine-tuning build; v2.14 is
their union.

| Archive | What it is | Disposition in v2.14 |
|---|---|---|
| v2.12 UNIFIED | v2.10 + moderation core, approvals, blacklist, `/connect`, 5 admin plugins; persona engine not carried | **Base** |
| v2.13 STRENGTHENED | patch over v2.12: `extras` plugin, text fun commands, `/lyrics` full text, 103-entry menu | Carried and **repaired** (5 of the project's 21 contract tests failed on it as shipped; `extras.py` could not import) |
| v2.11.1 FINAL | v2.11 + authorization records filled in from the owner's evidence (7 Oct 2026) | **Layered on the base**: persona engine, evaluation, reports, plugin/config wiring, records |
| v2.11 FINAL | the persona engine release; records `pending` | Superseded by v2.11.1 (same code) |
| v2.11 AUTHENTICATED | v2.11 with records filled, replies dated 05/10/26 | Superseded by v2.11.1 (dates differ from the owner's evidence) |
| v2.11 AUTH_BYPASS | v2.11 whose gate grants any person unconditionally | **Not carried** - the gate stays enforced; see the changelog |
| v2.11 PERSONA_ENGINE / PERSONA_FT (overlays on v2.10) | a small stand-alone engine, an adapter loader, ungated training scripts, derived splits | API, loader and script names **kept** on the main engine / gated toolkit; prompt, engine internals and leaky splits superseded (audit section 1) |
| v2.10 FINAL | v2.9 behaviour re-verified (2026-10-03) | contained in the base |
| v2.8 UNIFIED, v2.7 FINAL / FINAL_SYNTH, v2.6 FINAL / final, v2.5 FIXED, v2.4 unified, neko-bot-merged | earlier rounds | contained in the base (audit: 0 commands, 0 data paths missing; every missing definition dispositioned) |

Two corrections to earlier notes: v2.12 described the v2.11 overlay packs as "the v2.11 persona engine"; the full v2.11 / v2.11.1 builds contain a different, much larger engine (`bot/persona_engine/`, 15 modules,
78 tests) that **does** disclose that it is a bot, filters contact data and abusive tokens, and is wired into the bot. That engine is what v2.14 carries; the overlay prompt that tells the model to hide that it is an AI is not.

## v2.15: uploads of the third round

| Upload | What it is | Disposition |
|---|---|---|
| `NekoBot_Elite_v2_14_UNIFIED.zip` | the v2.14 deliverable, unchanged | **Base** |
| `NekoBot_Elite_v2_13_STRENGTHENED.zip`, `NekoBot_Elite_v2_11_1_FINAL.zip` | byte-identical to the earlier inputs | already contained |
| `NekoBot_Elite_v2_13_UNIFIED.zip` | a variant of the v2.13 patch: same 7 commands, simpler `/webss`, `/gsearch`, no text-fun menu entries, same `aiohttp.helpers.quote` call | all of its functionality exists in v2.14; nothing to port |
| `SONAL_MASTER.jsonl` | Sonal's export | new person `sonal` |
| `TANVI_MASTER.jsonl` (+ `TANVI_MASTER__1_.jsonl`, a byte-identical duplicate) | Tanvi's master export, 11,846 messages from both accounts | merged into `tanvi`: +1,125 rows, 10,721 already present |
| `You_are_mine_143_Iziiee_7883544685_dataset.jsonl` (10,721 rows), `..._MASTER_dataset.jsonl` (37,190 rows incl. other senders' messages and roster context), `extracted_dataset.json` (raw group export) | account-level / raw sources for Tanvi | read for context and roster; only the registered accounts' own messages become rows (already contained) |
| `YASHMEET_MASTER.jsonl` (+ `YASHMEET_MASTER__1_.jsonl`, byte-identical), `MASTER_PERSON_GALAXY_ANHILATOR.jsonl` (4,826 rows), `..._RICH.jsonl` (1,453 rows) | Yashmeet's exports | merged into `yashmeet`: all 4,826 messages were already present; provenance refreshed |

## v2.18 reconciliation (15 archives)

| Input | Role | Result |
|---|---|---|
| v2.6 FINAL, v2.7 FINAL, v2.7 FINAL_SYNTH | ancestors (wrapper zips with older bot snapshots) | every file accounted for; 98 snapshot files found nowhere else now in `docs/provenance/superseded/legacy_snapshots_v2_6_v2_7.zip` |
| v2.8 UNIFIED, v2.9 PERSONAS, v2.10 FINAL, v2.11 FINAL, v2.11.1 FINAL, v2.12 UNIFIED | earlier lines already merged into v2.14 | re-verified: 0 files, 0 commands missing; definitions as in the v2.14 table |
| v2.13 STRENGTHENED | overlay of six files on v2.12 (also contains a copy of v2.12) | contained in v2.14; re-verified |
| v2.14 UNIFIED | **common base** of the two later lines | the three-way merge base |
| v2.15 UNIFIED, v2.16 UNIFIED, v2.17 UNIFIED | **line A** (ChatFight, PsstRobot, Kugisaki/Masha/Saitama, SangMata, math, polls, welcome mute, MyAnimeList) | base of the v2.18 tree |
| v2.15.1 UNIFIED | **line B** (Sonal, dataset merge, authorization, 10 languages) | 50 files taken as they were; 8 files changed by both lines merged (see CHANGELOG v2.18) |

No archive was a superset of the others: v2.17 had 593 commands but lacked line B (the `/lang` plugin, `Chat.language`, Sonal, the refreshed Tanvi/Yashmeet data); v2.15.1 lacked everything of line A.

