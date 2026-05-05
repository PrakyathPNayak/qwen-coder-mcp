# qwen-coder-mcp — Rolling State

This file is appended to by the agentic loop. Each entry records one iteration.

## Loop 292 — tool-continuation visibility and streamed-think cleanup

**OBSERVE**: The operator reported that TUI generation appeared to stop after
tool calls. A live Qwen3.6-27B probe against the running vLLM server showed the
core `run_agent` loop continued across `fs_read`, but streamed replies leaked
unwrapped reasoning ending in `</think>` and the UI had no explicit status
between a tool result and the next model turn.

**ORIENT**: Two adjacent failure modes can look like "stopped after tool":
1. a streamed model turn with only hidden reasoning becomes an empty visible
   reply and was treated as final;
2. after a tool result, the TUI reset the live widget to a bare ellipsis while
   Qwen spent time thinking, so long hidden reasoning looked like a freeze.

**DECIDE**: sanitize complete streamed turns with the batch think-stripper before
tool parsing/history persistence, retry empty visible turns with an explicit
continuation nudge, and emit/render a `model_start` event after tool feedback.
Also refresh the static system-prompt tool summary so it includes the newer
`http_request`, `json_query`, `env_get`, and `cp` tools; the dynamic catalog was
already accurate.

**DEVIL**: Sanitizing after streaming cannot prevent already-emitted live chunks
from momentarily appearing, but `_start_agent_turn` clears the live buffer on
assistant-turn boundaries and final/history output is now clean. Retrying empty
visible turns could burn steps if the model repeatedly emits only thoughts, but
`max_steps` bounds it and surfacing a final empty answer was strictly worse.

**ACT**: Added targeted tests for streaming dangling-think cleanup, empty-visible
retry, and `model_start` sequencing. Re-ran the live Qwen probe: event sequence
is now tool call -> tool result -> model_start step 2 -> final, with no hidden
reasoning in the final answer.

## Loop 293 — enforce HTTP mutation approval boundary

**OBSERVE**: `TOOL_BLURBS["http_request"]` said POST/PUT/DELETE/PATCH require
write-mode and Copilot approval, but `DEFAULT_TOOLS` exposed the full
implementation. A read-only agent could therefore attempt mutating HTTP methods
without entering the same approval path used by filesystem and shell tools.

**ORIENT**: This was a prompt/registry contract bug and a permission-boundary
bug. It was not enough to update text; the registry needed separate read-only
and write-mode behavior under the same tool name so the dynamic catalog remains
simple for the model.

**DECIDE**: Add a read-only wrapper for `http_request` in `DEFAULT_TOOLS` that
allows GET/HEAD/OPTIONS and rejects mutating methods before network I/O. Add the
full implementation to `WRITE_TOOLS`, let `ALL_TOOLS` override the wrapper, and
make confirmation conditional on the requested HTTP method so safe GET calls in
write mode do not pop unnecessary prompts.

**DEVIL**: Method-level confirmation is more complex than the old static
`DESTRUCTIVE_TOOLS` check, but without it either read-only mode is unsafe or
safe GET calls become noisy in write mode. Keeping the same public tool name
avoids teaching the model two nearly identical HTTP tools.

## Loop 294 — parse Qwen newline tool-call JSON and sanitize plain streams

**OBSERVE**: The real-model benchmark had no scenario errors but exposed visible
reasoning in plain chat heads. A TUI-like live probe produced an even more
actionable failure: Qwen emitted an unwrapped `</think>` followed by a
`<tool_call>` whose `fs_write.content` JSON string contained literal newlines.
The old parser dropped that block as malformed JSON, so plain streaming could
display a raw tool call instead of switching into agent mode.

**ORIENT**: This directly explains a class of "tool calls stop / do nothing"
reports: the model did request a tool, but the parser rejected the common
Qwen-shaped JSON and the TUI finalized the response as normal text.

**DECIDE**: Retry `json.loads(..., strict=False)` for tool-call blocks after the
strict parse fails, which accepts literal control characters inside JSON strings
without broadly inventing a custom parser. Also sanitize `_finalize_stream` and
committed `chat_turn_stream` history with `_strip_think_blocks` so unwrapped
thinking does not affect tool detection or final display.

**DEVIL**: `strict=False` is permissive, but it is limited to blocks already
inside explicit `<tool_call>` tags and still requires syntactically valid JSON
objects otherwise. That is a safer compatibility fix than regex-editing string
contents by hand.

**ACT**: Added parser and TUI streaming tests. Live probe now parses the
newline-containing `fs_write` call from sanitized history.

## Loop 295 — make real-model benchmark chat scenarios mirror TUI

**OBSERVE**: The `loop294-scan` benchmark reported chat scenario reasoning
leaks, but `_bench_chat` sent bare user-only histories while the TUI always
injects `prompts.CODER_SYSTEM`. The JSON also stored scenario names only under
`scenario`, while downstream inspection expected `name`.

**ORIENT**: The benchmark is only useful if it tests the same contract users see.
Bare-user chat exaggerates reasoning leaks and can hide TUI-specific prompt
regressions.

**DECIDE**: Seed `_bench_chat` history with `ChatMessage("system",
prompts.CODER_SYSTEM)` followed by the user prompt, and apply the same
`_strip_think_blocks` final cleanup used by TUI streaming. Preserve both
`scenario` and `name` keys in each result for compatibility.

**DEVIL**: A bare-client benchmark can still be useful for raw model behavior,
but this script explicitly claims to exercise the TUI render path. A separate
raw-model benchmark can be added later if needed.

## Loop 296 — hide live unwrapped-reasoning chunks in TUI

**OBSERVE**: Loop 294 cleaned final/plain-stream history, but `_on_stream_chunk`
still rendered the raw accumulated stream while Qwen was mid-generation. When
Qwen starts with unwrapped reasoning like "The user wants..." and only later
emits `</think>`, the TUI could briefly show hidden reasoning before final
cleanup removed it.

**ORIENT**: This is a display-layer bug. The client and finalizer can keep their
batch cleanup, while the live widget needs a conservative partial-output guard.

**DECIDE**: Add `sanitize_live_stream_accum()` in `tui.py`: first apply
`_strip_think_blocks`, then hide common reasoning prefixes until a close tag,
code fence, or `<tool_call>` appears. `_on_stream_chunk` renders a neutral
"thinking..." placeholder while the visible stream is empty.

**DEVIL**: Prefix heuristics can hide a legitimate answer that starts with "The
user..." until it produces visible markers or completes, but that is preferable
to leaking hidden reasoning in the live UI. Final history/rendering remains the
source of truth.

## Loop 297 — append_file write tool

**OBSERVE**: The model can write and edit files, but appending to rolling state
or log files still required re-reading and rewriting the entire file or using
shell redirection. That is unnecessary risk for `.agent/loop_log.md`,
`STATE.md`, and similar append-only artifacts.

**ORIENT**: A dedicated append tool is simpler for the model, safer than shell,
and naturally fits the existing write-tool confirmation boundary.

**DECIDE**: Add `_tool_append_file(path, content, create_parents=False)`, register
it in `WRITE_TOOLS`, add a catalog blurb, and update the static prompt summary.
It appends UTF-8 text inside the workspace, creates the file if needed, rejects
directories, and only creates parent directories when explicitly requested.

**DEVIL**: Appending can still corrupt structured files if used blindly, but it
is less destructive than whole-file writes and remains behind the same
Copilot-style destructive approval path.

## Loop 298 — guarded rm write tool

**OBSERVE**: The agent could create, edit, copy, move, and append files, but
could not delete files except through shell commands. That made cleanup tasks
noisier and pushed deletion toward the broader `run_shell` surface.

**ORIENT**: Deletion should be explicit, workspace-scoped, and confirmation
gated. A dedicated `rm` tool lets the model perform cleanup without shelling out
and keeps the operation visible in the tool audit log.

**DECIDE**: Add `_tool_rm(path, recursive=False, missing_ok=False)`, register it
in `WRITE_TOOLS`, and document it in the prompt/catalog. It refuses to remove
the workspace root, requires `recursive=true` for directories, and supports
`missing_ok` for idempotent cleanup.

**DEVIL**: `rm` is inherently dangerous, but the implementation is less risky
than shell deletion because it is workspace-resolved, root-refusing, and routed
through the destructive confirmation hook.

## Loop 299 — relative-root safety for new filesystem tools

**OBSERVE**: The new `rm` and `append_file` tools used `cfg.root` directly after
`fs_tools._resolve_inside_root()` returned resolved absolute paths. External API
callers can construct `FsConfig(root=Path("."))`, making direct comparisons such
as `target == cfg.root` false even when `target` is the workspace root.

**ORIENT**: This is a safety bug in a destructive tool. With a relative root,
`rm(path=".", recursive=true)` could bypass the root-refusal check and attempt
to remove the workspace root.

**DECIDE**: Normalize `cfg.root` with `resolve(strict=False)` before comparing
or rendering paths in `rm` and `append_file`. Add regression tests using a
relative-root `FsConfig` under `monkeypatch.chdir(tmp_path)`.

**DEVIL**: Most in-app configs use absolute roots, but helper/API callers do not
have to. Root safety must hold for every valid `FsConfig`, not only the common
TUI path.

## Loop 300 — rm symlink semantics and escape hardening

**OBSERVE**: Probing `rm` against a broken symlink showed `Path.exists()` returns
false, so the tool reported "not found" and left the link behind. The parent-only
resolution needed to delete symlink leaves safely also required a devil-step: a
final `..` path could otherwise escape as the leaf.

**ORIENT**: Deleting a symlink should remove the link itself, not follow the
target. That allows cleanup of broken links and links pointing outside the
workspace without touching outside targets, while still rejecting symlinked
parents and non-symlink escapes.

**DECIDE**: Resolve and validate the parent first, refuse lexical workspace root,
delete leaf symlinks directly via `unlink()`, then resolve/validate non-symlink
targets before file/directory deletion.

**DEVIL**: Allowing deletion of symlinks to outside might look like weakening the
sandbox, but unlinking an in-workspace symlink does not mutate the outside
target. Rejecting it would make broken/outside symlink cleanup impossible and
push users back toward shell commands.
