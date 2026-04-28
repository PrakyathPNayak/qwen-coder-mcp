# Next Loop Candidates

1. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P6) `_iteration` — log time spent per phase to `.loop/timing.log` for budget tuning.
4. (P6) `_apply_diff` — emit machine-parseable error category for log-aggregation.
5. (P5) `_validate_changed_files` — also detect duplicate top-level table in TOML (tomllib already does, but verify and add a regression test).
