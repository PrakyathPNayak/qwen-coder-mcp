# Loop 162 candidates
1. Tool-calling layer: vLLM /v1/chat/completions function-calling so the model invokes web_search/fetch directly.
2. `@@<path>` for "include the WHOLE file even if huge" (current 8KB cap silently truncates).
3. `/grep --count` mode.
4. Session checkpoint 018-streaming-tui-extras.md (overdue since loop 158).
5. Save-on-Ctrl-S so users don't lose history if Textual crashes between turns.
