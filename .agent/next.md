# Next Loop Candidates

1. (P5) `.loop/history/*.md` — accumulating without retention; add cap on file count or total bytes.
2. (P6) `_apply_diff` — emit machine-parseable error category code (e.g. `unsafe_path`, `dir_conflict`) as stable field for log aggregation.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) Audit `_commit_and_push` for empty-diff race when scope check passes but nothing actually changed.
5. (P6) `_log` errors during rotation are silently swallowed; verify nothing important loses signal.
