# Next loop seed

## Candidates ranked
1. **(P3) `_parse_first_issue` false-positive on benign no-issue replies.**
   "No issues found." → returns "No issues found." as if it were an issue,
   wasting a coder + devil round-trip. Add a regex/allow-list of no-issue
   phrases ("no issues", "looks good", "clean", "lgtm", "nothing to fix",
   "no bugs", etc.) returning None when the entire response matches. Test
   with realistic Qwen-style "no findings" replies.

2. **(P3) `prompts.py` builders are uncovered.** Contract tests on each
   builder asserting the critical sentences are present.

3. **(P5) `_apply_diff` should reject diffs whose target path contains
   `..` traversal segments before invoking git apply.

4. **(P6) `server.py` builds `QwenClient` at `_build_server`.** Defer +
   smoke import test.

5. **(P8) `STATE.md` and `.agent/loop_log.md` rotation.**

## Reminder
- vLLM check (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
