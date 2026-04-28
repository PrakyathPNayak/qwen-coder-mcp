# Next Loop Candidates

1. (P8) `.agent/loop_log.md` rotation — log already > 30 entries; mirror STATE.md rotation logic.
2. (P6) `QwenClient.chat()` retry-loop wall-clock cap — per-call budget complementing the per-iteration one (loop 31).
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (`_apply_diff`) P5 verify behavior against `\ No newline at end of file` markers. 
5. (P5) `_iteration` — log the iteration outcome to `.loop/runtime.log` so post-mortem doesn't require parsing STATE.md.
