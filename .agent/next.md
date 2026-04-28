# Next Loop Candidates

1. (P5) Audit `_commit_and_push` for empty-diff race: status check could race with concurrent file writes from a parallel iteration; though we don't run iterations in parallel, document the assumption.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P7) `_strip_fence` nested triple-backticks (low risk — diff context lines have prefix).
4. (P5) `_iteration` writes outcome to STATE.md and runtime log, but timing.log only gets the outcome string raw — log the structured category alongside for fast aggregation.
5. (P6) `_outer_outcome_category` could be promoted to `_log` formatter — current `_log` writes the outcome verbatim.
