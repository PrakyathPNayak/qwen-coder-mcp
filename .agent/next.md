# Next Loop Candidates

1. (P5) `_has_unsafe_path` — also reject paths whose normalisation collapses to `.` (e.g. `./.././x`).
2. (P5) `_diff_paths` — handle paths with backslash-octal-escaped bytes when `core.quotePath=true`.
3. (P5) `_apply_diff` — reject diffs creating a new file at a path conflicting with an existing directory name.
4. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
5. (P6) `_iteration_budget_seconds` — clamp absurdly large values (currently any positive float accepted).
