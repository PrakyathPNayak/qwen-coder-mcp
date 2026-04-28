# Next loop seed

## Candidates ranked
1. **(P5) `_changed_paths` quoted-paths** — paths with spaces/unicode
   under `core.quotePath`. Confirm porcelain v1 -z gives unquoted.

2. **(P5) `qwen_client._extract_text` empty-content** — content=None,
   content=[], malformed shapes.

3. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

4. **(P6) `_revert_changes` idempotency on clean tree**.

5. **(P7) `_apply_diff`: timeout the `git apply` subprocess** — a
   pathological diff could hang.
