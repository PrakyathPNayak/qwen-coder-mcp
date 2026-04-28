# Next loop seed

## Candidates ranked
1. **(P3) `prompts.py` builders are uncovered.** A regression dropping the
   "respond ONLY with a unified diff inside ```diff fences" sentence from
   the coder prompt makes every fix `not_a_unified_diff`. Contract tests:
   `propose_fix_user` contains the diff-fence instruction, `devils_advocate_user`
   contains the VERDICT: ACCEPT/REJECT contract, `find_bugs_user` contains
   the "list each bug as a numbered/bulleted item" instruction.

2. **(P5) `_apply_diff` should reject diffs whose target paths contain
   `..` traversal segments before invoking git apply.**

3. **(P5) `_apply_diff` accepts diffs with no `+++ b/` line.** A diff that
   has only `--- a/file` may be a deletion-only patch, but malformed
   single-sided diffs should not crash. Sanity-check structure.

4. **(P6) `server.py` builds `QwenClient` at `_build_server`.** Defer +
   smoke import test.

5. **(P8) `STATE.md` and `.agent/loop_log.md` rotation.**

## Reminder
- vLLM check (`tail .loop/serve.log`, `ps -p 1493`) every few loops.
- Never end output with a question. Never pause. Always start the next OBSERVE
  immediately after commit+push.
