# Next loop seed

## Candidates ranked
1. **(P6) `server.py` constructs a live `QwenClient` at import time.**
   This makes `from qwen_coder_mcp.server import build_server` fail if
   vLLM isn't up, even if the caller intends to run unit tests of
   non-network helpers. Defer client construction to first tool-call OR
   wrap import in lazy property. Add a smoke `import` test that doesn't
   need vLLM.

2. **(P7) `_validate_changed_files` for non-Python files.** `.json`,
   `.toml`, `.yaml`, `.yml` should be parsed for syntactic validity —
   otherwise a model-written diff to `pyproject.toml` could silently
   ship broken TOML (test pipeline would catch it but we'd still commit).

3. **(P8) `STATE.md` and `.agent/loop_log.md` rotation** when over a
   threshold.

4. **(P6) `_pick_target_file` and cursor**: behaviour when the
   previously-cursor'd file is deleted/renamed.

5. **(P7) `_strip_fence` may leave behind ` ```diff `-only fenced
   blocks if the model emits no language tag and surrounds with a bare
   ``` fence**. Audit `_INNER_FENCE_RE` for empty-language case.

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
