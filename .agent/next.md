# Next loop seed

## Candidates ranked
1. **(P5) `_changed_paths` quoted-paths** — confirm porcelain v1 -z
   gives unquoted UTF-8.

2. **(P7) `_apply_diff`: timeout the `git apply` subprocess** — a
   pathological diff could hang.

3. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

4. **(P6) `_revert_changes` idempotency on clean tree**.

5. **(P5) `_pick_target_file` — what happens if `files` list is
   empty (no candidate files)? Verify safe shutdown / skip.**
