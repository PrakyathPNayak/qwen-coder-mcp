# Next loop seed

## Candidates ranked
1. **(P6) `_pick_target_file` and cursor**: behaviour when the
   previously-cursor'd file is deleted/renamed. Audit + test.

2. **(P7) `_strip_fence` empty-language case** — bare ``` fence with
   no language tag. Verify `_INNER_FENCE_RE` handles it.

3. **(P7) `qwen_client.system_user`** — passes through `temperature`
   and other kwargs to `chat`? Contract test.

4. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

5. **(P5) `_apply_diff` audit for binary-patch markers** —
   `Binary files differ` lines should be rejected; we don't want a
   model dropping binary blobs.

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
