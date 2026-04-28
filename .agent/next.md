# Next Loop Candidates

1. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) `_git_apply` timeout — currently a separate constant `_GIT_APPLY_TIMEOUT_SECONDS`; align with env override.
4. (P5) `_validate_changed_files` — extend to validate `.cfg` / `.ini` files via configparser.
5. (P6) `_iteration` — log time spent per phase to `.loop/timing.log` for budget tuning.
