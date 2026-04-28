# Next loop seed

## Candidates ranked
1. **(P3) Atomic `_save_cursor`.** Currently a non-atomic `write_text`. If the
   process is killed mid-write `cursor.json` ends up empty/corrupt. Today
   `_load_cursor` returns 0 in that case, so the loop silently restarts at
   index 0 (potentially re-scanning the same file forever in a small repo).
   Write to a tempfile + `os.replace`.

2. **(P3) `_call_model` / `client.system_user` round-trip has no test.** Mock
   `QwenClient.chat` and assert prompt assembly: includes file path, includes
   the "diff in scope" instruction, system+user split correct.

3. **(P5) `_apply_diff` accepts `diff` blocks but does not require trailing
   newline on every hunk** — sometimes git apply silently corrupts. Verify by
   normalising line endings on the input. Lower priority, may be fine.

4. **(P6) `server.py` builds `QwenClient` at `_build_server` import path.**
   Add a smoke test importing the module without `.env`.

5. **(P8) `STATE.md` and `.agent/loop_log.md` unbounded growth.** Rotation.

## Reminder
- Verify vLLM (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
