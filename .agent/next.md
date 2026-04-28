# Next Loop Candidates

1. (P5) `_validate_changed_files` — surface `SyntaxWarning` (e.g. invalid escape sequences) not just SyntaxError.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P6) `_unquote_diff_path` — lock down behaviour on incomplete trailing escape (e.g. `"a/foo\\"`).
4. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory.
5. (P6) `_apply_diff` — log `dir_conflict` distinctly in `_log_attempt` summary stats (categorisation).
