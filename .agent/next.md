# Next loop seed

## Candidates ranked
1. **(P3) `_iteration` orchestrator has no end-to-end test.** Wire a fake
   `QwenClient` (or directly inject a stub via dependency-style monkeypatch),
   feed it a scripted (issues, diff, verdict) tuple, and verify the happy
   path commits one change. Then verify each rejection path: not-a-diff,
   out-of-scope, validation-fails, devil-rejects.

2. **(P3) `prompts.py` builders are uncovered.** A regression that drops the
   "respond ONLY with a unified diff inside ```diff fences" sentence makes
   every fix `not_a_unified_diff`. Add contract tests that assert each
   prompt builder returns a string containing the critical instructions.

3. **(P5) `_apply_diff` does not validate that the diff touches files inside
   the repo.** A path-traversal diff (`a/../../etc/passwd`) is currently
   defended against only by `_diff_in_scope` AFTER apply. Sanity-check the
   diff text itself for `../` segments before invoking `git apply --check`.
   Probably git already refuses, but worth a unit test.

4. **(P6) `server.py` builds `QwenClient` at import path.** Defer + smoke
   import test.

5. **(P8) `STATE.md` and `.agent/loop_log.md` rotation.**

## Reminder
- vLLM check (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
