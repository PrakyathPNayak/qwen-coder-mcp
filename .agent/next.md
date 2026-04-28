# Next loop seed

## Candidates ranked
1. **(P3) `agent/loop.py` `_call_model` has no test coverage despite being the
   one place that talks to the LLM.** Mock `QwenClient.chat` and assert the
   prompt template is well-formed (system+user, includes the file path, the
   guard rails about scope, and a fenced-diff example).

2. **(P5) `_apply_diff` shells out to `git apply` but discards stderr.** When
   the model emits a diff that doesn't apply, the loop logs a generic
   "apply failed" without surfacing the actual git error, so debugging the
   model's output requires re-running by hand. Capture and log stderr.

3. **(P6) `server.py` builds a `QwenClient` at import time inside
   `_build_server` (line 18).** Not a crash because httpx.Client is lazy, but
   it means the env must be set even when the user just imports for tooling.
   Defer to first tool call, OR add a smoke test that imports the module
   without `.env` present and confirms no exception.

4. **(P8) `STATE.md` unbounded growth.** Loop appends forever. Cap at 1MB and
   rotate to `.loop/state-archive/STATE-<ts>.md`.

5. **(P8) `.agent/loop_log.md` will hit the same problem this agent has.**
   Same fix.

## Reminder
- Verify vLLM (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
