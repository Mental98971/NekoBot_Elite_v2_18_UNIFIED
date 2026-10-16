# Personality data

What the bot loads at runtime (resolved against `settings.data_dir / "personality"`):

| Path | Purpose |
|------|---------|
| `skins/` | Active character skins (one JSON per skin) |
| `presets/` | Instruction presets (keyword-matched personas) |
| `dataset/fewshot_bank.json` | Few-shot example bank used when building replies |
| `dataset/full_cleaned.jsonl` | Optional deeper example search (capped reads) |
| `dataset/category_index.json` | Category index for the example bank |
| `../jokes.json` | Joke bank (outside this folder, in `data/`) |
| `persons/registry.json` | Real-person registry: person → accounts → skins (authoritative for identity resolution) |
| `persons/<id>/profile.json`, `corpus.jsonl` | Computed style profile and scrubbed corpus for Tanvi / Yashmeet (rebuilt with `scripts/build_person_personas.py`) |
| `skins/tanvi*.json`, `skins/yashmeet.json` | Generated person skins (`persona_kind: "person"`) |

Content that is **not** loaded at runtime (source material and import inputs):

| Path | Purpose |
|------|---------|
| `nanora_dataset.jsonl` | Full Nanora conversation corpus |
| `packs/personality_pack/` | Generator, banks, personality JSONs and sample sets |
| `packs/personality_pack_full.zip`, `packs/all_personality_datasets.zip` | Packaged forms of the packs |
| `character_styles/character_styles_wave{1..41}.zip` | Drop-in persona waves for `scripts/import_personas.py` |
| `expansions/` | `missing_personalities_expansion*` import inputs |
| `catalog_manifest.json` | Record written/read by `scripts/import_personas.py` |

Everything here exists exactly once. Earlier builds also carried three byte-identical
copies of `all_personality_datasets.zip`, two of `personality_pack_full.zip` and
`nanora_dataset.jsonl`, a second identical `all_personality_pack/` tree, and five cumulative
`master_personality_datasets*.zip` snapshots. They were removed after a byte-level check
showed every file in them is present elsewhere in the tree or is an older copy of project
source; the snapshots' own README notes are preserved in
`docs/provenance/personality_master_archives.md`.
