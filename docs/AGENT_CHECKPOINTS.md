# Agent checkpoints

The TUI persists agent state during multi-step turns so a crash mid-loop
doesn't lose work. There are two storage layers:

| Layer | Path | What it holds | When written |
|-------|------|---------------|--------------|
| Chat history | `.qwen-coder-history.jsonl` (sandbox root) | Full chat transcript, every role | After every user/assistant exchange |
| Agent checkpoint | `.agent/agent_state.json` | In-flight agent transcript | After every tool round-trip during `/agent` |

The agent layer additionally keeps a rolling history under
`.agent/checkpoints/agent_state-<UTC-timestamp>.json` so you can roll back
to any recent step.

## Recovery flow

After a crash:

1. Restart the TUI.
2. If the JSONL chat history is intact, you'll see `restored N prior messages`
   and the session resumes normally.
3. If the JSONL is empty but an agent checkpoint exists, the TUI prints
   a yellow-bullet hint pointing at `/resume`.
4. `/resume` rehydrates `history` from `.agent/agent_state.json`, falling
   back to the newest readable rotation in `.agent/checkpoints/` if the
   primary is missing or corrupt.

## Slash commands

| Command | Effect |
|---------|--------|
| `/resume` | Reload the latest readable agent checkpoint into chat history (in-place). |
| `/checkpoints` | List rotated snapshots oldest-first, with mtime and size. |
| `/checkpoints load N` | Rehydrate snapshot N (1-based) into chat history. |
| `/checkpoints prune K` | Delete all but the newest K rotated snapshots. |
| `/lat` | Print the last agent turn's timing breakdown (TTFT, per-tool latencies, summary). |

## Configuration

| Env var | Default | Effect |
|---------|---------|--------|
| `QWEN_AGENT_ROTATION_KEEP` | `5` | Cap on rotated snapshots in `.agent/checkpoints/`. `0` retains everything. Empty/unparseable falls back to the default. |

### Why two storage layers?

The chat history is the durable record of every turn — written deterministically
once a turn completes. The agent checkpoint is a *mid-flight* snapshot that
includes partial transcripts (e.g. after the model has called `fs_read` and
`fs_grep` but before it produces the final answer). Merging the layers
silently would surprise users; the boot path keeps them distinct and lets
`/resume` opt-in to crossing over.

## File formats

Both layers are JSON arrays of `{role, content}` objects. Reads are
non-raising — corrupt files are treated as absent, and writes are atomic
(`.tmp` + `os.replace`) so a crash mid-write can't leave a half-written
checkpoint on disk.
