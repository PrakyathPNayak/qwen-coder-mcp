Loop 3 candidates (priority-ordered):

1. **`_commit_and_push` doesn't abort failed rebase (P4).** Half-rebased
   tree wedges every subsequent loop. Fix: detect non-zero rebase exit,
   run `git rebase --abort`, then `git reset --hard ORIG_HEAD`.

2. **`_strip_fence` fails on prose-around-fence (P4).** Anchored regex
   requires whole-input match. Fix: scan for first ```…``` block.

3. **`_iteration` doesn't validate diff scope (P6).** Model-emitted diff
   could rewrite a file we didn't ask about; loop commits it silently.
   Fix: refuse to apply if `_changed_paths()` after apply contains
   anything outside `{rel}`.

4. **`_iteration` doesn't check that the diff actually changes anything
   on the targeted file (P8).** Model could return an empty/no-op diff
   that passes apply check; we'd commit nothing.

5. **`_changed_paths` may miss deleted files (P8).** Uses `git diff
   --name-only` without `--cached --diff-filter=ACMRTUXB` — should be
   fine for unstaged changes but worth double-checking.

Pick #1: a wedged loop costs every subsequent iteration. Cheap fix, big
leverage. Will write a test that simulates a rebase conflict.

