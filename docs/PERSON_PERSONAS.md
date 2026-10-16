# Real-person personas (Tanvi, Yashmeet, Sonal)

Skins that imitate the **chat style** of three real people, built only from the chat exports supplied to the bot owner.
They go through the same pipeline as every other skin (`/cpersonality skin`, per-chat persistence, mention/reply/DM/group
handling, cooldowns, message limits, anti-repetition, LLM hook, logging) — nothing bypasses it.

## Identity model

```
person  ->  accounts (numeric Telegram id / username)   data/personality/persons/registry.json
        ->  conversation history                         persons/<id>/corpus.jsonl   (scrubbed, provenance per row)
        ->  persona profile                              persons/<id>/profile.json   (computed statistics)
        ->  behavioural model                            bot/personality/persons.py  (retrieval + LLM conditioning)
```

| Person | Accounts (all one person) | Skins |
|---|---|---|
| **Tanvi** | `7883544685` @You_are_mine_143 (display "Iziiee") · `7187486402` @ichiiren | `tanvi` (both accounts) · `tanvi_iziiee` / `tanvi_ichiiren` (one account each) · alias `iziiee` |
| **Yashmeet** | `5553376334` @concyclicity (display "GALAXY ANHILATOR") | `yashmeet` · aliases `galaxy_anhilator`, `galaxy_annihilator` |

* Resolution is deterministic: numeric id first, then username. Display names are **never** used to identify anyone
  (anyone can copy a display name); they only work as skin aliases.
* One person = one cooldown / per-user message-limit bucket across all their accounts.
* The account-specific variant skins exist because the data shows real differences between Tanvi's two accounts
  (e.g. 25% vs 9% romanised-Hindi messages, 24% vs 13% emoji) — the combined skin does not flatten them away
  for users who want one account's voice.
* Person skins reply only from that person's data. The Neko sub-plugins (programming/anime/gaming banter) are skipped, so no
  Neko text appears in their voice.

## What was learned from the data (computed, see `profile.json`)

| | Tanvi | Yashmeet |
|---|---|---|
| Messages kept | 10,721 (8,527 + 2,194) | 4,826 |
| Median / p90 words | 2 / 6 | 3 / 8 |
| Messages ≤ 3 words | 70% | 59% |
| Starts with a capital | 98% | 90% |
| All-lowercase | 1% | 8% |
| Ends in `. ! ?` | 9% | 10% |
| Contains emoji | 15% (😭 🗿 😂 😔 ✨) | 16% (☠ 💀 💔 😭 ✅) |
| Stretches words ("hieee", "nooo") | 10% | 1% |
| Romanised Hindi (Hinglish) | 12% (25% on ichiiren) | 38% |
| Commands to other bots | 4% | 17% |
| Media-only messages | 16% | 20% |

Nothing else is asserted about personality. Humour/sarcasm/"vibe" are not inferred from account names or guessed;
they enter the bot only as the person's own words (retrieval) and measured facts (LLM prompt).

## Architecture choice

The data is short, reply-structured chat (median 2–3 words). Through v2.9 the base model was **not** fine-tuned, and these two layers remain the default and the fallback. v2.10 adds an optional, authorization-gated weight-level layer (LoRA adapter per person) in front of them - [docs/FINE_TUNING.md](FINE_TUNING.md). The two base layers (v2.11 adds the free-first [persona engine](PERSONA_ENGINE.md) in front of them, off by default):

1. **Retrieval persona (no LLM needed).** IDF-weighted match of the incoming message against the contexts the person
   actually replied to; the reply is one of that person's own replay-safe messages. Falls back to class tables
   (reply-by-context-type, short replies by type), then to their most common quick replies.
2. **LLM conditioning (when `PERSONALITY_LLM_ENABLED=true`).** System prompt = disclosure/safety rules + measured style facts +
   up to 4 real exchanges nearest to the message; output is forced to one short message. Any failure falls back to (1).

## Privacy and safety

* The raw exports are **not** shipped. `corpus.jsonl` holds only the person's own messages, scrubbed: URLs, e-mails, phone-like
  numbers, ≥6-digit numbers, `tg://` links and `@handles` replaced by placeholders; chat ids are one-way hashes; other people's text appears
  only as a truncated, scrubbed reply context.
* Verbatim replay (`"rp": 1`) is limited to messages of ≤ 8 words built only from words the person uses habitually (≥ 4 uses),
  containing no roster member names and no abusive token (list in `registry.json → replay_filter`). Such lines still count in the statistics.
* The prompt tells the model it is a bot imitating the style, must say so if sincerely asked, and must not invent personal facts.
* Selecting one of these skins shows an imitation notice (`PERSON_PERSONA_DISCLOSURE`).
* **Opt-out:** `/pconsent optout` (by the person, from any of their accounts, or by the bot admin on their behalf) switches imitation
  off immediately; chats using the skin fall back to the default skin; the skin cannot be selected. Only the person can `optin`.
  If opt-out state is unreadable the bot fails closed (no imitation).
* `PERSON_PERSONAS_ENABLED=false` hides and disables every person skin.
* Groups: the skin is chosen per chat by that chat's admins; ambient replies remain off by default in groups.

## Commands

| Command | Who | Purpose |
|---|---|---|
| `/cpersonality skin tanvi\|tanvi_iziiee\|tanvi_ichiiren\|yashmeet` (or alias `galaxy_anhilator`) | chat admin | use a person skin |
| `/ppersons` | owner | registered people, accounts, skins, record counts, opt-out state |
| `/pwho [id\|@user]` (or reply) | owner | which person an account belongs to |
| `/pconsent [status\|optout\|optin]` | the person (admin may `optout` for them) | opt out of / back into imitation |

Existing `/plist /pshow /preload /padd /pdel /pmerge /pset /pdata /pmeta /pjson` work on these skins like any other
(`/preload` also reloads the registry and persona data).

## Rebuilding the data

```
python scripts/build_person_personas.py --source <each export file> ...   # .jsonl .json .csv .db; mixed schemas are detected
```

Deterministic (byte-identical output for identical input), stdlib-only, idempotent. Accepts the plain, "master", "rich" and
roster-shaped exports; duplicate messages across files are merged by (chat, message id); rows with an unknown sender are
counted and excluded. `data/personality/persons/build_report.json` records what each source contributed.

To add a person: add an entry to `registry.json`, rebuild. No code changes.

## Sonal, and the dataset update (v2.15)

- **Sonal** (`sonal`): account `5190868757` (`@FromPlanet_007897814`, display name 𝐼𝓂𝓈𝑒𝑜𝓅), real name Sonal; aliases `sonal`, `imseop`. 3,900 messages from the supplied
  `SONAL_MASTER.jsonl`; skin, profile, behaviour profile, hash-only reference manifest and an authorization record (reply of 09/10/26 8:49 pm) like the other two.
- **Updating without losing anything.** `python scripts/build_person_personas.py --merge-existing --source <exports...>` unions the new exports with the shipped corpus by row id
  (the id is derived from person, chat and message id, so the same message in an old and a new export is one row). Nothing in the old corpus is removed or rewritten; a row present in both
  only gains provenance (`src`) and context fields it lacked; an old row's replay flag (`rp`) can be withdrawn by a larger roster but is never newly granted. The merge reports `added`,
  `already_present`, `enriched`, `only_in_old`, `text_differs_kept_old` per person (`persons/build_report.json`).
- **This update:** Tanvi 10,721 -> 11,846 rows (+1,125 on account 7883544685; account 7187486402 unchanged at 2,194), Yashmeet 4,826 -> 4,826 (the new export held the same messages; provenance
  refreshed), Sonal 0 -> 3,900. Zero old rows lost, zero changed (`tests/test_persona_registry_identity.py` keeps size floors). `extracted_dataset.json` is a raw group export that also contains other
  people's messages: only registered accounts' own messages become rows; others appear only as the same scrubbed reply context as before, and nothing else from it is stored.

