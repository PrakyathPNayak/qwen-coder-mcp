# Next loop seed

## Candidates ranked
1. **(P7) `_validate_changed_files` for non-Python files.** `.json`,
   `.toml`, `.yaml`, `.yml` should be parsed. Otherwise a model-edited
   `pyproject.toml` could ship broken TOML, blocking pip/install of the
   package itself.

2. **(P8) `STATE.md` and `.agent/loop_log.md` rotation** when over a
   threshold (e.g., 200 KB).

3. **(P6) `_pick_target_file` and cursor**: behaviour when the
   previously-cursor'd file is deleted/renamed.

4. **(P7) `_strip_fence` empty-language case** — bare ``` fence with
   no language tag.

5. **(P7) `qwen_client.system_user`** — does it pass through
   `temperature` and other kwargs to `chat`? Verify with a contract
   test. (Currently `_dispatch` passes `temperature` explicitly to
   `system_user`; if that signature ever changes, every tool silently
   regresses to default temperature.)

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
