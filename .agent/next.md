# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P5) Drift-audit shape for `agent/loop.py` direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file for SIGTERM resilience.
4. (P4) `_iteration_budget_seconds` cap-check audit for non-int env values.
5. (P5) `_finish` and `_finish_no_file` shape unification.
6. (P5) Now that `validation_failed` outcomes carry the rule, document the new outcome format somewhere readable (README runtime-introspection section?).
7. (P4) `apply_failed:{category}:{rel}:{msg[:60]}` truncation could split a UTF-8 multibyte char. Trace `msg` source.
8. (P5) `_validate_changed_files` path-collision case (`dir_path_conflict:{path}`) -- if a diff tries to write to a path that already exists as a directory, the validation message contains a colon inside the leading segment. The new `syn_msg.split(':', 1)[0]` would yield `dir_path_conflict` cleanly though, so this is fine.
