# Next Loop Candidates

1. (P5) `_changed_paths` non-utf8 path bytes — current text=True relies on locale; consider explicit `encoding="utf-8"` + `errors="surrogateescape"`.
2. (P8) `.agent/loop_log.md` rotation — mirror STATE.md logic before file balloons.
3. (P6) `QwenClient.chat()` retry-loop wall-clock cap — per-iteration budget (loop 31) helps but a tighter per-call cap localises the bound.
4. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
5. (P5) `_apply_diff` — verify behavior against a diff with `\ No newline at end of file` markers; ensure validator still passes.
