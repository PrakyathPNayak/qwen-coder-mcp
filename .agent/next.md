# Next loop seed

## Candidates ranked
1. **(P3) `_call_model` / `client.system_user` round-trip has no test.** Wire
   in a fake QwenClient (already easy via `httpx.MockTransport`) and exercise
   one full `_iteration` happy path: file picked → bug found → fix proposed →
   accepted → applied → committed. Currently the orchestrator is unverified.

2. **(P3) `prompts.py` strings are never validated.** A regression that drops
   the "must be a unified diff inside ```diff fences" sentence would make
   every fix `not_a_unified_diff`. Snapshot/contract tests on the prompt
   builders.

3. **(P5) `_apply_diff` does not normalise CRLF in model output.** Some
   models emit Windows line endings; `git apply` then complains about
   "patch with CRLF line endings". Strip `\r` before `git apply --check`.

4. **(P6) `server.py` builds `QwenClient` at `_build_server`.** Defer + smoke
   import test.

5. **(P8) `STATE.md` and `.agent/loop_log.md` rotation.**

## Reminder
- Verify vLLM (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
