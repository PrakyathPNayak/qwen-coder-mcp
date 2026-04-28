# Next loop seed

## Candidates ranked
1. **(P6) Clamp model diff size** before apply — reject diffs >N
   bytes / >M lines. Prevents runaway model from emitting
   megabytes.

2. **(P7) `qwen_client.system_user` kwargs passthrough contract**.

3. **(P5) Verify `git apply --check` is run before `git apply`** —
   read the code path and prove the staged check exists.

4. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

5. **(P7) `_verdict_accepts` audit** — does it accept variants like
   `Verdict: accept` (lowercase) or `VERDICT : ACCEPT` (extra space)?
