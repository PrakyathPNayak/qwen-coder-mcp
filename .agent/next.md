# Next Loop Candidates

1. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) `_validate_changed_files` — extend to validate `.cfg` / `.ini` files via configparser.
4. (P6) `_iteration` — log time spent per phase to `.loop/timing.log` for budget tuning.
5. (P5) Audit `_apply_diff` ordering: should `dir_conflict` run before `unsafe_path` so directory-typed safe paths short-circuit early? (probably no — `unsafe_path` is cheap and the ordering keeps diagnostics consistent).
