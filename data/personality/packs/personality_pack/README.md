# Novabot / Neko Multi-Personality Dataset Pack

Production-ready expansion of the original Nanora personality system.

**Nanora note:** Core identity is dark comedy + confident/casual authority + short punchy sentences + occasional anime/gaming/internet humor. Replies give the user what they deserve (support, tough love, or knowledge/opinion). Pigeon references in the original dataset were largely adaptation to the user’s display name / emoji, not a fixed obsession.

## Contents

```
personality_pack/
├── schema.json                 # Formal JSON schema for personality definitions
├── generator.py                # Self-contained large-scale JSONL generator
├── README.md                   # This file
├── personalities/              # 24 complete personality definitions
│   ├── nanora.json
│   ├── neko.json
│   ├── chaotic.json
│   ├── mean.json
│   ├── cute.json
│   ├── energetic.json
│   ├── robot.json
│   ├── supportive.json
│   ├── shy.json
│   ├── serious.json
│   ├── pirate.json
│   ├── medieval.json
│   ├── tsundere.json
│   ├── yandere.json
│   ├── deadpan.json
│   ├── philosopher.json
│   ├── gamer.json
│   ├── weeb.json
│   ├── cyberpunk.json
│   ├── parental.json
│   ├── villain.json
│   ├── poet.json
│   ├── minimalist.json
│   └── chaotic_good.json
├── banks/
│   └── question_bank.json      # 1165 unique questions (original 1093 + expanded)
└── samples/                    # Ready-to-use sample datasets
    ├── nanora_400.jsonl
    ├── chaotic_300.jsonl
    ├── mean_250.jsonl
    ├── cute_250.jsonl
    ├── robot_200.jsonl
    ├── tsundere_250.jsonl
    ├── weeb_200.jsonl
    ├── cyberpunk_200.jsonl
    ├── deadpan_150.jsonl
    ├── villain_150.jsonl
    ├── supportive_200.jsonl
    ├── gamer_150.jsonl
    └── philosopher_150.jsonl
```

**Total sample examples shipped:** ~2 850  
**Personalities defined:** 24  
**Question bank size:** 1 165 unique prompts across 9 categories

## Personality format

Every file in `personalities/` follows the schema in `schema.json` and is directly compatible with the existing bot’s `custom_instructions.py` / preset loader. Key fields:

- `sarcasm_level`, `metaphor_frequency`, `deadpan`, `deadpan_safe`
- `persona_context` (system-prompt style instruction)
- `themed_openers` / `themed_closers` / `themed_prefixes`
- `emoji_pool`, `metaphor_pools`
- `example_responses` (few-shot gold)

## Generating large datasets (15k–30k+)

```bash
# Single personality, 5 000 examples
python generator.py --personality nanora --count 5000 --seed 42

# Every personality, 1 000 examples each
python generator.py --all --count 1000

# List available IDs
python generator.py --list
```

The generator is fully self-contained (no external LLM required). It combines:

1. The expanded question bank derived from the original Nanora 1 093 examples
2. Personality-specific openers, closers, prefixes, emoji and metaphor pools
3. Combinatorial templates + light deduplication

For maximum quality at 15k–30k scale you should:

- Run the generator multiple times with different seeds
- Deduplicate the resulting JSONL
- Optionally pass a subset through a real model for style refinement
- Or use the generated data as few-shot / soft-prompt material

## Integration with the existing bot

1. Copy any `personalities/*.json` into the bot’s `data/personality/presets/` (or skins) directory.
2. The existing `CustomInstructionProcessor` already understands `keywords`, `persona_context`, `themed_*` fields.
3. Sample JSONL files can be used for few-shot banks, evaluation, or fine-tuning.

## Source lineage

- Original Nanora dataset: 1 093 high-quality Q&A pairs (dark comedy, punchy, adaptive style; pigeon references were mostly situational adaptation to user display name, not core identity)
- Existing presets: chaotic, cute, energetic, mean, medieval, pirate, robot, serious, shy, supportive
- Existing skins: neko, cheerful, (stub) nanora
- This pack expands the preset set to 24 fully-specified personalities and provides the tooling to scale example data to production volumes.

## License / usage

Intended for use with the Novabot / Neko / Nanora codebase. Personality definitions and generated data may be used for training, evaluation, or product features.
