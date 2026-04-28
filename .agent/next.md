# Next loop seed

## Candidates ranked
1. **(P6) `_pick_target_file` and cursor**: behaviour when the
   previously-cursor'd file is deleted/renamed. Audit + test.

2. **(P7) `_strip_fence` empty-language case** — bare ``` fence with
   no language tag.

3. **(P7) `qwen_client.system_user`** — passes through `temperature`
   and other kwargs to `chat`? Contract test.

4. **(P8) `.agent/loop_log.md` rotation** in addition to STATE.md.
   Same logic, different file. (Lower prio because `.agent/` is
   inspected directly by the human, not consumed by the loop.)

5. **(P5) Audit `_apply_diff` for symlink-target hunks** — a diff
   that creates a symlink (`new file mode 120000`) could point
   anywhere. Reject `120000` mode in headers.

## Reminder
- vLLM check every few loops.
- Never end output with a question, never pause.
