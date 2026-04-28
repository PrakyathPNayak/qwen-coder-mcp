# Next Loop Candidates

1. (P5) `_changed_paths` decoding — confirm tolerance to non-utf8 bytes in path; consider errors='surrogateescape'.
2. (P8) `.agent/loop_log.md` rotation — mirror STATE.md logic.
3. (P6) `_revert_changes` — verify both git checkout AND git clean run even on first failure.
4. (P5) Apply per-call wall-clock cap inside QwenClient.chat() retry loop, not just per-request httpx timeout.
5. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block (currently `_INNER_FENCE_RE` matches first `\n````).
