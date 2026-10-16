# PERSONA_REPORT (v2.11) — measured behaviour, no asserted traits

Source: `data/personality/persons/<id>/behavior_profile.json` (machine-readable, aggregates only), built by `scripts/build_behavior_profiles.py` from the scrubbed corpus (8,584 Tanvi and 3,022
Yashmeet text/emoji messages). Every statement below is a measured rate; nothing is labelled that the data cannot support (no sarcasm/irony/diagnosis labels).

| Measure (share of messages) | Tanvi acct 1 | Tanvi acct 2 | Tanvi all | Yashmeet |
|---|---|---|---|---|
| messages | 7,148 | 1,436 | 8,584 | 3,022 |
| ≤ 2 words | | | 58.4 % | 48.4 % |
| 1 / 2 / 3 / 4–5 / 6–8 / 9+ words | | | 38 / 20 / 12 / 16 / 9 / 4 % | 32 / 17 / 14 / 20 / 11 / 7 % |
| contains an emoji | 13.1 % | 24.4 % | 15.0 % | 22.2 % |
| no end punctuation | | | 75.5 % | 68.0 % |
| contains `?` | | | 8.2 % | 7.9 % |
| stretched word ("soooo") | 11.1 % | 7.2 % | 10.4 % | 1.3 % |
| romanised-Hindi markers (≥ 20 % of words) | 5.5 % | 22.3 % | 8.3 % | 29.6 % |
| laughter-type replies / question replies | | | 4.7 % / 9.3 % | 4.0 % / 11.4 % |
| most used emoji | | | 😭 🗿 😂 😔 ✨ | ☠ 😂 💀 😭 💔 |

**Reply behaviour by situation** (what the message was → what they replied; "other" = statement/other): Tanvi, to a *wh*-question: other 83 % / question back 10 %; to a greeting: greeting 42 % / other 47 %;
to a statement: other 73 % / question 10 % / laughter 6 %. Yashmeet, to a *wh*-question: other 86 % / question 8 %; to a greeting: greeting 16 % (others vary). Distributions are smoothed toward the person-wide
distribution when a situation has < 12 examples (Yashmeet's greeting/yes-no rows are thin).

**Unusual words and recurring expressions** (words used ≥ 8 / ≥ 10 times, in ≥ 5 conversations, clearly more often than the people around them; plain-letter tokens only, blocked words and names removed) are in the profile
file; for example Tanvi's *lol, haha, lmao(o), ohh, hnn, ehh, cz* and Yashmeet's romanised-Hindi spellings. They are offered to the model as "use only when natural", and the anti-repetition guard caps each against
the person's own base rate.

**Timing** (seconds since the previous message in the same conversation): Tanvi n = 7,455: p10 2 · p50 6 · p90 43; Yashmeet n = 2,213: p10 4 · p50 16 · p90 113. Used only if `PERSONA_ENGINE_HUMAN_DELAY=true` (capped at 6 s).

## What is genuinely context-dependent (permutation test + held-out check)

| Habit | Depends on chat? | Depends on account? | Evidence |
|---|---|---|---|
| Tanvi emoji | **yes** (8 chats compared, p = 0.007; CV gain +0.0027, 5/6 splits) | **yes** (13.1 % vs 24.4 %, p = 0.003, CV +0.0065, 6/6) | held-out test log-loss gain +0.018 (chat), +0.022 (account) |
| Tanvi romanised Hindi | yes (p = 0.003, CV +0.024, 6/6) | yes (5.5 % vs 22.3 %, CV +0.024, 6/6) | |
| Tanvi stretched words | no | yes (p = 0.01, CV +0.0007, 5/6) | |
| Tanvi short replies | significant (p = 0.003) but **rejected in the shipped profile**: CV gain 0.0002, positive in 3/6 (in the train-only evaluation build it passed the gate and then showed −0.008 on held-out data) | | not a plan habit; not used |
| Tanvi `?`, no end punctuation, `!`, lowercase start | not shown to depend on chat/account after the validation gate | | person-wide rates |
| Yashmeet romanised Hindi | yes (2 chats compared, p = 0.003, CV +0.0017) | single account | |
| Yashmeet everything else | not shown | – | person-wide rates |

Only validated habits receive chat/account-specific rates in the sampled plan (emoji, stretched words for the plan; Hinglish is described, not conditioned). The two Tanvi accounts differ most in
emoji and Hinglish use: they are kept as separate layers rather than averaged.

## Limits

No humour taxonomy beyond observable cues; no claim about personality, mood or intent. The dialogue-act classifier is coarse and heuristic. 8 chats / 1 community: nothing is established outside it.
Habit tests have modest power for small chats (< 60 messages are not compared).
