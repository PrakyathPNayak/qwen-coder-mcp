# Next Loop Candidates

1. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory (rename-from-dir).
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P6) `_apply_diff` — surface `dir_conflict` and `py_syntax_warning` in attempt summary stats.
4. (P5) `_validate_changed_files` — extend to validate `.cfg` / `.ini` files (configparser).
5. (P6) `_run_git` — make timeout configurable via env (currently hard-coded 60s).
