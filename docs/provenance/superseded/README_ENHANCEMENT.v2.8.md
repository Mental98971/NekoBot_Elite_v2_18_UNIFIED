# NekoBot Elite — Tanvi & Yashmeet Personality Layers + Identity Resolution (Final)

## Overview
Complete, zero-regression integration of two real-person personas derived from multi-chat Telegram corpora, plus deterministic identity resolution.

### PERSONA A — Tanvi
- Real name: Tanvi
- Accounts (same person):
  - 7883544685 / @You_are_mine_143 / Iziiee⋆𐙚 (primary)
  - 7187486402 / @ichiiren
- Style (dataset-derived): soft, playful, affectionate, elongated words (hahahaaaaa, goodddnightttt, awwwwww), heart/cute emojis, light Hinglish, kdrama/anime/cartoon/shipping/minion energy, never mean.

### PERSONA B — Yashmeet
- Real name: Yashmeet
- Account: 5553376334 / @concyclicity / GALAXY ANHILATOR
- Style (dataset-derived): casual Hinglish, dry-playful, heavy 🗿 💀 ☠️, short blunt + sudden soft ("Aww", "Srryyy 😭😭"), anime/gaming/college (BITS) references, group energy.

## Files added / updated
```
data/personality/presets/tanvi.json
data/personality/presets/yashmeet.json
data/personality/identity_map.json          # person ↔ account mapping
bot/personality/identity_resolver.py        # optional drop-in resolver
data/personality/datasets/custom/           # full corpora (nothing dropped)
scripts/import_personas.py                  # existing importer (compatible)
```

## Installation (drop-in)
1. Copy `data/personality/presets/*.json` into your Elite install’s `data/personality/presets/`.
2. Copy `data/personality/identity_map.json` into `data/personality/`.
3. (Optional) Copy `bot/personality/identity_resolver.py` if you want programmatic resolution.
4. (Optional) Copy `data/personality/datasets/custom/` for memory/RAG/few-shot.
5. Restart the bot.

Presets auto-load via the existing TraitPresetLoader. Identity map is used only when the resolver is imported.

## Identity resolution
`identity_map.json` + `identity_resolver.py` provide deterministic:
person_id ← telegram_user_id / username / display_name / alias

Both of Tanvi’s accounts resolve to the single `tanvi` persona. Yashmeet’s account resolves to `yashmeet`. No cross-contamination.

## Activation
```
/personality set tanvi
/personality set yashmeet
# or free-form custom instructions containing the keywords
```

## Compatibility
Works with NekoBot Elite v2.5 – v2.8 UNIFIED. No core code, plugins, middleware, economy, music, games, or existing personas are modified. Zero regressions. All supplied datasets are preserved intact.
