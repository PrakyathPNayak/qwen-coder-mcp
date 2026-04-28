# Loop 160 candidates
1. **Internet access for the model** (user-requested):
   - Audit `src/qwen_coder_mcp/web_tools.py` for what's exposed.
   - Add `/search <q>` and `/fetch <url>` slash commands that paste results into history as system context.
   - Extend `expand_at_mentions` to handle `@web:<url>` analogous to `@<path>`.
   - Update `prompts.CODER_SYSTEM` so the model knows these capabilities exist.
2. Status footer shows live "streaming..." indicator while worker runs.
3. `/find_bugs` -> `/apply` autochain for single-diff replies.
4. Session checkpoint 018-streaming-tui-extras.md.
