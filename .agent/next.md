# Next Loop Candidates

1. (P5) `STATE_ARCHIVE_DIR` rotation — verify it's actually triggered; possibly bounded.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) Audit `_commit_and_push` for empty-diff race.
4. (P6) `state.md` (append target of `_append_state`) — also unbounded line growth; rotation policy needed.
5. (P7) Use `APPLY_ERROR_CATEGORIES` to convert `apply_failed:{rel}:{msg[:80]}` outcome strings into a structured-tag form for `_iteration` return values.
