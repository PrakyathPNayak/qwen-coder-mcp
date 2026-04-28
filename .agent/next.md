# Next loop seed

## Candidates ranked
1. **(P7) `_strip_fence` empty-language fence audit** — bare ``` with
   no language tag.

2. **(P6) Clamp model diff size** before apply — reject diffs >N
   bytes / >M lines. Prevents a runaway model from emitting
   megabytes of patch.

3. **(P7) `qwen_client.system_user` kwargs passthrough contract**.

4. **(P8) `.agent/loop_log.md` rotation**.

5. **(P5) `_apply_diff`: ensure `git apply --check` is run before
   `git apply` (it might already be — verify, add test).**
