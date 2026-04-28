# Next loop seed

## Candidates ranked
1. **(P5) `_changed_paths` quoted-paths handling** — porcelain v1 -z
   on paths with spaces/unicode. Verify and add tests.

2. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

3. **(P5) `_pick_target_file` empty-file-list edge case** —
   ZeroDivisionError if `len(files)==0`.

4. **(P6) Timeout the validator subprocesses** —
   `_validate_changed_files` calls `python -m py_compile` on each
   `.py` file; one infinitely-recursive import in user code could
   wedge it. Cap.

5. **(P7) `_pick_target_file` should skip files that became empty**
   (size 0) — model can't fix nothing.
