# EVALUATION_REPORT (v2.11)

**Read this first.** This environment has no language model (no network, no GPU, no local weights). The four **generation arms the protocol asks for — baseline LLM, pure RAG, persona engine,
persona engine + reranking — were NOT run**, and no number below should be read as "how well the bot imitates". What *was* run on the real held-out data are the offline components the engine
depends on: its scorers, retrieval, repetition control and context-dependence rules, plus the v2.9-style retrieval replay as the only runnable baseline. The command that produces the missing
arms is at the end.

## Method

* Data: the prepared datasets of [DATASET_REPORT](DATASET_REPORT.md). **Conversation-level** train/val/test (whole conversations, not messages). Index, profile, persona language model and association
  model are built from **train only**; the report asserts 0 overlapping conversations and 0 test ids in the index. Design decisions (retrieval bonus, association weight) were made on **val**; the numbers below are **test**.
* Evaluated: 600 randomly drawn answerable test examples for scorer discrimination (Tanvi), 500 for retrieval, 300 for the replay arm; **Yashmeet has only 52 answerable test examples**
  (all numbers for him are low-confidence). Seeds are fixed; `training/persona_eval.py` regenerates everything.
* The report files hold aggregates only (no message text).

## 1. Can the scorers recognise the person's real reply? (offline, real data)

Each real reply is mixed with 3 distractors; MRR = mean reciprocal rank of the real reply (chance 0.521), top-1 = real reply ranked strictly first (chance 0.25; ties count against it).

| Distractors | Score | Tanvi MRR / top-1 | Yashmeet MRR / top-1 |
|---|---|---|---|
| **Assistant-style replies** ("Sure! Here's …") | persona (char-n-gram LLR) | 0.888 / 0.833 | 0.923 / 0.885 |
| | linguistic fit to plan | 0.761 / 0.667 | 0.731 / 0.635 |
| | **composite** | **0.940 / 0.900** | **0.958 / 0.923** |
| **The other person's real replies** | persona | 0.828 / 0.685 | 0.902 / 0.827 |
| | context | 0.532 / 0.170 | 0.481 / 0.077 |
| | **composite** | **0.790 / 0.642** | **0.803 / 0.673** |
| **Same person, replies to other situations** | persona | 0.481 / 0.152 | 0.481 / 0.212 |
| | context (act + trigger→reply association) | 0.514 / 0.163 | 0.520 / 0.173 |
| | **composite** | **0.499 / 0.227** | **0.490 / 0.231** |

Interpretation: the composite reliably rejects assistant voice and other-person style (persona separation, the protocol's contamination concern) — which is what the reranker is for. It is **at chance
for picking, among the same person's plausible replies, the one that fits this particular message**. Context fit therefore rests on the prompt and the model; an LLM judge (`--judge`) is the
planned measurement. The trigger→reply association added to the context score lifted its val MRR from 0.471 to 0.526 (weight 0.35 chosen on val); it is a weak signal, not a semantic model.

## 2. Retrieval (offline, test)

| | Tanvi (500 requests) | Yashmeet (52) |
|---|---|---|
| leakage: index conversations overlapping test / test ids in index | 0 / 0 | 0 / 0 |
| requests with no usable evidence | 0 % | 0 % |
| examples per request | 4.0 | 4.0 |
| retrieved examples whose situation type equals the query's | 95.9 % | 96.2 % |
| mean pairwise reply similarity: selective vs plain top-k | 0.004 vs 0.021 | 0.004 vs 0.019 |
| distinct examples used / most-used example's share | 1,235 / 0.45 % | 141 / 2.4 % |
| same-chat share (no chat bonus; share expected if chat were irrelevant) | 29.6 % (22.7 %) | 87.0 % (83.8 %; one chat dominates) |

Retrieval draws substantially on other chats (70 % for Tanvi): context is evidence, not a boundary. **Validation of that choice** (val, 500 requests): adding same-chat/account preference moved reply-type
agreement 0.553 → 0.556–0.564, length agreement and emoji agreement by ≤ 0.01, while same-chat share rose 0.31 → 0.75 — no behavioural gain, so the weights are 0.

## 3. Repetition control (offline, 30 simulated chats × 12 replies, repetition-prone replay generator)

| | near-duplicate of a recent reply | opening word repeated 3× | same emoji as previous |
|---|---|---|---|
| Tanvi without guard → with guard | 1.9 % → **0 %** | 0.3 % → 0 % | 0.3 % → 0 % |
| Yashmeet without guard → with guard | 16.9 % → **0 %** | 0.8 % → 0 % | 0.6 % → 0 % |

This measures the guard on a generator that tends to repeat, not a model.

## 4. Context dependence on held-out data (offline, test; nats per message, positive = helps)

Tanvi emoji: chat-specific rate **+0.0175**, account-specific rate **+0.0223** (val +0.009 / +0.009). "Short replies" (chat) was flagged in this train-only build too (it passed the permutation test and the
cross-validation gate there) but shows **−0.0075** on held-out test: the gate reduces false positives, it does not eliminate them. "Short" is not a plan habit, so it changes nothing in generation; the shipped
full-corpus profile does not flag it. Yashmeet: no plan habit flagged.

## 5. The only runnable end-to-end baseline: v2.9-style retrieval replay (test, novel subset)

| | length JS | char-3gram JS (floor†) | surface gap | token-F1 | persona attribution (real replies) | composite |
|---|---|---|---|---|---|---|
| Tanvi (n = 300) | 0.033 | 0.378 (0.235) | 0.030 | 0.002 | 91.7 % (86.3 %) | 0.748 (context 0.50 · style 0.82 · persona 1.0 · safety 1.0 · novelty **0.0** · coverage 1.0) |
| Yashmeet (n = 52) | 0.067 | 0.677 (0.363) | 0.051 | 0.011 | 84.6 % (78.8 %) | 0.709 |

† expected divergence between two samples of the person's own real replies at this size. The replay copies training replies verbatim (copy rate of 3+-word replies 100 %), matches style
well but almost never answers the actual message (token-F1 ≈ 0.01); its "context" sub-score is ≈ 0.5 only because it samples replies by class. **A generating system must beat 0.75 / 0.71 on the
composite, but the composite's context part is weak without the LLM judge** (see Limits).

## 6. Not run (pending, with the exact command)

| Arm | Status | Needed |
|---|---|---|
| baseline LLM · pure RAG · engine (1 candidate) · engine + reranking (3 candidates) | **NOT RUN** | a model; run the command below |
| LLM judge of contextual fit | **NOT RUN** | same |
| human check ("would someone who knows them recognise it?") | **NOT RUN** | people |

```bash
python -m training.persona_eval --data training/_private/data/tanvi --other training/_private/data/yashmeet \
  --out eval_tanvi_llm.json --provider-url <OpenAI-compatible URL> --provider-model <model> --candidates 3 --judge --limit 300
```

Success criterion to apply when it has run: the engine (and engine + reranking) should beat **both** the baseline LLM and pure RAG on the composite *and* on the judged context score, with copy rate far below the replay,
persona attribution within 0.9× of the real replies' share, and zero safety hits — otherwise do not call it an improvement.

## Limits

Composite weights are a documented judgement, not a fitted objective; without the judge its context part is dominated by reply-class agreement. Metrics compare with *one* real reply per situation although many replies
would be plausible. Yashmeet's sample (52) cannot support fine distinctions. The held-out set shares chats with training (same community); out-of-community generalisation is untested.
