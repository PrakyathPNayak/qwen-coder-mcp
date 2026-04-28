# Loop log

Append-only. Read this at the start of every OBSERVE step.

---

## Loop 1 — pytest harness

**OBSERVE**: zero tests in repo; every parser in `agent/loop.py` was untested
critical-path code. `pytest.ini` and `tests/` did not exist.

**ORIENT**: highest-leverage gap was test scaffolding. Without it, no fix
can be verified and no refactor is safe.

**DECIDE**: add `pytest.ini`, `tests/{__init__,conftest}.py`, and
`tests/test_loop_parsers.py` covering `_strip_fence`, `_parse_first_issue`,
`_verdict_accepts`, `_apply_diff` (4 cases incl. fenced-diff), and
`_python_syntax_ok`.

**DEVIL**: (correctness) tests on private helpers freeze internal API —
acceptable since these helpers ARE the loop's contract with model output;
(scope) bugs vs tests order — tests must come first so fixes can be
verified; (priority) P1 data-corruption (JSON/TOML not validated) outranks
P3 test gaps — accepted, but the fix is unverifiable without tests, so
testing is the prerequisite. Plan stood.

**ACT**: 25 tests, all green. Added `[project.optional-dependencies] dev =
["pytest>=8.0"]` to `pyproject.toml`. Discovered and fixed a separate bug
mid-loop: `.gitignore` did not exclude `.venv-serve/` so the first commit
attempted to add the entire vLLM venv. Patched gitignore to ignore the
whole `.loop/` and `.venv-serve/` trees, kept `.agent/` tracked
(per directive: state must persist across sessions). Force-push replaced
the polluted commit on the remote.

**COMMIT**: `b69ec4f test: pytest harness + parser/diff/syntax coverage;
ignore venv/loop runtime`. Pushed to `origin/main`.

**Revealed**: parsers are now under test but their *known* weaknesses
(fence-around-prose, JSON/TOML validation) are not yet exercised by tests
that fail. The next loop should pick the highest-leverage *real* bug.

---

## Loop 2 — validate JSON / TOML / YAML before commit (P1 fix)

**OBSERVE**: previous loop log identified `_python_syntax_ok` as P1
data-corruption — only `.py` files were validated, so any diff that broke
`pyproject.toml`, JSON, or YAML config could be committed silently.

**ORIENT**: contained fix, immediate test verification possible thanks to
loop 1's harness. Highest leverage among open issues.

**DECIDE**: replace with `_validate_changed_files` covering py
(compileall), json (stdlib), toml (`tomllib` / fallback `tomli`), and
optional yaml (PyYAML if present). Keep `_python_syntax_ok` as a
backward-compat alias. Update call site in `_iteration` to use new name
and surface validation failure reason in revert path.

**DEVIL**: (correctness) heavy import at top level breaks startup if
PyYAML missing — mitigated by importing inside the function and skipping
silently on `ModuleNotFoundError`. (scope) is the real cause that the
loop blindly trusts diffs? — partially, but `git apply --check` plus this
gate IS the right level; deeper trust models would mean refusing all
diffs, defeating the purpose. (priority) should rebase-abort go first?
— no, P1 corruption beats P4 wedge; wedge is recoverable with one
manual command, corrupted config silently bricks the project.

**ACT**: 8 new tests covering py-invalid, json-valid/invalid,
toml-valid/invalid, missing-file (deleted-by-diff path) gracefully
skipped. 33/33 green. `python -m compileall agent src` clean.

**COMMIT**: pending — `fix(loop): validate JSON/TOML/YAML touched by
diffs before commit (was: only Python)`.

**Revealed**: `_iteration`'s revert path still doesn't tell `_changed_paths`
to include staged-but-not-yet-committed deletions; the validator now
gracefully skips missing files but the change-detection itself may miss
them. Worth checking.

---

## Loop 3 — abort failed rebase so the loop never wedges

**OBSERVE**: per `next.md`, `_commit_and_push` ran `git pull --rebase
--autostash` with `check=False` and ignored its return code. On a real
rebase conflict the working tree would be left mid-rebase; every
subsequent `git apply --check` would fail and the loop would be wedged
permanently until manual intervention.

**ORIENT**: rare but catastrophic — a single conflict could brick a
multi-day autonomous run. Cheap fix, high leverage.

**DECIDE**: introduce `_abort_rebase_if_any()` that detects
`.git/rebase-merge` or `.git/rebase-apply`, runs `git rebase --abort`,
then `git reset --hard HEAD` + `git clean -fd` if the tree is still
dirty. Wire `_commit_and_push` to call it on `git pull --rebase`
failure and return False. Verified the suspected `serve_qwen.sh`
double-`$MODEL` is NOT a bug — vLLM accepts multiple
`--served-model-name` aliases (just confirmed: PID 1493 booted with
`served_model_name=['qwen3.6-27b','Lorbus/...']`).

**DEVIL**: (correctness) `ORIG_HEAD` may not exist before any rebase —
mitigated by using `HEAD` directly and abort-first. (scope) Fighting
other writers is the symptom, deeper cause is single-writer assumption —
out of scope; loop is meant for autonomous solo use, conflict-handling
just needs to not wedge. (priority) Empty-diff no-op edge case is
guarded by existing `if not status.strip(): return False`. Plan stood.

**ACT**: 3 new tests in `tests/test_commit_and_push.py`:
1. End-to-end: bare remote + two clones, force a conflict, assert
   `_commit_and_push` returns False and tree is clean afterward.
2. `_abort_rebase_if_any` is a no-op on a clean repo.
3. `_abort_rebase_if_any` resets a dirty working tree.
36/36 tests green. Compileall clean.

**COMMIT**: pending — `fix(loop): abort failed git rebase so the loop
never wedges`.

**Revealed**: `_iteration` still has no scope check on the diff — model
could rewrite an unrelated file. Next loop.

---

## Loop 4 — diff-scope guard against silent multi-file rewrites

**OBSERVE**: `_iteration` calls `_apply_diff` then `_validate_changed_files`
then commits — never asserted the diff only touched the file the loop
asked the model to fix.

**ORIENT**: silent-trust violation; defeats the loop's "one file, one
fix" contract. Defensive code beats prompt constraints.

**DECIDE**: add `_diff_in_scope(changed, target)` and wire it into
`_iteration` between apply and validation; revert + emit
`out_of_scope:<path>` on failure (max 3 offenders in the message).

**DEVIL**: (correctness) some legit fixes need cross-file changes →
strict scope is safer; cross-file fixes can be a follow-up loop.
(scope) is the cause prompts? — partially, but defensive validation is
more reliable. (priority) just coverage? — no, it's correctness.
Plan stood.

**ACT**: 6 unit tests for `_diff_in_scope` covering single-target,
empty, foreign-file, posix/Path equivalence, and message truncation
on >3 offenders. Wired into `_iteration`. 42/42 green. compileall clean.

**COMMIT**: pending — `feat(loop): reject diffs that touch files outside
the targeted scope`.

**Revealed next**: `_strip_fence`'s anchored regex still requires
whole-input match; prose-around-fence will fall through.

---

## Loop 5 — robust fence/diff extractor

**OBSERVE**: `_FENCE_RE` was anchored with `^...$` and matched via
`.match()`, so any prose around the fence ("Here is the diff:\n```…```")
fell through to the raw text and `_apply_diff` rejected it. Also no
support for raw unified diffs that omit the fence.

**ORIENT**: `_strip_fence` runs on every iteration's coder + devil
output. Robustness here is high-leverage.

**DECIDE**: rewrite to (a) recognise raw `diff --git` / `--- ` outputs
without modification, (b) extract the inner text of the *first*
embedded fence via non-anchored search, (c) fall back to stripped
original.

**DEVIL**: (correctness) "first-fence" can pick wrong block when model
emits multiples. Initial draft kept a whole-input fast path which made
multi-fence inputs greedily match across fences. Caught by a multi-fence
test that failed. Fixed by removing the fast path entirely; non-greedy
embedded search picks the first fence deterministically. (scope) raw
diffs vs fences — handle both, model-friendly. (priority) crash-prone
`_load_cursor` deferred since `_strip_fence` runs on every iteration.

**ACT**: 7 new tests (prose-before, prose-after, prose-both,
multi-fence, raw `diff --git`, raw `--- `, no-fence-no-diff). 49/49
green. Removed unused `_FENCE_RE`. compileall clean.

**COMMIT**: pending — `fix(loop): tolerate prose around fences and raw
diffs in model output`.

**Revealed next**: `_load_cursor` has no error handling; malformed
`.loop/cursor.json` would crash the iteration. Then: `STATE.md`
unbounded growth.






## Loop 6 — 4xx fail-fast in QwenClient
**Pivot**: original target (`_load_cursor` crash) was already protected by
`except Exception: return 0`. Pivoted mid-DECIDE to candidate #4.

**Problem**: `qwen_client.chat` retried *all* 4xx responses 3x with exponential
backoff. Auth errors (401), bad payloads (400), permissions (403), unprocessable
(422) etc. wasted 7s + 3 token charges and never succeeded.

**Devil**: 408 (timeout) and 429 (rate limit) ARE legitimately retriable; a
naive "all 4xx fail-fast" rule would regress those. Refined plan: retry on
408/425/429 + 5xx + network; fail-fast on every other 4xx via a new
`QwenFatalError` (subclass of `QwenError`) caught and re-raised inside the loop.

**Fix**:
- Added `QwenFatalError` and `_RETRIABLE_4XX = {408, 425, 429}`.
- 5xx → `QwenError` (retried).
- 4xx in `_RETRIABLE_4XX` → `QwenError` (retried).
- Other 4xx → `QwenFatalError` (re-raised before backoff).

**Tests**: `tests/test_qwen_client.py`, 11 cases using `httpx.MockTransport`
and a monkey-patched `time.sleep`. Covers 200 happy path, fail-fast on
400/401/403/422 (each verified to call the transport exactly once),
retry-and-succeed on 408/429, retry-and-give-up on 500, recovery after one
503, malformed empty `choices`, and content-as-list-of-blocks extraction.

**Result**: 60/60 green. Commit `<filled in by git>`.

## Loop 7 — Scope guard now sees untracked files
**Bug**: `_changed_paths` used `git diff --name-only`, which lists only
modifications to tracked files. A model-produced diff that *creates* a new
file was invisible to `_diff_in_scope`, so the loop-4 scope guard could be
bypassed: a diff against `agent/loop.py` could secretly add `evil.py` at the
repo root and the loop would commit & push it. Compounding bug:
`_revert_changes` (`git checkout -- .`) cannot delete untracked files, so
even if the guard had caught it the file would have stayed.

**Devil**: parsing porcelain output is fragile around whitespace, renames,
and quoted paths. Counter: use `--porcelain=v1 -z -uall` (NUL-separated,
includes all untracked, stable format), and handle the rename/copy two-path
record explicitly.

**Fix**:
- `_changed_paths` rewritten to parse `git status --porcelain=v1 -z -uall`,
  yielding modified + added + deleted + renamed (both source and dest).
- `_revert_changes` now also runs `git clean -fd` so untracked files and
  empty dirs are wiped along with tracked-file restoration.

**Tests**: `tests/test_changed_paths.py`, 9 cases on a real per-test git
repo: modified, untracked, untracked-in-subdir, path-with-spaces, deleted,
revert-removes-untracked, revert-restores-modified, revert-clears-mixed,
end-to-end `_diff_in_scope` catches an untracked new file.

**Result**: 69/69 green. Commit `<filled by git>`.

## Loop 8 — Atomic cursor persistence
**Bug**: `_save_cursor` called `Path.write_text`, which is not atomic.
A SIGTERM/OOM/power loss between the implicit truncate and the actual
write leaves `cursor.json` empty. `_load_cursor` then falls back to 0
and the loop silently re-scans files it already covered — possibly the
same file forever in a small repo.

**Devil**: leaving a stale `.tmp` if the rename fails. Counter: catch
OSError around `os.replace`, unlink the tmp before re-raising. Verified
in `test_save_atomicity_no_partial_state_visible` — the original file
keeps its prior value when `os.replace` raises.

**Fix**: write to `cursor.json.tmp` then `os.replace` to `cursor.json`.
Atomic on POSIX and on same-volume Windows. Cleans up tmp on rename
failure.

**Tests**: `tests/test_cursor.py`, 8 cases — round-trip, overwrite,
no leftover tmp, missing file, empty file (simulated crash), corrupt
JSON, atomicity under simulated rename failure, deep parent creation.

**Result**: 77/77 green. Commit `<filled by git>`.

## Loop 9 — Normalise CRLF in `_apply_diff`
**Bug**: many code-tuned models emit Windows line endings (CRLF) or even
bare CR. `git apply` rejects "patch with CRLF line endings" by default,
so otherwise-correct fixes silently failed at the apply step.

**Devil**: stripping `\r` could destroy a legitimate patch encoding CRs in
file content. Counter: git apply itself refuses CRLF in patches; normalising
to LF is the documented workaround. Hunk *content* lines that need a literal
CR can encode it via the patch byte content (the patch format is
LF-line-oriented). Acceptable.

**Fix**: in `_apply_diff`, after `_strip_fence`, run
`diff.replace("\\r\\n", "\\n").replace("\\r", "\\n")` before piping to git.

**Tests**: `tests/test_apply_diff.py`, 6 cases on a real per-test git repo:
plain LF diff applies, CRLF diff applies after normalisation, bare-CR diff
applies, CRLF inside a markdown fence applies, non-diff input rejected,
mismatched-base diff fails at apply-check with a structured message.

**Result**: 83/83 green. Commit `<filled by git>`.

## Loop 10 — End-to-end orchestrator coverage
**Gap**: `_iteration` — the heart of the loop — had zero tests. Every
branch (clean / rejected / apply_failed / out_of_scope / validation_failed
/ applied) was production code that ran in the wild without any executable
contract.

**Devil**: a stub client doesn't exercise the real client (which is fine —
loop 6 covers the client). It exercises the orchestrator's branching, which
is what's untested. Acceptable.

**Fix**: `tests/test_iteration.py`, 6 cases on a real per-test git repo
with a scripted `_ScriptedClient.system_user(...)`. Verifies:
  - empty response → clean
  - devil REJECT → reverts, logs to STATE.md
  - non-diff prose → apply_failed
  - diff targeting the wrong file → out_of_scope, tree restored
  - syntactically broken diff → validation_failed, tree restored
  - happy path → applied, exactly one new commit, devil's-advocate ran

**Side-discoveries** (filed in next.md, not fixed this loop):
1. `_parse_first_issue` interprets a benign sentence like "No issues found."
   as a real issue, wasting a coder round-trip. The model should produce
   empty output for the no-issues case, but the parser should be lenient.
2. The test required adding `.gitignore` to mirror production. Confirms
   production correctness depends on `.loop/` being ignored — there is no
   test covering "what if a user installs without .gitignore". Filed.

**Result**: 89/89 green. Commit `<filled by git>`.

## Loop 11 — Self-defense against missing `.gitignore`
**Bug**: every iteration writes `.loop/cursor.json` and `.loop/runtime.log`,
and rolls `STATE.md`. After loop 7 made `_changed_paths` honour untracked
files, the loop became silently dependent on `.gitignore` excluding `.loop/`
and `STATE.md`. If a contributor cloned with a stale `.gitignore`, ran
`git rm` on it, or used `git update-index --skip-worktree` and drifted —
*every* iteration's diff would be tagged out_of_scope (because of the
loop's own untracked files) and the loop would run forever doing nothing
useful. Catastrophic fail-open.

**Devil**: should `.agent/loop_log.md` also be filtered? No — we
deliberately commit it every loop. So filter only the truly runtime-only
artefacts.

**Fix**: filter `.loop/...` and root-level `STATE.md` out of
`_changed_paths` directly. `_INTERNAL_PATHS = {Path(".loop"), Path("STATE.md")}`
+ `_is_internal_path()` helper that checks the first path segment.
`.agent/` is intentionally NOT filtered.

**Tests added**:
- `test_changed_paths_filters_loop_internals` — `.loop/cursor.json` and
  `STATE.md` invisible to scope checks.
- `test_changed_paths_does_not_filter_dot_agent` — `.agent/loop_log.md`
  remains visible.
- `test_changed_paths_filters_state_md_in_root_only` — `docs/STATE.md`
  (a hypothetical user file) NOT filtered.
- `test_applied_path_works_without_gitignore` — end-to-end: deleting
  `.gitignore` from the repo entirely doesn't break the happy-path
  iteration. Pre-fix this would have failed with out_of_scope.

**Result**: 93/93 green. Commit `<filled by git>`.

## Loop 12 — `_parse_first_issue` no-issue allow-list
**Bug**: "No issues found." was returned as the first "issue", causing
the loop to send a wasted `propose_fix_user` + `devils_advocate_user`
round-trip on every clean file. With a 27B model that's ~30s of GPU time
+ token cost burned per clean scan, multiplied by every loop.

**Devil**: false negatives — what if a real issue happens to mention
"no issues"? Counter: short-circuit only fires when (a) the entire reply
is one line, (b) it has no list markers, and (c) it matches a strict
allow-list regex of "no findings"/"looks good"/"lgtm"/"nothing to fix"
phrases. A bullet that contains "no issues" (e.g. "- There is no bound
check…") is parsed as a real issue. Verified by
`test_real_issue_containing_word_no_is_still_parsed` and
`test_multiline_with_bullet_after_benign_intro_uses_bullet`.

**Fix**: added `_NO_ISSUE_RE` (verbose regex covering: no
{issues|bugs|problems|errors|findings|defects|concerns} [found] [in/with
{this|the} {file|code}]; [everything|this [code]|the code] looks
{good|fine|clean|ok|okay|correct} [to me]; lgtm; nothing to {fix|change|
do|report}/wrong/broken; clean; all good). Short-circuit applied at the
top of `_parse_first_issue` (when no list markers in the text) AND at
the prose fallback (last-line rejection).

**Tests**: 25 new cases in `tests/test_loop_parsers.py` —
parametrized over 22 benign replies (all return None), plus two
adversarial cases ensuring real bullets containing "no" still parse.

**Result**: 117/117 green. Commit `<filled by git>`.

## Loop 13 — `prompts.py` contract tests
**Bug** (latent): `prompts.py` is what the loop says to the model. Every
parser in `loop.py` makes brittle assumptions about specific tokens
("VERDICT: ACCEPT", "NO_ISSUES", `--- a/` / `+++ b/`, "numbered list").
A future copy-edit could silently drop one of those instructions and
turn every iteration garbage into every fix would be tagged 
`not_a_unified_diff`, every verdict `no_verdict`, every clean file
re-prompted forever — *with the test suite still green*.

**Devil**: too-strict tests bind us to phrasing instead of intent.
Counter: tests assert presence of *the exact tokens parsers look for*,
not whole sentences. Phrasing around them can change. Verified by
keeping assertions to substrings ("unified diff", "NO_ISSUES",
"VERDICT: ACCEPT", "--- a/", "+++ b/", "numbered list").

**Fix**: new `tests/test_prompts.py` with 12 cases covering all 9
prompt builders + 3 system prompts. Each high-stakes prompt has at
least one assertion that, if removed by a regression, the parser would
break in production.

**Result**: 129/129 green.

## Loop 14 — `_apply_diff` rejects unsafe paths pre-apply
**Bug**: `_apply_diff` trusted `git apply` to be the only line of
defence against malicious model output containing path traversal
(`+++ b/../../etc/passwd`), absolute paths (`+++ b//etc/passwd`),
Windows drive paths (`+++ b/C:/...`), or backslash paths. `git apply`
*does* refuse most of these, but the failure was logged generically as
`apply_failed: ...` and indistinguishable from a normal patch-conflict
failure. Worse: any future change to git's safety posture would
silently broaden our trust boundary.

**Devil**: (a) Could a legit diff have `..` in a header path? No —
repo paths are POSIX-relative in `--- a/PATH` / `+++ b/PATH`.
(b) Does this hide real issues? No — the new error is *more* specific,
not less; clean diffs are unaffected (verified by
`test_apply_diff_allows_normal_relative_paths` that actually runs git
apply against a temp repo and gets `"applied"`).
(c) `/dev/null` for new-file diffs — handled: `_diff_paths` skips it,
verified by `test_diff_paths_skips_dev_null`.

**Fix**: added `_DIFF_PATH_HEADER_RE`, `_diff_paths()`,
`_has_unsafe_path()` in `agent/loop.py`. `_apply_diff` calls
`_has_unsafe_path` after CRLF normalisation and returns
`unsafe_path: <reason>` distinctly. Reasons: `path_traversal:<p>`,
`absolute_path:<p>`, `backslash_in_path:<p>`, `empty_path`.

**Tests**: 11 new in `tests/test_apply_diff_paths.py` —
parametrised traversal cases, absolute (POSIX & Windows-drive),
backslash, /dev/null preservation, and a real-git-apply happy-path.

**Result**: 140/140 green.

## Loop 15 — `_apply_diff` structural defect detection
**Bug**: A diff missing `+++ b/PATH` or with no `@@` hunks was rejected
opaquely as `apply_check_failed: ...`. Distinct outcomes make logs
greppable and let the loop later distinguish "model emits malformed
diffs" from "model emits diffs against stale file content".

**Devil**: (a) Are there valid diffs without hunks? Pure rename/mode-
chmod payloads exist in git, but a coding model fixing a bug should
never need them, and `git apply` against rename-only without hunks is
also a no-op risk. Worth refusing. (b) Will `/dev/null` new-file diffs
be flagged? No — they have `--- /dev/null`, `+++ b/PATH`, and `@@`
hunks. Verified by `test_apply_diff_accepts_well_formed_diff_with_dev_null`.
(c) `+++` without `---` is already caught earlier as
`not_a_unified_diff`; verified by
`test_apply_diff_plus_only_caught_as_not_a_unified_diff`.

**Fix**: `_has_structural_defect()` returns one of
`missing_plus_header`, `missing_minus_header`, `no_hunks`. Wired into
`_apply_diff` after `_has_unsafe_path`. Returns `malformed_diff: <reason>`.

**Tests**: 4 new in `tests/test_apply_diff_paths.py`.

**Result**: 144/144 green.

## Loop 16 — `_build_server` accepts injected client; smoke tests for server
**Bug** (latent): `_build_server` always constructed a fresh
`QwenClient`, so any future test of the MCP-server surface (tool
registration, dispatch routing, temperature bindings) would either need
to mock httpx globally or accept a real-client side-effect. There was
also no test guarding the import path.

**Devil**: (a) Resource-leak risk if the caller passes a client they
own and `_run` later closes it? Not in the current call chain — only
`_run` calls `_build_server` in production, with no client arg.
Injection is purely a test seam. (b) Should we make `_build_server`
*always* accept an injected client (no auto-construct)? No — that
breaks `main()`. Keep optional. (c) Did this fix ship a side-effect?
No: 144 → 164 tests, 100% green; no production code path changed.

**Fix**: `_build_server(client: QwenClient | None = None)` —
auto-constructs only when `client is None`.

**Tests**: 20 in `tests/test_server.py`:
- import without I/O,
- `_build_server` accepts injected stub,
- default path makes a real `QwenClient`,
- 9 dispatch routes (one per tool),
- unknown-tool ValueError,
- propose_fix temperature=0.1,
- devils_advocate temperature=0.0,
- chat default temperature=0.2 + caller-override,
- find_bugs uses REVIEWER_SYSTEM,
- devils_advocate uses DEVILS_ADVOCATE_SYSTEM,
- handlers registered ≥2 (list_tools + call_tool).

**Result**: 164/164 green.

## Loop 17 — STATE.md rotation
**Bug**: `STATE.md` was append-only. Over months of loop runs it would
grow unbounded — slowing every read of the file, eventually pushing
file system limits, and crowding `git status` even though it's
internally filtered. (Discovery: candidate #1 from next.md —
JSON/TOML/YAML validation — turned out to already be implemented;
audit-as-OBSERVE caught the stale next.md entry. Pivoted to candidate
#2.)

**Devil**: (a) Could rotation lose data? `os.replace` is atomic on the
same filesystem; archive lives under `.loop/state_archive/` (same
volume as STATE.md). Tests verify body integrity end-to-end.
(b) Could rotation expose archived state to git? `.loop/` is in
`.gitignore` and in `_INTERNAL_PATHS`, so the archive is filtered
twice. Verified via `test_archive_dir_is_under_loop`.
(c) Could same-second rotations collide? Yes: dedupe via
`STATE.<ts>.<N>.md` suffix; verified by
`test_rotation_dedupes_same_second_collision`.
(d) Could the freshly-rotated file (with header) re-trigger rotation
on the next call? Yes if threshold < header size (~130 bytes); in
production threshold is 256 KB so this is impossible. The two tests
that probe this use threshold ≥ 500 to stay above the header.

**Fix**: added `STATE_MAX_BYTES=256*1024`, `STATE_ARCHIVE_DIR`,
`_rotate_state_if_needed()`. Wired into `_append_state` so every
write checks size first.

**Tests**: 8 in `tests/test_state_rotation.py` —
no-op-under-threshold, rotation-when-over, missing-file, append-
triggers-rotation, same-second-collision, archive-is-internal-path,
threshold-default-sane, idempotent-when-fresh.

**Result**: 172/172 green.

## Loop 18 — `_apply_diff` rejects symlink and gitlink modes
**Bug** (security): a model-emitted diff with `new file mode 120000`
followed by a hunk creating a symlink would land in the worktree
unchallenged. The symlink target (the `+` content line) is just bytes
to git — it could point at `/etc/passwd`, `~/.ssh/authorized_keys`, or
any path; subsequent reads of the "fixed" file by the loop's
validation (or downstream tools) would read the linked content. Same
risk class as `120000` (symlink) — `160000` (gitlink/submodule) is
similarly out of scope for a code-fix loop.

**Devil**: (a) Could a legitimate fix add a symlink? In a Python repo,
effectively never; build-system symlinks are pre-existing.
(b) False positives on mode-numbers in *content* lines? No — the
predicate only matches lines starting with `new file mode `,
`new mode `, `old mode `, or `deleted file mode `. Verified by
`test_has_unsafe_mode_ignores_mode_in_content_lines`.
(c) Does this hide info? No — the new error tag (`unsafe_mode:`) is
distinct from `unsafe_path:`/`apply_failed:`/`malformed_diff:`,
making logs greppable.

**Fix**: `_has_unsafe_mode()` predicate; wired into `_apply_diff`
between `_has_unsafe_path` and `_has_structural_defect`.

**Tests**: 7 new in `tests/test_apply_diff_paths.py` —
parametrised over 5 mode-header variants (4 symlink + 1 gitlink),
plus normal-mode allow-list and content-line false-positive guard.

**Result**: 179/179 green.

## Loop 19 — `_save_cursor` is non-raising on rename failure
**Bug**: when `os.replace` failed (disk full, permissions, etc.),
`_save_cursor` re-raised the OSError. The outer `_main` loop catches
all exceptions, BUT the iteration aborts AFTER the model round-trip
already cost time/tokens, and the next iteration calls
`_load_cursor()` which reads from disk and gets the OLD cursor again
 so the loop *forever* re-scans the same file (and forever fails
the same save) until disk recovers. Cost: unbounded GPU/token waste
for the duration of the disk problem.

**Devil**: (a) Could swallowing hide a real disk problem? `_log` also
writes to disk; if disk is full, the log write fails too. But we
already wrap `_log` in try/except in the new code, so nothing
propagates. The operator finds out via either the log (when disk
recovers) or via the empty `STATE.md` writes (also failing). Net: no
worse than re-raising. (b) Could it mask cursor inconsistency? No —
the previous CURSOR_FILE is preserved by atomic-replace contract.
Verified by retained `test_save_atomicity_no_partial_state_visible`.
(c) Could the wrapped-`_log` catch ever swallow an exception we *do*
care about? It catches `Exception`, not `BaseException` (so SIGINT /
SystemExit propagate). That's the right boundary.

**Fix**: `_save_cursor` catches `OSError`, cleans tmp, logs,
returns. Inner `_log` is wrapped in `try/except Exception: pass`.

**Tests**: existing `test_save_atomicity_no_partial_state_visible`
updated (no longer expects raise; still asserts state preservation).
Two new tests: `test_save_cursor_swallows_rename_failure_and_logs`
asserts the log message is emitted; `test_save_cursor_swallows
_rename_failure_even_if_log_fails` asserts a broken logger doesn't
crash the loop.

**Result**: 181/181 green.

## Loop 20 — `_apply_diff` accepted binary patches
**Bug**: `_apply_diff`'s safety stack rejected unsafe paths and modes,
but a model emitting `Binary files a/x and b/y differ` (no-op marker)
or a real `GIT binary patch\nliteral N\n<base85>` block would slip
through to `git apply`. The latter could write arbitrary bytes to
files in the repo. The former is benign but indicates the model
misunderstood the task and we'd silently commit a no-op as "applied".

**Devil**: (a) Correctness — does our predicate flag content that
just *mentions* the phrase? Initial impl used line-strip; a markdown
context line ` Binary files a/x and b/y differ` would match. Fixed by
restricting the scan to header lines (everything before `@@` per
file), and re-arming after a new `diff --git` / `---` boundary.
Added two regression tests. (b) Scope — root cause is "model output
is untrusted"; this is one more layer of the existing defense in
depth, alongside path/mode/structural checks. Not addressing
"validate model is sane", which is a different problem. (c) Priority
 image-corruption / arbitrary-bytes scenario is plausible and the
fix is small.

**Fix**: `_has_binary_patch(diff)` returns `git_binary_patch` /
`binary_files_marker` / None. Wired into `_apply_diff` between path
and mode checks. Returns `binary_patch:<reason>` on rejection.

**Tests**: 6 new in `tests/test_apply_diff_paths.py` — accept-reject
for both markers, ignore-in-content (+ line, - line, context line),
re-arm after second file.

**Result**: 187/187 green.

## Loop 21 — `_has_unsafe_path` ignored rename/copy headers
**Bug**: `_DIFF_PATH_HEADER_RE` only matched `diff --git a/X b/Y`,
`--- a/X`, `+++ b/X`. But `git apply` honours `rename from <path>`,
`rename to <path>`, `copy from <path>`, `copy to <path>` headers
(unprefixed paths). A diff with safe `--- a/foo.py` / `+++ b/bar.py`
plus `rename to ../../etc/passwd` would slip past the path check
even though `git apply` would happily perform the rename to the
traversal target. CVE-class.

**Devil**: (a) Correctness — does the new regex match content lines?
The pattern is anchored with `^` + multiline; `rename from` /
`rename to` / `copy from` / `copy to` strings *do* appear in some
prose, but a content line begins with `+`/`-`/space, so the literal
"rename" at column 0 only happens in headers. (b) Scope — could
there be other rename-form headers? `similarity index N%`,
`dissimilarity index N%`, `index <sha>..<sha>` don't carry paths.
`old mode`/`new mode` are mode lines (already covered). The set
{rename from, rename to, copy from, copy to} is closed. (c) Priority
 write-out-of-tree is the biggest threat the path check exists to
defend against; closing this gap is essential.

**Fix**: New `_DIFF_RENAME_COPY_RE`; `_diff_paths` iterates both
regexes; `_has_unsafe_path` now sees rename/copy paths and applies
the same absolute / traversal / backslash rejection.

**Tests**: 5 new — `_diff_paths` includes rename-to path,
traversal/absolute/backslash all rejected via `unsafe_path:`, and a
safe rename still passes.

**Result**: 192/192 green.

## Loop 22 — `_has_unsafe_mode` ignored mode encoded on `index` line
**Bug**: `_has_unsafe_mode` only inspected the four explicit mode-
header forms. But `git diff` emits the file mode on the `index <sha>
..<sha> <mode>` line for new files. A minimal symlink-creating diff
can omit `new file mode 120000` and rely solely on
`index 0000000..abc 120000`. Our previous defense missed it; only
`git apply`'s own refusal would have caught it (and that's an opaque
"apply_failed:").

**Devil**: (a) Correctness — `index <sha>..<sha>` with NO mode
(short form) must NOT be flagged; tested. (b) The split() count must
be exactly 3 to avoid matching `index a..b 100644 weird_extra`;
tested with normal-mode case. (c) Scope — does git ever encode mode
elsewhere we missed? `<mode>` only appears in (1) explicit mode
headers, (2) the index line, (3) the `:<mode>` raw-format prefix
(which we never see — that's diff-tree output, not patch). Closed
set. (d) Priority — closes the same write-out-of-tree class as loop
21; necessary completion.

**Fix**: extended `_has_unsafe_mode` to scan `index ` lines, parsing
the trailing token only when the line has exactly 3 space-separated
fields and the last is `120000` / `160000`.

**Tests**: 5 new — symlink/gitlink via index, normal mode accepted,
short index line accepted, end-to-end `_apply_diff` rejection.

**Result**: 197/197 green.

## Loop 23 — `_strip_fence` could not salvage unclosed fences
**Bug**: when the model emits an opening fence ``` ```diff ``` but
forgets to emit the closing ``` ``` ```, `_INNER_FENCE_RE` (which
requires `\n```` to terminate) doesn't match. `_strip_fence` then
falls through to "return text" — leaving the literal `\`\`\`diff`
opener prepended to the diff body. `_apply_diff` rejects it as
`not_a_unified_diff`. A salvageable, common LLM failure was being
turned into a dropped iteration.

**Devil**: (a) Correctness — could the salvage fire when it
shouldn't? It only fires when text *starts* with the open fence
(after .strip() of outer whitespace), so prose-before-fence still
falls through (tested). (b) Could the body legitimately contain a
trailing ``` ``` ``` we shouldn't strip? In a unified diff, no line
begins with three backticks; content lines start with `+`, `-`, or
space. So the trailing-strip is safe. Tested. (c) Priority — common
model failure, recoverable, low risk. Worth doing.

**Fix**: added `_OPEN_FENCE_RE` / `_CLOSE_FENCE_RE`. When inner-fence
regex doesn't match but text starts with an open fence, strip the
opener and any straggling closer; return the salvaged body.

**Tests**: 5 new in `tests/test_loop_parsers.py` —
unclosed-with-lang, unclosed-bare, prose-before doesn't salvage,
already-closed still works, dangling-close stripped.

**Result**: 202/202 green.

## Loop 24 — `_apply_diff` had no diff-size clamp
**Bug**: any diff size, in bytes or lines, was accepted up to the
limits of `git apply` itself. A model emitting a 50,000-line patch
(entire repo rewritten, hallucinated mass refactor, context-window
leak) would consume seconds of `git apply`, scribble across
hundreds of files, then trigger validation failures and a revert
costing more time. Worse, an oversized patch with one valid hunk
might apply partly before a failure mid-patch.

**Devil**: (a) Correctness — what bound? Set 256 KB / 5000 lines —
~50× larger than any realistic single-fix diff. False-positive risk
is near zero. (b) Scope — root cause is "untrusted model"; this is
a resource bound, not a security check. Fits the defense-in-depth
stack. (c) Priority — caps a real failure mode; cheap and safe.
(d) Ordering — placed BEFORE path/mode/structural checks because
those iterate the whole diff and on an oversized diff that work is
itself the cost we're trying to avoid. Tested.

**Fix**: `_MAX_DIFF_BYTES = 256*1024`, `_MAX_DIFF_LINES = 5000`.
`_has_oversized_diff` returns `size_bytes:` / `size_lines:` / None,
wired into `_apply_diff` immediately after CRLF normalisation.

**Tests**: 5 new — accept-small, reject-bytes (monkeypatched cap),
reject-lines, end-to-end `oversized_diff:` prefix, ordering check
(oversized takes precedence over path-unsafe).

**Result**: 207/207 green.

## Loop 25 — `system_user` lossy kwargs passthrough
**Bug**: `QwenClient.system_user` only forwarded `temperature` and
`max_tokens` to `chat`. Callers couldn't set `top_p`, `stop`,
`extra`, or `max_retries`. The agent loop happens to use only the
two forwarded kwargs, but the contract was silently lossy: a future
caller asking for `stop=["</s>"]` would have it discarded with no
warning.

**Devil**: (a) Correctness — adding kwargs with same defaults as
`chat` doesn't change any current behavior. Tested via "defaults
match" assertion. (b) Could `extra` be misused to override `model`
field? Yes, but `chat` already does `payload.update(extra)` with
that risk; not new. (c) Priority — moderate; contract gap with no
current bug, but inevitable foot-gun. Cheap fix.

**Fix**: `system_user` now accepts `top_p`, `stop`, `extra`,
`max_retries` and forwards them. Defaults match `chat`.

**Tests**: 3 new in `tests/test_qwen_client.py` — full passthrough
verified by inspecting outbound payload, defaults match `chat`
defaults, `max_retries=1` actually limits retries.

**Result**: 210/210 green.

## Loop 26 — `_verdict_accepts` brittle on whitespace + truncation
**Bug**: `_verdict_accepts` did `text.upper(); "VERDICT: ACCEPT" in
upper`. That accepts only the exact form with one space. It missed
`VERDICT : ACCEPT`, `VERDICT:ACCEPT`, `VERDICT:  ACCEPT`, etc. The
prompts tell the model to emit `VERDICT: ACCEPT` exactly, but
in-the-wild model output drifts on spacing. Conservative reject was
the safe fallback, but it also turned legitimate accepts into
rejects, dropping work. Also: reject reason captured everything
after the verdict via `.*` (non-DOTALL) → just the line, but then
`.strip()` could carry pages of commentary into the log.

**Devil**: (a) Correctness — could a more permissive regex accept
ACCEPTANCE? Word-boundary `\b` prevents that. Tested. Could it
accept REJECTED as REJECT? Same — `\b` after REJECT prevents match,
falls through to `no_verdict`. Tested. (b) Scope — root cause is
"verdict-grammar contract is fragile". A regex tightens the parser
without making the contract more permissive in a way that risks
runaway accepts. (c) Priority — directly governs whether a fix is
committed; a missed accept costs an entire loop iteration.

**Fix**: `_VERDICT_ACCEPT_RE = re.compile(r"VERDICT\s*:\s*ACCEPT\b",
re.IGNORECASE)`, `_VERDICT_REJECT_RE = re.compile(r"VERDICT\s*:\s*
REJECT\b\s*(.*)", IGNORECASE | DOTALL)`. Reject reason truncated to
first line.

**Tests**: 7 new — extra spaces around colon (accept+reject),
no-space, multiple-spaces, reject reason single-line truncation,
ACCEPTANCE doesn't false-accept, REJECTED falls through.

**Result**: 217/217 green.

## Loop 27 — `_extract_text` silently dropped empty content
**Bug**: `_extract_text` returned `""` for `content=None`,
`content=[]`, `content="   "`, or a blocks list of empty texts. The
chat() call then returned `""` to the agent loop. Downstream
parsers misclassified empty as: `_parse_first_issue` → "no
findings"; `_verdict_accepts` → "no_verdict" (conservative reject).
Both classifications drop the iteration. The real failure (backend
returned no text) was invisible to the retry path — chat() retries
on QwenError, but `_extract_text` raised QwenError only on shape
errors, never on empty content.

**Devil**: (a) Correctness — could a legitimate empty assistant
answer exist? In this agent's domain every prompt requires
substantive output (diff / issue / verdict). Empty IS a failure.
(b) Could whitespace-only be valid? After strip → empty, treated as
failure. Acceptable (no legitimate diff is whitespace). (c) Scope —
root cause is "extract was overly forgiving"; fix tightens the
contract without changing the happy path. (d) Priority — directly
costs loop iterations to silent failures.

**Fix**: `_extract_text` now raises `QwenError` when the extracted
text is empty after strip (regardless of None / [] / "" / blocks
list / object). chat() retries 3× then surfaces.

**Tests**: 5 new — empty string with retry-count check, None,
empty list, blocks-with-empty-text, whitespace-only.

**Result**: 222/222 green.

## Loop 28 — `_apply_diff` had no subprocess timeout
**Bug**: `subprocess.run(["git", "apply", ...])` had no `timeout=`
kwarg. A pathological diff could hang `git apply` indefinitely
(e.g., a malformed binary patch hint, certain renames, or simply a
disk-bound git operation). The agent loop would block forever on a
single iteration with no recovery — the supposed-infinite-improvement
loop becomes stuck.

**Devil**: (a) Correctness — what timeout? 30s is generous for a
file-level patch (git apply on 5000 lines at most takes <1s). False
positives on slow disks are possible; the tradeoff is "iteration
slow" vs "loop wedged forever". Pick wedged-recovery. (b) Could we
miss a legitimate large patch? `_has_oversized_diff` already caps at
256KB / 5000 lines; within that, 30s is huge. (c) Scope — root
cause is "subprocess can hang"; this caps the bound. (d) Priority —
keeps the operating-law promise that the loop never stops. Without
this, a single hung subprocess defeats the whole agent.

**Fix**: `_GIT_APPLY_TIMEOUT_SECONDS = 30`; new `_run_git_apply`
helper wraps subprocess.run with timeout, kills on TimeoutExpired,
returns `(124, "timed_out_after_30s")`. Both check and apply calls
now go through it.

**Tests**: 4 new — wrapper returns 124 on timeout, end-to-end
`apply_check_failed:` and `apply_failed:` carry the timeout marker,
timeout kwarg is actually forwarded.

**Result**: 226/226 green.

## Loop 29 — `_run_git` had no timeout, `_revert_changes` could wedge
**Bug**: `_run_git` is the canonical wrapper for non-apply git
calls (status, checkout, clean, diff, log, commit). None of them
had a timeout. `_revert_changes()` calls `git checkout -- .` then
`git clean -fd`; on a slow / failing filesystem either could hang
indefinitely. Loop 28 capped `_apply_diff` but the recovery path
remained vulnerable: an applied-then-rejected diff could wedge the
loop in revert.

**Devil**: (a) Correctness — what about callers passing
`check=True`? They want the exception to surface (they're verifying
preconditions); preserve that semantics. With `check=False` the
caller is in best-effort cleanup mode; synthesise a 124
CompletedProcess. (b) Could timeout-then-synthesised-CP hide a real
bug? The synthesised stderr explicitly says
`timed_out_after_<N>s`; loggable. (c) Scope — the same remedy
should apply uniformly across the wrapper, not be retro-fitted at
each call site. (d) Priority — backstops the operating-law
guarantee.

**Fix**: `_GIT_CMD_TIMEOUT_SECONDS = 60`. `_run_git` passes
`timeout=` to subprocess.run; on TimeoutExpired with check=False it
logs and returns rc=124, stderr="timed_out_after_60s"; with
check=True it re-raises (preserves caller contract).

**Tests**: 4 new — check=False synthesises 124, check=True
re-raises, timeout kwarg forwarded, `_revert_changes` survives
double-timeout.

**Result**: 230/230 green.

## Loop 30 — `_validate_changed_files` had no timeout on `compileall`
**Bug**: `_validate_changed_files` ran `python -m compileall -q ...`
with no timeout. compileall imports the target modules (it
byte-compiles them — but does NOT execute their top-level code; OK).
Wait — no: `compileall` does NOT import; it parses and writes
.pyc. But `py_compile` similarly. However: pathological inputs
(extremely large file, recursive globs of cyclic symlinks) could
still hang the subprocess. And since validation runs INSIDE the
loop's recovery window, a hang here wedges the iteration.

Also confirmed: `_pick_target_file` is called via `_iteration` with
the `if not files: return "no_candidate_files"` guard already in
place — empty-list case was a non-bug. Pivoted to validator
timeout.

**Devil**: (a) Correctness — what if compileall genuinely takes >30s
on a large set? Within the loop the change-set is bounded by the
diff scope (already capped by `_diff_in_scope` and oversized check),
realistically a single file. 30s is plenty. (b) Could the timeout
mask a real syntax error? No — TimeoutExpired is distinct from a
non-zero return code; we tag the message `timed_out_after_<N>s`
inside `py_invalid:` so logs are unambiguous. (c) Priority — same
class as loops 28/29; symmetric backstop.

**Fix**: `_VALIDATE_TIMEOUT_SECONDS = 30`, threaded into the
compileall subprocess.run; on TimeoutExpired return
`(False, "py_invalid: timed_out_after_30s")`.

**Tests**: 2 new — TimeoutExpired -> py_invalid:timed_out, timeout
kwarg actually forwarded. Existing tests still pass.

**Result**: 232/232 green.

## Loop 31 — iteration wall-clock budget
- OBSERVE: Three model calls per iteration; httpx 120s timeout × max_retries=3 with backoff means a single iteration could hang ~20 minutes if backend flaps. No outer deadline.
- ORIENT: Real risk during vLLM warmup or partial outages. Highest impact loop-control gap remaining.
- DECIDE: Add `_iteration_budget_seconds()` (env-overridable via `QWEN_LOOP_ITER_BUDGET_S`, default 600s, fallback on bad/<=0 input). Compute `deadline = monotonic() + budget` once per iteration; check between phases (after find_bugs, after propose_fix, after devils_advocate). Return distinct `budget_exceeded:<file>:<phase>` so logs separate this from network errors.
- DEVIL:
  - Correctness: cannot cancel an in-flight httpx call; one call may complete past deadline. Acceptable — bounds at 1× call beyond budget instead of 3×.
  - Scope: addresses cause (no outer deadline), not symptom.
  - Priority: yes, hangs are higher impact than minor cleanups.
- ACT: Added helper + 3 between-phase checks in `_iteration`. 4 new tests. 236/236 green.
- COMMIT: pending.

## Loop 32 — qwen_client: gate `extra` against reserved keys
- OBSERVE: `chat()` does `payload.update(extra)` unconditionally. After loop 25 made `system_user` forward `extra`, callers can silently overwrite `model`, `messages`, `stream` — the last would break `_extract_text` (not built for streaming chunks).
- ORIENT: Real correctness footgun reachable from the agent loop. Other candidates audited in this loop (`_revert_changes` symmetric, `_strip_fence` nested fences low-impact) didn't show actionable bugs.
- DECIDE: Reject `extra` payloads that intersect `{model, messages, stream}` with `QwenFatalError` (non-retriable). Lists conflicting keys for the caller.
- DEVIL:
  - Correctness: any legitimate caller currently passing one of these via `extra`? `system_user` only forwards from typed kwargs; no test/source path passes them. Safe.
  - Scope: addresses the cause (no validation), not a symptom.
  - Priority: defensive but cheap; aligns with earlier loops hardening untrusted-input paths.
- ACT: Added reserved-key intersection check; raised QwenFatalError. 5 new tests (override-model, override-messages, override-stream, multi-conflict, safe-keys-pass-through). 241/241 green.
- COMMIT: pending.

## Loop 33 — surrogateescape decoding for git subprocesses
- OBSERVE: `_run_git`, `_run_git_apply`, and `_validate_changed_files` use `subprocess.run(text=True)` with default error handling. A path with bytes invalid in the locale's encoding (e.g. `weird-\xff.txt`) crashes `_changed_paths` with UnicodeDecodeError, killing the whole iteration.
- ORIENT: Niche but real — repos created on different locales or imported from non-utf8 sources can carry such paths. Higher impact than rotation/cosmetic items.
- DECIDE: Pass `errors="surrogateescape"` to all three subprocess.run sites. Round-trips raw bytes through str via low surrogates; Path/os ops re-encode losslessly via `os.fsencode`.
- DEVIL:
  - Correctness: surrogate-escaped strings interact badly with anything that re-encodes via .encode() without errors='surrogateescape'. We never .encode() these strings directly; they pass through Path() and back to os via fsencode. Safe.
  - Scope: cause-level, not symptom.
  - Priority: cheap, high robustness.
- ACT: 3 subprocess.run sites now pass `errors="surrogateescape"`. Regression test creates a real `weird-\xff.txt`, calls `_changed_paths()`, and verifies the path round-trips via `os.fsencode`. 242/242.
- COMMIT: pending.

## Loop 34 — `_read_file` symlink-escape guard
- OBSERVE: `_read_file` did `path.read_bytes()` directly, so a symlink committed in the repo pointing at e.g. `/etc/passwd` would have its content fed into the model prompt. Higher-impact than rotation/cosmetic items already triaged.
- ORIENT: Real attack surface for shared/cloned repos. `_candidate_files` uses `os.walk(followlinks=False)` which prevents directory symlink traversal but leaves file symlinks intact.
- DECIDE: Resolve the path strictly, require `is_relative_to(_REPO.resolve())`, then read. Defense in depth: rejects out-of-repo symlinks AND dangling links (resolve strict=True raises).
- DEVIL:
  - Correctness: in-repo symlinks (legitimate) still resolve under `_REPO` and read fine. Verified by test.
  - Scope: cause-level fix.
  - Priority: security — high.
- ACT: Added resolve+is_relative_to guard. New file `tests/test_read_file.py` with 7 cases (normal, outside-symlink, inside-symlink, too-large, invalid-utf8, missing, dangling-symlink). 249/249 green.
- COMMIT: pending.

(Note: candidate `runtime.log iteration outcome` was already implemented at line 950 — `_log(f"iteration -> {outcome}")` — so loop pivoted to the symlink fix.)

## Loop 35 — `_candidate_files` skip symlinks
- OBSERVE: After loop 34's `_read_file` symlink-escape guard, `_candidate_files` still enumerates symlinks. Each iteration that picks one wastes a cursor slot on a guaranteed `skip:<file>(unreadable_or_too_large)` for out-of-repo links, or duplicates work on in-repo aliases.
- ORIENT: Cleanup with measurable iteration efficiency benefit. Higher leverage than rotation/cosmetic candidates.
- DECIDE: Use `lstat()` (not stat) so we see the symlink itself, then skip via `_stat.S_ISLNK`. Hoist `import stat` to module top.
- DEVIL:
  - Correctness: lstat raises on dangling links → caught by existing OSError handler. Tested.
  - Scope: cause-level fix.
  - Priority: P5/P6 — defense-in-depth + perf.
- ACT: switched stat→lstat, added S_ISLNK guard. New `tests/test_candidate_files.py` with 4 cases (outside-symlink, intra-repo-symlink, dangling, empty). 253/253 green.
- COMMIT: pending.

## Loop 36 — `_has_unsafe_path` precise Windows-drive detection
- OBSERVE: While auditing the safety stack against `\ No newline at end of file` diffs (no real bug — added a regression test), discovered `_has_unsafe_path` rule `path[1] == ":"` is over-broad. Any path whose 2nd char is `:` is flagged as a Windows drive, false-positive on legitimate POSIX paths like `dir/note:1.py`.
- ORIENT: Real correctness bug in a security-adjacent function. Higher leverage than cosmetic locks-in.
- DECIDE: Tighten the Windows-drive check: `path[0]` must be an ASCII letter `[A-Za-z]` AND `path[1]` must be `:`. Anything else with `:` at position 1 (digit, punctuation, etc.) is just a weird filename, not a drive.
- DEVIL:
  - Correctness: `C:foo` and `z:foo` still rejected. `1:foo.py`, `dir/note:1.py` now accepted (legal POSIX). `a:b.py` borderline still flagged.
  - Scope: cause-level fix.
  - Priority: precision in the safety stack — false-positives reject legit fixes.
- ACT: tightened condition. Added 3 tests + 1 no-newline lock-in. 257/257 green.
- COMMIT: pending.

## Loop 37 — `QwenClient.chat()` wall-clock budget
- OBSERVE: per-iteration budget bounds outer loop; `chat()` itself can still consume max_retries × (timeout + backoff) before the iteration check fires.
- ORIENT: defense in depth — let the per-call ceiling engage so the iteration budget can advance.
- DECIDE: introduce `_chat_total_budget_seconds()` (env `QWEN_CHAT_BUDGET_S`, default 300s, fallback on bad/<=0). Compute `chat_deadline` once at chat() entry; pre-attempt check raises QwenError with "budget exceeded"; backoff sleep is clamped to remaining budget.
- DEVIL: (1) Default 300s leaves headroom over httpx 120s + backoff; configurable. (2) Cause-level. (3) Higher leverage than further path-validator polish.
- ACT: edits in qwen_client.py; 5 new tests covering helper defaults/override/invalid, aborted retry, and happy path. 262/262.
- COMMIT: pending.

## Loop 38 — `_diff_paths` quoted-path decoding (real safety bypass)
- OBSERVE: `_DIFF_PATH_HEADER_RE` used `a/\S+`/`b/\S+`. A diff with quoted paths (`"a/../etc/passwd"`) — git's standard format for paths with spaces or non-ASCII bytes when `core.quotePath=true` — was completely missed by the regex. `_has_unsafe_path` saw no paths and returned None, letting traversal/absolute-path diffs slip past the safety stack to `git apply`.
- ORIENT: real correctness regression in a security-adjacent function. Confirmed by REPL: a quoted `..` path returned [] paths and unsafe=None.
- DECIDE: extend the regex to accept either unquoted (`a/\S+`) or quoted (`"a/(?:\\.|[^"\\])*"`) forms; add `_unquote_diff_path` helper that strips the C-string quotes, decodes via `codecs.escape_decode`, then strips the `a/`/`b/` prefix; apply the same treatment to rename/copy paths.
- DEVIL:
  - Correctness: `escape_decode` returns bytes; decoded with `errors="surrogateescape"` for byte-fidelity. Quoted forms fall through to literal-body on decode failure so unsafe checks still see a path string. Verified non-ASCII octal `\303\251` → `é`.
  - Scope: cause-level — `_has_unsafe_path`/`_apply_diff` consumers now see real paths, not regex non-matches.
  - Priority: higher than budget-clamp candidate; this gap was a path-traversal bypass.
- ACT: agent/loop.py edits (codecs import + regex + helper + decode loop). 7 new tests covering: quoted traversal in diff-git, in --- /+++, octal escapes, rename-to traversal, quoted with space, unquoted regression, quoted absolute. 269/269.
- COMMIT: pending.

## Loop 39 — clamp budget env values
- OBSERVE: `_iteration_budget_seconds` and `_chat_total_budget_seconds` accepted any positive float. A typo (`6000000` instead of `600`) effectively disables the cap. Defense in depth wants the env override to be bounded.
- ORIENT: low blast radius but cause-level — the budgets exist precisely to bound runaway calls; an unbounded ceiling is a non-ceiling.
- DECIDE: clamp iteration budget to (0, 24h]; clamp chat budget to (0, 1h]. Out-of-range falls to default for non-positive, to max for too-large.
- DEVIL: 24h is generous enough no legitimate run hits it; 1h covers any reasonable single-prompt latency on this hardware. Both env hooks remain configurable below the cap.
- ACT: small constant + clamp in both helpers; 5 new tests. 274/274.
- COMMIT: pending.

## Loop 40 — `_has_unsafe_path` reject NUL / newline in decoded paths
- OBSERVE: After loop 38, `_unquote_diff_path` decodes C-string escapes including `\0`, `\n`, `\r`. None of these belong in a real filename. NUL is especially dangerous: many POSIX path APIs truncate at NUL silently, so a decoded `"a/safe.py\0../etc/passwd"` could behave one way during the safety check and another at filesystem write time.
- ORIENT: cause-level reinforcement of the safety stack — without this, an attacker-shaped diff could embed a NUL after a benign-looking prefix.
- DECIDE: in `_has_unsafe_path`, after empty-check, reject any path containing `\x00`, `\n`, or `\r`. Distinct error tags for log clarity.
- DEVIL: legitimate filenames never contain these; tab is preserved (already split off above). NUL check uses literal `\x00` not regex, so cost is O(len). Order: nul/newline before absolute/traversal so the most diagnostic error fires.
- ACT: 5-line addition; 4 new tests. 278/278.
- COMMIT: pending.

## Loop 41 — `_apply_diff` reject new-file-vs-existing-directory
- OBSERVE: Verified live: a diff with `+++ b/mydir` where `mydir/` exists passes `git apply --check` but fails the actual apply with `unable to write file 'mydir' mode 100644: Directory not empty`. Generic `apply_failed:` is logged — no clean diagnostic, and we already commit to running apply.
- ORIENT: precise diagnostic; cause-level — git's check/apply mismatch is real.
- DECIDE: new helper `_has_dir_path_conflict` runs after `_has_structural_defect`, before `git apply --check`. Iterates `_diff_paths` and rejects any whose `_REPO / path` is an existing real directory (symlinks excluded — handled by mode/symlink checks).
- DEVIL:
  - Correctness: only flags real dirs, not symlinks; OSError swallowed (let git surface it). Source-side overlaps are uncommon and would already be rejected by hunk-content mismatch.
  - Scope: complements existing apply pipeline; doesn't replace `git apply --check`.
  - Priority: low-frequency but recoverable cleanly.
- ACT: helper + pipeline insertion + 3 tests (clash / no-clash / symlink-ignored). 281/281.
- COMMIT: pending.

## Loop 42 — `_validate_changed_files` surface SyntaxWarning
- OBSERVE: `compileall -q` exits 0 even when SyntaxWarning fires (e.g. `is "literal"`, invalid escape `'\d'`). The warning text goes to stderr; we discard it. A model-emitted fix introducing one of these patterns gets committed silently.
- ORIENT: SyntaxWarning is almost always a real bug in coding-loop output; raising it as a validation failure prevents the worst class.
- DECIDE: after the existing returncode!=0 branch, if `"SyntaxWarning"` substring appears in `proc.stderr`, return `py_syntax_warning:<stderr-snippet>`.
- DEVIL:
  - Correctness: stderr substring match is robust — SyntaxWarning text always contains the literal token. False-positives on legitimate `# SyntaxWarning` mention in stderr — unlikely from compileall.
  - Scope: cause-level — failing the validation triggers `_revert_changes` and rejects the diff.
  - Priority: medium-high; covers a class of regressions tests don't always catch (`is "x"` returns truthy in some tests by accident).
- ACT: 6-line addition. 3 new tests, one skipped on Python <3.12 since invalid-escape was DeprecationWarning before then. 283 passed, 1 skipped.
- COMMIT: pending.

## Loop 43 — `_run_git` configurable timeout
- OBSERVE: hard-coded 60s. On a slow remote (rate-limited push) or pre-push hook this aborts mid-iteration with no override path.
- ORIENT: low blast radius but improves operability; also brings git timeout in line with the other env-tunable budgets so the whole loop is configurable from one knob set.
- DECIDE: helper `_git_cmd_timeout_seconds()` reading `QWEN_GIT_CMD_TIMEOUT_S`, default 60, clamp (0, 600]. `_run_git` reads it once per call. Legacy `_GIT_CMD_TIMEOUT_SECONDS` constant retained for any external referer.
- DEVIL:
  - Correctness: int conversion via `int(float(raw))` so `"60.0"` works. NaN clamped via `<= 0` branch.
  - Scope: behaviour-preserving when env unset.
  - Priority: medium; reduces operational toil without weakening any safety guarantee.
- ACT: 6 new tests covering default/override/invalid/non-positive/clamp/at-max. 289 passed, 1 skipped.
- COMMIT: pending.

## Loop 44 — `_GIT_APPLY_TIMEOUT_SECONDS` + `_VALIDATE_TIMEOUT_SECONDS` env-configurable
- OBSERVE: After loop 43 made `_run_git`'s timeout env-tunable, the `_run_git_apply` and `_validate_changed_files` subprocess timeouts remained hard-coded.
- ORIENT: consistency across the timeout knobs reduces operator surprise; introducing one shared helper `_env_timeout_seconds(env_key, default, max_value)` removes the cut-and-paste pattern.
- DECIDE: extract `_env_timeout_seconds`; route all three callers through it. New env vars `QWEN_GIT_APPLY_TIMEOUT_S` and `QWEN_VALIDATE_TIMEOUT_S` (defaults 30, max 600).
- DEVIL:
  - Correctness: legacy module-level `_GIT_APPLY_TIMEOUT_SECONDS` / `_VALIDATE_TIMEOUT_SECONDS` constants kept as legacy aliases — no external referers but harmless to retain.
  - Scope: pure refactor + extension; behaviour unchanged when env unset.
  - Priority: medium; tightens the operability story.
- ACT: 7 new tests across the helpers and shared `_env_timeout_seconds`. 296 passed, 1 skipped.
- COMMIT: pending.

## Loop 45 — `_validate_changed_files` accept `.cfg` / `.ini`
- OBSERVE: validator covers .py / .json / .toml / .yml. setup.cfg, tox.ini, pytest.ini are common in Python projects (this repo has `pyproject.toml` only, but a model fix could touch any .cfg/.ini introduced later).
- ORIENT: small but cause-level: malformed configparser files break tooling at runtime, not at compile.
- DECIDE: add suffix branch using `configparser.RawConfigParser` (no interpolation, so `%` in values is fine).
- DEVIL:
  - Correctness: RawConfigParser still rejects duplicate sections, missing headers — the actual structural bugs.
  - Scope: cause-level. Forward-compat for repo evolution.
  - Priority: medium.
- ACT: 5 new tests covering valid cfg/ini, duplicate-section, missing-header, percent-in-value. 301 passed, 1 skipped.
- COMMIT: pending.

## Loop 46 — JSON duplicate-key detection
- OBSERVE: `json.loads` silently keeps the last value on duplicate keys. A model-emitted fix that accidentally inserts a duplicate `"version"` or `"main"` in `package.json` round-trips green and corrupts config without any signal.
- ORIENT: silent corruption — exactly the priority-1 class. Fixing the validator is cause-level.
- DECIDE: parse with `object_pairs_hook` that raises on duplicates inside any object.
- DEVIL:
  - Correctness: `_no_dup` runs at every nesting level; raises a plain ValueError caught by the existing `except Exception`. Output prefix `json_invalid:` already covers the message.
  - Scope: only affects .json validation — other branches untouched.
  - Priority: high — matches priority bucket 1 (silent data corruption).
- ACT: rewrote .json branch; 3 new tests for top-level dup, nested dup, unique-keys-pass. 304 passed, 1 skipped.
- COMMIT: pending.

## Loop 47 — TOML duplicate-table/key regression lock-in
- OBSERVE: After loop 46 added JSON dup-key detection, the TOML branch was unchanged. Verified tomllib already raises on duplicate sections AND duplicate keys within a section.
- ORIENT: no bug — but locking the property as a regression test prevents a future swap to a permissive parser (e.g. `tomli-w` round-trip lib) from silently weakening validation.
- DECIDE: 2 regression tests only; no production change.
- DEVIL: pure regression coverage; cost ~zero.
- ACT: 306 passed, 1 skipped.
- COMMIT: pending.

## Loop 48 — per-phase timing log
- OBSERVE: No telemetry on where wallclock goes per iteration. Tuning `QWEN_CHAT_BUDGET_S` / `QWEN_LOOP_ITER_BUDGET_S` requires guesswork. With 5+ phases (find_bugs, propose_fix, devils_advocate, apply_diff, validate, commit_push) and unknown vLLM latency, blind-tuning is wrong.
- ORIENT: pure observability. Cause-level: enables data-driven budget tuning.
- DECIDE: `_PhaseTimer` context manager + `_write_timing` JSONL appender to `.loop/timing.log`. Wrap the 3 LLM calls + apply + validate + commit. Timing failures are swallowed.
- DEVIL:
  - Correctness: `__exit__` records elapsed even on exception (verified by test).
  - Scope: zero behavior change to the loop's logic; only adds I/O.
  - Priority: bucket 8 (helps the next person who tunes); but it unblocks data-driven future loops, so net high-leverage.
- ACT: 4 new tests, all phases instrumented, all `return`s replaced with `_finish()` so timing always flushes. 310 passed, 1 skipped.
- COMMIT: pending.

## Loop 49 — `.loop/timing.log` size-bounded rotation
- OBSERVE: Loop 48 introduced an unbounded append-only JSONL log. Continuous run will fill the disk eventually. Bucket 5 (resource leak).
- ORIENT: priority class is high — fixing immediately after introducing the leak prevents a long-running session from exhibiting it.
- DECIDE: single-slot rotation. Cap default 1MB, env `QWEN_TIMING_MAX_BYTES` (clamped (0, 100MB]). On every write, if oversized, rename to `.1` (overwriting any old `.1`) before opening fresh.
- DEVIL:
  - Correctness: rotation runs *before* the open-append, so the new write goes to a fresh file; verified by test.
  - Scope: 100MB cap may still be huge — but it bounds it.
  - Priority: yes, this is bucket 5 and it's caused by my own loop 48; can't be deferred.
- ACT: 10 new tests covering env parsing (default/override/clamp/invalid/nonpositive), undersized noop, oversized rename, overwriting old rotated, missing-file noop, write-triggers-rotation. 320 passed, 1 skipped.
- COMMIT: pending.

## Loop 50 — `.loop/runtime.log` rotation + helper extraction
- OBSERVE: `_log()` writes to runtime.log unbounded; same class as loop 49 but on the older logger. Worse: every iteration writes ≥1 line, indefinitely.
- ORIENT: bucket 5 again. Best fix: extract a generic `_rotate_log_if_oversized(path, max_bytes)` and reuse from both call sites — DRY and consistent semantics.
- DECIDE: extract helper, add `_runtime_log_max_bytes()` (default 5MB, cap 100MB, env `QWEN_RUNTIME_LOG_MAX_BYTES`), call rotation before append in `_log`.
- DEVIL:
  - Correctness: `_log` is called by error paths; if rotation raises, the error swallow keeps the loop alive. The new helper has its own bare `except` so it cannot escape.
  - Scope: refactor of timing rotation to delegate — verified by pre-existing 10 tests still passing.
  - Priority: yes, bucket 5; introduced by the original loop, latent for many iterations now.
- ACT: 7 new tests, 327 passed, 1 skipped. All previous loop-49 timing-rotation tests still green via delegation.
- COMMIT: pending.

## Loop 51 — extract canonical `_env_int_capped`
- OBSERVE: 3 near-duplicate env-int parsers (`_env_timeout_seconds`, `_timing_max_bytes`, `_runtime_log_max_bytes`). Last loop showed the duplication grow; drift will eventually create inconsistent semantics.
- ORIENT: bucket 8 (correctness-fragility). Better to canonicalize *now* before a 4th caller appears.
- DECIDE: rename body to `_env_int_capped(env_key, default, max_value)`, keep `_env_timeout_seconds` as a thin alias for backward compat, route both byte-cap helpers through it.
- DEVIL:
  - Correctness: prior byte-cap helpers used `os.environ.get(key)` (None on unset → default); canonical uses `os.environ.get(key, str(default))` (still parses to default). Equivalent. Verified — 327 pre-existing tests still green.
  - Scope: pure refactor. No behavior change observable from outside the module.
  - Priority: bucket 8 but cheap; combined with the next concrete fix this still helps.
- ACT: 8 new tests for the canonical helper (default/parse/float/invalid/zero/negative/clamp/alias). 335 passed, 1 skipped.
- COMMIT: pending.

## Loop 52 — `.loop/history/*.md` retention cap
- OBSERVE: every iteration may write 1 history file (rejected/applied/apply-failed/out-of-scope/syntax-failed). Continuous run grows inodes unbounded; bucket 5 (resource leak).
- ORIENT: same class as loops 49 + 50 but for an inode-count limit not a byte size. File-count ceiling fits the directory shape.
- DECIDE: `_history_max_files()` (default 500, cap 100k, env `QWEN_HISTORY_MAX_FILES`) + `_prune_history(max)` deletes oldest by mtime; called at the end of `_write_history`.
- DEVIL:
  - Correctness: prune ignores subdirectories (verified by test). Failure paths swallow exceptions. Pruning runs *after* the write so the new file is always persisted before any cleanup.
  - Scope: only deletes regular files in HISTORY_DIR; nothing else touched.
  - Priority: yes, bucket 5; postponing means hitting an fs-inode wall on long runs.
- ACT: 9 new tests (default/override/clamp/invalid; prune noop/oldest/missing-dir/triggered-from-write/skips-subdirs). Added missing `import os` to test file. 344 passed, 1 skipped.
- COMMIT: pending.

## Loop 53 — `_apply_diff` error category contract
- OBSERVE: Errors are already shaped `"category: detail"` but no constant or extractor exists; future log-aggregator code would have to copy a hard-coded list.
- ORIENT: bucket 8 — fragile-for-the-next-developer. Cheap to formalize.
- DECIDE: introduce `APPLY_ERROR_CATEGORIES` (frozenset), `APPLY_OK_CATEGORY`, and `_apply_error_category(msg)` helper; add contract tests that real `_apply_diff` outputs return categories *in* the set.
- DEVIL:
  - Correctness: helper is a 1-line split. Existing call sites (`apply_failed:{rel}:{msg[:80]}`) unchanged — they slice the *outer* prefix. No behavior change.
  - Scope: pure documentation+helper. Doesn't touch any control flow.
  - Priority: low-impact alone, but it locks the contract so future loops can rely on it.
- ACT: 6 new tests; 350 passed, 1 skipped.
- COMMIT: pending.

## Loop 54 — `STATE_ARCHIVE_DIR` retention cap
- OBSERVE: `_rotate_state_if_needed` already moves oversized STATE.md into the archive dir, but the archive itself was unbounded — every rotation adds a file forever. Bucket 5.
- ORIENT: same shape as loop 52's history retention. Reuse the same pattern.
- DECIDE: `_state_archive_max_files()` (default 50, cap 10k, env `QWEN_STATE_ARCHIVE_MAX_FILES`) + `_prune_state_archive` called from inside `_rotate_state_if_needed` *after* the new archive is in place.
- DEVIL:
  - Correctness: prune runs after `os.replace`, so the new archive is visible; the cap=2 e2e test confirms the freshly-rotated archive is among the survivors.
  - Scope: only deletes regular files in STATE_ARCHIVE_DIR (subdirs preserved, verified).
  - Priority: bucket 5; combined with loop 52 closes the inode-leak class for `.loop/`.
- ACT: 8 new tests including end-to-end rotation+prune. Required patching `_REPO` in the e2e test for `archive.relative_to(_REPO)` to work. 358 passed, 1 skipped.
- COMMIT: pending.

## Loop 55 — DRY `_prune_dir_oldest`
- OBSERVE: `_prune_history` (loop 52) and `_prune_state_archive` (loop 54) are byte-identical except for the directory and log message. Future drift risk — bucket 8.
- ORIENT: cheap canonicalize before a 3rd call site appears (e.g. eventual `.loop/timing` archive folder).
- DECIDE: extract `_prune_dir_oldest(directory, max_files)`; both prior helpers delegate.
- DEVIL:
  - Correctness: pure refactor; all 358 prior tests stay green.
  - Scope: zero behavior change.
  - Priority: bucket 8 but cheap.
- ACT: 3 new tests on the canonical helper. 361 passed, 1 skipped.
- COMMIT: pending.

## Loop 56 — `_revert_changes` rc check + reset fallback
- OBSERVE: `_revert_changes` was fire-and-forget. If checkout/clean failed (lock contention, EBUSY, weird mode bits), the next iteration could see a *dirty tree* and apply on top — silently committing stale + new changes together. Bucket 4 (logic bug → silent corruption).
- ORIENT: Highest-impact fix in current candidates. Cause is the missing rc inspection.
- DECIDE: inspect both subprocess rcs, log on failure, attempt `git reset --hard HEAD` as fallback when either failed; return bool to allow callers to react if needed (currently advisory).
- DEVIL:
  - Correctness: reset --hard is safe — it discards both staged and unstaged changes; same intent as the (failed) checkout+clean. Test verifies recovery path.
  - Scope: kept signature compatible; existing callers ignore the return — no behavior change for green path. Verified — 361 pre-existing tests still pass.
  - Priority: this is bucket 4 — exactly the priority-1 class. Right call.
- ACT: 4 new tests covering success / partial-failure-recovery / total-failure / no-reset-on-success. 365 passed, 1 skipped.
- COMMIT: pending.

## Loop 57 — propagate `_revert_changes` failure + dirty-tree pre-check
- OBSERVE: Loop 56 added the rc check & return value, but no caller used it. A failed revert silently let the *next* iteration read a dirty-tree file, possibly committing stale + new content together. Bucket 4.
- ORIENT: Two angles fix this together: (a) callers must surface revert failure as a distinct outcome; (b) the next iteration must not trust the working tree. Both, not either.
- DECIDE:
  - Wire `rev_ok` from `_revert_changes()` into all 3 callers (out-of-scope, validation, commit-push fail). On failure, return `revert_failed:{rel}:after_<phase>`.
  - Insert `_abort_rebase_if_any()` at the *start* of `_iteration` so even if a revert truly silently failed, the tree is reset before the next read.
- DEVIL:
  - Correctness: `_abort_rebase_if_any` does a hard-reset only when tree is dirty (`status --porcelain`). On a clean tree, it's effectively a status check. Not a no-op but cheap.
  - Scope: now we hard-reset at start of every iteration, which discards any user-introduced manual change to the tree. But the loop is the sole writer in production; manual edits would already conflict with `_commit_and_push`. Acceptable.
  - Priority: bucket 4. Right call.
- ACT: 1 contract test (string-presence in source). 366 passed, 1 skipped.
- COMMIT: pending.

## Loop 58 — `QWEN_STATE_MAX_BYTES` env override
- OBSERVE: `STATE_MAX_BYTES` was the only byte-cap not env-tunable.
- ORIENT: Pure consistency / operability win. Bucket 9-ish, but very low risk.
- DECIDE: Add `_state_max_bytes()` reading env; preserve monkeypatch on the legacy constant for existing tests.
- DEVIL:
  - Correctness: existing tests monkeypatch `STATE_MAX_BYTES` directly. Solution: env wins, otherwise use the (potentially monkeypatched) constant. Verified.
  - Scope: not a bandaid — every other cap follows this pattern.
  - Priority: low, but next.md #1 was this; doing it clears the queue.
- ACT: `_state_max_bytes` + 8 tests (env, default, invalid, zero/neg, cap, monkeypatch interop, env-vs-constant precedence). 374 passed.
- COMMIT: pending.

## Loop 59 — tag `apply_failed` outcome with structured category
- OBSERVE: Loop 53 introduced `APPLY_ERROR_CATEGORIES` and `_apply_error_category`, but the iteration's `apply_failed` outcome string was still the freeform truncated msg. Monitoring/grepping `runtime.log` couldn't filter by category.
- ORIENT: Bucket 6 — outcome strings are an interface contract. Inconsistency between the structured frozenset and the actual emitted outcome.
- DECIDE: Use `_apply_error_category(msg)` to embed a stable category tag in the outcome and the STATE.md row. Format: `apply_failed:{category}:{rel}:{msg[:60]}`.
- DEVIL:
  - Correctness: writing my own roundtrip test exposed a sample-message mismatch (a real "obvious-once-tested" find — initial test inputs used spaces; real `_apply_diff` returns are snake_case-prefixed). Test sample fixed to match the actual contract; round-trip now passes for all 9 categories.
  - Scope: addresses cause (interface inconsistency), not symptom.
  - Priority: bucket 6 over the remaining bucket-7+ candidates.
- ACT: outcome + STATE.md row tag added. 3 contract tests including 9-category round-trip. 377 passed.
- COMMIT: pending.

## Loop 60 — `OUTER_OUTCOME_CATEGORIES` taxonomy + drift audit
- OBSERVE: We had `APPLY_ERROR_CATEGORIES` for inner apply errors but no equivalent for the 15 outer-loop outcome categories. Drift is silent.
- ORIENT: Bucket 6 — outcome tokens are the loop's external API for monitoring. Without a frozenset + audit, future outcome changes won't be caught.
- DECIDE: Add `OUTER_OUTCOME_CATEGORIES` frozenset + `_outer_outcome_category()` helper, plus two source-level audit tests: (a) every `_finish(f"X..."` token is in the frozenset, (b) every frozenset entry appears in source. Bidirectional contract.
- DEVIL:
  - Correctness: regex-based source audit could miss `_finish` calls that aren't on the same line as `return` or that use computed strings. Mitigated: every existing `_finish` call in `_iteration` matches the simple `return _finish(f"..."` pattern (verified by passing test).
  - Scope: this is the cause-level fix; without the taxonomy, future drift goes silent.
  - Priority: bucket 6 — same level as loop 59, complementary.
- ACT: 6 tests including bidirectional source audit. 383 passed.
- COMMIT: pending.

## Loop 61 — timing.log category field
- OBSERVE: `_write_timing` JSONL emitted `outcome` but not the structured category. Aggregating timing-by-category required reparsing the outcome string.
- ORIENT: Bucket 5/6 — observability cliff. Single-line fix unlocks fast aggregation.
- DECIDE: Add `category` field via `_outer_outcome_category(outcome)`. Keep `outcome` verbatim for forensics.
- DEVIL:
  - Correctness: `_outer_outcome_category` returns the literal leading token even if it's not in the frozenset — passes drift through rather than masking it. Verified by test.
  - Scope: not a bandaid; this is the missing observability primitive.
  - Priority: bucket 5 over the remaining bucket 6/7 work.
- ACT: 3 tests covering known/no-colon/unknown outcomes. 386 passed.
- COMMIT: pending.

## Loop 62 — log empty-staged-tree path in `_commit_and_push`
- OBSERVE: `_commit_and_push` returned False silently when `git add -A && git status --porcelain` produced an empty tree. Caller (`_iteration`) sees only `commit_failed:{rel}` — indistinguishable from a real git error.
- ORIENT: This *is* anomalous: caller only invokes us after `_apply_diff` succeeded and `_changed_paths()` was non-empty. Empty staged tree means changes were lost between apply and commit (external reset, all-gitignored changes, etc.) — exactly the kind of silent corruption the operating-law prioritizes catching.
- DECIDE: Add an explicit `_log` line before returning False from the empty-tree branch.
- DEVIL:
  - Correctness: log-only change, no behavior change for callers. Test verifies log present + no spurious commit attempt.
  - Scope: this is forensics, not the root cause; but making the cause discoverable IS the work — without a log, the root cause is invisible.
  - Priority: bucket 1/4 — silent data-flow corruption observability.
- ACT: 1 test (stubbed `_run_git` + log capture). 387 passed.
- COMMIT: pending.

## Loop 63 — `_commit_and_push` tri-state return + `commit_skipped_empty` outcome
- OBSERVE: After loop 62 logged the empty-tree path, the return value was still `False` — same as real failure. Outer loop emitted identical `commit_failed:{rel}` for both, indistinguishable in `runtime.log`/timing.log.
- ORIENT: Bucket 6 — interface contract lies about distinct conditions. Two callers (the iteration + the rebase-conflict e2e test) need updating; the change is mechanical.
- DECIDE: Switch return type to `Literal["ok", "empty", "failed"]`. Update the iteration to map `"empty"` to a new outcome `commit_skipped_empty:{rel}`. Add to `OUTER_OUTCOME_CATEGORIES`. Update both existing tests.
- DEVIL:
  - Correctness: drift audit (loop 60) immediately catches if I forgot to add to the frozenset — it failed once, then passed after the addition.
  - Scope: this *is* the cause-level fix to loop 62's symptom-only log.
  - Priority: bucket 6, complementary to loop 62.
- ACT: 4 new tests (add-fail, commit-fail, ok, source-audit). Updated 2 existing assertions. 391 passed.
- COMMIT: pending.

## Loop 64 — runtime.log category prefix
- OBSERVE: `runtime.log`'s `iteration -> {outcome}` lines lacked the structured category. timing.log got it in loop 61; mirror it in runtime.log so `grep '\[applied\]' runtime.log` works.
- ORIENT: Bucket 5/6 — observability symmetry between the two log surfaces.
- DECIDE: Wrap category in brackets: `iteration [{category}] -> {outcome}`. Bracket form is unambiguous against outcome strings (which never contain `[`).
- DEVIL:
  - Correctness: `_outer_outcome_category` is pure; can't crash. `_log` already swallows on its own write path.
  - Scope: this is the symmetric fix to loop 61, not a bandaid.
  - Priority: bucket 5/6, parallel to loop 61.
- ACT: 2 tests (source-format + format-for-known-categories). 393 passed.
- COMMIT: pending.

## Loop 65 — drift-audit AST visitor
- OBSERVE: `test_every_finish_call_in_source_uses_known_category` used a single-line regex `return\s+_finish\(\s*f?"([a-z_]+)[":]`. A multi-line `_finish(\n    f"..."` call would silently slip through — false-negative drift detection.
- ORIENT: This is bucket 8 (fragile assumption that future maintainers will trip over). The audit's job is to catch drift; if it has a hole, drift will hit it.
- DECIDE: Convert to AST visitor. Walk the parse tree, find every `Call` to `_finish`, extract the leading token from `Constant` or `JoinedStr` first arg. Fail loudly on unrecognised call shapes.
- DEVIL:
  - Correctness: Verified the AST visitor sees a synthetic multi-line `_finish()` call that the old regex would miss (token list includes `multiline_outcome` and `BOGUS_CATEGORY`).
  - Scope: this is the cause-level fix to the audit's blind spot.
  - Priority: bucket 8 — but the audit is the safety rail for bucket 6 work in loops 53/60/63. Worth hardening.
- ACT: Replaced regex with AST walker; added recognised-shape guard so a future syntax change (e.g., kwarg, *args) fails the audit instead of silently passing. 393 passed; verified false-negative case manually.
- COMMIT: pending.

## Loop 66 — `APPLY_ERROR_CATEGORIES` AST drift audit
- OBSERVE: Loop 53 added `APPLY_ERROR_CATEGORIES`; loop 60 added the AST drift audit for outer outcomes; the symmetric audit for `_apply_diff`'s error returns was missing.
- ORIENT: Bucket 6/8 — same drift surface. If a future fix adds a new error message in `_apply_diff` without updating the frozenset, both `_apply_error_category` and the `apply_failed:{category}:` outcome (loop 59) silently emit a non-canonical token.
- DECIDE: AST visitor that locates `_apply_diff`'s FunctionDef and walks every `return False, "<msg>"` / `return False, f"<msg>..."`. Bidirectional audit: tokens-in-source ⊆ frozenset, frozenset ⊆ tokens-in-source.
- DEVIL:
  - Correctness: handles both `Constant` and `JoinedStr` (f-string) message shapes; raises on unrecognised shapes (kwarg, computed string, etc.) so future syntax changes can't silently bypass the check.
  - Scope: this is the symmetric guard rail to loop 65, not a bandaid.
  - Priority: bucket 6/8 — close the second drift surface before moving on.
- ACT: 2 tests in `TestApplyDiffErrorCategoryDriftAudit`. 395 passed.
- COMMIT: pending.
