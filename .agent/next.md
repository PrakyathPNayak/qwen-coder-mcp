# Next loop seed

## Candidates ranked
1. **(P5) `_changed_paths` quoted-paths** — porcelain v1 -z claims
   unquoted; verify what happens for unicode/space paths.

2. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

3. **(P6) `_revert_changes` idempotency on clean tree**.

4. **(P5) `_pick_target_file` — empty file list / single-file repo
   edge cases**.

5. **(P6) Same timeout treatment for `_revert_changes`'s `git clean
   -fd` and `git checkout` calls** — they could hang too.
