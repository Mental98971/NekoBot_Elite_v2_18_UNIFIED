# Personality data — provenance of the retired master archives
Earlier builds shipped five cumulative `master_personality_datasets*.zip` snapshots under
`data/personality/archives/`. Every file inside them is either already present in the
loose data tree, or is an older copy of project source that lives (newer) in this codebase,
so the archives were retired to remove ~16 MB of duplicated bytes. Their per-version
README notes are kept here verbatim.

---

## master_personality_datasets.zip

```markdown
# Master Personality Datasets Archive

This archive contains **all** personality datasets from the original collection. Nothing has been omitted.

## Contents

### Core Packages
- **all_personality_datasets.zip** – Master package with nanora_dataset.jsonl, personality_pack/, and extensive neko/ skins (~100+ personalities)
- **personality_pack_full.zip** – Full personality pack with 22+ core definitions, sample JSONL datasets, question bank, generator, and nanora variants
- **neko-bot-merged.zip** – Complete Neko bot source code including personality system, skins, jokes, memory, triggers
- **nanora_dataset.jsonl** – Standalone Nanora conversation dataset (1093+ Q&A pairs)

### Expansions
- **missing_personalities_expansion.zip**
- **missing_personalities_expansion_2.zip**

### Character Styles (Waves 1–12)
- character_styles_wave1.zip through character_styles_wave12.zip
  - Anime, game, and comic character personality styles (Ainz, Aizen, Elric brothers, Anya, Batman, Denji, Eren, Frieren, Goku, Guts, Hisoka, Homelander, Itachi, Johan, Joker, Killua, L, and many more)

## Recommended usage
1. Start with `personality_pack_full.zip` or `all_personality_datasets.zip` for the core system.
2. Use the character_styles waves for specific character imitations.
3. `nanora_dataset.jsonl` is ready for fine-tuning or evaluation.

Generated: 2026-09-27
```

---

## master_personality_datasets_v2.zip

```markdown
# Master Personality Datasets Archive v2
**Generated:** 2026-09-28  
**Total packages:** Complete collection – nothing omitted

## Directory Structure

```
master_v2/
├── core/                          # Primary personality systems & datasets
│   ├── all_personality_datasets.zip
│   ├── personality_pack_full.zip
│   └── nanora_dataset.jsonl
├── expansions/                    # Additional personality definitions
│   ├── missing_personalities_expansion.zip
│   └── missing_personalities_expansion_2.zip
├── character_styles/              # Character imitation packs (Waves 1–27)
│   └── character_styles_wave{1..27}.zip
├── neko/                          # Full Neko bot + personality engine
│   └── neko-bot-merged.zip
└── README.md
```

## Contents Overview

### core/
| File | Description |
|------|-------------|
| `all_personality_datasets.zip` | Master package containing nanora_dataset.jsonl, full personality_pack (definitions + samples + generator + question bank), and extensive neko/ skins collection (~100+ JSON personality skins) |
| `personality_pack_full.zip` | Standalone personality pack: 22+ core personality JSONs, sample JSONL datasets (chaotic_300, cute_250, nanora_400, tsundere_250, etc.), nanora original/modified banks, generator.py, schema.json, README |
| `nanora_dataset.jsonl` | Standalone Nanora conversation dataset (1093+ Q&A pairs with categories, timestamps, responses) |

### expansions/
| File | Description |
|------|-------------|
| `missing_personalities_expansion.zip` | First expansion of additional personality definitions |
| `missing_personalities_expansion_2.zip` | Second, larger expansion of personality definitions |

### character_styles/ (Waves 1–27)
27 independent packs of character-style personality definitions.  
Includes anime, manga, game, and comic characters (examples from early waves: Ainz Ooal Gown, Aizen Sousuke, Alphonse/Edward Elric, Anya Forger, Batman, Denji, Eren Yeager, Frieren, Goku, Guts, Hisoka, Homelander, Itachi, Johan Liebert, Joker, Killua, L Lawliet, and many more across later waves).

### neko/
| File | Description |
|------|-------------|
| `neko-bot-merged.zip` | Complete Neko bot source tree including personality system, skins, jokes, memory, triggers, custom instructions, subplugins, economy, audio engine, etc. |

## Engineering Notes

- **No data dropped**: Every original archive from the collection is present.
- **Intelligent organization**: Grouped by purpose (core system, expansions, character styles, bot implementation) to avoid a flat dump of 30+ zips.
- **Nested zips preserved**: Original packaging is kept intact so consumers can extract only what they need.
- **Size**: ~3–4 MB compressed (exact size after final zip).
- **Recommended entry points**:
  1. Start with `core/personality_pack_full.zip` or `core/all_personality_datasets.zip` for the personality system.
  2. Use `character_styles/` for specific character role-play / imitation.
  3. Use `neko/neko-bot-merged.zip` if integrating into the Neko bot.
  4. `core/nanora_dataset.jsonl` is ready for fine-tuning or evaluation.

## File Counts
- Core packages: 3
- Expansion packages: 2
- Character style waves: 27
- Neko bot: 1
- **Total source archives: 33** + this README
```

---

## master_personality_datasets_v3.zip

```markdown
# Master Personality Datasets Archive v3
**Generated:** 2026-09-28  
**Policy:** Nothing skipped or dropped. All source archives included.

## Directory Structure

```
master_v3/
├── core/                          # Primary personality systems & datasets
│   ├── all_personality_datasets.zip
│   ├── personality_pack_full.zip
│   └── nanora_dataset.jsonl
├── expansions/                    # Additional personality definitions
│   ├── missing_personalities_expansion.zip
│   └── missing_personalities_expansion_2.zip
├── character_styles/              # Character imitation packs (Waves 1–38)
│   └── character_styles_wave{1..38}.zip
├── neko/                          # Neko bot implementations
│   ├── neko-bot-merged.zip
│   └── NekoBot_Production.zip
└── README.md
```

## Contents Overview

### core/
| File | Description |
|------|-------------|
| `all_personality_datasets.zip` | Master package: nanora_dataset.jsonl + full personality_pack (definitions, samples, generator, question bank) + extensive neko skins (~100+ JSON personalities) |
| `personality_pack_full.zip` | Standalone pack: 22+ core personality JSONs, sample JSONL datasets (chaotic_300, cute_250, nanora_400, tsundere_250, etc.), nanora banks, generator.py, schema, README |
| `nanora_dataset.jsonl` | Standalone Nanora conversation dataset (1093+ Q&A pairs) |

### expansions/
| File | Description |
|------|-------------|
| `missing_personalities_expansion.zip` | First expansion of additional personality definitions |
| `missing_personalities_expansion_2.zip` | Second, larger expansion |

### character_styles/ (Waves 1–38)
38 independent packs of character-style personality definitions covering anime, manga, games, comics and related characters.

### neko/
| File | Description |
|------|-------------|
| `neko-bot-merged.zip` | Full Neko bot source tree (personality system, skins, jokes, memory, triggers, custom instructions, subplugins, etc.) |
| `NekoBot_Production.zip` | Production-oriented Neko bot package |

## Engineering Notes

- **Completeness**: Every individual archive present in the collection is included. Previous master zips were not nested (to avoid recursive bloat); only the source packages are kept.
- **Latest versions preferred**: Where multiple versions of the same file existed historically, the current attachments versions are used.
- **Structure**: Grouped by purpose for easy navigation instead of a flat list of 40+ files.
- **Nested originals preserved**: Consumers can extract only the packages they need without unpacking everything.
- **Recommended entry points**:
  1. `core/personality_pack_full.zip` or `core/all_personality_datasets.zip` for the personality system.
  2. `character_styles/` for character role-play / imitation packs.
  3. `neko/` for bot integration (use Production or Merged according to needs).
  4. `core/nanora_dataset.jsonl` for fine-tuning or evaluation.

## File Counts
- Core packages: 3
- Expansion packages: 2
- Character style waves: 38
- Neko packages: 2
- **Total source archives: 45** + this README
```

---

## master_personality_datasets_v4.zip

```markdown
# Master Personality Datasets Archive v4

**Generated:** 2026-09-28  
**Policy:** Nothing skipped or dropped. All source archives from v3 retained. VTuber expansion added.

## Directory Structure

```
master_v4/
├── core/                          # Primary personality systems & datasets
│   ├── all_personality_datasets.zip
│   ├── personality_pack_full.zip
│   └── nanora_dataset.jsonl
├── expansions/                    # Additional personality definitions
│   ├── missing_personalities_expansion.zip
│   └── missing_personalities_expansion_2.zip
├── character_styles/              # Character imitation packs (Waves 1–39)
│   └── character_styles_wave{1..39}.zip
├── neko/                          # Neko bot implementations
│   ├── neko-bot-merged.zip
│   └── NekoBot_Production.zip
└── README.md
```

## What Changed in v4

- **Added** `character_styles/character_styles_wave39.zip` — 30 famous VTuber personality styles
- **Nothing removed or altered** from v3 core, expansions, waves 1–38, or neko packages
- README updated to reflect Wave 39 and VTuber coverage

## Wave 39 — VTuber Personalities (30)

| # | ID | Name |
|---|-----|------|
| 1 | kizuna_ai | Kizuna AI |
| 2 | gawr_gura | Gawr Gura |
| 3 | houshou_marine | Houshou Marine |
| 4 | usada_pekora | Usada Pekora |
| 5 | hoshimachi_suisei | Hoshimachi Suisei |
| 6 | shirakami_fubuki | Shirakami Fubuki |
| 7 | sakura_miko | Sakura Miko |
| 8 | kuzuha | Kuzuha |
| 9 | inugami_korone | Inugami Korone |
| 10 | mori_calliope | Mori Calliope |
| 11 | ironmouse | Ironmouse |
| 12 | kobo_kanaeru | Kobo Kanaeru |
| 13 | nekomata_okayu | Nekomata Okayu |
| 14 | minato_aqua | Minato Aqua |
| 15 | oozora_subaru | Oozora Subaru |
| 16 | tokino_sora | Tokino Sora |
| 17 | shigure_ui | Shigure Ui |
| 18 | filian | Filian |
| 19 | ninomae_inanis | Ninomae Ina'nis |
| 20 | watson_amelia | Watson Amelia |
| 21 | kanae | Kanae |
| 22 | koseki_bijou | Koseki Bijou |
| 23 | pmaru_sama | Pmaru-sama |
| 24 | shylily | Shylily |
| 25 | neuro_sama | Neuro-sama |
| 26 | zentreya | Zentreya |
| 27 | ookami_mio | Ookami Mio |
| 28 | tsunomaki_watame | Tsunomaki Watame |
| 29 | kaguya_luna | Kaguya Luna |
| 30 | woowakgood | Woowakgood |

Schema matches Waves 1–38 character_styles (id, name, description, keywords, sarcasm/metaphor/exaggeration levels, deadpan, persona_context, openers, closers, emoji_pool, example_responses).

## Contents Overview (unchanged from v3 + Wave 39)

### core/
| File | Description |
|------|-------------|
| `all_personality_datasets.zip` | Master package: nanora_dataset.jsonl + full personality_pack + extensive neko skins |
| `personality_pack_full.zip` | Standalone pack: core personality JSONs, sample JSONL datasets, generator, schema |
| `nanora_dataset.jsonl` | Standalone Nanora conversation dataset |

### expansions/
| File | Description |
|------|-------------|
| `missing_personalities_expansion.zip` | First expansion of additional personality definitions |
| `missing_personalities_expansion_2.zip` | Second, larger expansion |

### character_styles/ (Waves 1–39)
39 independent packs of character-style personality definitions covering anime, manga, games, comics, and now VTubers.

### neko/
| File | Description |
|------|-------------|
| `neko-bot-merged.zip` | Full Neko bot source tree |
| `NekoBot_Production.zip` | Production-oriented Neko bot package |

## Engineering Notes

- **Completeness**: Every archive from v3 is retained. Wave 39 is additive only.
- **No mutations**: core/, expansions/, waves 1–38, and neko/ are byte-identical to v3 sources.
- **Schema continuity**: VTuber JSONs use the same character_styles schema as prior waves for drop-in loader compatibility.
- **Recommended entry points**:
  1. `core/personality_pack_full.zip` or `core/all_personality_datasets.zip` for the personality system
  2. `character_styles/` for character / VTuber role-play packs
  3. `neko/` for bot integration
  4. `core/nanora_dataset.jsonl` for fine-tuning or evaluation

## File Counts
- Core packages: 3
- Expansion packages: 2
- Character style waves: **39** (was 38)
- Neko packages: 2
- **Total source archives: 46** + this README
```

---

## master_personality_datasets_v5.zip

```markdown
# Master Personality Datasets Archive v5

**Generated:** 2026-09-28  
**Policy:** Nothing skipped or dropped. All source archives from v3/v4 retained. Animal + historical/cultural expansions added.

## Directory Structure

```
master_v5/
├── core/                          # Primary personality systems & datasets
│   ├── all_personality_datasets.zip
│   ├── personality_pack_full.zip
│   └── nanora_dataset.jsonl
├── expansions/                    # Additional personality definitions
│   ├── missing_personalities_expansion.zip
│   └── missing_personalities_expansion_2.zip
├── character_styles/              # Character imitation packs (Waves 1–41)
│   └── character_styles_wave{1..41}.zip
├── neko/                          # Neko bot implementations
│   ├── neko-bot-merged.zip
│   └── NekoBot_Production.zip
└── README.md
```

## What Changed in v5

- **Added** `character_styles_wave40.zip` — 35 animal archetypes
- **Added** `character_styles_wave41.zip` — 51 historical / cultural / sports figures
- **Nothing removed or altered** from v4 (core, expansions, waves 1–39, neko)

## Wave 40 — Animal Archetypes (35)

Dog, Cat, Panda, Rabbit, Koala, Dolphin, Penguin, Lion, Elephant, Fox, Tiger, Otter, Horse, Butterfly, Deer, Bear, Parrot, Seal, Giraffe, Whale, Swan, Flamingo, Turtle, Bumblebee, Squirrel, Raccoon, Hedgehog, Frog, Peacock, Dove, Puffin, Firefly, Sloth, Hamster, Alpaca

## Wave 41 — Historical / Cultural / Sports Figures (51)

Jesus Christ, Muhammad, Gautama Buddha, Confucius, Alexander the Great, Julius Caesar, Genghis Khan, Napoleon Bonaparte, Mahatma Gandhi, Albert Einstein, Leonardo da Vinci, William Shakespeare, Isaac Newton, Aristotle, Plato, Socrates, Adolf Hitler (educational/historical framing only), Abraham Lincoln, George Washington, Christopher Columbus, Martin Luther, Karl Marx, Charles Darwin, Galileo Galilei, Nicolaus Copernicus, Thomas Edison, Louis Pasteur, Nikola Tesla, Michael Jackson, Elvis Presley, Marilyn Monroe, Charlie Chaplin, Walt Disney, Steve Jobs, Bill Gates, Cristiano Ronaldo, Lionel Messi, Neymar, Pelé, Muhammad Ali, Michael Jordan, Queen Elizabeth II, Cleopatra, Joan of Arc, Mother Teresa, Nelson Mandela, Martin Luther King Jr., Winston Churchill, Sun Tzu, Marco Polo, Neil Armstrong

**Note on sensitive figures:** Hitler is included strictly as an educational/historical persona for understanding and warning, not glorification. Religious founders are framed respectfully as historical-spiritual teaching voices.

## Schema

Same character_styles schema as Waves 1–39 for drop-in loader compatibility.

## File Counts

- Core packages: 3
- Expansion packages: 2
- Character style waves: **41** (was 39)
- Neko packages: 2
- **Total source archives: 48** + this README
```
