# Zero-loss audit (v2.14)

Question: is anything that any of the 17 supplied archives (and the older bot snapshots nested inside the personality archives) contained missing from v2.14?
Answered at four levels, by script (the scripts are plain `ast`/`hashlib`; nothing was judged by eye except the dispositions below).

## 1. Definitions (functions, classes, methods, tests)

Every top-level and class-level `def`/`class` in `bot/ training/ scripts/ tests/ dashboard/` of every input, looked up by (file, qualified name) in v2.14; if the file moved, by qualified name anywhere
("relocated").

| Input | Definitions | Present | Relocated | Not found |
|---|---|---|---|---|
| v2.4 unified | 1,096 | 1,083 | 8 | 5 |
| v2.5 FIXED | 841 | 828 | 8 | 5 |
| v2.6 FINAL | 875 | 861 | 8 | 6 |
| v2.6 final | 1,180 | 1,166 | 8 | 6 |
| v2.7 FINAL | 1,231 | 1,218 | 8 | 5 |
| v2.7 FINAL_SYNTH | 1,231 | 1,218 | 8 | 5 |
| v2.8 UNIFIED | 1,407 | 1,396 | 8 | 3 |
| v2.10 FINAL | 1,525 | 1,514 | 8 | 3 |
| v2.11 FINAL | 2,179 | 2,167 | 8 | 4 |
| v2.11 AUTHENTICATED | 2,179 | 2,167 | 8 | 4 |
| v2.11 AUTH_BYPASS | 2,179 | 2,167 | 8 | 4 |
| v2.11.1 FINAL | 2,206 | 2,195 | 8 | 3 |
| v2.11 PERSONA_ENGINE (overlay) | 107 | 58 | 24 | 25 |
| v2.11 PERSONA_FT (overlay) | 67 | 55 | 5 | 7 |
| v2.12 UNIFIED | 2,312 | 2,303 | 8 | 1 |
| v2.13 STRENGTHENED (patch) | 47 | 47 | 0 | 0 |
| neko-bot-merged | 1,143 | 1,094 | 27 | 22 |
| NekoBot_Production (nested in the archives) | 933 | 916 | 8 | 9 |
| neko-bot-merged (nested in the archives) | 990 | 976 | 8 | 6 |

The "not found" column contains **52 distinct definitions** in total (many inputs share them). Disposition of every one:

| Definition(s) | Inputs | Disposition |
|---|---|---|
| `middleware/antiflood._is_chat_admin`, `middleware/antispam._is_chat_admin` | v2.4-v2.11.1, older snapshots | Private duplicates of the same admin check; one cached, error-tolerant implementation `bot.utils.modtools.is_chat_admin` serves both (v2.12) |
| `plugins/group_mgmt.log_cmd` | v2.4-v2.11.1, older | `/log` is now `modsettings.setlog_cmd` (`/setlog`, `/log`) (v2.12) |
| `utils/logger._redact_event`, `_redact_value`, `configure_secret_redaction` | v2.4-v2.7 lineage, older snapshots | Moved to `bot/utils/redact.py` (`redact_event`, `redact_value`, `install_stdlib_redaction`) so structlog **and** stdlib logging share one implementation (v2.8) |
| `plugins/stickers._get_source_image_bytes` | v2.6, neko-bot-merged | Replaced by `_get_reply_media` + `_download_file_bytes` (photo, static sticker, image document - and now video stickers/GIFs) |
| `plugins/personality._message_addresses_bot`, `_run_personality_pipeline` | NekoBot_Production snapshot | Reply/mention gating and the shared generate-and-send path were refactored into the current `_is_reply_to_bot` / mention helpers and `handle_message` (behaviour covered by `test_personality_e2e_reply`) |
| 15 overlay internals: `PersonaGenerator*`, `BehavioralProfile*`, `build_profile`, `SmartRetriever*`, `ConversationMemory.context_string`, `_person_dir` ... | PERSONA_ENGINE overlay | The overlay's stand-alone engine is superseded by `bot/persona_engine/` (profile, retrieval, prompt, repetition guard, memory); its documented entry points (`get_engine`, `prepare_persona_reply`, `list_available_persons`) are kept in `bot/personality/persona_engine/integration.py` on top of the main engine. Its prompt ("Do NOT claim to be an AI") is deliberately not kept |
| 5 overlay script functions: `01_preprocess.{is_usable,process_person,stable_split,to_example}`, `03_evaluate.stats_from_jsonl` | PERSONA_ENGINE / PERSONA_FT overlays | Same command names (`scripts/persona_ft/01..03`), now delegating to `training.prepare_sft` / `evaluate`; the message-id hash split (leaks a conversation into train and test) is replaced by the conversation-level split |
| 5 overlay tests (`tests/persona/*`) | overlays | Ported to `tests/test_persona_registry_identity.py` (identity + engine smoke on the shipped data) and `tests/test_persona_two_step_api.py` |
| 15 old menu tests (`tests/test_menu_coverage.py`, `TestMenuContractSource.test_every_page_button_has_a_real_category` ...) | neko-bot-merged and older | They test the first `CATEGORIES` menu; the menu is now `MAIN_MENU/PAGE_2/PAGE_3 -> MENU_PANELS` and is covered by `test_hardening_contracts.TestMenuContractSource` |
| `test_personality_pack.TestImportedPersonaCatalog.test_manifest_is_internally_consistent_with_the_shipped_files` | neko-bot-merged | `test_catalog_integrity.test_manifest_is_consistent_with_the_shipped_files` |
| `TestPresetLoaderCoreProtection` (5 tests) | neko-bot-merged | **Restored** in `tests/test_preset_core_protection.py` (they had been dropped; the guard they protect is intact). One test was renamed |
| `TestAuthorization.test_shipped_template_grants_nothing` | v2.11, v2.12 | Replaced in v2.11.1 by tests that the *filled* records are consistent with the gate (a "ships as pending" test cannot hold once the records are granted) |

## 2. Commands

Every registered command name in every input (static `CommandHandler` strings and lists, plus the names built at registration time in `fonts.py`/`reactions.py`) against v2.14's 479 names:
**no command is absent.** Two names were *colliding*, not missing (`/slap`, `/pat` in v2.13); both features exist (GIF `/slap` `/pat`; text `/slaptext` `/pattext`).

## 3. Data

All 1,823 files under `data/` of v2.14 are byte-identical to v2.12's plus the two `behavior_profile.json` files of v2.11.1. Every `data/` path of every older input is present in v2.14, except what is
listed here (all content verified by SHA-256 against the final tree, including the contents of the old wrapper zips):

- the seven source wrapper zips in `data/personality/archives/` (v2.4-v2.7; about 16 MB): every member is present in unpacked form in the packs/presets, except three leftovers that are kept in
  [provenance/superseded](provenance/superseded/): an earlier `neko.json` skin variant (one description string differs), the `master_v5` README, and nested older bot snapshots (their code is covered by section 1);
- `all_personality_pack/` (41 files, v2.4-v2.7): byte-identical duplicates of `personality_pack/`.

The overlays' derived files (`train.jsonl`, `val.jsonl`, `test.jsonl`, `eval_report.json`, `manifest.json`, `behavioral_profile.json`, about 6 MB of chat-derived text) are regenerable from the shipped corpora
(`scripts/persona_ft/01_preprocess.py`) and are not shipped.

## 4. Bytes of everything else

`docs/`, `scripts/`, `training/`, configuration and CI files were compared with `sha256` across all inputs (the matrix in the build notes); the only files that differ between the two lines are listed and
resolved in the [changelog](CHANGELOG.md#what-came-from-where). Files present only in an overlay (`adapter_loader.py`, `persona_ft/*`, `persona_engine/*`) are accounted for in section 1.

## Limits of this audit

It proves that nothing *named* in an input has disappeared; it cannot prove that an unchanged name still behaves the same. For that, the inherited test suites were kept (v2.12's 594 and v2.11.1's engine and
training suites), and what could be run in this environment is in [TEST_REPORT](TEST_REPORT.md).


## Addendum v2.16 - Saitama / Masha / SangMata

Every command of KugisakiNobara-master, NobaraKugisaki-prince, SangMataBot-main and chatfight-main was extracted and compared with this project's command set (static scan of handler registrations, so renamed or callback-only names were checked by hand).

**Merged in v2.16:** see docs/CHANGELOG.md (federations, ban enforcement, warn filters, sticker blacklist, name-history lookup/alerts, paste/wiki/github/country/covid, notes, sed, encrypt/decrypt, RSS, gbanlist, whois, trig math, weebify).

**Covered by an existing equivalent (no change; the old command names are not aliased):** remote moderation (`/rban /rmute /runban /runmute`) is done with `/connect` plus the normal command; `/broadcastall /broadcastgroups /broadcastusers` -> `/broadcast`; force-subscribe -> the force-sub middleware; `/rm_warn` -> `/rmwarn`; polls -> `/poll`; ChatFight and PsstRobot features -> `chatfight.py` / `whisper.py` (re-checked, nothing missing).

**Not carried over - reasons:**

| Item | Reason |
|---|---|
| Telethon/Pyrogram-only modules: `/gogo` anime watch, `/saavn`, `/song`, `/stt`, `/Emcee` voice chatbot, `/grs` reverse search, `/gifid` | need a second MTProto client or third-party scraping that this PTB-based bot does not run |
| Heroku/dev operations: `/usage /set /logs /gitpull /reboot /install /dbcleanup`, `/listmodules /unload /disablemodule /enablemodule /listcmds` | deployment-specific; `/restart` and `/update` already exist and plugins are not hot-loadable here |
| `/logo /wlogo`, `/animation` set (`/bombs /clock /earth /hack /kill /love /moon`), `/bubble /fbubble /fsquare /lined /square` | cosmetic, depend on removed asset hosts or very large static frames; `/style` covers text styling |
| `/kayo /kaizoku`, power-level lists (`/heroes /dragons /demons ...`), `/punch /punchme /roar`, `/rlg /sanitize`, `/cash`, `/cs` cricket, `/gps /json /wttr` | external-API or joke-list modules; low value, not ported. `/convert` and `/weather` cover currency and weather |
| `/addnsfw /rmnsfw` NSFW image watch | needs an image-classification service |
| `/devlist /sudolist /supportlist /whitelistlist` role tiers | this project has owner / ADMIN_IDS / approved users instead of five tiers |
| `/ginfo /getchats /pingall /plet /time /markdownhelp /errors /delgpic /setgpic /setsticker /setdescription /admincache /export /import /ignoredlist /notice /donate /test ` | assessed as duplicates or out of scope; candidates for a later pass |

Nothing in this table was dropped silently: each is listed so a later pass can pick it up.

### Update v2.17

The four groups left over from v2.16 are now merged (see docs/CHANGELOG.md, v2.17):

| Item | Where | Notes |
|---|---|---|
| `/simplify /factor /derive /integrate /zeroes /tangent /area`, exact `/sin /cos ...` | `plugins/symbolic_math.py`, `utils/symbolic.py` | local sympy in a sandboxed worker instead of the Newton web API; `/log` stays the chat-log command, natural log is `/ln` |
| `/apoll`, `/poll <5 digits> \| ...`, `/stoppoll`, `/forgotpollid` | `plugins/pollsplus.py` | Bot-API polls with a persisted id registry (the original used Telethon + MongoDB) |
| `/cleanblue /ignoreblue /unignoreblue /listblue /gignoreblue /ungignoreblue` | `plugins/cleanblue.py` | `/bluetext` in this bot is a fun text command, unrelated; the cleaner did not exist here |
| `/welcomemute`, `/welcomemutehelp` | `plugins/welcomemute.py` | also makes the existing `/captcha` flag effective (it was stored but never read) |
| `/myanime /mymanga /mycharacter /myupcoming` | `plugins/myanimelist.py` | Jikan v4 REST (jikanpy/v3 is retired) |

Still not carried over: everything else in the table above.

### Update v2.18 - audit of all 15 supplied archives

v2.18 merges two branches of v2.14 (see docs/CHANGELOG.md), so every input was compared with the finished tree, not only the newest ones. Four checks, all scripted and re-runnable against the archives:

**1. Files.** Every file of every input is byte-identical to the file at the same path, edited later (code and docs were rewritten, checked in 2-4), present unchanged at another path, or - the six personality wrapper zips of v2.6 / v2.7 - a container whose every member is in the tree.

| Input | Files | Identical | Edited later | At another path | Wrapper containers | Missing |
|---|---|---|---|---|---|---|
| v2.6 FINAL | 499 | 450 | 41 | 2 | 6 | **0** |
| v2.7 FINAL | 1,975 | 1,879 | 45 | 45 | 6 | **0** |
| v2.7 FINAL_SYNTH | 1,973 | 1,878 | 44 | 45 | 6 | **0** |
| v2.8 UNIFIED | 1,937 | 1,899 | 38 | 0 | 0 | **0** |
| v2.9 PERSONAS | 1,953 | 1,904 | 49 | 0 | 0 | **0** |
| v2.10 FINAL | 1,982 | 1,921 | 61 | 0 | 0 | **0** |
| v2.11 FINAL | 2,008 | 1,943 | 65 | 0 | 0 | **0** |
| v2.11.1 FINAL | 2,009 | 1,944 | 65 | 0 | 0 | **0** |
| v2.12 UNIFIED | 2,001 | 1,947 | 54 | 0 | 0 | **0** |
| v2.13 STRENGTHENED (overlay) | 6 | 0 | 6 | 0 | 0 | **0** |
| v2.14 UNIFIED | 2,047 | 1,993 | 54 | 0 | 0 | **0** |
| v2.15 UNIFIED | 2,063 | 2,014 | 49 | 0 | 0 | **0** |
| v2.15.1 UNIFIED | 2,069 | 2,031 | 38 | 0 | 0 | **0** |
| v2.16 UNIFIED | 2,072 | 2,026 | 46 | 0 | 0 | **0** |
| v2.17 UNIFIED | 2,083 | 2,043 | 40 | 0 | 0 | **0** |

The six wrapper containers (`data/personality/archives/*.zip`, zips nested up to four deep): 29,166 distinct members, all present as loose files, in `data/personality/packs/`, or - 98 files found nowhere else (old bot snapshots, READMEs, nested archives' contents) - in
`docs/provenance/superseded/legacy_snapshots_v2_6_v2_7.zip` (with a SHA-256 manifest).

**2. Functions and classes** (every `def` / `class`, methods and nested functions included, in `bot/ training/ scripts/ tests/ dashboard/`):

| Input | Definitions | Same file | Moved to another file | Not found by name |
|---|---|---|---|---|
| v2.6 FINAL | 908 | 894 | 8 | 6 |
| v2.7 FINAL | 1,250 | 1,236 | 8 | 6 |
| v2.7 FINAL_SYNTH | 1,275 | 1,262 | 8 | 5 |
| v2.8 UNIFIED | 1,479 | 1,468 | 8 | 3 |
| v2.9 PERSONAS | 1,599 | 1,588 | 8 | 3 |
| v2.10 FINAL | 1,927 | 1,907 | 16 | 4 |
| v2.11 FINAL | 2,283 | 2,271 | 8 | 4 |
| v2.11.1 FINAL | 2,313 | 2,302 | 8 | 3 |
| v2.12 UNIFIED | 2,414 | 2,405 | 8 | 1 |
| v2.13 STRENGTHENED (overlay) | 47 | 47 | 0 | 0 |
| v2.14 UNIFIED | 2,966 | 2,965 | 0 | 1 |
| v2.15 UNIFIED | 3,226 | 3,225 | 0 | 1 |
| v2.15.1 UNIFIED | 3,057 | 3,057 | 0 | 0 |
| v2.16 UNIFIED | 3,419 | 3,418 | 0 | 1 |
| v2.17 UNIFIED | 3,622 | 3,621 | 0 | 1 |

The 8 names not found in any input's successor, all accounted for: `middleware/antiflood._is_chat_admin`, `middleware/antispam._is_chat_admin`, `plugins/group_mgmt.log_cmd`, `plugins/stickers._get_source_image_bytes`, `utils/logger._redact_event`,
`utils/logger._redact_value` - replaced by `utils/modtools.is_chat_admin`, `modsettings.setlog_cmd`, `_get_reply_media` + `_download_file_bytes` and `utils/redact.py` (dispositions in the v2.14 table above, unchanged); and two tests:
`test_lists_both_persons` -> `test_lists_all_persons` (there are three registered people now) and `test_shipped_template_grants_nothing` (v2.10 - v2.12, replaced by the `TestAuthorization` checks of the shipped records).
Nothing from line A (v2.15 - v2.17) or line B (v2.15.1) is among them.

**3. Commands** (static registry, fonts and reaction families expanded; this tree: **597 commands, 45 plugins, 0 duplicates**):

| Input | Commands | Missing from this tree |
|---|---|---|
| v2.6 FINAL | 351 | **0** |
| v2.7 FINAL | 351 | **0** |
| v2.7 FINAL_SYNTH | 363 | **0** |
| v2.8 UNIFIED | 366 | **0** |
| v2.9 PERSONAS | 369 | **0** |
| v2.10 FINAL | 369 | **0** |
| v2.11 FINAL | 373 | **0** |
| v2.11.1 FINAL | 373 | **0** |
| v2.12 UNIFIED | 452 | **0** |
| v2.13 STRENGTHENED (overlay) | 32 | **0** |
| v2.14 UNIFIED | 479 | **0** |
| v2.15 UNIFIED | 501 | **0** |
| v2.15.1 UNIFIED | 483 | **0** |
| v2.16 UNIFIED | 571 | **0** |
| v2.17 UNIFIED | 593 | **0** |

(The v2.13 overlay holds only the `extras` plugin and a few others; its 32 literal names are all present.) The 4 commands that exist only here are line B's `/lang /language /languages /setlang`.

**4. Data.** Persona corpora (`data/personality/persons/*/corpus.jsonl`): every string of every older version is still in the current corpus (Tanvi 32,945 -> 37,169 strings, Yashmeet 13,270 -> 14,754, Sonal 12,062 new; 0 absent for all 15 inputs).
Every other changed JSON / JSONL / TXT / CSV / YAML file: each string of the old file is in the new file or in a preserved copy under `docs/provenance/superseded/derived/` (**0 not preserved** for all 15 inputs). Those copies are the persona-derived
files v2.15.1 rebuilt from the larger corpora (profiles, behaviour profiles, skins, reference manifests, `build_report.json`), the authorization record when it was `pending`, the English UI fixture before its `help` text was refreshed, and older build/deploy files (`ci.yml`, `requirements.txt`, `dashboard/requirements.txt`, `docker-compose.yml`).
Requirement pins that older versions listed and the tree does not (`aiofiles`, `aiohttp-socks`, `alembic`, `lxml`, `orjson`, `prometheus-client`) are imported by no module (checked); `pyrogram` and `py-tgcalls` are still pinned.

**Not carried, as before:** nothing new. Everything listed as not carried in the earlier sections of this file still applies.
