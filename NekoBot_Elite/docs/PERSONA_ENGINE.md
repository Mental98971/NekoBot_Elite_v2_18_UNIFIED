# Persona engine (v2.11) — free-first persona replication, no fine-tuning required

The engine answers "given this message, what would this person most plausibly say?" **without** weight training, a GPU or paid
infrastructure. It is a modular subsystem (`bot/persona_engine/`, standard library only) behind one switch
(`PERSONA_ENGINE_ENABLED`, off by default = v2.10 behaviour). Retrieval is *evidence*, not the personality: the personality comes from a
**measured behaviour profile**, a **sampled response plan** and a **language model** that writes a *new* reply, which is then checked and ranked.

> **Status.** Built and tested here with scripted stand-in models and the real data. **No real language model was reachable from this
> environment**, so generation quality (baseline LLM vs pure RAG vs engine vs engine + reranking) has **not** been measured. The
> offline parts have been (see [EVALUATION_REPORT](EVALUATION_REPORT.md)); the command that produces the generation numbers is in §7.

## 1. Pipeline

```
Telegram message (+ reply target, + short-term memory)
  │ identity question? ──► fixed disclosure ("I'm a bot imitating …")      [never reaches a model]
  ▼
SITUATION        dialogue act (question wh/yn, request, greeting, laughter, …), language/script, cues        situation.py → core.py
  ▼
RESPONSE PLAN    sampled from the person's own P(reply act | situation), P(length | …), habit rates          profile.py
                 (length bucket, emoji?, lowercase?, end punctuation?, question back?, elongation?)           → natural variation, no fixed template
  ▼
EVIDENCE         ≤ 4 past reactions in similar situations: tf-idf + char-n-gram match on the message they
                 answered, situation match, MMR diversity, no duplicate replies, recently-shown ones demoted  store.py
  ▼
PROMPT           rules → measured profile → BOT_MEMORY → conversation → situation → plan → evidence          prompt.py
  ▼
LLM (n candidates)   any provider: local server, free tier, or the bot's own AI chain                          providers.py
  ▼
GATES            contact data · blocked words · "I am <person>" · AI-talk · too long · copies a shown example   safety.py / scoring.py
  ▼
SCORING          context · persona (char-n-gram log-likelihood ratio vs the people around them) · linguistic fit
                 to the plan · diversity · naturalness · evidence support  → composite                          scoring.py
  ▼
REPETITION       similarity to recent replies, repeated openings/emoji, catch-phrase budget vs the person's base rate   antirepeat.py
  ▼
best candidate (+ 🤖 marker)   — or None → the v2.10 retrieval persona answers (no provider, breaker open, all rejected)
```

Provenance labels are kept apart end to end: `HISTORICAL_EVIDENCE` (something the person wrote — the example store, read-only),
`BOT_MEMORY` (facts people asked the bot to remember), `CURRENT_CONTEXT` (the live chat), `GENERATED_CONTENT` (anything the bot wrote). Generated
text is never written into the evidence store; memory facts are shown in their own labelled section, marked "not things <person> said".

## 2. What "context is evidence, not a restriction" means here (measured, not assumed)

* **Retrieval never filters by chat.** A same-chat/same-account bonus existed in the first draft; on held-out validation it did **not** make the
  retrieved examples agree better with the person's real reply (reply-type, length and emoji agreement moved by ≤ 0.01) while it multiplied
  same-chat selection (0.31 → 0.75). Shipped weights are 0.
* **Context dependence is tested, then used where it was shown to matter.** For each habit, a conversation-level permutation test asks whether it
  differs between chats (or between a person's accounts); a habit is only flagged if the effect is practically meaningful **and** group-specific
  rates beat the person-wide rate on held-out conversations (6 random half-splits, positive in ≥ 5). Flagged habits get chat-/account-specific
  rates *inside the sampled plan*; nothing else is conditioned on chat. On held-out test data the flagged emoji habit improves log-loss
  (Tanvi: chat +0.018, account +0.022 nats/message). "Short replies" also passed the permutation test; the cross-validation gate drops it in the shipped profile, but in
  the train-only evaluation build it slipped through and the held-out check shows no gain (−0.008) — the gate lowers false positives, it does not remove them. Only plan habits (emoji,
  `?`, end punctuation, lowercase, stretched words) are ever conditioned on chat/account, so such a false positive has no effect on generation.
* A chat the person never joined contributes no special case: unseen chats use the person-wide rates.

## 3. Install / configure

Nothing to install beyond the bot's own requirements. Put in `.env`:

```ini
PERSONA_ENGINE_ENABLED=true
# Provider - pick ONE:
PERSONA_ENGINE_PROVIDER=auto          # auto: endpoint below if set, else the bot's AI plugin (its free-first chain: Gemini, Groq, free endpoint, OpenRouter …)
PERSONA_ENGINE_BASE_URL=http://localhost:11434/v1   # any OpenAI-compatible server: Ollama, llama.cpp server, vLLM, or a hosted free tier
PERSONA_ENGINE_MODEL=<model name on that server>
PERSONA_ENGINE_API_KEY=               # only for hosted endpoints
PERSONA_ENGINE_CANDIDATES=3           # >1 enables reranking; use 1-2 on slow/CPU-only models
PERSONA_ENGINE_EXAMPLES=4
PERSONA_ENGINE_MEMORY_ENABLED=false   # true enables /premember, /pmemory, /pforget (opt-in long-term memory)
```

Then in a chat an admin selects the person as before (`/cpersonality skin <tanvi|yashmeet|…>`; this shows the disclosure notice). The persona is
activated **only** by that explicit command and by numeric account ids in `data/personality/persons/registry.json` — never by display names or by
typing a name. `/pconsent` opt-out still disables a person entirely.

* **CPU-only:** works; use a small instruct model and `PERSONA_ENGINE_CANDIDATES=1`. Timeouts and a circuit breaker (3 failures → 60 s pause)
  fall back to the retrieval persona instead of stacking delays. *Model names above are placeholders; check what your server offers.*
* **Hosted free tiers:** the scrubbed examples and the user's message are sent to that provider. Check its current terms, limits and data policy.
* **Commands:** `/pengine [status|last|debug on|off]` (owner; counters and metadata only), `/premember <fact>`, `/pmemory`, `/pforget <id|all>` (chat admins).
* **Timing:** `PERSONA_ENGINE_HUMAN_DELAY=true` delays replies by the person's measured reply latency (median 6 s Tanvi, 16 s Yashmeet; capped at 6 s).

## 4. Data and profiles

1. Chat exports → `scripts/build_person_personas.py` (v2.9): identity by numeric id, scrubbing, `data/personality/persons/<id>/corpus.jsonl`.
2. `python scripts/build_behavior_profiles.py [--latency-from training/_private/data]` → `behavior_profile.json` (deterministic; aggregates only:
   conditional distributions, habit rates, per-chat/account tables, specificity-tested unusual words and recurring expressions, reply latency).
   It records the corpus hash; the bot rebuilds in memory if the corpus changed.
3. **New target:** add the person and their numeric account ids to `registry.json`, run the two scripts, add a skin (v2.9 builder), done. Nothing is hand-written about
   the person: traits are measured or absent. Humour is reported only as observable cues (laughter replies, emoji, stretched words); sarcasm/irony are **not**
   labelled because the offline evidence cannot support it.

## 5. Memory

* **Short-term** (RAM, per chat, ≤ 12 turns, expires after 30 min of silence): the exchange with the bot, speakers pseudonymised (`u1`, `u2`), the previous bot
  reply, topic words, an unanswered-question flag.
* **Long-term** (opt-in, SQLite `data/persona_engine/memory.sqlite3`): only facts a chat admin explicitly stores with `/premember` (≤ 200 characters, no links/handles/numbers,
  ≤ 50 per chat, optional expiry), retrieved by word overlap, deletable with `/pforget`. The bot never mines conversation for personal facts.

## 6. Privacy

Shipped: aggregate profiles and the already-shipped scrubbed corpus (v2.9). Not shipped: raw exports, prepared training data, weights. `/pengine`, traces and
`status()` expose counters, ids and scores — never dataset text or conversation text. See [DATASET_REPORT](DATASET_REPORT.md).

## 7. Evaluate (conversation-level split, no leakage)

```bash
python -m training.prepare_sft --source <exports…> --out training/_private/data          # splits whole conversations into train/val/test
# offline, no model: scorer discrimination, retrieval diversity + leakage check, repetition control, context-dependence validation, replay baseline
python -m training.persona_eval --data training/_private/data/tanvi --other training/_private/data/yashmeet --out eval_tanvi.json
# with a model: adds baseline LLM / pure RAG / engine / engine + reranking (+ optional LLM judge of contextual fit)
python -m training.persona_eval --data … --other … --out eval_tanvi_llm.json --provider-url http://localhost:11434/v1 --provider-model <name> --candidates 3 --judge --limit 300
```

The index, profile and scoring models are built from the **train** split only; the report holds aggregates, never message text, and labels every arm it did not run.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Persona answers like v2.10 | `/pengine status`: provider `none`? (set `PERSONA_ENGINE_BASE_URL`+`MODEL` or an AI-plugin key); `breaker OPEN`? (server down/slow) |
| Many fallbacks | `rejected` high: gates rejecting (see `/pengine last`); try more candidates, a stronger model, or fewer examples |
| Replies too generic / assistant-like | stronger instruct model; the scorer already penalises assistant phrasing but cannot rescue a model that ignores the plan |
| Slow | `PERSONA_ENGINE_CANDIDATES=1`, smaller model, lower `PERSONA_ENGINE_TIMEOUT_SECONDS` |
| `/premember` says memory off | `PERSONA_ENGINE_MEMORY_ENABLED=true` |
| Profile rebuilt on every start | `behavior_profile.json` is stale: rerun `scripts/build_behavior_profiles.py` (a test guards this) |

## 9. Limitations (what the numbers do and do not say)

* Generation quality is **unmeasured** here. Do not read the offline numbers as "the bot sounds like her/him".
* The reranker separates in-persona replies from assistant-style or other-person replies (top-1 0.83–0.92 vs chance 0.25) but is **at chance** at choosing the reply that fits
  *this* message among the same person's other replies. Context fit relies on the prompt and the model; an LLM judge is available in the evaluation, not at runtime.
* The dialogue-act taxonomy is coarse (≈ 76–78 % of the person's replies fall in "statement/other") and heuristic for English + romanised Hindi; its accuracy was not measured (no labels).
* Yashmeet's data is thin for reply behaviour (≈ 35 % of his messages answer something; 52 answerable test examples): treat his results as low-confidence.
* The live bot supplies a short window of context; evaluation requests use up to 6 earlier turns.
* The imitation can still state things in the first person that the person never said. The marker, disclosure, identity-question route and gates reduce, not remove, this.

## 10. Future fine-tuning (optional, not required)

The same dataset pipeline (`training/prepare_sft.py`, conversation-level splits, hash manifests), evaluation (`training/evaluate.py`, `training/persona_eval.py`) and promotion gates
serve the LoRA/QLoRA path in [FINE_TUNING](FINE_TUNING.md). A trained adapter (authorization for both persons is recorded; see FINE_TUNING), once evaluated and promoted, plugs in ahead of the engine (`PERSONA_MODEL_*`);
the engine remains the fallback. Nothing in v2.11 claims or requires fine-tuning.

## Two-step API and the overlay-compatible entry points (v2.14)

For callers that run their own model (or want the prompt without the call):

```python
prep = engine.prepare(EngineRequest(chat_id=cid, text=text))   # situation -> plan -> evidence -> finished prompt; no model call
if prep.direct_reply:                       # "are you really <person>?" / "are you a bot?": the fixed disclosure, never a model reply
    reply = prep.direct_reply
else:
    raw = await my_llm.chat(prep.messages)
    reply = engine.finalize(cid, raw, [ev.example.id for ev in prep.evidence])   # None: a hard gate rejected it - fall back
```

`finalize` applies the same cleaning and gates as `respond` (contact data, blocked words, claims to be the real person, AI talk, length), then records the reply for the repetition guard and short-term memory.

The PERSONA_ENGINE overlay's documented API is kept in `bot/personality/persona_engine/integration.py`, implemented on this engine:

```python
from bot.personality.persona_engine.integration import get_engine, prepare_persona_reply, list_available_persons, is_imitation_allowed
engine = get_engine("tanvi")                                   # registry person with a corpus; cached
payload = prepare_persona_reply(engine, text, chat_id="123")  # {person_id, messages, direct_reply, evidence[{id, score}], profile_summary}
final = engine.finalize("123", raw_model_output)               # str, or None when a gate rejects it
if not await is_imitation_allowed("tanvi"): ...               # the opt-out flag (/pconsent optout) is checked by the caller; fails closed
```

