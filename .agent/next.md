Loop 5 candidates (priority-ordered):

1. **`_strip_fence` only handles whole-input fences (P4 robustness).**
   Anchored `^...$` regex falls through whenever the model prepends
   prose like "Here is the diff:". Fix: locate first ```…``` block and
   return its inner text.

2. **`_load_cursor` has no error handling (P4).** Malformed
   `.loop/cursor.json` crashes the loop on the next iteration.

3. **`STATE.md` grows unbounded (P8).** No rotation / cap.

4. **`server.py` instantiates `QwenClient` at import time (P6).** If
   `.env` is missing or endpoint is unreachable, MCP server import
   crashes — should lazy-init.

5. **`qwen_client.py` retries on 4xx (P4).** 4xx errors won't succeed
   on retry; should fail fast.

Pick #1 — `_strip_fence`. Highest model-output-robustness leverage and
already has a partial test scaffold.



