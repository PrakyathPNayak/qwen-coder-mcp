# Next loop seed

## Candidates ranked
1. **(P5) `_apply_diff` accepts diffs missing `+++ b/`**. If a model
   emits only `--- a/PATH` headers (deletion-only or malformed),
   `_diff_in_scope` and `_validate_changed_files` need a sane outcome.
   Add a structural sanity check that there's at least one `+++ b/` or
   `+++ /dev/null` header.

2. **(P6) `server.py` constructs a live `QwenClient` at import time.**
   Defer until first tool call; add a smoke `import` test that doesn't
   require vLLM up.

3. **(P8) `STATE.md` and `.agent/loop_log.md` rotation** when over a
   threshold. Currently append-only.

4. **(P6) `_pick_target_file` and cursor**: verify behaviour when the
   previously-cursor'd file is deleted/renamed.

5. **(P7) `_validate_changed_files` only runs `python -c compile` for
   `.py`** — what about JSON/TOML/YAML? At least add `json.load` for
   `.json` files since this repo will eventually edit `pyproject.toml`
   etc.

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
