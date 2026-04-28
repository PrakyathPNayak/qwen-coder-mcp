# Next loop seed

## Candidates ranked
1. **(P5) `_changed_paths` quoted-paths handling** — porcelain v1 -z
   on paths with spaces/unicode. Verify behavior.

2. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

3. **(P7) `_pick_target_file` should skip files that became empty**
   (size 0).

4. **(P6) `_iteration` model-call wall-clock budget** — if the model
   takes too long (vLLM hung, network stuck), the loop should bail
   the iteration.

5. **(P5) `_diff_in_scope` audit — what if `target` doesn't appear
   in `changed`? E.g. model emits a no-op diff that touches NOTHING.
   Currently the function returns "in scope" or rejects?**
