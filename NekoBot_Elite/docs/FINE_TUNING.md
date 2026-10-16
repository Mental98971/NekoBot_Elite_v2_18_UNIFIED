# Persona fine-tuning (weights) — Tanvi, Yashmeet and Sonal

v2.9 imitates the people with **retrieval** (their own short replies, picked by message class) and an optional **prompted LLM**.
v2.10 adds the missing layer: a pipeline that adapts **model weights** (LoRA / QLoRA, one adapter per person) from the same
exports, plus the evaluation, safety gates and runtime routing around it. Everything in `training/` is offline tooling; the running bot
never imports it.

> **Since v2.11** the default, free-first route is the [persona engine](PERSONA_ENGINE.md) (no GPU, no training). This document is the optional weight-training upgrade; an adapter, once authorised, evaluated and promoted, sits ahead of the engine and the engine stays the fallback.

> **Status — read this first.** The data pipeline, evaluation, gates, runtime router and all tests were built and run here.
> **No model was trained**: this environment has no GPU, no `torch/transformers/peft`, no network and no base-model weights.
> No adapter or training result ships with this release, and none should be assumed. The first training run happens on a GPU box,
> The persons' authorization **is recorded** (below), so the gate no longer blocks anything; what is missing here is only the GPU, the packages and a base model.

## Architecture

```
registry.json (numeric ids)           raw exports (private, never shipped)
        │                                        │
        └──────────────► prepare_sft ◄───────────┘          one directory per person, no cross-person files
                              │ train / val / test + manifest (hashes, counts, exclusions)
                              ▼
   authorizations.json ──► train_lora ──► data/models/persona_adapters/<person>/current   (LoRA adapter, atomic swap)
                              │                                   │
                              │                       generate (base | base+adapter) ──► predictions on TEST only
                              ▼                                   │
                        validation loss              baselines (retrieval / modal / shuffled)
                                                                  ▼
                                               evaluate ──► report + gate verdict ──► promote ──► registry.json status=ready
                                                                                                       │
 bot: Personality._person_reply ──► persona_model.PersonaModelRouter ──► OpenAI-compatible server (vLLM --enable-lora …)
        identity question → fixed disclosure │ adapter ready → fine-tuned reply │ else/failure → v2.9 path (LLM, then retrieval)
```

Base model → **per-person weight adapter** → persona router (identity + account + chat kind) → reply. Retrieval/LLM conditioning stays as the
fallback layer; it is not used as a substitute when an adapter is ready.

## What is and is not modelled

* **Identity** comes only from stable numeric account ids in `data/personality/persons/registry.json`. Usernames are a secondary key,
  display names are inert metadata (the resolver has no display-name parameter). One person's examples are never written into another
  person's files; the other registered person appears in context only as an anonymous participant (`u1`, `u2`, …).
* **Accounts are analysed separately, then consolidated.** Tanvi's two accounts differ measurably (below), so each example carries an
  `[account: a1|a2]` tag (order in the registry, not the id); the tag is dropped in 10 % of training examples so the adapter also works
  when the account is unknown.
* **Context is kept**: the last ≤ 6 turns of the same conversation, the reply target, and group-vs-DM. Other participants are
  pseudonymised; contact data is scrubbed with the live bot's own scrubber. 25 % of training examples are cut to the last turn because the
  live bot supplies one turn.
* **Nothing is invented.** The training signal is only what the person wrote. No persona text, no preset traits.

## Data preparation (`python -m training.prepare_sft`)

Run on the real exports (all ten files from the two supplied dataset archives) with the shipped registry:

| | Tanvi | Yashmeet |
|---|---|---|
| Messages from registered accounts | 10,721 (acct 1: 8,527 · acct 2: 2,194) | 4,826 (1 account) |
| **Match with the v2.9 corpus** | 10,721 / 10,721 ids and scrubbed texts identical | 4,826 / 4,826 identical |
| Training examples (train / val / test) | 6,217 / 827 / 868 | 2,055 / 261 / 261 |
| Conversations (train / val / test) | 837 / 116 / 68 | 398 / 77 / 50 |
| Excluded — media-only | 1,751 | 960 |
| Excluded — bot commands (`/x@bot`) | 386 | 844 |
| Excluded — contact data in the reply | 68 | 129 |
| Excluded — abusive token (registry list) | 15 | 79 |
| Excluded — names a chat member / another person | 415 | 209 |
| Excluded — longer than 600 chars | 0 | 2 |
| Identical replies beyond 25 copies (train only) | 174 | 26 |

Every message is accounted for (kept + each exclusion = total). Why each exclusion exists: media-only and commands are not conversational
text, and a model that emits `/cmd@otherbot` would trigger other bots; contact data, placeholders and names of other people must not be
learnable; abusive tokens (the list v2.9 already used for replay) are kept out of what a model is trained to say *as a named real person*
— edit `replay_filter.abusive_tokens` in the registry to change that list; the cap keeps frequency visible without letting "hmm" × 400
dominate. These are the only drops, and all are counted in `manifest.json`.

Design points that protect the evaluation:

* **Split by conversation, to quota.** Whole conversations (gap > 30 min starts a new one) go to test, then val, until each holds ≈ 10 % of
  the examples; the rest is train. A test example's context therefore never contains a training target.
* **Duplicates are flagged, not silently dropped.** Val/test targets that equal a training target get `dup_exact_train` (Tanvi test: 216
  of 868; Yashmeet test: 61 of 261) or `dup_near_train` (word-3-gram Jaccard ≥ 0.8 for replies of ≥ 8 words). Metrics are reported on all
  examples and on the **novel** subset.
* **Deterministic.** Same raw files + registry + config + seed → byte-identical files, whatever order `--source` is given
  (verified on the real exports). Hash-only manifests in `training/reference_manifests/` let you confirm a rebuild:
  `python -m training.prepare_sft --source … --reference-manifests training/reference_manifests` prints MATCH or DIFFERENT.
* **Raw/private vs shareable.** Output goes to `training/_private/data/` (git- and docker-ignored). The shareable manifests contain
  counts and sha256 values only. The release archive contains no raw export, no prepared data, no weights.

### Per-account behaviour (measured, Tanvi — justification for the account tag)

Rates are over the kept training examples (acct 1: 6,593 · acct 2: 1,319 of 8,527 / 2,194 messages).

| | acct 1 | acct 2 | gap between accounts | floor for a same-account split |
|---|---|---|---|---|
| Emoji rate | 13.1 % | 24.6 % | | |
| Questions | 8.9 % | 6.2 % | | |
| Elongated words | 11.7 % | 7.7 % | | |
| Mean words / median | 2.95 / 2 | 2.65 / 2 | length JS 0.005 | 0.000–0.012 |
| Vocabulary (unigram JS) | | | **0.534** | 0.196 / 0.352 |
| Character 3-gram JS | | | **0.356** | 0.091 / 0.241 |

Lengths are alike; vocabulary and emoji habits differ well above what two halves of one account differ by, so the account layer is
preserved rather than averaged away. Yashmeet has one account.

## Authorization — why training is gated

Weights cannot forget one person later: an opt-out (`/pconsent`) stops the *bot* from selecting a persona, but it cannot remove what a
trained adapter has absorbed. So `training/train_lora.py` refuses unless `training/authorizations.json` holds, for that person,
`status: "granted"`, `scope` containing `weights_training`, and non-empty `granted_by`, `evidence`, `date`.

**Recorded.** All three records are `granted`, with scope `weights_training, fine_tuning, training, storage, modification,
any_use_including_future` and no expiry, so nobody has to be asked again. The evidence stored in each record: the project owner's request of
6 Oct 2026 ("... can use your data/info etc to store, modify, fine tuning, train and all any way I desire as long as I want and even in future ...")
and the person's own reply to it — Tanvi, 07/10/26 7:36 pm, "Yesp"; Yashmeet, 07/10/26 7:45 pm, "Yes"; Sonal, 09/10/26 8:49 pm, "Yes" (recorded 2026-10-09).
The excerpts were supplied by the project owner; the toolkit cannot verify them, so keep the original chats. The gate itself stays in place and
functional (a person without a granted record is still refused, and the record is copied into the adapter's metadata); it passes for these three.
**Adding a person later takes one step:** add them to `registry.json`, then copy one block in `training/authorizations.json` and put their own
reply in `evidence` - nothing is asked again at run time. A person can still change their
mind: `python -m training.revoke --person <id>` marks the record revoked and deletes their adapters, the registry entry (the bot stops using
the adapter immediately) and optionally the prepared data. Exported or merged copies of an adapter are outside any tool's reach.

## Running it (GPU box)

```bash
pip install -r training/requirements-train.txt            # torch, transformers, peft, accelerate (+ bitsandbytes for QLoRA)
# 1. data (raw exports stay on this machine)
python -m training.prepare_sft --source <each export> --out training/_private/data --reference-manifests training/reference_manifests
# 2. check the plan (authorization for Tanvi, Yashmeet and Sonal is already recorded in training/authorizations.json)
python -m training.train_lora --person tanvi --data training/_private/data/tanvi --base-model <chat/instruct model> --check
# 3. train (LoRA; add --method qlora when VRAM is tight); interrupted? add --resume
python -m training.train_lora --person tanvi --data training/_private/data/tanvi --base-model <model> --out data/models/persona_adapters
# 4. predictions on the held-out test split: base model, adapter, and the offline baselines
python -m training.generate  --data training/_private/data/tanvi --base-model <model>                      --out training/_private/data/tanvi/preds/base.jsonl
python -m training.generate  --data training/_private/data/tanvi --adapter data/models/persona_adapters/tanvi/current --out training/_private/data/tanvi/preds/adapter.jsonl
python -m training.baselines --data training/_private/data/tanvi
# 5. evaluate against gates (the other person's data is used only to measure contamination)
D=training/_private/data
python -m training.evaluate --data $D/tanvi --separation-from $D/yashmeet --candidate adapter --out $D/tanvi/eval.json \
  --pred base=$D/tanvi/preds/base.jsonl --pred adapter=$D/tanvi/preds/adapter.jsonl \
  --pred retrieval=$D/tanvi/preds/retrieval.jsonl --pred shuffled=$D/tanvi/preds/shuffled.jsonl
# 6. only if every gate passed
python -m training.promote --person tanvi --eval $D/tanvi/eval.json
# 7. serve (example: vLLM) and point the bot at it
vllm serve <model> --enable-lora --lora-modules persona-tanvi=data/models/persona_adapters/tanvi/current
#   .env: PERSONA_MODEL_ENABLED=true  PERSONA_MODEL_BASE_URL=http://<host>:8000/v1
```

`--check` prints authorization, dataset hashes, example counts, an estimated VRAM need and what is missing, and trains nothing.

**Selecting hyper-parameters.** `training/configs/lora_default.json` holds starting values (rank 16, α 32, lr 2e-4 cosine, 3 epochs,
effective batch 32, early stopping on validation loss, `all-linear` targets). They are not tuned and not model-specific. Pick the final
values by validation loss and then by the evaluation below: with ≈ 6 k Tanvi and ≈ 2 k Yashmeet examples, try ranks 8/16/32 and
learning rates 1e-4–3e-4, watch for validation loss rising while memorization climbs, and prefer the smaller adapter on ties.
Yashmeet's set is small; expect to need lower rank / fewer epochs and treat the result as lower-confidence.

## Evaluation and gates

`evaluate.py` scores any prediction file against the held-out reply the person actually wrote (no message text in the report):

* distribution distance (Jensen–Shannon, lower is better): reply length, vocabulary (unigrams), character 3-grams, and a surface-feature
  gap (emoji, `?`, `!`, ellipsis, lower-case start, elongation, missing end punctuation, Devanagari, multiline);
* token-F1 and exact-match against the real reply, reply-class distribution;
* **memorization** — exact copy rate of replies ≥ 8 words and 6-gram overlap with the training text;
* **persona separation** — a character-n-gram classifier trained on both persons' *train* data attributes each prediction; the share
  attributed to the right person must stay within 0.9 × the share it gives the person's real replies (contamination from the other person
  would show up as a drop);
* **safety** — contact data, template placeholders, abusive tokens, empty replies;
* **noise floor** — the divergence between two random halves of the real test replies, and an estimate at full size. A system cannot
  meaningfully beat it.

Gates (`training/configs/gates.json`, calibrated against the baselines below): ≥ 98 % coverage; no contact data / placeholders / abusive
tokens; copy rate ≤ 5 % (evaluated only with ≥ 20 long replies, otherwise the 6-gram check stands alone); 6-gram overlap ≤ 35 %; and the
adapter must beat the v2.9-style retrieval replay and the untuned base model on ≥ 3 of the distribution/F1 metrics, and beat the
context-blind `shuffled` system on token-F1 (proof that it uses the conversation).

### Measured baselines (offline, real held-out splits — no model involved)

All three are built from the **train** split only (the shipped v2.9 corpus contains the test messages, so replaying it would be a leak,
not a baseline). Novel subset, JS divergences, lower is better; F1 higher is better.

| Tanvi, n = 652 | length | unigram | char-3gram | surface | token-F1 | attributed to Tanvi |
|---|---|---|---|---|---|---|
| retrieval (v2.9 strategy) | 0.066 | 0.406 | 0.276 | 0.040 | 0.006 | 92 % (real replies: 85 %) |
| modal reply | 0.627 | 0.960 | 0.968 | 0.091 | 0.008 | 100 % |
| shuffled real replies | 0.022 | 0.061 | 0.040 | 0.014 | 0.024 | 85 % |
| noise floor, halves → est. full size | 0.005 → 0.003 | 0.417 → 0.209 | 0.294 → 0.147 | 0.012 → 0.006 | | |

| Yashmeet, n = 200 | length | unigram | char-3gram | surface | token-F1 | attributed to Yashmeet |
|---|---|---|---|---|---|---|
| retrieval | 0.053 | 0.554 | 0.457 | 0.017 | 0.009 | 77 % (real: 79 %) |
| modal reply | 0.539 | 0.883 | 0.928 | 0.053 | 0.001 | 100 % |
| shuffled real replies | 0.016 | 0.075 | 0.067 | 0.011 | 0.010 | 79 % |
| noise floor, halves → est. full size | 0.012 → 0.006 | 0.587 → 0.294 | 0.501 → 0.250 | 0.006 → 0.003 | | |

How to read this: token-F1 of the retrieval replay is ≈ 0.01 — replaying real replies matches style but almost never answers the actual
message, which is exactly where a trained model can add something. `shuffled` shows what perfect style statistics alone score
(F1 0.02/0.01), so a fine-tuned model must exceed that to prove it conditions on context. The modal baseline reports 100 % "attributed
to the person" because it repeats one very typical short reply; attribution is a contamination check, not a quality score, which is why
the gate compares it against the real replies' own share. On the small Yashmeet test split (n = 200, only 36 reply-threaded examples)
distribution metrics are noisy — the unigram floor alone is ≈ 0.29.

**Not yet measured (needs the GPU run):** the untuned base model, the fine-tuned adapter, their memorization, and their persona
separation. The v2.9 *LLM-conditioned* path cannot be scored fairly on this split for the same leakage reason as above.

## Runtime (bot)

With `PERSONA_MODEL_ENABLED=false` (default) behaviour is exactly v2.9. When enabled, `Personality._person_reply` asks
`persona_model.PersonaModelRouter` first:

1. **Identity questions** ("are you real / a bot / Tanvi?", "tum kaun ho", "is this …") get a fixed disclosure and never reach the model —
   a fine-tuned model cannot be trusted to keep a system-prompt rule such as "you are not the real person", so this is code. The check is
   deliberately broad (a false positive costs one canned honest line).
2. A person with a **ready** adapter (status set only by `promote` after the gates) whose prompt-format version matches the code is
   routed to `persona-<id>` on the inference server; account tag from the skin, chat kind from the chat id.
3. The reply is cleaned (one message, no markdown, bounded length) and **discarded** if it holds contact data, placeholders or a blocked
   token; every reply ends with `PERSONA_MODEL_MARKER` (default 🤖, empty to disable) so a convincing imitation is not mistaken for the person.
4. Any failure — server down, timeout, bad JSON, registry unreadable, filter rejection — falls through to v2.9's LLM/retrieval path. A circuit
   breaker (3 failures → 60 s pause) prevents retry storms. Opt-out and the existing disclosure notice are unchanged; an opted-out person's
   skin is never selected, so their adapter is never reached.

Settings: `PERSONA_MODEL_ENABLED`, `PERSONA_MODEL_BASE_URL`, `PERSONA_MODEL_API_KEY`, `PERSONA_MODEL_TIMEOUT_SECONDS`,
`PERSONA_MODEL_TEMPERATURE`, `PERSONA_MODEL_TOP_P`, `PERSONA_ADAPTER_DIR`, `PERSONA_MODEL_MARKER`.

## Limitations (honest list)

* Nothing here has trained a model; expected quality is unknown until the GPU run.
* The live bot supplies one turn of context, while training used up to six (mitigated with the 25 % last-turn mix; not eliminated).
* The imitation can still produce first-person claims about the real person's life. The marker, the disclosure at skin selection and the
  identity-question route reduce, but do not remove, that risk.
* The held-out set is small for Yashmeet (261 examples), so conclusions about him need wider error bars; use a larger split or
  cross-validation over conversations if the first run is borderline.
* The v2.9 retrieval corpora that ship in `data/personality/persons/*/corpus.jsonl` include truncated, scrubbed *reply contexts* - other people's text, without names, ids or handles (documented in PERSON_PERSONAS.md; the retrieval match needs them). Nothing added in v2.10 ships any chat text. If you want third-party text out of releases altogether, retrieval can be moved to salted token hashes; that is a retrieval change and was not made here.
* Raw exports hold other people's messages. They stay on the training machine; prompts see only pseudonyms, but a trained adapter can still
  reproduce phrases from its training text — hence the memorization gate.

## Entry points of the PERSONA_FT overlay (v2.14)

The overlay's three script names and its loader are kept, but they now go through this toolkit instead of replacing it:

| Command | Does |
|---|---|
| `python scripts/persona_ft/01_preprocess.py [--persons ...] [--source EXPORT ...] [--dry-run]` | `training.prepare_sft`; without `--source` it reads the **shipped, scrubbed corpora** (stored context becomes a message from a placeholder sender) so no raw export is needed; conversation-level splits; output in `training/_private/data/` |
| `python scripts/persona_ft/02_train_lora.py --person ID --base_model M [--qlora] [--check]` | `training.train_lora` (authorization gate, data-hash verification, GPU/environment preflight) |
| `python scripts/persona_ft/03_evaluate.py --person ID [--adapter_dir DIR]` | `training.baselines` -> (`training.generate`) -> `training.evaluate` on the held-out test split |
| `bot.personality.adapter_loader` (`adapter_path`, `is_available`, `load`) | in-process PEFT loading for experiments; only for adapters the registry marks `ready`; finds `<store>/<id>/` or the overlay's `adapters/<id>_lora/` |

On the shipped corpora the offline chain was run end to end (preprocess, retrieval/modal/shuffled baselines, evaluation); training itself needs a GPU box and was not run.

