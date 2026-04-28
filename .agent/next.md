# Loop 161 candidates
1. Status footer: live "streaming..." indicator while worker runs (small UX win after loop 159).
2. `/search` command should accept `--max <n>` flag.
3. Tool-calling layer: wire vLLM /v1/chat/completions function-calling so the model can directly invoke web_search/fetch instead of relying on user-typed `@web:`.
4. `expand_at_mentions` should also support `@@<path>` for "include the WHOLE file even if huge" (current 8KB cap silently truncates).
5. Session checkpoint 018-streaming-tui-extras.md now overdue.
