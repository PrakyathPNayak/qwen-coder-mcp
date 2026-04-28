# Next Loop Candidates

1. (P6) `_apply_diff` — reject diffs whose destination is itself a symlink (would silently rewrite the link target's content elsewhere).
2. (P5) `_apply_diff` — verify `\ No newline at end of file` markers don't break the validator.
3. (P6) `QwenClient.chat()` retry-loop wall-clock cap.
4. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
5. (P5) `_diff_paths` — ensure paths with backslash-octal-escaped bytes (rare git output) are flagged unsafe.
