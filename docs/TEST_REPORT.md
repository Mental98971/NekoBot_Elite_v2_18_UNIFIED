# Test report (v2.18)

Honest scope: this build was assembled in an environment **without network access and without** python-telegram-bot, SQLAlchemy, pydantic, aiohttp, httpx, cachetools or pytest. Everything that can run on the
standard library alone was run; everything else was checked statically and is listed below as **not run**. Nothing here claims a green run of the full suite.

## Run (Python 3.12, `python -m unittest tests.<module>`): 18 modules, 410 tests (10 skipped), 0 failures

| Module | Tests | Note |
|---|---|---|
| `test_catalog_integrity` | 18 | 4 skipped (need a dependency) |
| `test_concurrency` | 13 | 5 skipped (need a dependency) |
| `test_hardening_contracts` | 14 |  |
| `test_i18n` | 34 |  |
| `test_person_personas` | 24 |  |
| `test_persona_engine` | 78 |  |
| `test_persona_eval` | 11 |  |
| `test_persona_ft_wrappers` | 13 |  |
| `test_persona_registry_identity` | 11 |  |
| `test_persona_training` | 105 |  |
| `test_persona_two_step_api` | 14 |  |
| `test_ptb_names` | 2 | 1 skipped |
| `test_static_contracts` | 21 |  |
| `test_text_and_redaction` | 14 |  |
| `test_v212_wiring` | 4 |  |
| `test_v26_synthesis_contracts` | 4 |  |
| `test_webtools_menu` | 16 |  |
| `test_wordgames_uno` | 14 |  |

Includes the persona suites for all three registered people, `test_i18n` against the merged `/help`, `/menu` (53 buttons) and the 10 catalogs (113 keys each), and `test_static_contracts` (command registry, menu <-> help drift,
`.env.example` <-> config, generated command reference, Dockerfile closure).

## Not run: 35 modules (the first import needs a package that cannot be installed here)

| Missing package | Modules | Which |
|---|---|---|
| sqlalchemy | 21 | `test_admin_cmds`, `test_antichannel`, `test_automod_v212`, `test_chatfight`, `test_cleanblue`, `test_collector`, `test_collector_v212`, `test_economy`, `test_fedtools`, `test_fun`, `test_group_mgmt_v212`, `test_kugisaki_extras`, `test_migration_v212`, `test_modsettings_connect_utils`, `test_modtools`, `test_name_history`, `test_pollsplus`, `test_reactions`, `test_toolbox`, `test_warnfilters`, `test_welcomemute` |
| telegram (python-telegram-bot) | 5 | `test_group_mgmt`, `test_restart_and_admin`, `test_stickers`, `test_symbolic`, `test_whisper` |
| pydantic | 2 | `test_config`, `test_personality_e2e_reply` |
| aiohttp | 3 | `test_music_download`, `test_reference`, `test_utilities` |
| httpx | 1 | `test_myanimelist` |
| cachetools | 2 | `test_personality_pack`, `test_preset_core_protection` |
| cachetools + pydantic + telegram | 1 | `test_personality (39 of its tests ran; the rest need the stack)` |

They cover the plugins of both lines (`chatfight`, `whisper`, `fedtools`, `warnfilters`, `name_history`, `kugisaki_extras`, `toolbox`, `symbolic`, `pollsplus`, `cleanblue`, `welcomemute`, `myanimelist`, the collector, moderation and music) and everything that
touches the `/lang` plugin, `Chat.language` and the rewritten `/start`, `/help`, `/settings` and menu callbacks. They were checked statically but **not executed**. Run
`pip install -r requirements.txt -r requirements-dev.txt && python -m pytest tests/ -q` once before deploying, then `python scripts/gen_command_reference.py --check`.

## Static checks (all 215 Python files)

| Check | Result |
|---|---|
| `compileall` | clean |
| Syntax, undefined names, unresolved `bot.*` / `training.*` imports and names | 0 findings (the checker was verified on planted faults) |
| New unused imports / unused locals (a pyflakes-style approximation; `ruff` is not installed here) | 0 compared with v2.17 and v2.15.1 |
| `from telegram... import X` names against python-telegram-bot's public API | all real (`tests/test_ptb_names.py`) |
| Command registry | 597 names, 45 plugins, 0 duplicates; command menu 100 entries (includes `/lang /whisper /rankings /mytop`), all registered and valid |
| `docs/COMMANDS.md` | current (`--check`) |
| Catalogs | 10 x 113 keys, same keys and placeholders as English, HTML balanced, `menu.btn.*` matches the 53 real menu buttons one-to-one |

## Merge checks (v2.18)

- Three-way merge against v2.14 of the 8 files changed by both lines: 3 merged without conflict, 2 resolved by hand (`bot/core/bot.py` menu, `bot/plugins/start.py` `/help`), 3 documents merged or regenerated.
- English `/help` built from the catalog equals v2.17's English `/help` plus the language hint (fixture refreshed, test green).
- Callback patterns: nothing else matches `lang:*`; the plugin loader discovers `language.py` by itself; the automatic column migration adds `chats.language` and the line A columns (the chat cache derives its columns from the model).
- Zero-loss audit of all 15 archives (files, definitions, commands, corpora, structured data): 0 missing - see [ZERO_LOSS_AUDIT.md](ZERO_LOSS_AUDIT.md).

## Data checks

- Tanvi / Yashmeet / Sonal corpora: 11,846 / 4,826 / 3,900 rows, byte-identical to v2.15.1's; every string of every older corpus is still present (0 absent over all 15 inputs).
- Preprocessing from the shipped corpora keeps every message and splits by conversation; the hash-only reference manifests reproduce from the raw exports (`--reference-manifests` exit 0, v2.15.1).

## What no test here can show

- Behaviour against a real database and the live Telegram API (`/lang`, menu callbacks, `set_my_commands`, inline whispers, HTML captions); calls to the outside services behind `/truth /dare /carbon /wallpaper /webss /gps /gsearch` and Jikan.
- Translation quality (AI-written catalogs, no native-speaker review) and persona quality with a real model (none available); LoRA training and adapter loading (no GPU); live voice-chat playback.
