# Superseded inputs (inactive reference copies)

These four files came from `NekoBot_Elite_v2_8_TANVI_YASHMEET_FINAL_INTEGRATED` (the v2.7 PERSONALITY_LAYERS archive held earlier
revisions of the same presets). They are **not loaded by anything**; they are kept so no input is lost and a reviewer can see what
was set aside and why.

| File | Why it is not active |
|---|---|
| `tanvi.preset.v2.8.json`, `yashmeet.preset.v2.8.json` | Hand-written trait presets ("soft, affectionate, never mean", "dry-playful"…). The traits are assertions, not measurements; v2.9's skins are built from statistics computed on the supplied exports, and the fine-tuning pipeline learns only from what each person actually wrote. Evidence outranks preset assumptions. |
| `identity_map.v2.8.json` | Its account ids and usernames are all in `data/personality/persons/registry.json`; its two extra display-name variants were merged there as inert metadata. Its `resolution_rules` rank `display_name_exact` and `alias` as matchers after the username - that lets anyone who copies a display name trigger a real person's persona, so that rule is **not** adopted (the resolver takes only numeric id and username). |
| `README_ENHANCEMENT.v2.8.md` | Notes for the overlay archive. |

The archive's `bot/personality/identity_resolver.py` (display-name/alias matching) was not carried over for the same reason.

## Added in v2.14

- `neko.skin.v2.4.json` - the earlier `neko` skin from `all_personality_datasets.zip` (v2.4-v2.7). Identical to the shipped skin except one `description` string; kept for the record.
- `master_personality_datasets_v5.README.md` - the README of the `master_v5` wrapper archive (the archives themselves are unpacked into `data/personality/`).
- The older bot snapshots nested inside `master_personality_datasets_v3-v5.zip` (`NekoBot_Production`, `neko-bot-merged`) are not copied; their code is covered by the definition-level audit in [../../ZERO_LOSS_AUDIT.md](../../ZERO_LOSS_AUDIT.md).

## Added in v2.18

- `legacy_snapshots_v2_6_v2_7.zip` - the 98 files from the six `data/personality/archives/` wrapper zips of v2.6 / v2.7 (old bot snapshots nested inside the dataset archives, READMEs, ...) whose content is not byte-identical to
  anything else in the tree. Everything else inside those wrappers is already in `data/personality/` (loose files or `packs/`). `MANIFEST.tsv` inside the zip lists every file with its SHA-256.
- `derived/` - the previous versions of persona-derived files and of build/deployment files that v2.15.1 / v2.8+ replaced (see `derived/INDEX.md`).

