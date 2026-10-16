# DATASET_REPORT (v2.11)

Aggregate counts and hashes only — no message text. Produced by `training/prepare_sft.py` on the supplied exports; reproducible with
`python -m training.prepare_sft --source … --reference-manifests training/reference_manifests` (prints MATCH).

## Inventory (10 source files in the two supplied dataset archives)

| File | Size | Rows read |
|---|---|---|
| `extracted_group_dataset.json` | 5.61 MB | 11,383 messages |
| `tanvi_iziiee_dataset.csv` / `.jsonl` | 1.83 / 4.05 MB | 10,721 messages each |
| `tanvi_iziiee_master.csv` / `.jsonl` | 5.38 / 11.30 MB | 23,736 messages + 13,429 roster rows each |
| `yashmeet_galaxy_anhilator.db` / `_full.jsonl` | 0.71 / 1.41 MB | 4,826 messages each |
| `yashmeet_galaxy_anhilator.jsonl` / `_rich.jsonl` / `_rich.db` | 1.24 / 1.24 / 0.97 MB | 1,453 messages each |

* **94,308 message rows → 29,011 distinct messages.** 69.2 % of rows are repeats of the same message across overlapping exports; they are merged (first non-empty field wins; source order
  is fixed by content, not by command-line order), not discarded, so no field is lost.
* 9 group chats, 0 direct chats; the largest chat holds 32.2 % of the messages. 373 distinct senders, 3 of them registered accounts (2 persons).
* 2024-03-15 → 2026-10-02; 0 missing timestamps; 0 unknown senders; 888 messages without text (media/stickers); 21,120 have a reply link; 15,051 carry reply-context text; 0 unparseable
  conversation-flow blocks. No corrupt rows were found.
* **Language/script (messages with text, excluding media and bot commands).** Tanvi (n = 10,334): 98.9 % Latin script, 6.8 % with ≥ 20 % romanised-Hindi markers, 0.5 % emoji-only.
  Yashmeet (n = 3,095): 92.1 % Latin, 29.0 % Hinglish-marked, 7.0 % emoji-only. Everyone else (n = 13,068): 96.4 % Latin, 6.8 % Hinglish-marked, 2.7 % emoji-only. No Devanagari-script text was found.

## Identity

`data/personality/persons/registry.json` is the explicit registry: Tanvi = numeric ids 7883544685 (`@You_are_mine_143`, display "Iziiee") and 7187486402 (`@ichiiren`); Yashmeet =
5553376334 (`@concyclicity`, display "GALAXY ANHILATOR"). Resolution is by numeric id, then username; **display names are inert metadata** (the resolver has no display-name input) —
tests cover impostor display names. Two persons never share an id or username (the loader refuses a registry that does). No renamed-account or migration evidence exists in the
exports beyond the listed ids/usernames; if one appears, add the id to the person's `accounts`.

## Person corpora and training examples

| | Tanvi | Yashmeet |
|---|---|---|
| Messages from registered accounts | 10,721 (acct 1: 8,527 · acct 2: 2,194) | 4,826 |
| Match with the shipped v2.9 corpus | 10,721 / 10,721 ids and scrubbed texts identical | 4,826 / 4,826 |
| Examples train / val / test | 6,217 / 827 / 868 | 2,055 / 261 / 261 |
| Conversations train / val / test | 837 / 116 / 68 | 398 / 77 / 50 |
| Test examples with a message to answer | 834 | **52** |
| Excluded: media-only / bot commands | 1,751 / 386 | 960 / 844 |
| Excluded: contact data / abusive token / other people's names | 68 / 15 / 415 | 129 / 79 / 209 |
| Excluded: longer than 600 chars / duplicates beyond 25 copies (train) | 0 / 174 | 2 / 26 |

Each example: up to 6 preceding turns (speakers pseudonymised), reply target, chat kind, a one-way chat reference, account tag, timestamp, gap to the previous message. Every excluded message
is counted with its reason (kept + exclusions = total). The shipped v2.9 corpus is the scrubbed per-person text; **raw exports, prepared datasets and weights are never shipped**
(`.gitignore`/`.dockerignore`, `TestReleaseHygiene`). Prepared datasets: `training/_private/` (git/docker-ignored); shareable hash-only manifests: `training/reference_manifests/`.

## Leakage controls

Splits are whole conversations (gap > 30 min starts a new one), assigned to quota by seeded hash; test/val targets that equal or near-equal a training target are **flagged**
(`dup_exact_train`: Tanvi test 216 of 868, Yashmeet 61 of 261) so metrics can be read on the novel subset; evaluation indexes/profiles/scorers are built from train only and the report
checks "index conversations overlapping test = 0, index ids that are test ids = 0". A rebuild reproduces the same dataset hash regardless of source order.

## Limitations

Yashmeet's data is dominated by unprompted messages (1,971 of 3,022 text messages have no message they answer); reply-behaviour estimates for him rest on ≈ 1,050 examples. Nine chats from one
community: habits are not validated outside it. No timing is available for the *message replied to* in all cases; the latency statistic is the gap to the previous message in the conversation.
