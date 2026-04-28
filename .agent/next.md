# Next Loop Candidates

1. (P5) `_log` formatter — include category for outer-iteration outcomes in `runtime.log`.
2. (P5) `_write_timing` failure counter — repeated swallowed exceptions could mask permission bug.
3. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_commit_and_push`: distinguish "empty staged tree" from "git commit failed" with a tri-state return so the outer loop can emit a different outcome category.
