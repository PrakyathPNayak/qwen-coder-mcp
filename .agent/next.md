Loop 6 candidates (priority-ordered):

1. **`_load_cursor` crashes on malformed `.loop/cursor.json` (P4).**
   First iteration after disk corruption / partial write would raise
   and the whole loop would die.

2. **`STATE.md` grows without bound (P8).** Multi-day autonomous run
   produces an enormous file.

3. **`server.py` instantiates `QwenClient` at import (P6).** Misconfig
   crashes MCP startup before any tool is called.

4. **`qwen_client.py` retries on 4xx (P4).** Wastes time and tokens on
   non-retriable errors.

Pick #1: malformed cursor crash is a real "loop dies forever" risk.




