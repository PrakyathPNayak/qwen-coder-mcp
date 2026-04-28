# Next loop seed

## Candidates ranked
1. **(P5) `_apply_diff` should reject `..` traversal in target paths.**
   A model could emit `+++ b/../../etc/passwd`. Currently `git apply`
   would refuse with a top-of-tree check, but the loop would silently
   tag this `apply_failed` and move on. Better: refuse pre-apply with
   a distinct outcome so the case is visible in the log.

2. **(P5) `_apply_diff` accepts diffs missing `+++ b/`** — sanity-check
   structure before invoking git apply.

3. **(P6) `server.py` constructs a live `QwenClient` at import time.**
   Defer until first tool call; add a smoke `import` test.

4. **(P8) `STATE.md` and `.agent/loop_log.md` rotation** when over a
   threshold. Currently append-only.

5. **(P6) `_iteration` reads `_pick_target_file` deterministically by
   alphabetical sort but `cursor.json` advances independently from the
   filesystem state — verify behaviour when the previously-cursor'd
   file is deleted.**

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
