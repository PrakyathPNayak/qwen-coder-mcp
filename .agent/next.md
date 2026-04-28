# Next loop seed

## Candidates ranked
1. **(P3) `_parse_first_issue` false-positive on benign no-issue replies.**
   Today, "No issues found." returns "No issues found." as the "issue",
   wasting a coder round-trip. Add a lightweight allow-list of no-issue
   phrases ("no issues", "looks good", "clean", "lgtm", "nothing to fix",
   etc.) returning None when the entire response matches.

2. **(P3) Loop fails open if `.gitignore` is missing or doesn't cover
   `.loop/`.** Every `_iteration` would then misclassify cursor/runtime
   updates as untracked → every diff becomes out_of_scope. Either: (a)
   exclude `.loop/` and `STATE.md` from `_changed_paths` directly, or
   (b) bootstrap a minimal `.gitignore` if absent. (a) is safer.

3. **(P3) `prompts.py` builders are uncovered.** Contract tests on each
   builder ensuring the critical instructions are present.

4. **(P5) `_apply_diff` should reject diffs whose path components contain
   `..` (path traversal).** git apply may accept some forms.

5. **(P6) `server.py` builds `QwenClient` at `_build_server`.** Defer +
   smoke import test.

6. **(P8) `STATE.md` and `.agent/loop_log.md` rotation.**

## Reminder
- vLLM check (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
