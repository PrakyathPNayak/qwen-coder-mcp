# Next Loop Candidates

1. (P6) `QwenClient.chat()` — retry-loop wall-clock cap complementing per-iteration budget.
2. (P5) `_has_unsafe_path` — also reject paths whose normalisation collapses to `.` (e.g. `./.././x`).
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) `_diff_paths` — paths with backslash-octal-escaped bytes when `core.quotePath=true`.
5. (P5) `_apply_diff` — reject diffs creating new files at paths that conflict with existing directory names.
