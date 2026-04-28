# Next loop seed

## Candidates ranked
1. **(P7) `_strip_fence` empty-language fence audit** —
   bare ``` with no language tag.

2. **(P7) `qwen_client.system_user` kwargs passthrough contract** —
   does it forward `temperature` / `max_tokens` to `chat`?

3. **(P8) `.agent/loop_log.md` rotation** — mirror STATE.md rotation.

4. **(P6) `_iteration` clamp the model's diff size** before apply —
   reject diffs larger than configurable threshold (e.g. 64KB or
   N*original).

5. **(P5) `_apply_diff` audit for `rename from`/`rename to`** —
   does git-apply use them and do they evade our path check?

6. Re-read `agent/loop.py` for next round.
