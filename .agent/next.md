# Next loop seed

## Candidates ranked
1. **(P5) `_apply_diff` audit for binary-patch markers** —
   `Binary files differ` lines should be rejected; we don't want a
   model dropping binary blobs. Plus `GIT binary patch`
   block-format hunks.

2. **(P7) `_strip_fence` empty-language case** — bare ``` fence with
   no language tag.

3. **(P7) `qwen_client.system_user`** — passes through `temperature`
   and other kwargs to `chat`? Contract test.

4. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

5. **(P6) `_iteration` should clamp the model's diff size** before
   apply — a diff larger than the original file is suspicious. Add
   a configurable max-diff-size guard.

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
