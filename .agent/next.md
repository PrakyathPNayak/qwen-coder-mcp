Loop 4 candidates (priority-ordered):

1. **`_iteration` doesn't validate diff scope (P6).** The model is asked
   to fix `rel`, but its diff might touch other files. Fix: after
   `_apply_diff` succeeds, call `_changed_paths()` and revert+reject if
   any path is outside `{rel}` (or, more permissively, outside the
   ancestor dirs of `rel`). Add a test that simulates a multi-file diff
   and asserts the loop reverts.

2. **`_strip_fence` only handles whole-input fences (P4).** Anchored
   regex requires the entire input to match; prose-around-fence falls
   through. Fix: scan for the first ```…``` block and return its inner
   text.

3. **`_load_cursor` is fragile (P8).** No tests; if `.loop/cursor.json`
   contains malformed JSON the loop crashes.

4. **`STATE.md` grows unbounded (P8).** No rotation.

5. **`_iteration` no-op-diff guard (P8).** Currently `_apply_diff` would
   succeed with an empty diff but `git status --porcelain` would be
   empty so `_commit_and_push` returns False — already handled. Lower.

Pick #1 — diff-scope validation. P6 (interface inconsistency / silent
trust violation), but it's the next clearest correctness gap with a
contained, testable fix.


