# Next Loop Candidates

1. (P5) `_apply_diff` — reject diffs creating a new file at a path conflicting with an existing directory name.
2. (P6) `_iteration_budget_seconds` — clamp absurdly large env values (e.g. cap at 24h).
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) `_has_unsafe_path` — also reject NUL bytes / newlines in paths (decoded via surrogateescape).
5. (P6) `_unquote_diff_path` — bound input length to avoid pathological backslash-escape blow-ups.
