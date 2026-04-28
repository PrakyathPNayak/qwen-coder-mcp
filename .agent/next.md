# Next loop seed

## Candidates ranked
1. **(P7) `qwen_client.system_user` kwargs passthrough contract**.

2. **(P7) `_verdict_accepts` audit** — accept variants like
   `Verdict: accept` (lowercase), `VERDICT : ACCEPT` (extra spaces).

3. **(P8) `.agent/loop_log.md` rotation**.

4. **(P5) `_changed_paths` — handle quoted paths** (paths with
   spaces / unicode encoded by `core.quotePath`).

5. **(P6) `_revert_changes` — verify it's idempotent on a clean tree**.
