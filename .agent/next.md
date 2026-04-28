# Next Loop Candidates

1. (P5) `_apply_diff` — reject diffs creating a new file at a path conflicting with an existing directory name.
2. (P5) `_has_unsafe_path` — also reject NUL bytes / newlines in paths.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P6) `_unquote_diff_path` — return path stripped of trailing whitespace too (defensive).
5. (P5) `_validate_changed_files` — also surface `SyntaxWarning` not just SyntaxError.
