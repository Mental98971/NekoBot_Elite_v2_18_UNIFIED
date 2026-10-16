# Audit tools (v2.18)

The scripts that produced the numbers in `docs/ZERO_LOSS_AUDIT.md` (v2.18 addendum) and `docs/TEST_REPORT.md`. They are not part of the bot and are not imported by it (`docs/` is excluded from the Docker image).

| Script | What it does |
|---|---|
| `final_audit.py` | compares every input archive (extracted under `$NEKO_SRC`, one folder per version, layout in `roots.json`) with the tree in `$NEKO_BUILD`: files, definitions, commands, structured data and persona corpora; prints the tables and writes `final_audit.json` |
| `static_check.py ROOT` | syntax, undefined names, unresolved project imports and names, duplicate definitions |
| `lint_lite.py ROOT` | pyflakes-style approximation (unused imports and locals) for environments without `ruff` |
| `run_tests.py ROOT` | runs every `tests/test_*.py` under `unittest` and reports which modules cannot import a third-party package |

`python docs/provenance/audit_tools/static_check.py .` and `python docs/provenance/audit_tools/run_tests.py .` work from a checkout; `final_audit.py` needs the original archives extracted.
