# Engineering Summary — NekoBot Elite v2.18

How the system is built, which invariants it relies on, and what enforces them. For *what changed* in this release and how the
input builds were reconciled (v2.14: 17 archives, two diverging lines; v2.18: 15 archives, the two lines that diverged again after v2.14), see [docs/CHANGELOG.md](docs/CHANGELOG.md); for operation and configuration, the [README](README.md).

## 1. Shape of the system

| Concern | Decision |
|---|---|
| Runtime | Python 3.12 (Docker image and CI); sources are kept 3.11-compatible and a test enforces it |
| Telegram | `python-telegram-bot` ≥ 21 for the Bot API; Pyrogram + PyTgCalls only for live voice-chat music (an MTProto session the Bot API cannot provide), started from the same `post_init` hook on the same event loop |
| Persistence | One SQLAlchemy 2 async database (`bot/core/database.py`): SQLite by default, Postgres via `DATABASE_URL`. Schema upgrades are additive auto-migrations (columns and indexes are added, never dropped or altered). Handler state that must survive restarts (reminders, temp bans, queues) is in the DB; ephemeral game state is in memory by design |
| Configuration | One pydantic-settings class (`bot/config.py`), documented 1:1 in `.env.example` |
| Features | One module per feature area in `bot/plugins/`, each exposing `register(app)`; `bot/core/bot.py` discovers and loads them, and a plugin that fails to import is skipped and logged rather than taking the bot down |
| Dashboard | Separate read-only FastAPI service with its own image, sharing only `bot.config` and `bot.core.database` |

## 2. Update pipeline

1. **Concurrency.** `bot/core/concurrency.py` provides a per-chat-ordered, cross-chat-parallel update processor (size: `CONCURRENT_UPDATES`).
   Same-chat updates run one at a time in arrival order; different chats overlap. The chat's turn is taken *before* a global slot, so a backlog in one
   chat cannot starve the others. `0` restores PTB's sequential default.
2. **Middleware** (distinct negative handler groups, so each always runs): access control (−5) → force-subscribe (−4) → logging (−3) →
   antiflood (−2, groups; also slow mode) → antispam (−1, groups; also the word blacklist). Access control, force-subscribe, flood, slow mode, the blacklist and the spam
   filter stop the update with `ApplicationHandlerStop` when they act.
3. **Plugin handlers.** python-telegram-bot invokes only the *first* matching handler per group, so every catch-all message handler owns its group;
   the complete map is documented in `group_mgmt.register()` and verified against the live registrations (the only shared group, 8, holds two handlers
   with disjoint filters).
4. **Error boundary.** `@safe_handler` (`bot/core/errors.py`) turns an exception into a short, non-leaking user message and a structured log entry;
   `user_facing_error()` is the equivalent for code that handles its own exceptions.

## 2a. Person personas (v2.9)

Registry (`data/personality/persons/registry.json`) → `bot/personality/persons.py` (identity resolution, retrieval, prompt) →
`PersonalityLayer._resolve_active_skin` (the single gate: feature flag, opt-out, data present, else fall back to the default skin) →
`rewrite` / `generate_direct` (a person skin short-circuits to `_person_reply`; Neko templates and sub-plugins are never mixed in).
Cooldown and message-limit buckets are per *person*, not per account. Opt-out (`persona_optouts`) fails closed. Details: `docs/PERSON_PERSONAS.md`.

## 2b. Persona fine-tuning (v2.10)

Offline toolkit `training/` (never imported by the bot; stdlib-only except the GPU steps) → per-person LoRA adapters served by an
OpenAI-compatible inference server → `bot/personality/persona_model.py`, called first by `Personality._person_reply` when
`PERSONA_MODEL_ENABLED` is set (default off = v2.9 behaviour). Invariants and what enforces them:

| Invariant | Enforced by |
|---|---|
| Identity = numeric account id; one person's data never reaches another person's files or prompts (speakers are pseudonyms) | `training/prepare_sft.py`, `tests/test_persona_training.py::TestPrepare` |
| Training/serving prompts are the same text shape (single source `bot/personality/persona_sft_format.py`, versioned) | `TestFormat`, `TestRouter`, registry `format_version` check |
| No training without the person's recorded authorization; revocation deletes adapters and stops serving | `training.common.require_authorization`, `training/revoke.py`, `TestAuthorization`, `TestPromoteRevoke` |
| The test split is never opened by training; an adapter is only evaluated on the dataset build it was trained on | `TestTrainerLogic`, `generate.check_adapter_matches_dataset` |
| An interrupted run cannot damage the last good adapter (stage → validate → atomic swap, `previous` kept) | `TestAdapterStore` |
| Only gate-passing adapters are served; identity questions never reach a model; unsafe output is dropped; failures fall back to v2.9 | `training/promote.py`, `persona_model.py`, `TestRouter`, `TestPersonalityHook` |
| No raw/prepared data or weights in the repo or image | `.gitignore`/`.dockerignore`, `TestReleaseHygiene` |

Not exercised in CI: the GPU steps (`train_lora.run_training`, `generate.main`) - their pure logic is tested with a fake tokenizer; the
rest needs a GPU run (see docs/FINE_TUNING.md).

## 2c. Moderation core (v2.12)

Every moderation command and automatic filter goes through `bot/utils/modtools.py` and `bot/utils/chatcache.py`; nothing re-implements these:

| Invariant | Enforced by |
|---|---|
| The caller must hold the *specific* Telegram admin right, and so must the bot; a missing right is named, never echoed as a raw Telegram error | `modtools.check_caller/check_bot`, `@mod_command`; `tests/test_admin_cmds.py`, `tests/test_modtools.py` |
| Admins, the owner, the bot, bot owners and anonymous/channel identities are never moderation targets and never rate-limited by automatic filters | `modtools.target_block_reason`, `modtools.is_exempt`; `tests/test_admin_cmds.py`, `tests/test_automod_v212.py` |
| Durations never depend on the host time zone and never fall outside Telegram's 30 s – 366 d window (outside it Telegram treats the action as permanent) | `modtools.epoch_after/validate_duration`; `tests/test_modtools.py` (reproduces the IST failure) |
| Nobody can hand out an admin right they do not hold | `adminutils._promote`; `tests/test_modsettings_connect_utils.py` |
| Per-chat settings and approvals are cached for ≤ 20 s / 60 s and invalidated at commit, so automatic checks add no per-message queries; bulk DML must call `invalidate_chat` | `chatcache` commit hook; `tests/test_modtools.py::TestChatCache` |
| A remote (`/connect`) command re-verifies the sender's current membership and admin right in the target chat on every call | `modtools.resolve_ctx`, `check_caller`; `TestConnectedDm` |
| Failed enforcement is never announced as success and never raises into the update pipeline (automatic actions log and stay silent) | `modtools.enforce_action`; `TestEnforceAction`, `TestBlacklistEnforcement` |
| A v2.10 database upgrades in place (additive columns/tables, duplicate notes collapsed before the unique index) and the upgrade is idempotent | `tests/test_migration_v212.py` |
| Every `*_cmd/_callback/_listener` in the moderation plugins is registered; the handler-group map in `group_mgmt.register()` lists every group in use | `tests/test_v212_wiring.py` |

## 2d. Persona engine and the unified layering (v2.11.1 / v2.14)

Order of precedence for a real-person skin (`Personality._person_reply`): identity question -> fixed disclosure (no model) -> fine-tuned adapter if `ready` (`PERSONA_MODEL_ENABLED`) ->
**persona engine** (`PERSONA_ENGINE_ENABLED`; `bot/persona_engine/`: situation -> measured profile -> sampled plan -> diverse evidence -> prompt -> candidates -> hard gates -> rerank -> anti-repetition -> memory)
-> the v2.9 retrieval path. Every layer is off or falls back to the next one on any failure; the opt-out in `_resolve_active_skin` sits in front of all of them.

| Invariant | Enforced by |
|---|---|
| A sincere "are you X / a bot?" is answered by a fixed disclosure, in every layer and in the two-step API (`prepare().direct_reply`) | `persona_engine/identity.py`, `tests/test_persona_engine.py`, `tests/test_persona_two_step_api.py` |
| A reply that contains contact data, a blocked token, a claim to be the real person or AI talk is never sent (`finalize` returns `None`) | `persona_engine/safety.py`, same tests |
| Evidence is selective and provenance-labelled; traces hold ids/scores, never dataset text; dataset text is never a restriction on who may chat | `persona_engine/{retrieval,prompt,runtime}.py` |
| Training needs a *granted* record (owner-supplied evidence, copied into the adapter); the gate cannot be disabled by a script, flag or wrapper | `training.common.require_authorization`, `tests/test_persona_training.py::TestAuthorization`, `test_persona_ft_wrappers` |
| Revocation stops weights (`training.revoke`) and the live imitation (`/pconsent optout`) | `training/revoke.py`, `person_consent.py` |

Static layers added in v2.14 (all stdlib, run anywhere): `tests/test_ptb_names.py` (every `telegram*` import names a real python-telegram-bot object), `bot/core/menu.py` + `test_webtools_menu.py`
(a menu Telegram would reject can no longer abort startup), `bot/utils/webtools.py` (outside-URL validation, safe `href`, stable seeds).

## 2e. Activity rankings, whispers and the shared inline router (v2.15)

| Invariant | Enforced by |
|---|---|
| Only one `InlineQueryHandler` exists; plugins contribute providers and a failing provider cannot break the answer | `bot/core/inline.py`, `tests/test_whisper.py::TestInlineRouter/TestInlineDispatch` |
| A one-shot whisper is read by exactly one non-sender; a failed delivery restores it; only the recipient (id or current @username) or sender can read | `whisper_store.claim_whisper`, `tests/test_whisper.py::TestStore/TestShowCallback` |
| Whisper text is never rendered unescaped; deep-link ids are legal Telegram payloads; small numbers never become recipients | `tests/test_whisper.py::TestParsing`, `TestPmFlow` |
| Message counts survive a failed write or a restart; day/week rollover needs no job; boards include unflushed counts | `chatfight.flush_counts/flush_on_shutdown/leaderboard`, `tests/test_chatfight.py` |
| `/update` never puts the token in argv/URL/output; restart runs shutdown hooks first | `tests/test_restart_and_admin.py` |
| The composer cannot leak a secret to the personality layer | group-0 `compose_listener` stops later groups; documented in `group_mgmt.register()` |

## 2f. Languages (v2.15.1 line, merged in v2.18)

| Invariant | Enforced by |
|---|---|
| English output is byte-identical to the pre-i18n text (`/start`, `/help`, `/settings`, menu header) | `tests/english_ui_fixture.json`, `tests/test_i18n.py` |
| Every catalog has exactly English's keys and placeholders, balanced HTML, every `/command` and `ENV_VAR` of the English text, and non-Latin catalogs use their script | `tests/test_i18n.py` |
| `menu.btn.*` matches the real menu one-to-one; translating a label never changes a callback | `tests/test_i18n.py`, `bot/plugins/start.py` (`_localized_label`) |
| A chat's language lives in `chats.language` (empty = automatic); a missing key falls back to English, never to the raw key | `bot/utils/i18n.py`, `bot/utils/langctx.py` |
| Only admins change a group's language; `/ai` and the personality layer are told to answer in the chat language | `bot/plugins/language.py`, `bot/plugins/ai.py`, `bot/personality/personality.py` |

## 2g. Real-person personas: Tanvi, Yashmeet, Sonal (v2.15.1 line, merged in v2.18)

| Invariant | Enforced by |
|---|---|
| A person is only trained on or imitated with a `granted` record; a person without one is refused | `training/authorizations.json`, `tests/test_persona_training.py` |
| Every registered person is complete: corpus, skin, behaviour profile matching the corpus hash, reference manifest, granted record, size floors | `tests/test_persona_registry_identity.py` |
| Dataset updates only add: old rows are never removed or rewritten, replay flags are never newly granted | `scripts/build_person_personas.py --merge-existing` |
| Synthetic message ids are unique across people, so a multi-person run cannot overwrite another person's messages | `scripts/persona_ft/_bridge.py` (`ID_BLOCK`) and its regression tests |

## 2h. Federation, warn-filter and utility additions (v2.16 - v2.17, line A)

What each module does and where it came from: [docs/CHANGELOG.md](docs/CHANGELOG.md) (v2.16, v2.17). Each has a test module (`test_fedtools`, `test_warnfilters`, `test_name_history`, `test_kugisaki_extras`, `test_toolbox`,
`test_symbolic`, `test_pollsplus`, `test_cleanblue`, `test_welcomemute`, `test_myanimelist`). The schema they add is created by the automatic column migration; new handler groups: -7 (welcome mute), 14 (rankings), 17 (clean blue).

## 3. Cross-cutting rules

- **Output safety.** HTML-mode messages escape every member-controlled value (`escape_html`, `mention_html`). Admin-authored text (rules, welcome/goodbye)
  keeps its markup but falls back to plain text if Telegram rejects it (`html_or_plain`), and templates substitute known `{tokens}` only (`render_template`) —
  never `str.format`.
- **Secrets.** `bot/utils/redact.py` scrubs bot tokens, bearer tokens and common API-key shapes from structlog events and from stdlib log records and
  tracebacks; httpx/httpcore stay at WARNING. API keys travel in headers, not URLs. `.dockerignore` and `.gitignore` exclude `.env`, sessions and databases.
- **Atomicity.** Coin balances use conditional single-statement updates and transfers run in one transaction, so cross-chat concurrency cannot overdraw
  or lose updates (`tests/test_economy.py` fires real concurrent callers).
- **Network calls** carry explicit timeouts; user-visible failures are reported, not swallowed.
- **Defaults.** Optional gates are off (force-subscribe, font gate); the dashboard binds to localhost; features that need a key say so instead of failing.

## 4. Quality gates and what enforces them

| Gate | Enforced by |
|---|---|
| Lint (`ruff --select=F,E9`), byte-compilation | CI (`.github/workflows/ci.yml`) |
| Every command is registered once — including the dynamically registered font, effect and reaction families | `tests/test_static_contracts.py` |
| `/help`, the `/menu` panels and the Telegram command menu advertise only commands that have handlers | same |
| No two catch-all handlers share a handler group; no inline-button callback lacks a handler | same, `tests/test_hardening_contracts.py` |
| No unescaped member names/reasons in HTML messages; no removed PTB attributes; Python 3.11 syntax only | same |
| Every setting is in `.env.example` and vice versa; every `settings.<attr>` exists | same |
| Requirements ⊇ imports, and every requirement is imported or a documented implicit need | same |
| The dashboard image contains every module it imports and every package it needs | same |
| `docs/COMMANDS.md` matches the code | same + `scripts/gen_command_reference.py --check` |
| Update processor ordering, parallelism and starvation behaviour | `tests/test_concurrency.py` |
| Log redaction, safe templating | `tests/test_text_and_redaction.py` |
| Welcome/goodbye/locks behaviour | `tests/test_group_mgmt.py`, `tests/test_group_mgmt_v212.py` |
| Moderation commands, automatic filters, settings, `/connect` | `tests/test_admin_cmds.py`, `test_automod_v212.py`, `test_modsettings_connect_utils.py`, `test_modtools.py` |
| Upgrade from a v2.10 database | `tests/test_migration_v212.py` |
| Telegram import names are real; command menu valid; outside-URL validation | `tests/test_ptb_names.py`, `tests/test_webtools_menu.py` |
| Persona engine, two-step API, legacy integration, offline evaluation chain, adapter registry gate | `tests/test_persona_engine.py`, `test_persona_eval.py`, `test_persona_two_step_api.py`, `test_persona_ft_wrappers.py`, `test_persona_registry_identity.py` |
| Economy and collector atomicity | `tests/test_economy.py`, `tests/test_collector.py` |

## 5. Limits worth knowing

- Game state (trivia, tic-tac-toe, word games, chess) is in memory; a restart ends a game in progress.
- Content that a *lock* or a keyword filter deletes can still receive a later handler group's reply for the same update (cosmetic, not a moderation bypass); the word blacklist, spam filter, flood limit and slow mode run earlier and stop the update.
- Flood, slow-mode and report-cooldown counters are per process and reset on restart; slow mode is bot-enforced because the Bot API cannot set Telegram's native slow mode.
- The dashboard reads what the bot last wrote via its heartbeat job (default every 30 s); it is not a live feed.
- PyTgCalls has had breaking API changes between releases. `requirements.txt` now bounds it (`py-tgcalls<4`, `pyrogram<3`); a fresh install resolves py-tgcalls 3.0.0, whose imports and the symbols the engine uses were checked, but live voice-chat playback with it was not executed (see README, *Verifying live-music streaming*).
- The v2.14 - v2.18 builds ran without network access: the test modules that need python-telegram-bot, SQLAlchemy, pydantic or aiohttp were not executed there (list and counts in `docs/TEST_REPORT.md`); run `python -m pytest tests/ -q` once.
- Outside services used by `/truth /dare /carbon /wallpaper /webss /gps /gsearch` (truthordarebot, carbonara, Wallhaven, a public screenshot API, Nominatim, DuckDuckGo) were not called from the build environment; each has a timeout and a failure message, and `/webss` can be pointed at another service with `WEBSS_API_URL`.

## 6. Where to look

`docs/COMMANDS.md` (all commands) · `docs/FEATURE_MATRIX.md` (status by area) · `docs/FEATURE_INVENTORY.md` (per-feature history and the explicitly deferred list) ·
`docs/HISTORY.md` (earlier rounds) · `data/personality/README.md` (personality data layout).
