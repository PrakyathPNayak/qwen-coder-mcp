# Next Loop Candidates

1. (P5) `_log` formatter could include the category for outer-iteration outcome lines so `runtime.log` is also fast-greppable.
2. (P5) Audit `_commit_and_push` for empty-diff race.
3. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_write_timing` swallows all exceptions but doesn't bubble up to a separate failure counter — repeated silent failures could mask a permission bug. Add a counter & log on first failure.
