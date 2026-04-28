# Next Loop Candidates

1. (P5) `_apply_diff` — verify `\ No newline at end of file` markers don't break the validator.
2. (P6) `QwenClient.chat()` retry-loop wall-clock cap.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) `_candidate_files` — currently follows symlinks for individual files; with loop 34 they no longer leak content but they're also pointless to scan. Skip-on-symlink up front.
5. (P6) `_apply_diff` — reject diffs where the destination is itself a symlink (would clobber symlink target).
