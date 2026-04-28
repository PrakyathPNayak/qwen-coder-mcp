# Next Loop Candidates

1. (P5) `_log` formatter — include category for outer-iteration outcomes in `runtime.log`.
2. (P5) `_write_timing` failure counter — repeated swallowed exceptions could mask a permission bug.
3. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_revert_changes` falls back to `git reset --hard HEAD`, but if HEAD itself is detached or unreachable, the fallback also fails. Consider a final fallback to `git -c core.hooksPath=/dev/null reset --hard <known_good_sha>`.
