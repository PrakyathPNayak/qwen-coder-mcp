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

## Loop 67 — `_log` broadens swallow to all `Exception`
- OBSERVE: `_log` only caught `OSError` on the file path, and `print` was unguarded. A `UnicodeEncodeError` from print, or a `RuntimeError` from the write path, would propagate — killing the iteration via the outer `Exception` catch in `main`. Logging is observability, not correctness.
- ORIENT: Bucket 5 (resource cliff) — under filesystem pressure or weird msg payloads, the loop becomes its own foot-gun.
- DECIDE: Wrap `print` with a separate `try/except Exception`, broaden file-write swallow from `OSError` to `Exception`. Still suppresses inside `_rotate_log_if_oversized` separately.
- DEVIL:
  - Correctness: broad `except` is generally bad; here it's the right call because `_log`'s *only contract* is "never break the caller". Test fixture verifies `RuntimeError` from a fake handle is now swallowed.
  - Scope: this addresses the cause (narrow except), not just one symptom. Symmetric with `_write_timing` and `_rotate_log_if_oversized` which already use broad except.
  - Priority: bucket 5 — observability stability is loop-survival.
- ACT: 3 tests covering OSError on open, UnicodeError on print, RuntimeError on write. 398 passed.
- COMMIT: pending.

## Loop 68 — `_append_state` & `_write_history` exception swallow
- OBSERVE: After loop 67 hardened `_log`, the audit (next.md #5) called out two more observability sinks with NO exception handling: `_append_state` (writes STATE.md mid-iteration) and `_write_history` (writes `.loop/history/*.md`). Disk full / permission denied would propagate into `_iteration` and only get caught by `main`'s outer `except Exception`, which loses any state already accumulated for that loop.
- ORIENT: Bucket 5 — symmetric fix to loop 67. Observability state-persistence failures should not destroy an iteration's outcome reporting.
- DECIDE: Wrap both bodies in `try/except Exception: _log(...)`. `_write_history` return type narrowed to `Path | None`.
- DEVIL:
  - Correctness: callers don't use `_write_history`'s return value (verified by grep). Narrowing to `Optional` is contract-safe.
  - Scope: cause-level — same audit covers the symmetric site.
  - Priority: bucket 5, parallel to loop 67.
- ACT: 5 tests covering OSError, RuntimeError, success, prune-failure-after-write. 403 passed.
- COMMIT: pending.

## Loop 69 — `_write_timing` rate-limited swallow-log
- OBSERVE: After loops 67–68 hardened `_log`, `_append_state`, `_write_history`, the remaining swallow-log site `_write_timing` would emit one log line per iteration on persistent failure (e.g., disk full). On a 24h run with 1k loops that's 1k spam lines.
- ORIENT: Bucket 5 — observability cliff. We want the FIRST failure visible (so the operator notices) and Nth subsequent failures (so the count is observable) but not 1k duplicates.
- DECIDE: Module-level `_TIMING_FAILURE_COUNT` + `_TIMING_FAILURE_LOG_EVERY=100`. Log when count==1 or count%N==0. Includes count in message.
- DEVIL:
  - Correctness: global counter; loop is single-threaded so no race. ✓
  - Scope: addresses cause (log spam) not symptom (rotate runtime.log).
  - Priority: bucket 5, was next.md #1.
- ACT: 4 tests (first-failure, rate-limit, every-Nth-with-different-N, success-doesn't-increment). 407 passed.
- COMMIT: pending.

## Loop 70 — generalise rate-limited swallow logger
- OBSERVE: After loop 69, `_write_timing` had a rate-limited swallow-log but `_append_state` and `_write_history` (added in loop 68) still logged every failure. Same problem class, two more spam sources.
- ORIENT: Cause-level fix: extract a helper. The honest design is one class with three module-level instances.
- DECIDE: `_RateLimitedSwallowLogger(label, every=100)` with `report(exc)` and `reset()`. Three instances: `_TIMING_SWALLOW_LOG`, `_STATE_SWALLOW_LOG`, `_HISTORY_SWALLOW_LOG`. Refactor `_write_timing` to use the helper too, retiring the loop-69 module globals.
- DEVIL:
  - Correctness: refactor changes the "every" knob from a module global to an instance attribute. Loop-69 tests that monkeypatched `_TIMING_FAILURE_LOG_EVERY` need migration. ✓ migrated.
  - Scope: addresses cause (no shared helper). _log itself can't use it (chicken/egg).
  - Priority: bucket 5, was next.md #1.
- ACT: New helper class, applied at 3 sites; loop-69 tests migrated; 5 new tests for the helper + cross-site usage. 412 passed.
- COMMIT: pending.

## Loop 71 — exponential schedule for swallow loggers
- OBSERVE: Loop 70's `_RateLimitedSwallowLogger` defaulted to linear every=100 — counts 2..99 silent. Operator can't see "still failing" on a short fault that resolves before count 100. The cliff: a 50-iteration sustained fault would only show count=1.
- ORIENT: Cause: linear schedule. Better: exponential 1, 2, 4, 8, …, every; then linear past every. Surfaces persistent faults fast in early iterations while still reporting infrequent late faults.
- DECIDE: Add `schedule: str = "linear"` param. Mode "exponential" logs powers of two ≤ every, then every-N past that. Class default stays "linear" for explicit-construction callers; the 3 module-level instances opt into exponential.
- DEVIL:
  - Correctness: loop-69 tests broke because they assumed linear. Migrated; some assert exponential cadence directly.
  - Scope: addresses cause (early-warning latency).
  - Priority: bucket 5; was next.md #5.
- ACT: Helper extended; 3 module instances switched; 2 new tests for exponential mode (powers-of-2 and post-every linear fallback). 414 passed.
- COMMIT: pending.

## Loop 72 — cache `_now()` as `iter_ts` in `_iteration`
- OBSERVE: `_iteration` called `_now()` 7 times across state.md/history.md narrative lines. On a slow loop spanning a clock-second boundary (qwen 27B inference is slow), records emitted late could carry a different `ts` than records emitted early — making logs falsely look like multiple iterations.
- ORIENT: Cause: stale per-call timestamping. The narrative records describe ONE iteration's narrative; they should share one timestamp.
- DECIDE: Capture `iter_ts = _now()` at top of `_iteration` after `_abort_rebase_if_any()`. Replace all 7 `_now()` calls inside the function body with `iter_ts`. Keep `_write_timing`'s own `_now()` (that field is "when this record was written", not "when this iteration began"). Keep `_log`'s `_now()` (real-time runtime.log).
- DEVIL:
  - Correctness: callers that grep state.md for "iteration start" already implicitly assume one ts per iteration; this enforces it. No regression vector.
  - Scope: addresses cause (per-call drift) not symptom (deduping).
  - Priority: bucket 5; was next.md #3.
- ACT: 7 sites edited; 2 new tests using a sequenced fake `_now` to verify only the FIRST timestamp ever appears in state.md across rejected and apply_failed branches. 416 passed.
- COMMIT: pending.

## Loop 73 — `_revert_changes` final fallback to `origin/main`
- OBSERVE: `_revert_changes` cascade was checkout → clean → reset HEAD. If HEAD itself is broken (corrupted ref, missing object) the reset fallback fails and the iteration limps on with a dirty tree. The next iteration's `_abort_rebase_if_any` won't recover from a broken HEAD either.
- ORIENT: Cause: no recovery for broken-HEAD case. The loop is the SOLE writer to origin/main, so origin/main = ground truth. Reset to it = exactly the recovery semantic we want.
- DECIDE: Add `git reset --hard origin/main` as final fallback after `git reset --hard HEAD` fails.
- DEVIL:
  - Correctness: `origin/main` discards anything between local-HEAD and origin/main. But the only thing local-HEAD could have past origin/main is the iteration's failed commit/push attempt — exactly what `_revert_changes` exists to discard. ✓
  - Scope: addresses cause (broken HEAD).
  - Priority: bucket 5; was next.md #1.
  - Edge: if origin/main resolution fails (no remote, no fetch), we log and return False — same outcome as before, no regression.
- ACT: Function extended; 3 new tests (HEAD-broken recovers via origin/main; both fallbacks fail; origin/main NOT attempted when HEAD reset succeeds). 419 passed.
- COMMIT: pending.

## Loop 74 — `_RateLimitedSwallowLogger.summary()`
- OBSERVE: After loops 70-71 added rate-limited logging, the suppression count was internal only. Operator wanting to know "is _write_timing currently failing 50 times in a row?" would have to grep runtime.log for the most-recent count= line. Programmatic query missing.
- ORIENT: Cause: no public introspection method. Bucket 5 observability completion.
- DECIDE: Add `last_logged_count` field; `report()` updates it on emit; `summary()` returns dict with label, count, last_logged_count, suppressed=count-last_logged, schedule, every. `reset()` clears both counters.
- DEVIL:
  - Correctness: existing report/reset logic unchanged for callers; only adds tracking. ✓
  - Scope: addresses programmatic-query gap.
  - Priority: bucket 5; was next.md #3.
- ACT: 6 tests (zero-state, one-logged, all-suppressed, every-n-advance, reset clears both, exponential summary). 425 passed.
- COMMIT: pending.

## Loop 75 — periodic swallow-summary at iteration boundaries
- OBSERVE: After loop 74 added `summary()`, the suppression state was queryable but never auto-reported. A sink could be silently failing 50× per iteration with the rate limiter logging once at count=1 and again at count=100, blind to the operator in between.
- ORIENT: Cause: no automatic surfacing. Hook into `_finish` so every iteration reports any logger with growing `count` since last summary.
- DECIDE: Add `_log_swallow_summaries()` + `_LAST_SWALLOW_SUMMARY_COUNTS` dict tracking. Only emit when `count > last`. `_finish` calls it.
- DEVIL:
  - Correctness: on a fault that *stopped* (count not growing), `count <= last` short-circuits, no spam. ✓
  - Scope: addresses cause (no automatic surfacing) without re-spamming.
  - Priority: bucket 5; was next.md #5.
  - Edge: counters persist across iterations, so manually resetting individual loggers (e.g. tests) requires also clearing `_LAST_SWALLOW_SUMMARY_COUNTS`. Documented in test setup.
- ACT: Helper added, `_finish` updated, 6 new tests (no-failure, suppressed-grows, count-unchanged, re-emits-after-more, never-raises, end-to-end finish-calls-summary). 431 passed.
- COMMIT: pending.

## Loop 76 — rate-limit `_prune_dir_oldest` and `_save_cursor` log paths
- OBSERVE: `_log` audit (per next.md #3) found two callers vulnerable to per-iteration spam: `_prune_dir_oldest` (runs every iteration; a perm-denied state archive prune would log every loop) and `_save_cursor` (runs each iteration). Both used raw `_log` with no rate limiting.
- ORIENT: Same bug shape as loops 68-71 covered. Apply same fix: route through `_RateLimitedSwallowLogger`.
- DECIDE: Add `_PRUNE_SWALLOW_LOG` and `_CURSOR_SWALLOW_LOG` (both exponential). Extend `report()` with optional `context` arg so the directory path / idx is preserved in the emitted line. Register both in `_swallow_loggers()` so they get periodic summaries.
- DEVIL:
  - Correctness: was the `directory` / `idx` info important to ops? Yes — directory tells you which path is failing. Solved by `context` arg.
  - Scope: This is the same root cause class (unbounded log emission from per-iter sinks). Are there others? `_run_git timeout` is conditional/rare; commit/push failures are also iteration-bounded but already gated by branch logic. The two added here are the highest-frequency repeat offenders.
  - Priority: bucket 5/6; was next.md #3.
- ACT: 5 new tests (prune linear cadence + path context, cursor linear cadence + idx context, swallow_loggers registration, context format, no-context legacy format). Migrated 1 existing cursor test to new label. 436 passed.
- COMMIT: pending.

## Loop 77 — rate-limit `_commit_and_push` git fault paths
- OBSERVE: Continuing the `_log` audit from loop 76. `_commit_and_push` had four spam-vulnerable paths: `git add failed`, `git commit failed`, `git pull --rebase failed`, `git push failed`. A network outage or stale credential would log every iteration where push=True.
- ORIENT: Two distinct fault classes: remote (pull/push, network) and local (add/commit, repo state). Two loggers, exponential schedule.
- DECIDE: Wrap each `_log` call with `_GIT_REMOTE_SWALLOW_LOG.report(RuntimeError(stderr), context=...)` or `_GIT_LOCAL_SWALLOW_LOG`. Register both in `_swallow_loggers()`.
- DEVIL:
  - Correctness: stderr previously appeared bare; now it's wrapped as a RuntimeError. The `_log` call still emits the stderr text via the exception's str. ✓
  - Scope: leaves the "empty staged tree" anomaly path alone — that's an indicator of a different bug, not iteration spam, and it's already rare-by-construction.
  - Priority: bucket 5; was next.md #5.
  - Edge: tests for stderr text format ("commit-err", "pull-err") still pass — exception str is the bare stderr.
- ACT: 5 new tests covering push, pull, add, commit rate limits + logger registration. 441 passed.
- COMMIT: pending.

## Loop 78 — rate-limit `_revert_changes` failure paths
- OBSERVE: Final piece of the `_log` audit. `_revert_changes` has 4 failure-path `_log` calls (checkout/clean/reset HEAD/reset origin/main). On a persistently corrupt repo, all 4 fire each iteration.
- ORIENT: Same fix shape. Add `_REVERT_SWALLOW_LOG` exponential. Keep the SUCCESS "recovered via reset --hard..." info logs as bare `_log` so successful recoveries stay maximally visible to operators.
- DECIDE: Convert all 4 failure paths; preserve success info logs.
- DEVIL:
  - Correctness: success info logs intentionally not rate-limited so a single recovery still announces itself. ✓
  - Scope: addresses the spam class. Rate-limiter context strings preserve rc and stderr snippet. ✓
  - Priority: bucket 5/6; was next.md #7.
  - Edge: existing loop-73 test asserted exact "reset --hard fallback FAILED" text which no longer exists. Updated assertions to substring "reset --hard HEAD" / "reset --hard origin/main" + forced linear cadence in the test so all 4 failures still log there.
- ACT: 3 new tests (registration, repeated-failures rate-limited, success recovery info still logs). Migrated 1 loop-73 test. 444 passed.
- COMMIT: pending.

## Loop 79 — document recovery contract + swallow-logger sinks at module level
- OBSERVE: After 11 loops of cumulative observability work, the contract is undocumented at the module level. Future contributors (and future-self) need to know the "never wedged" guarantee and which sinks are rate-limited.
- ORIENT: Promote tribal-knowledge from loop_log into module docstring, and add a drift audit so any new logger label registered in `_swallow_loggers()` must also appear in the docstring.
- DECIDE: Add a "Recovery contract" section explaining `_abort_rebase_if_any` + `_revert_changes` cascade and an "Observability swallow loggers" section listing every wrapped sink.
- DEVIL:
  - Correctness: docstring is non-load-bearing, can drift from code. Solution: `test_docstring_lists_swallow_logger_sinks` iterates over `_swallow_loggers()` and asserts every label is in the docstring; CI fails if a new logger is added without docstring update.
  - Scope: doesn't fix a runtime bug — but addresses the "code that is correct but will break the next person who touches it" priority bucket from the operating doctrine. This *is* high-leverage when 7 distinct loggers all share the same shape; without docstring, the pattern looks ad-hoc.
  - Priority: bucket 8.
- ACT: 2 new tests (cascade mention + label drift). 446 passed.
- COMMIT: pending.

## Loop 80 — rate-limit `_run_git` timeout path
- OBSERVE: Last spam-vulnerable `_log` call (`_run_git timeout: ...`). On a hung git binary or unreachable remote, this fires per `_run_git` call — many per iteration.
- ORIENT: Same fix shape. Add `_GIT_TIMEOUT_SWALLOW_LOG` exponential. Note: timeout returns rc=124 + synthetic stderr "timed_out_after_Ns" which then flows up to `_GIT_REMOTE_SWALLOW_LOG` for push/pull paths — these are complementary (different layer, different context), not duplicate.
- DECIDE: Wrap the `_log` in the timeout branch only (check=True keeps raising as documented). Register in `_swallow_loggers()`. Update docstring drift label.
- DEVIL:
  - Correctness: check=True path untouched, still raises. Synthetic CompletedProcess unchanged. ✓
  - Scope: rate-limiter context preserves the git args so operators can see *which* command is hanging. ✓
  - Priority: bucket 5/6; was next.md #3.
- ACT: 3 new tests (registration, repeated-timeouts cadence + context, check=True still raises). 449 passed.
- COMMIT: pending.

## Loop 81 — `_RateLimitedSwallowLogger.report` returns bool
- OBSERVE: API enabling change. Loop 80 left no remaining spam-vulnerable `_log` callers (audit complete). Now move to API quality so future SIGUSR1 dump (next.md #5) can be built cleanly.
- ORIENT: Adding a return value is purely additive; existing call sites discard returns.
- DECIDE: `report()` returns True iff it emitted a log line this call. Documented in docstring.
- DEVIL:
  - Correctness: type signature change (`None` → `bool`). Tests don't currently bind on return value, no breakage. ✓
  - Scope: enabling change for SIGUSR1 dump and any "extra context only on log iterations" caller. Not over-engineering — same level as `summary()` from loop 74.
  - Priority: bucket 8/9; was next.md #4.
- ACT: 5 new tests (first=T, suppressed=F, periodic, exponential cadence vector, demo callsite). 454 passed.
- COMMIT: pending.

## Loop 82 — `main()` periodic aggregate swallow-summary
- OBSERVE: `_log_swallow_summaries` (loop 75) only fires when count *grew* this iteration. A long run where suppression has plateaued (e.g., 1000 iters into a stable 1-failure-per-iter fault — exponential schedule has stopped logging) loses visibility.
- ORIENT: Need a complementary cumulative snapshot at iteration boundaries.
- DECIDE: Add `_log_aggregate_swallow_summary(iter)` emitting one line `aggregate-swallow-summary iter=N label1=count1 label2=count2 ...` whenever any count > 0. Cadence env-tunable via `QWEN_AGGREGATE_SUMMARY_EVERY`, default 100, clamped to (0, 100000].
- DEVIL:
  - Correctness: only emits when ANY count > 0 — healthy runs stay silent. ✓
  - Scope: per-iter delta channel (loop 75) + cumulative-every-N (this loop) = full visibility. The two are complementary, not redundant.
  - Edge: a logger whose `summary()` raises gets silently skipped (audited in test). The aggregate helper itself is double-wrapped in try/except so it can't break the loop.
  - Edge: clamping uses `_env_int_capped`, identical to other env vars.
- ACT: 4 new tests (silent on zero, emits-with-counts, broken-logger-survives, env clamp). 458 passed.
- COMMIT: pending.

## Loop 83 — total iteration wallclock in timing.log via `wall_s`
- OBSERVE: timing.log records per-phase wallclock but doesn't capture total iteration time. `sum(phases.values())` excludes scaffolding (between-phase work, error paths). Long iterations from slow scaffolding aren't visible.
- ORIENT: Capture `iter_monotonic = time.monotonic()` at top of `_iteration`, pass through `_finish` → `_write_timing`, emit as `wall_s` field.
- DECIDE: Optional kwarg `iter_monotonic` on `_write_timing` keeps backward-compat (direct test callers without the arg still work). When provided, record gets `wall_s = round(monotonic() - iter_monotonic, 4)`.
- DEVIL:
  - Correctness: monotonic clock guaranteed by stdlib. ✓
  - Scope: deadline still uses its own `time.monotonic()` call (semantics: "from this point forward"); intentionally not collapsed with iter_monotonic to keep budget arithmetic local.
  - Edge: existing budget test patched `time.monotonic` with a 1-element chain; my new `iter_monotonic = time.monotonic()` consumed that tick. Migrated the test to `[1000.0, 1000.0]` so deadline still gets a low base.
  - Edge: `wall_s` rounded to 4 decimal places, matching phase-time precision.
- ACT: 3 new tests (write_timing-includes-wall_s, omits-without-arg, end-to-end). Migrated 1 budget test. 461 passed.
- COMMIT: pending.

## Loop 84 — `_RateLimitedSwallowLogger.last_log_message`
- OBSERVE: `summary()` exposes counts but no preview of the actual error text. Operators dumping logger state from a debugger or future SIGUSR1 handler still need to grep loop.log.
- ORIENT: Cache the last emitted line on the instance. Only update on actual emit (not suppressed reports), so it serves as a faithful "last surfaced" snapshot.
- DECIDE: Add `last_log_message: str | None` attribute, initialized None, set on emit, cleared on reset, included in summary().
- DEVIL:
  - Correctness: Suppressed reports must NOT overwrite — verified by test.
  - Privacy: messages contain str(exc); no PII expected since we wrap stdlib OSError/CalledProcessError. Acceptable.
  - Memory: O(1) per logger, 9 loggers → trivial.
- ACT: 7 new tests. 468 passed.
- COMMIT: pending.

## Loop 85 — _dump_logger_state SIGUSR1 handler
- OBSERVE: last_log_message is now stored, but no operator-facing path to dump it. Long-running daemon needs runtime introspection without restart.
- ORIENT: Add _dump_logger_state(reason) that walks _swallow_loggers() and emits each summary on its own line. Wire SIGUSR1 to _dump_logger_state(reason=sigusr1) in main(). Per-line emit because grep-friendly; Windows-safe since signal.SIGUSR1 is gated.
- DECIDE: Two helpers: pure _dump_logger_state(reason) (testable, no signal coupling) and _install_sigusr1_handler() (installs handler, returns bool for platform support). main() calls install but ignores return.
- DEVIL:
  - Correctness: Each summary call wrapped in try/except so one bad logger cannot abort the dump. Verified by test injecting a bad logger.
  - Scope: Devil caught a pre-existing test pollution bug -- TestSwallowSummaries direct L._log = lambda m: None assignments never restored, so any dump test that relied on actual file I/O failed under suite ordering. Migrated both to monkeypatch.
  - Priority: SIGUSR1 itself cannot be tested without spawning a process; the test verifies install succeeds on POSIX. Acceptable.
- ACT: 2 helpers + 5 tests + 2 pre-existing bug fixes. 473 passed.
- COMMIT: pending.

## Loop 86 — main() aggregate cadence test coverage
- OBSERVE: _log_aggregate_swallow_summary is invoked from main() but no test exercised the cadence logic. Off-by-one or modulo mistakes would be silent.
- ORIENT: Test the boundary explicitly. main() loops forever; break out via sleep stub raising KeyboardInterrupt after N iterations.
- DECIDE: 4 cases: correct cadence boundaries, every=0 disables, every=large never fires within window, exception path still increments count.
- DEVIL:
  - Correctness: iteration_count++ happens unconditionally before cadence check. Even on _iteration crash. Test_aggregate_fires_on_iteration_crash_too verifies. Good.
  - Scope: KeyboardInterrupt is the right interrupt to use because main()'s try/except catches Exception only, not BaseException.
  - Edge: every=0 path is the disable knob (verified). Negative would also be disabled by `if aggregate_every > 0`.
- ACT: 4 new tests, no production code changes. 477 passed.
- COMMIT: pending.

## Loop 87 — _dump_logger_state extended with iteration + last-summary-counts
- OBSERVE: SIGUSR1 dump shows logger summaries but not current loop position or the cached delta-summary state. Operator pulling a snapshot mid-run cannot tell which iteration they froze.
- ORIENT: Add optional iteration kwarg to _dump_logger_state, surface it in begin/end markers. Also dump _LAST_SWALLOW_SUMMARY_COUNTS so suppressed-but-not-yet-summarised counts are visible. Track _CURRENT_ITERATION at module level so the signal handler closure has a live value.
- DECIDE: Module-level _CURRENT_ITERATION updated inside main()'s loop body with `global` decl. Signal handler reads it at handler-fire time (closure captures binding, not value).
- DEVIL:
  - Correctness: Race -- signal can fire between iteration_count++ and _CURRENT_ITERATION = iteration_count. Worst case: dump shows previous iteration. Acceptable for observability.
  - Scope: Could subsume `iteration` arg by always reading _CURRENT_ITERATION, but explicit arg makes the helper testable without module mutation. Keep both.
  - Edge: `iter=` token only appears when iteration is not None -- confirmed via test_iteration_marker_omitted_when_none.
- ACT: 4 new tests, _CURRENT_ITERATION + dump arg + cache emit. 481 passed.
- COMMIT: pending.

## Loop 88 — startup diagnostics + SIGUSR1 documentation
- OBSERVE: SIGUSR1 + aggregate cadence are not visible to operators tailing loop.log. They have to reverse-engineer from grep patterns. Module docstring also unaware of new introspection capability.
- ORIENT: Two complementary fixes. (1) Emit a single `loop diagnostics` line at startup right after `loop starting` showing aggregate cadence and SIGUSR1 install status. (2) Append a "Runtime introspection" section to the module docstring covering both capabilities + the env var name.
- DECIDE: Use the existing `_install_sigusr1_handler` return bool (already True on POSIX, False on Windows) and surface it in the startup line so the install-failure case is visible too.
- DEVIL:
  - Correctness: handler must be installed BEFORE the diagnostics line emits (the line prints its outcome). Confirmed by reading order.
  - Drift: docstring drift audit could be tightened later. Current 3 assertions cover the new content keywords.
  - Scope: env var QWEN_AGGREGATE_SUMMARY_EVERY now mentioned in two places (docstring + startup line); single source of truth would be cleaner but cost is one-line updates which is acceptable.
- ACT: 5 new tests, docstring + startup line. 486 passed.
- COMMIT: pending.

## Loop 89 — drift guard against direct L.<attr> = ... in tests
- OBSERVE: Loop 85 caught one instance of test pollution (TestSwallowSummaries directly rebinding L._log). Loop 87 introduced another (L._CURRENT_ITERATION = 0). The shape repeats; humans + agents will keep adding it.
- ORIENT: Make this category mechanically un-introducible. AST scan of every test_*.py for Assign nodes with targets like L.foo or loop.foo, exempting attribute writes that occur inside Try.finalbody (the deliberate restoration pattern with try/finally and a real_xxx capture).
- DECIDE: Single drift-audit test class with one assertion. Allowed-list set holds explicitly-permitted restoration assignments (currently just `L.os.replace`). Any new offender breaks the test with a precise file:line pointer.
- DEVIL:
  - Correctness: AST walker tracks Try contexts via in_finally_stack so `with` blocks and nested Try blocks are handled. Verified against current source -- 0 violations.
  - Scope: The allowed-list could grow into a maintenance burden, but each entry should be paired with a finally clause so reviewer pressure stays high. Acceptable.
  - Edge: monkeypatch.setattr(L, "x", ...) is a Call, not an Assign with L.x target, so it's correctly ignored.
  - Migrated loop 87's `L._CURRENT_ITERATION = 0` to `monkeypatch.setattr` first to make the new test pass clean.
- ACT: 1 new test (drift audit) + 1 fix in loop-87 test. 487 passed.
- COMMIT: pending.

## Loop 90 — wall_s invariant: wall_s >= sum(phases)
- OBSERVE: wall_s emitted in loop 83 has no test guarding the semantic invariant. A future change that wires wall_s to a different clock source (e.g., per-phase delta sum or wall-clock from start of file) would silently break analytics that depend on wall_s being the "outermost" measurement.
- ORIENT: Three cases. (1) Direct write_timing call with synthetic phases asserts wall_s == 100, phases sum = 3.5, invariant holds. (2) Empty phases dict still yields nonneg wall_s. (3) End-to-end iteration produces a record where wall_s >= sum(phases) modulo float epsilon.
- DECIDE: Use direct _write_timing for the deterministic case (mocking time.monotonic for full _iteration is fragile). Skip the e2e check when wall_s field absent (defensive against future tests that mock _write_timing itself).
- DEVIL:
  - Correctness: 1e-9 epsilon catches float rounding without masking real regressions (e.g., negative drift from clock source mixing).
  - Scope: Could also assert wall_s <= sum(phases) + small_epsilon when no scaffolding is expected, but in practice scaffolding (file IO, history.md write, state.md append) easily takes 10ms+. Skipping that direction.
  - Edge: empty phases dict test confirms wall_s alone is meaningful.
- ACT: 3 new tests, no production code change. 490 passed.
- COMMIT: pending.

## Loop 91 — Empty-commit log path was unrate-limited
- OBSERVE: `_log("git commit skipped: empty staged tree ...")` at L1434 is a fixed string emitted unconditionally. A persistent fault (e.g., `.gitignore` rule capturing every diff target, or external repo state where applies always produce empty staged trees) would emit one line per iteration.
- ORIENT: This is exactly the pattern the swallow-logger registry was built for. 8 of 9 fault paths use rate limiting; this one slipped because it predates the registry.
- DECIDE: Add `_EMPTY_COMMIT_SWALLOW_LOG`, route the existing log call through it via `.report(RuntimeError("apply produced no committable changes"))`. Update `_swallow_loggers()` 9-tuple → 10-tuple. Update module docstring (drift audit).
- DEVIL:
  - Correctness: `report()` returns True only when emitted, but the function doesn't branch on the return value. Behaviour preserved (one log on first hit, exponential backoff after).
  - Scope: A normal session never hits this branch; it's specifically for the anomalous case. Rate-limiting it doesn't hide signal — the per-iteration aggregate-summary still surfaces accumulated count.
  - Priority: Loop 62 added the log; loop 91 is the natural follow-up (rate-limit the registry's last unmanaged path). Higher-leverage than `_strip_fence` or README polish.
- ACT: 
  - `agent/loop.py`: new logger decl, swallow-loggers tuple updated, docstring updated, `_log(...)` → `_EMPTY_COMMIT_SWALLOW_LOG.report(...)`.
  - `tests/test_iteration.py`: `TestEmptyCommitRateLimited` (3 tests). Loop 62's `TestCommitAndPushEmptyTreeLog` updated to assert the new message format.
- 493 passed.

## Loop 92 — Swallow logger registry hygiene invariants
- OBSERVE: 10 swallow loggers now in `_swallow_loggers()`. Loop 91 was a near-miss: had the new logger been declared but not appended to the tuple, the rate-limited `report` calls would still emit per-emission lines but the per-iteration delta channel and aggregate summary would silently skip them (because they iterate the registry, not module globals).
- ORIENT: Three invariants guard the registry: (1) every label is non-empty stripped string, (2) labels are unique across registry, (3) every `_*_SWALLOW_LOG` module-level binding is registered (and no phantom that isn't a module binding).
- DECIDE: 3 tests, no production code change.
- DEVIL:
  - Correctness: `_RateLimitedSwallowLogger` is a class; the introspection `isinstance(getattr(L, name), L._RateLimitedSwallowLogger)` is the cleanest gate. Module-level filter `name.endswith("_SWALLOW_LOG")` is a naming convention but tests catch it via the isinstance check too.
  - Scope: Could also assert `schedule in {"linear", "exponential"}` but that's enforced at construction time by the class; redundant.
  - Priority: Drift audit. Loop 91 demonstrated this gap matters in practice.
- ACT: `TestSwallowLoggerLabelHygiene` (3 tests). 496 passed.

## Loop 93 — wall_s_delta_phases for fast scaffolding-overhead detection
- OBSERVE: `wall_s` and per-phase wallclock are emitted, but every analytics consumer that wants "scaffolding overhead" (file IO, history.md write, git ops outside of named phases) has to recompute `wall_s - sum(phases.values())`. The loop 90 invariant test guards `wall_s >= sum(phases)`; surfacing the delta directly makes scaffolding spikes queryable with one field.
- ORIENT: Tiny additive change. Float-dust risk when `wall_s` is rounded to 4dp but phase sum is not -- floor at 0.
- DECIDE: Add `wall_s_delta_phases = round(max(0.0, wall_s - sum(phases)), 4)` next to `wall_s`. Same gate (only when iter_monotonic provided).
- DEVIL:
  - Correctness: floor at 0 is defensive; the loop 90 invariant says raw delta is always >= 0, but rounding asymmetry can produce negative dust. Test `test_delta_phases_floored_at_zero` engineers that case explicitly.
  - Scope: Could also emit `wall_s_phase_ratio = sum(phases) / wall_s` but that requires nonzero division and is trivially derivable. Skipping.
  - Priority: P6 from next.md, but cheap and pairs naturally with the loop 90 invariant.
- ACT: 1-line code change, 4 tests. 500 passed.

## Loop 94 — Autouse fixture: reset every swallow logger between tests
- OBSERVE: 32 occurrences of `_SWALLOW_LOG.reset()` scattered across `test_iteration.py`. Loop 91 added another. Each is a try/finally guarding against count contamination. The recurring pattern is symptomatic of missing centralisation.
- ORIENT: A single autouse fixture in `conftest.py` that iterates `_swallow_loggers()` and calls `.reset()` before AND after each test removes the entire class of contamination bugs. Pre-test reset protects against contamination from a prior test that didn't clean up; post-test reset is belt-and-suspenders.
- DECIDE: Centralise in `tests/conftest.py`. Add 2 tests that verify the fixture works (test_a leaves count=3 behind, test_b asserts count=0 — sequence-dependent but both will pass because pytest collects in file order).
- DEVIL:
  - Correctness: pytest does NOT guarantee test execution order across classes within a file; it does within a class. The two tests are in `TestSwallowLoggerAutoReset` and pytest preserves declaration order within a class. Verified.
  - Scope: The autouse fixture wraps EVERY test, including ones that don't touch loggers. Cost is iterating a 10-element tuple twice — negligible.
  - Risk: Could mask real contamination bugs (a test that secretly relies on another test's count). Mitigated because tests should be independent; if any test breaks under autouse-reset, it was relying on contamination and was wrong.
  - Pre-existing try/finally resets are now redundant but harmless — leave them in (they document intent and protect against fixture removal).
- ACT: 5-line conftest fixture, 2 tests. 502 passed.

## Loop 95 — Extend autouse fixture to clear _LAST_SWALLOW_SUMMARY_COUNTS
- OBSERVE: 4 occurrences of `L._LAST_SWALLOW_SUMMARY_COUNTS.clear()` in `test_iteration.py`. Same contamination risk as the per-logger counts: a test that seeds the dict to verify "summary suppressed because count unchanged" leaks state.
- ORIENT: Trivial extension to the loop 94 fixture. One-line addition before and after `yield`.
- DECIDE: Add `_LAST_SWALLOW_SUMMARY_COUNTS.clear()` to both halves of the fixture. Add 2-test sequence verifying the cleanup.
- DEVIL:
  - Correctness: `dict.clear()` is in-place, so the module-level reference is preserved. No risk of replacing the dict object and breaking imports.
  - Scope: Other module-level state? `_CURRENT_ITERATION` is reset by tests via monkeypatch (per loop 89 audit). No other dict-shaped contamination vectors found.
  - Priority: Direct follow-up to loop 94. Cheap.
- ACT: 1-line conftest addition x2, 2 tests. 504 passed.

## Loop 96 — _revert_changes left untracked files after reset --hard fallback (REAL BUG)
- OBSERVE: `_revert_changes` already runs `git clean -fd` on the happy path to remove untracked files (a brand-new file produced by a bad model diff). But the fallback path triggered when the initial clean fails uses `git reset --hard HEAD`, which only restores tracked content -- the untracked file survives. Same bug on the `reset --hard origin/main` second fallback.
- ORIENT: P1 priority (correctness regression on data-discarding code path). The fallback was added to recover from a checkout failure but inadvertently broke the contract that the function leaves the tree identical to HEAD.
- DECIDE: After every successful reset (HEAD or origin/main), best-effort re-run `git clean -fd`. Best-effort because if it fails again we can't recover further, but ok=True is still semantically correct: the tracked tree IS restored, and the next iteration's in-scope check will reject any diff that touches a file outside the iteration's chosen target.
- DEVIL:
  - Correctness: A second `clean -fd` failure leaves the file behind. The next iteration's `_diff_in_scope` check guards downstream — even if the orphan persists, it can't be patched on top of without an in-scope reject. Acceptable.
  - Scope: Could escalate to `git clean -fdx` (also remove ignored files) but that's destructive: a developer's local untracked notes/scratch would die. Stay with `-fd`.
  - Priority: This is the highest-leverage fix in the queue (correctness > observability > polish). Took priority over the next.md candidates.
- ACT: 2-line code change (new clean-after-reset call in both fallback branches with explanatory comment). 3 new tests covering HEAD-reset clean, origin-reset clean, and post-reset clean failure not flipping ok. 507 passed.

## Loop 97 — README: Runtime introspection section + SIGUSR1 example
- OBSERVE: SIGUSR1 logger dump (loop 85) and `QWEN_AGGREGATE_SUMMARY_EVERY` (loops 86-88) documented in module docstring of `agent/loop.py` but absent from README. An operator running the loop in production would not know to send SIGUSR1.
- ORIENT: README is the entry point. Adding a "Runtime introspection" section with the process-signalling invocation makes the feature discoverable.
- DECIDE: New README section with the recipe, what the dump contains, sensitivity note, and a small tunables table.
- DEVIL: SIGUSR1 is POSIX-only; Windows lacks it (handled by `hasattr(signal, "SIGUSR1")`). Dump is safe -- only label/count/schedule/last_log_message bounded-size strings.
- ACT: README +21 lines. 3-test docstring drift audit. 510 passed.

## Loop 98 — _abort_rebase_if_any silent reset failure (CORRECTNESS GAP)
- OBSERVE: `_abort_rebase_if_any` called `_run_git("reset", "--hard", "HEAD", check=False)` and ignored the rc. If HEAD is broken or the reset fails, tree stays dirty, next iteration starts compromised, no diagnostic emitted.
- ORIENT: P4 priority. Mirrors the loop-96 fix shape (`_revert_changes` already does HEAD->origin/main fallback with rate-limited logging). Symmetry restoration.
- DECIDE: Lift the `_revert_changes` recovery pattern: try HEAD reset, log failure on rate-limited swallow logger, fall back to origin/main reset, log that failure too if it also fails. `clean -fd` runs unconditionally at the end (since loop 96, post-reset clean is mandatory to remove untracked).
- DEVIL:
  - Correctness: If even origin/main reset fails (worst case: detached HEAD with no remote), tree stays dirty. Comment notes: downstream `_diff_in_scope` rejects diffs touching the orphaned files, so the loop remains correct -- just slower.
  - Scope: Could escalate to `clean -fdx` (also remove ignored files) but that destroys legitimate local untracked artifacts.
  - Rate-limit: Every reset failure goes through `_REVERT_SWALLOW_LOG` with a distinct context label so the per-iteration delta channel can distinguish abort_rebase failures from `_revert_changes` failures.
- ACT: ~25-line code change plus expanded docstring. 3 new tests. 513 passed.

## Loop 99 — Early-exit iterations bypassed _finish (OBSERVABILITY GAP)
- OBSERVE: `_iteration`'s two early-return paths (`no_candidate_files`, unreadable file `skip:..`) returned strings directly without calling `_finish`. Result: those iterations produce zero records in timing.log AND no per-iteration swallow summary line. A persistent fault that always trips one of these paths (e.g., every candidate file became too large after a vendoring drop, or `.gitignore` swallowed every code path) would silently stop emitting timing.log entries and stop calling `_log_swallow_summaries()` -- the swallow registry's per-iteration delta channel goes dark.
- ORIENT: P3 priority (test gap on existing functionality + observability cliff). Higher than the cleanup candidates because it directly causes diagnostic loss in exactly the failure modes operators most want visibility into.
- DECIDE: Add a `_finish_no_file` helper that takes a `rel_for_timing` Path (sentinel `Path(".")` for the no-files case, the actual rel for the unreadable case), emits an empty-phases timing record, then runs the summary cycle. Two early-returns updated to use it.
- DEVIL:
  - Correctness: Need a non-None `rel` for `_write_timing(rel)` because it calls `rel.as_posix()`. Sentinel `Path(".")` is safe (always a valid path).
  - Scope: Could also hoist `iter_monotonic` capture earlier so a crash in `_abort_rebase_if_any` is timed -- but that helper is best-effort and never raises (already audited). Skipping.
  - Risk: Now timing.log rows for `category=no_candidate_files` are common when the candidate-files cache is being rebuilt; downstream analytics querying outcome counts may surprise. But that's the correct behaviour: visibility of no-op iterations was the goal.
- ACT: ~12-line refactor adding `_finish_no_file`; 3 new tests covering both early-exit paths and the summary side-effect. 516 passed.

## Loop 100 — Categories drift audit didn't see _finish_no_file
- OBSERVE: Loop 99 added `_finish_no_file(...)` for the early-exit paths but the AST audit `test_every_finish_call_in_source_uses_known_category` only walks `_finish` calls. A future drift like `_finish_no_file("not_a_real_category")` would slip past the audit.
- ORIENT: This is a near-miss from loop 99. Caught it now before it becomes a real bug.
- DECIDE: Expand the AST audit's func-name match from `{"_finish"}` to `{"_finish", "_finish_no_file"}`. Add a paired test that explicitly extracts `_finish_no_file` tokens and asserts they're a subset of `OUTER_OUTCOME_CATEGORIES`.
- DEVIL:
  - Correctness: Both outcomes ("no_candidate_files", "skip") are already in the frozenset (verified via the existing `_outer_outcome_category` tests). The audit expansion just makes that constraint enforceable going forward.
  - Test redundancy: Two new tests overlap with the existing audit. Kept anyway because (a) `test_finish_no_file_tokens_in_frozenset` explicitly fails if `_finish_no_file` is renamed without updating the audit, and (b) `test_finish_no_file_audit_actually_runs` confirms the expansion isn't dead code.
- ACT: 4-line audit expansion + 2 new tests. 518 passed.

## Loop 101 — Removed 38 redundant in-test swallow-state resets
- OBSERVE: Loops 94-95 added a conftest autouse fixture that resets every swallow logger and clears `_LAST_SWALLOW_SUMMARY_COUNTS` BEFORE AND AFTER every test. In-test calls to `L._FOO_SWALLOW_LOG.reset()` (head and tail of test) are now dead code -- they fire after the autouse pre-reset and before the autouse post-reset, accomplishing nothing. 38 such calls existed across the test file.
- ORIENT: P5 cleanup, but the cost-of-staleness is real: a reader sees `L._TIMING_SWALLOW_LOG.reset()` at the top of a test and assumes the test cares about that state in a way that the fixture doesn't already cover. The redundancy is actively misleading.
- DECIDE: Remove all 38 calls, with one wrinkle: a few were the only statement in a `try:` or `finally:` block, so deleting them leaves a syntactically empty block. Replace those with `pass`. Add an AST audit test that walks the test file and fails if any future test reintroduces the pattern.
- DEVIL:
  - Correctness: the autouse fixture runs `before AND after` each test. Mid-test resets (between two operations within one test) WOULD be load-bearing -- but a manual scan showed every removed call is a head-of-test or tail-of-test reset. None mid-test.
  - Scope: the audit test catches future drift but doesn't catch the inverse: someone adding a state mutation that DOES need a reset. That's correctly out of scope -- the autouse fixture covers the general case.
  - Risk: blindly running sed left a `finally:` block empty and broke pytest collection. Hand-audited the structural-context pass.
- ACT: Custom Python script preserves block-only resets via `pass` substitution. 38 calls removed. New `TestNoRedundantSwallowResetsInTests` audits via AST. 519 passed.

## Loop 102 — Tightened audit caught real stale token: `no_hunks` was wrongly in OUTER_OUTCOME_CATEGORIES
- OBSERVE: `test_no_extras_beyond_emitted` previously did `cat in src` (substring scan), which would match a category name that survived only in a comment, docstring, or unrelated string literal. Tightened to an AST literal scan over `_finish`/`_finish_no_file` first-arg leading tokens.
- ORIENT: P3 -- the tightened audit immediately failed on `no_hunks`. That's a REAL drift bug, not a test artefact: `no_hunks` is returned by `_has_structural_defect`, a diff-validation helper, which gets wrapped into `validation_failed:{rel}` -- so the LEADING outer outcome token is `validation_failed`, never `no_hunks`. The frozenset's purpose is to enumerate leading-token categories, so `no_hunks` was a contract violation that the old substring audit happily passed because the literal "no_hunks" string appears in `_has_structural_defect` source.
- DECIDE: Tighten the audit AND fix the underlying drift -- remove `no_hunks` from `OUTER_OUTCOME_CATEGORIES` AND from `test_required_categories_present`'s expected set.
- DEVIL:
  - Correctness: removing `no_hunks` from the frozenset doesn't break anything because no `_finish` call ever had `no_hunks` as its leading token. The validation helper still returns "no_hunks" -- but that's an internal sub-error, opaque to the outer category contract.
  - Scope: `missing_plus_header`, `missing_minus_header`, `dir_path_conflict` are also sub-errors returned by validation helpers and are also NOT in OUTER_OUTCOME_CATEGORIES (correctly). The fix restores symmetry.
  - Risk: a tighter audit catches real bugs but rejects the laissez-faire "any string-literal mention counts as emission". Pre-loop-102 behaviour relied on that laxity to paper over `no_hunks`. New behaviour requires categories to be actually emitted as outer outcomes.
- ACT: AST-literal scan replacing substring scan; remove `no_hunks` from frozenset and expected set. 519 passed.

## Loop 103 — validation_failed outcome dropped the sub-rule, killing analytics breakdown
- OBSERVE: From loop 102's next.md candidate 5: `_finish(f"validation_failed:{rel}")` was the only path that didn't embed its sub-error in the outcome string. `apply_failed`, `rejected`, `out_of_scope` all do (`apply_failed:{category}:{rel}:{msg}`, `rejected:{rel}:{reason}`, `out_of_scope:{rel}:{scope_msg}`). The validation path put `syn_msg` in history-md and STATE.md but stripped it from the timing.log outcome -- so analytics could count validation_failed iterations but couldn't tell whether py_invalid, py_syntax_warning, or a yaml.safe_load failure was the proximate cause.
- ORIENT: P3 (observability gap on a critical correctness path). Same shape as loop 99's gap, different surface.
- DECIDE: Change `validation_failed:{rel}` -> `validation_failed:{rel}:{syn_msg.split(':', 1)[0]}`. Use the leading sub-token only because syn_msg can be `py_invalid: <multiline compile output>`; we want a clean third segment, not the full output. Same for `revert_failed:{rel}:after_validation` -> `revert_failed:{rel}:after_validation:{rule}`.
- DEVIL:
  - Correctness: `_outer_outcome_category` splits once on `:` so the leading token is preserved (still `validation_failed` / `revert_failed`). Verified via new test.
  - Scope: Why not embed the full syn_msg? Because syn_msg can be hundreds of bytes of compile output -- timing.log records would balloon and category dashboards would explode the cardinality. Leading token is the right granularity.
  - Symmetry: The other paths embed `[:60]`-truncated detail, not just leading token. Inconsistent with this fix. But syn_msg's sub-token IS clean (py_invalid / py_syntax_warning / json_invalid / etc.) -- it's already the right granularity by construction. Keeping it as leading-token only.
- ACT: Two-line change in `_iteration` + 3 tests asserting source format and category preservation. 522 passed.

## Loop 104 — main()'s iteration-crashed branch silently buried sink failures
- OBSERVE: When `_iteration` raises, the crash handler in `main()` only logged the traceback. Both `_finish` and `_finish_no_file` (which call `_log_swallow_summaries`) were skipped. So any swallow-logger increments that fired before the crash point (e.g., `_save_cursor` failing with disk full at line 1682, then `_candidate_files` raising on the next call) would leave their counts unflushed in `_LAST_SWALLOW_SUMMARY_COUNTS`. The next iteration that completes normally picks up the delta -- BUT if every iteration crashes (a regression that always fires before `_finish`), the delta channel goes dark forever even though the per-failure rate-limited logs from inside the swallowed sinks are also rate-limiting themselves. Operator sees the crashed traceback in runtime.log, has no idea the underlying sink is also failing.
- ORIENT: P3, same shape as the loop 99 bug (early-return paths skip _finish), now on the crash path. Same fix family.
- DECIDE: Add `_log_swallow_summaries()` call in the `except Exception` branch of `main()`'s while loop, after the traceback log. Best-effort; the helper is itself try/except-wrapped so it cannot re-raise.
- DEVIL:
  - Correctness: `_log_swallow_summaries` is idempotent (verified by a dedicated test): if the crash flush fires AND the next normal iteration also flushes, the second call sees no delta vs `_LAST_SWALLOW_SUMMARY_COUNTS` and emits nothing. So no duplicate noise.
  - Scope: should I also flush `_log_aggregate_swallow_summary` here? No -- aggregate is by definition a periodic summary, not a per-iteration one. Firing it on every crash would rate-limit incorrectly with the iteration_count modulo check.
  - Risk: AST audit (`TestNoDirectModuleAssignmentInTests` from loop 89) caught my first test's `L._log = lambda` rebind -- correct catch, fixed via monkeypatch. The audit IS load-bearing.
- ACT: 1-call addition in `main()` + 2 tests (AST audit that the call exists in the right except handler, behavioural test for idempotency). 524 passed.

## Loop 105 — Synthetic timing.log record on iteration crash
- OBSERVE: After loop 104, the crash branch flushes the swallow summary cycle but timing.log STILL has no record of the crash. Crash-rate dashboards parsing timing.log can count `applied`/`clean`/`validation_failed` etc. but have no signal for "iteration crashed". The traceback is in runtime.log only, which is unstructured and hard to aggregate.
- ORIENT: P4. Observability completeness, not correctness, but builds directly on loop 104's foundation.
- DECIDE: New `crashed` outer outcome category. Capture `iter_monotonic_outer = time.monotonic()` at the top of each main() while-iteration so the crash branch can emit wall_s. Synthesize `_write_timing(Path("."), "crashed", {}, iter_monotonic=iter_monotonic_outer)` in the except branch. Add `crashed` to OUTER_OUTCOME_CATEGORIES.
- DEVIL:
  - Correctness: `wall_s` will reflect time-to-crash, which is what an operator wants (catches "always crashes after 0.5s" vs "always crashes after 60s"). `phases={}` is correct because we have no phase signal at the main() level.
  - Audit drift: the loop 102 inverse audit (`test_no_extras_beyond_emitted`) walks `_finish`/`_finish_no_file` first-arg literals; this new emission goes through `_write_timing` second-arg literal which would FAIL the audit. Fix: extend the audit to also walk `_write_timing(_, outcome_literal, ...)` calls. This is the right shape because direct `_write_timing` calls ARE valid emission sites (loop 105 is precedent; future synthetic outcomes may follow).
  - Scope: should I emit `_log_swallow_summaries` BEFORE `_write_timing(crashed)` so the summary line precedes the timing record? Yes -- that's the order I implemented (matches `_finish`'s order: write_timing first then log_swallow_summaries -- wait actually `_finish` is the opposite, let me recheck). `_finish`: write_timing then log_swallow_summaries. My crash branch: log_swallow_summaries then write_timing. Slight asymmetry. But the crash flush prevents the summary going dark in the next iteration's finish, and the timing record is purely for analytics -- order doesn't matter for either consumer. Leave as is.
  - Risk: `_write_timing` is rate-limited internally so the crash emission could itself be suppressed. Wrapped the call in try/except as a belt-and-suspenders for the case where rate-limited rotation itself raises after emitting the swallow log.
- ACT: 1 frozenset addition + ~14-line main() crash branch + audit extension to walk `_write_timing` literals + 3 new tests. 527 passed.

## Loop 106 — Document outcome schema and timing.log fields in README
- OBSERVE: After loops 99/103/105 all changed outcome semantics, an operator using SIGUSR1 dumps + timing.log analytics has no canonical reference for what the categories mean or what fields each timing record contains. README was silent.
- ORIENT: P5 documentation, with drift-audit shape borrowed from loop 97 (README runtime-introspection audit). High value because the schema HAS been changing fast (3 loops in this segment touched it).
- DECIDE: New "## Iteration outcome schema" section in README covering all 16 OUTER_OUTCOME_CATEGORIES tokens (each as backticked) and the timing.log JSON-line field set. Add `TestReadmeOutcomeSchemaDocumented` with two tests: (a) every category in the frozenset appears in the section as a backticked token, (b) every timing.log field is mentioned somewhere in the README.
- DEVIL:
  - Drift cost: README is now coupled to OUTER_OUTCOME_CATEGORIES via the audit. Future categories must be documented OR the audit fails -- which is the whole point.
  - Granularity: `apply_failed:{category}:{rel}:{msg[:60]}` has internal sub-categories not documented. Out of scope -- the main contract is the leading token.
  - Risk: backticked-token search could false-positive if a category name appears backticked elsewhere in the section for a different reason. Acceptable -- the table format is deterministic.
- ACT: ~33 README lines + 2 audit tests. 529 passed.

## Loop 107 — Iteration budget didn't cover file discovery + read
- OBSERVE: `deadline = time.monotonic() + _iteration_budget_seconds()` was set AFTER `_candidate_files()` and `_read_file()` had already executed, so a slow file-discovery phase (cold filesystem cache, huge repo, network filesystem hiccup) could burn arbitrary wall-clock without the budget caring. Budget started counting only when discovery+read finished. The first `_over_budget()` check was after find_bugs, by which point the iteration was already deep in.
- ORIENT: P4. Real correctness gap on the budgeting contract -- the docstring says "Wall-clock ceiling for one `_iteration` call" but that ceiling didn't apply to non-Qwen phases. Probably fine for typical repos but pathological for a giant repo on a slow disk.
- DECIDE: (1) Hoist `deadline` capture above `_candidate_files()` so the budget covers everything from iteration start. (2) Add a fresh `_over_budget()` check immediately before find_bugs so a discovery-only blowout exits with `budget_exceeded:{rel}:after_discovery`.
- DEVIL:
  - Correctness: hoisting `deadline` doesn't break early-exit paths. `_finish_no_file` doesn't reference `deadline` at all -- it's purely a non-Qwen sentinel. Safe.
  - Test cascade: existing test `test_iteration_aborts_on_budget_after_find_bugs` mocks `time.monotonic()` with a fixed tick sequence. The new pre-find_bugs check consumes one extra tick. Updated tick-list to include a third in-budget value so the test still reaches the after_find_bugs branch it's asserting. Did NOT delete the old test -- after_find_bugs is still a valid downstream branch.
  - Scope: should I also wrap discovery in a `_PhaseTimer` so timing.log shows discovery wall-clock? Tempting, but would change the schema and require README + audit updates -- defer to a separate loop.
- ACT: Hoist `deadline` capture, add `after_discovery` check, fix existing tick-list test, add 3 new audits (deadline precedes _candidate_files in source, new outcome string present, category extraction). 532 passed.

## Loop 108 — Wrap discovery in `_PhaseTimer` so timing.log shows file-selection cost
- OBSERVE: After loop 107, the budget covers discovery but the timing record still doesn't show how much of that wall_s was spent in `_candidate_files()` + cursor + `_read_file()` vs the three Qwen calls. Slow filesystem just inflates `wall_s_delta_phases` -- meaningful but not directly attributable. Operators looking at timing.log can't tell "discovery is slow" from "post-devil git operations are slow" -- both surface as wall_s_delta_phases.
- ORIENT: P5 observability gap. Not a correctness issue, but every named phase makes timing.log more diagnostic.
- DECIDE: Wrap `_candidate_files` + cursor + read in a `discovery` phase. Records that take a real candidate get `phases.discovery`; early-exit no-file paths still emit `phases={}` (no real iteration ran).
- DEVIL:
  - Correctness: putting `return _finish_no_file(...)` inside a `with _PhaseTimer(phases, "discovery"):` block would dirty `phases` because `_PhaseTimer.__exit__` writes unconditionally. First attempt did `phases.clear()` inside the with -- doesn't work because exit runs after clear. Fixed: use a separate `discovery_phases` dict, only copy into `phases` once we know we have a real candidate.
  - Test cascade: existing budget-after_find_bugs test mocks `time.monotonic`. The new `_PhaseTimer` consumes 2 ticks (enter + exit), the after_discovery `_over_budget` consumes 1, plus the existing 2 (iter_monotonic + deadline-base). Updated tick-list to 6 in-budget values. Test still asserts after_find_bugs branch -- still valid.
  - Scope: Should I also wrap revert + commit in named phases? Worth doing but separate loop -- less obvious mapping (commit can fail in 4 places).
  - Priority: README must document the new phase or operators won't know to look for it. Updated.
- ACT: Restructured discovery+read into a separate `discovery_phases` dict that only graduates into `phases` once a real candidate is found. README schema section now lists `discovery`/`find_bugs`/`propose_fix`/`devils_advocate` and notes that early-exit outcomes emit `phases={}`. 4 new tests, 536 passed.

## Loop 109 — Phase-name drift audit + README catch-up
- OBSERVE: Loop 108's README said the named phases were `discovery`/`find_bugs`/`propose_fix`/`devils_advocate`. Searching `_PhaseTimer` calls in agent/loop.py reveals 7 phases: those 4 plus `apply_diff`, `validate`, `commit_push`. The undocumented three predate loop 108 -- they were already silently missing from the docs. Loop 108's README addition merely codified the gap.
- ORIENT: P5+ documentation correctness. Operators reading the schema would mis-attribute `apply_diff`/`validate`/`commit_push` time to `wall_s_delta_phases`.
- DECIDE: Update README to list all 7 phases. Add an AST-based drift audit so any future `_PhaseTimer(_, "<name>")` call must also have a backticked `\`<name>\`` in the README. Plus a hard-coded set check so adding a new phase fails loudly until both are updated.
- DEVIL:
  - Correctness: AST walks every `_PhaseTimer(_, str)` call and intersects with README content. First implementation also asserted args[0] was the literal name `phases`, but loop 108 uses `discovery_phases` (a separate dict to avoid early-exit pollution) -- the audit missed it. Loosened the filter to any Name node first arg.
  - Scope: Should the audit also assert each phase name appears in the timing.log JSON example? README doesn't have an example -- punt.
  - Priority: This is the right fix because every future phase addition will now be guarded by the audit, not just loop 108's. The hard-coded set is brittle (must update on every new phase) but that's the point -- forces the author to also update README.
- ACT: README schema section now lists all 7 phases with one-line descriptions. 2 new tests. 538 passed.

## Loop 110 — Wrap revert in named `revert` phase
- OBSERVE: `_revert_changes()` is called from 3 mutually exclusive branches in `_iteration` (after out_of_scope, after validation_failed, after commit_push failure). All three call sites time spent rolling the tree back ends up in `wall_s_delta_phases` -- invisible at the per-phase level.
- ORIENT: P5 observability gap. Revert can be slow on a dirty tree (untracked files, unstaged changes, network filesystems). Operators have no way to attribute that cost.
- DECIDE: Wrap each `_revert_changes()` call site in `_PhaseTimer(phases, "revert")`. Update README. Update audit hard-coded set. Add 3 tests (positive, negative, AST audit that EVERY `_revert_changes()` in `_iteration` is inside a revert PhaseTimer).
- DEVIL:
  - Correctness: only one of the 3 branches runs per iteration, so no overwrite risk -- if it ever changed (e.g., a future loop adds a 4th revert), the AST audit fails until that one is also wrapped.
  - Scope: Should I also wrap `_abort_rebase_if_any()` at iteration start? Not really a revert -- it's belt-and-suspenders cleanup before the iteration even reads anything. Punt.
  - Priority: This catches a real observability hole that complements loop 108's discovery phase. The AST audit is the load-bearing piece -- prevents future regressions.
  - Test fix: first test draft used `VERDICT: APPLY\n` (wrong). Production matches `VERDICT: ACCEPT`. Without the verdict regex match, the iteration short-circuits to `rejected:no_verdict` before even hitting the revert path. Fixed.
- ACT: 3 PhaseTimer wraps in agent/loop.py. README updated. Audit set extended to 8. 3 new tests. 541 passed.

## Loop 111 — README JSON example for timing.log
- OBSERVE: README schema section describes every field but doesn't show what one record actually looks like. Operators writing parsers have to infer the JSON shape from prose.
- ORIENT: P5 documentation usability gap. A concrete parseable example is worth more than a paragraph.
- DECIDE: Add two ```json blocks -- one applied iteration (covers happy-path phases), one validation_failed (covers the revert phase that doesn't appear on the happy path). Audit asserts every example parses, has every required field, and that the union of phase keys across all examples covers the production phase set.
- DEVIL:
  - Correctness: First attempt had ONE example with applied outcome, asserting the example's phase keys equal the production set. Failed: applied iterations have no `revert` phase. Two choices -- either the example is artificial (include all phases) or audit is loosened. Picked: two examples whose union covers the set. More honest, also documents that revert is conditional.
  - Scope: Should I add a third example for `crashed` (empty phases) or `qwen_error_*` (only first phase)? The current 2 examples already cover the happy + recovery paths -- third would be diminishing returns. Punt to next.md.
  - Priority: Documentation completeness, low risk.
- ACT: 2 ```json fences in README, AST audit walks all fences after the marker. 2 new tests. 543 passed.

## Loop 112 — wall_s analytics CLI
- OBSERVE: timing.log accumulates JSON-line records but there's no tooling to summarise it. To find regressions an operator has to run ad-hoc jq incantations or write a one-shot script. The schema is documented (loop 106, 109, 111) and stable enough to ship a real consumer.
- ORIENT: P5 observability tooling. Useful for debugging regressions in the loop itself (the agent is improving its own code -- if a recent commit makes find_bugs slower, p95 drift in the analytics surfaces it).
- DECIDE: New `agent/timing_analyze.py` module with `parse_records`, `analyze`, `format_report` pure functions and a `main(argv)` CLI entry. Default reads `.loop/timing.log`. Supports `--json` for machine-readable output and `--file` for non-default paths. Tolerates malformed lines (rotation races leave half-written final lines in the wild).
- DEVIL:
  - Correctness: Records without `wall_s` (early-exit, crashed) need to count toward category counts but contribute 0 to wall_s totals -- otherwise mean is over-stated. Implemented: `cat_counts` is incremented unconditionally; `by_cat[wall_s]` is appended only when `wall_s` is numeric.
  - Quantile: `statistics.quantiles` requires n >= 2; using a hand-rolled linear-interp `_quantile` to handle 0/1-element lists. Tested.
  - Scope: This is OBSERVABILITY of an OBSERVABILITY tool. Not directly fixing a bug. But it pays back next time the agent introduces a regression and the analytics catch it.
  - Priority: Lower than fixing live bugs but higher than cosmetic cleanup. The phase audit (loop 109) hard-codes a set of 8 phases -- if I miss documenting a new phase, the analytics still ingest it correctly since it walks `phases.keys()` dynamically.
- ACT: New module + 15 tests + README "Analysing timing.log" section. 558 passed.

## Loop 113 — wall_s_delta_phases p95 in analytics
- OBSERVE: Loop 112 shipped per-category and per-phase summaries but ignored `wall_s_delta_phases` -- the field that flags time spent OUTSIDE named phases. Without surfacing this, an operator looking at the analytics can't tell whether p95 wall_s is dominated by named phases (Qwen calls) or unaccounted-for time (filesystem, git, scheduling jitter).
- ORIENT: P5 observability gap, completes the analytics surface.
- DECIDE: Extend `analyze()` to collect `wall_s_delta_phases` across all records emitting it, return as a `_summarize` dict. `format_report` adds a section. If no records emit the field, say so explicitly (don't print zeros which would imply 0 wall time, not "unmeasured").
- DEVIL:
  - Correctness: Records emit `wall_s_delta_phases` only when `iter_monotonic` was provided (so wall_s was computed). Early-exit and crashed records via `_finish_no_file` don't get it. The collector has to skip non-numeric values (already implemented). Tested.
  - Scope: Should I also break down delta by category? Possible -- a high delta on `applied` is more concerning than on `no_candidate_files`. Punted -- caller can grep the JSON output. Adding it would clutter the text report.
  - Priority: Useful complement to the per-phase view, low risk.
- ACT: Extended `analyze` + `format_report`. 5 new tests. 563 passed.

## Loop 114 — Audit `agent.timing_analyze` is documented in README
- OBSERVE: Loop 112 added the analyzer + README section. Nothing prevents a future commit from removing the README block while keeping the module -- the analyzer would silently become undiscoverable.
- ORIENT: P5 doc-drift insurance.
- DECIDE: 3 small README-content audits in test_timing_analyze (module invocation, --json flag, --file flag, "Analysing timing.log" header).
- DEVIL: Correctness -- string-match audits are low-leverage but cheap. Scope: should I assert the exact heading? No -- the heading wording is incidental, the exact module invocation is what matters. Priority: trivial.
- ACT: 3 audits. Hit a self-inflicted import order bug (sed prepended a duplicate Path import at top of file before docstring). Fixed by reverting to canonical header. 566 passed.

## Loop 115 — Analytics aggregate rotated `.1` log too
- OBSERVE: `_rotate_log_if_oversized` renames the live timing.log to `timing.log.1` on rotation. The analyzer (loop 112) only reads the live file -- so the moment a rotation fires, all prior history vanishes from analytics until enough new records accumulate. p50/p95 estimates become unreliable for ~hours after each rotation.
- ORIENT: P5 observability gap. Rotation is supposed to be operationally invisible.
- DECIDE: New `_resolve_inputs(file, include_rotated)` helper. By default ingest both `<file>` and `<file>.1` (sorted by mtime ascending so older history is appended chronologically). New `--no-rotated` flag opts out. README doc updated with new usage example.
- DEVIL:
  - Correctness: What if `<file>.1` is malformed beyond just a half-written line? `parse_records` already silently drops malformed JSON. Safe.
  - Order: I sort by mtime to preserve chronology. mtime can lie (cp -p, network filesystem). For analytics this is fine -- aggregations are mtime-independent. p95 doesn't care about order.
  - Scope: What about `<file>.2`, `.3`? Current rotation is single-slot. If we ever extend to multi-slot, the helper needs updating, but no point future-proofing speculatively.
  - Priority: useful improvement, low blast radius, fully tested.
- ACT: New `_resolve_inputs` helper, `--no-rotated` flag, README usage block. 6 new tests. 572 passed.

## Loop 116 — Document `apply_failed` internal sub-categories in README
- OBSERVE: README schema table says "`apply_failed`: `git apply` rejected the diff." That's it. But production emits `apply_failed:<sub_category>:<file>:<msg>` where `<sub_category>` is one of 9 values in `APPLY_ERROR_CATEGORIES`. Operators inspecting timing.log or runtime.log have no way to know what `oversized_diff` vs `binary_patch` vs `dir_conflict` mean without reading source.
- ORIENT: P5 doc gap. Switched away from candidate 1 (forbid module-state mutation outside `global` decls) because audit found only ONE `global` decl in the entire codebase -- nearly all module state is dict/list mutated, which doesn't need `global`. Audit would be mostly false-positive prone.
- DECIDE: Expand the `apply_failed` row in README outcome schema table to list all 9 sub-categories, reference `APPLY_ERROR_CATEGORIES` constant. Add audit asserting every value in that frozenset is backticked in the README + the constant name itself appears.
- DEVIL:
  - Correctness: the audit is dynamic (reads `L.APPLY_ERROR_CATEGORIES` at test time), so any future addition automatically forces a README update or the test fails.
  - Scope: should I also document the `<msg>` truncation (60 chars)? Marginal -- the message is informational and varies. Punt.
  - Priority: small but solidly useful. Pure docs change with audits.
- ACT: Updated README row. 2 audits. 574 passed.

## Loop 117 — README mentions `--no-rotated` flag audit
- DECIDE: 1-line audit asserting README contains literal `--no-rotated`. Loop 115 added the flag and a usage example but no audit; if README rot strips the example it goes silently undetected.
- DEVIL: trivial test, no risk. Catches the regression class.
- ACT: Added `test_readme_mentions_no_rotated_flag` next to existing `test_readme_mentions_file_flag`. (Bundled with loop 118 commit -- both are tiny.)

## Loop 118 — Per-category breakdown of `wall_s_delta_phases` in analytics
- OBSERVE: loop 113 added overall `wall_s_delta_phases` summary but a 5s delta on a `clean` outcome (where phases=={}) is uninteresting whereas 5s delta on `applied` (where 7 phases ran) signals real unaccounted work. The single global summary obscures this signal.
- DECIDE: Add `category_wall_s_delta_phases: {category: summary}` to `analyze()` output, render in `format_report` as a sorted block sandwiched after the global summary. Skip block when empty.
- DEVIL:
  - Correctness: only collect delta when `category` is a string AND delta is numeric -- mirrors `by_cat` collection guards.
  - Scope: should the JSON output include the new key? Yes -- tests assert against `out["category_wall_s_delta_phases"]`. format_report drives off it too.
  - Priority: P5 -- sharper signal for ops triage.
- ACT: Extended `analyze()` and `format_report()`, added 3 tests. 578 passed.

## Loop 119 — Document 60-char truncation of `apply_failed` `<msg>`
- DECIDE: README expansion + 2 audits: one asserts README mentions 60+truncated+apply_failed; one asserts production source still has `msg[:60]`.
- DEVIL: linking README claim to source ensures docs and behavior stay coupled. If a future loop changes the truncation length the source-side audit fires.
- ACT: README updated, 2 tests. 580 passed.

## Loop 120 — Document why `_abort_rebase_if_any` is deliberately untimed
- OBSERVE: candidate said "wrap in `precheck` phase OR document why deliberately untimed". Wrapping requires moving `iter_monotonic` capture earlier or re-architecting -- both risk regressions in well-tested code paths. Documenting is the lower-risk equivalent.
- DECIDE: Multi-line comment above the call explaining (a) it must precede iter_monotonic (b) it's a no-op on the happy path (c) wrapping would pollute phase distributions with effectively-zero values for 99% of iters. 2 audits: position vs iter_monotonic, and "no PhaseTimer precheck" + comment marker.
- DEVIL:
  - Correctness: if a future loop wants to add a precheck phase it would have to delete the marker comment, satisfying both audits and forcing reflection.
  - Scope: the audit checks structural invariants, not just the comment text -- text drift would let through silent removals.
  - Priority: P5 long-term hygiene, prevents a class of "why is precheck phase always 0?" debugging.
- ACT: Comment + 2 audits. 582 passed.

## Loop 121 — Orphan-category audit for `OUTER_OUTCOME_CATEGORIES`
- OBSERVE: existing audit asserts every declared category is documented in README. Inverse direction was unguarded: a category could be added to the frozenset, documented in README, but no `_finish*` call site actually emits it. Pure-frozenset bloat that lies to ops.
- DECIDE: AST audit walking every `_finish`, `_finish_no_file`, `_write_timing` call. For each, extract first-arg literal (handling f-strings by reading the leading constant segment before any interp), split on `:` to grab the category prefix, accumulate. Set difference against `OUTER_OUTCOME_CATEGORIES` must be empty.
- DEVIL:
  - Correctness: f-string handling via `JoinedStr.values[0]` Constant grabs the literal prefix that comes before the interpolated `{rel}` -- which is exactly the category. If a future loop builds an outcome via `f"{var}:..."` (no leading literal), that category becomes invisible to the audit -- but that pattern is also bad practice (categories should be source-greppable).
  - Scope: `crashed` is emitted from `main()` not `_iteration`, but the AST walks the entire module so it's included.
  - Priority: P5 dead-code detection.
- ACT: `TestOuterOutcomeCategoriesAllReachable` test class. 583 passed.

## Loop 122 — Sister audit: every emit-site category is declared
- DECIDE: mirror loop 121's AST walker but invert the assertion -- emitted set must be subset of `OUTER_OUTCOME_CATEGORIES`. Catches typo'd category strings since `_outer_outcome_category` falls back to raw token.
- DEVIL: same f-string handling caveat; bad-pattern emit sites would dodge the audit but those are also flagged by review.
- ACT: `TestEverySourceCategoryIsDeclared`. 584 passed.

## Loop 123 — Third README example: early-exit outcome with empty phases
- OBSERVE: schema text says early-exit outcomes emit `phases: {}` but the two JSON examples both have populated phases. Ops reading the examples might assume `phases: {}` is malformed.
- DECIDE: Add a third ```json fence right after the validation_failed example showing a `no_candidate_files` early-exit -- empty `file`, empty `phases`, only wall_s and wall_s_delta_phases populated.
- DEVIL:
  - Correctness: existing loop-111 audit walks ALL fences and asserts each parses + has phase keys subset of AST set. Empty set is a subset of any set so the new example passes.
  - Scope: new audit asserts at least one example has `phases == {}` so future README rewrites can't drop the early-exit example silently.
  - Priority: P5 docs hardening.
- ACT: New JSON fence + 1 audit. 585 passed.

## Loop 124 — `--top-n` flag for analytics
- OBSERVE: real timing.log eventually accumulates 8+ phases. Per-phase block alphabetical-sorted means triaging "what is slow" requires eye-grepping. The phase distributions are the highest-leverage signal in the report.
- DECIDE: Add optional `top_n` arg to `format_report` that re-sorts phase items by p95 desc and slices. CLI flag `--top-n N` passes through. When unset, behavior unchanged (alphabetical). Header line changes to "by phase (top N by p95 wall-clock):" when active.
- DEVIL:
  - Correctness: tests assert (a) only N kept (b) descending order (c) None preserves alphabetical.
  - Scope: should `--top-n` also limit category block? No -- categories are bounded at 16, the phase block is the one that grows.
  - Priority: P5 ergonomics. Strict superset of prior CLI (default unchanged).
- ACT: format_report signature extended, CLI flag added, README usage example added, 5 tests. 590 passed.

## Loop 125 — `--since <iso>` filter
- DECIDE: pure helper `filter_since(records, since)` -- lex ISO-8601 compare works because `_write_timing` writes UTC Z-suffix. CLI flag passes through. Records with non-string `ts` excluded when filter active so partial logs cannot leak past.
- DEVIL:
  - Correctness: lex compare is mathematically valid for sortable ISO-8601 with same precision and fixed `Z` suffix. If a future loop changes the timestamp format to include offset like `+00:00`, the compare still works because of leading-digit dominance, but timezone variations would silently break.
  - Scope: should `--until <iso>` symmetric counterpart exist? Not yet -- premature; YAGNI.
  - Priority: P5. Big logs make this useful when triaging "what changed since the last deploy".
- ACT: helper + CLI flag + 5 tests + README example. 595 passed.

## Loop 126 — `--until <iso>` symmetric counterpart
- DECIDE: mirror `filter_since` as `filter_until` (`<=` instead of `>=`). CLI flag. Compose: `filter_until(filter_since(recs, since), until)` yields closed `[since, until]` interval.
- DEVIL:
  - Correctness: composition order doesn't matter (filters are commutative), but applying since first reduces work for until on the typical case.
  - Scope: leave as two separate functions rather than a single `filter_range(recs, since, until)` -- composition is honest about the two independent boundaries and `--since` alone or `--until` alone are equally valid.
  - Priority: P5 ergonomics. Tiny code, big ROI for log triage.
- ACT: helper + CLI flag + 5 tests + README example. 600 passed (milestone crossed).

## Loop 127 — `--category` and `--phase` filters
- OBSERVE: analytics CLI had time-window filters (since/until) but no outcome-focused filters. Real triage: "show me stats for only applied iterations" or "how slow is devils_advocate across all runs".
- DECIDE: two pure filters: `filter_category(records, category)` for exact match on `category` field, `filter_phase(records, phase)` for records whose `phases` dict contains the phase name as a key. CLI flags apply in chain after time filters. Both pass through on None/empty.
- DEVIL:
  - Correctness: phase filter checks `phase in phases` (key existence) not value >= threshold -- correct because the schema is phase name → wall_s, and a phase with 0.0 is still a phase that ran.
  - Scope: should `--phase` support multiple phases as a union/intersection? Premature; CLI stays simple, analysts use `--phase A | combine-json | --phase B` if needed.
  - Priority: P5. Unlocks drill-down analysis: "were devils_advocate rejections due to slow time or fast rejections on bad proposals".
- ACT: 2 helpers + 2 CLI flags + 6 tests + README example. 606 passed.

## Loop 128 — `web_search` and `fetch_url` MCP tools
- DECIDE: pure module `web_tools.py` with `web_search` (DDG html, no key), `fetch_url` (httpx wrapper with content-type/byte-cap safety), `parse_search_results` (regex-tolerant). Wire as 2 new MCP tools. Tests use `httpx.MockTransport`.
- DEVIL:
  - Correctness: regex parses DDG markup loosely so a small layout change doesn't crash; falls back to empty list. fetch_url refuses non-http(s) schemes (no `file://` exfil), refuses non-text content types (no binary blobs leaking back through Qwen).
  - Scope: no caching, no robots.txt parsing -- defer; single-call use case dominates.
  - Priority: P3 user-requested feature gap. claude-code style web access.
- ACT: 200-line module, 21 tests, README features list expanded. 627 passed.

## Loop 129 — filesystem MCP tools (read_file, list_dir, write_file, apply_patch)
- DECIDE: claude-code parity needs filesystem access. Pure module fs_tools.py with FsConfig (root, byte caps, entry caps), 4 helpers + 2 formatters, all sandboxed via realpath relative_to check.
- DEVIL:
  - Correctness: symlink escape covered by realpath resolution before relative_to. Binary detection via UTF-8 decode error. apply_patch shells `git apply` with timeout=30 and a tempfile so a malformed diff cannot stall the server.
  - Scope: write_file refuses missing parent unless create_parents=True -- prevents accidental directory typos. apply_patch has check_only mode for TUI preview.
  - Priority: P3 user-requested (claude-code parity). Lays groundwork for TUI agent loop.
- ACT: 200-line module + 27 tests + 4 server tool entries with closure dispatch through fs_cfg. README updated. 654 passed.

## Loop 130 — Textual TUI scaffolding (tui.py + slash commands + multi-turn memory)
- DECIDE: claude-code parity needs an interactive TUI. Ship Textual-based tui.py with a pure parse_slash and dispatch_slash so logic is fully testable without spinning an App. Slash commands: help search fetch read ls find_bugs explain quit. Plain text becomes a chat turn with multi-turn ChatMessage history. Add `tui` extra and `qwen-coder-tui` console script.
- DEVIL:
  - Correctness: parser strips and lowercases; empty-after-slash handled. dispatch_slash returns (text, quit) tuple so the App layer never has to inspect command names. chat_turn injects system message only if absent so user-supplied system survives.
  - Scope: textual import is lazy inside _build_app so MCP-only users do not pay the dep cost. Tests use FakeClient so no Qwen server needed.
  - Priority: P3 user-requested feature (TUI parity).
- ACT: 240-line module, 21 tests, README section, pyproject.toml `tui` extra and `qwen-coder-tui` script. 675 passed.

## Loop 131 — TUI extract_diff + /apply + /history + Pilot smoke test
- DECIDE: TUI useful only when assistant replies can be acted upon. Add extract_diff (fence then bare git header), /apply (check-only first then real apply), /history [n], and a Pilot-driven smoke test of the actual App.
- DEVIL:
  - Correctness: extract_diff prefers ```diff/```patch fence, falls back to bare `diff --git`, returns None when neither present. /apply runs git apply --check first; if check fails, leaves the tree untouched and returns the message. /history truncates each line to 400 chars so a giant reply does not blow the terminal.
  - Scope: dispatch_slash now takes optional history kwarg so existing call sites without history still work; only /apply and /history require it.
  - Priority: P3 user-requested feature (TUI parity for actually executing model output).
- ACT: ~80 lines TUI code + 12 tests (incl one anyio Pilot test that types /help and checks RichLog content). 687 passed.

## Loop 132 — chat_stream SSE in QwenClient + chat_turn_stream in TUI
- DECIDE: chat() blocks the TUI. Add chat_stream() that yields incremental tokens via OpenAI-compatible SSE protocol. Wire chat_turn_stream() into the TUI App so plain-text turns render as chunks accumulate. Keep non-streaming chat() unchanged so MCP tools and tests still work.
- DEVIL:
  - Correctness: parser skips comments, malformed JSON, and non-data lines. [DONE] marker terminates cleanly. 5xx and 408/425/429 raise QwenError; other 4xx raise QwenFatalError to mirror chat() classification. extra={"model":...} still rejected.
  - Scope: no retries -- streaming is interactive, partial output is more useful than blocking. chat_turn_stream rolls back on error (no half-committed assistant message in history).
  - Priority: P3 user-requested (TUI live render).
- ACT: chat_stream method (~80 lines) + chat_turn_stream helper + TUI App wiring + 12 new tests (9 client SSE + 3 TUI). 699 passed.

## Loop 133 — /diff slash command
- DECIDE: TUI needs file comparison. Add /diff <pathA> <pathB> using stdlib difflib.unified_diff via fs_tools.read_file (so sandboxing applies).
- DEVIL:
  - Correctness: identical files return a parseable note instead of empty string. fs_tools errors surface as "diff error: ..." with the same path-escape rejection as /read.
  - Scope: stdlib only -- no new deps. n=3 context lines is the python diff default.
  - Priority: P3 user-requested.
- ACT: ~20 lines code + 6 tests. 705 passed.

## Loop 134 — fix TUI connection-refused UX + serve OOM defaults
- OBSERVE: user reports the TUI shows raw "connection refused" with no hint. .loop/serve.log shows vLLM CUDA OOM during warmup with default max_model_len=32768 and gpu_util=0.92 because the int4 27B weights plus 32k KV cache overflow 24 GB on a 4090.
- DECIDE: make defaults safe out of the box and surface a friendly banner with an actionable hint when the server is unreachable.
- DEVIL:
  - Correctness: lower max_model_len to 8192 and gpu_util to 0.85 -- still room for full int4 27B with KV cache headroom on a 24 GB card; users with bigger cards can override via env. health_check uses a short timeout (default two seconds) so UI startup is not blocked when the server is genuinely down.
  - Scope: friendly error path applies to chat_turn (non-streaming). chat_turn_stream already accumulates the error inline; not regressed. health_check returns dict, never raises -- callers safe.
  - Priority: P1 user-blocker -- TUI literally unusable without this.
- ACT: scripts/serve_qwen.sh new defaults; QwenClient.health_check returning ok/models or error/hint dict; TUI App on_mount now writes a green or red banner; chat_turn now routes through _friendly_chat_error which sniffs ConnectError and prints a serve_qwen hint. 9 new tests (5 health_check + 2 friendly chat error + 2 Pilot-driven banner). 714 passed.


## Loop 135 — /run /grep /find /clear /save slash commands + shell_tools module
- DECIDE: claude-code parity needs bounded shell exec plus repo grep and find. Add shell_tools module with run_shell (deny list, timeout, output cap, sandboxed cwd), grep (pure python recursive regex, skips dot git and binary), and find (rglob with excluded dirs). Wire five new slash commands.
- DEVIL:
  - Correctness: deny list catches rm -rf root, sudo, mkfs, dd, fork bomb, shutdown, chmod -R 777 /, chown -R /. Allows rm -rf build (relative). cwd resolves through fs_tools resolve inside root so a cwd arg of dot dot is rejected at the fs boundary. timeout wraps subprocess.run; on TimeoutExpired we tag stderr with timeout and set timed out flag. output capped at sixty four KB per stream with a truncation note.
  - Scope: pure python grep avoids the ripgrep dependency. Skips dot git node modules dot venv etc and binary files (NUL in first four KB). max hits cap two hundred so a runaway pattern does not flood the TUI.
  - Priority: P2 user-requested (claude code parity).
- ACT: shell_tools.py (~250 lines) + 36 shell_tools tests + 16 TUI dispatch tests. Five new slash commands. README will be updated next loop. 765 passed.


## Loop 136 — /git read-only + /tests + @file mention expansion
- DECIDE: copilot and claude code both expand @file mentions inline and let the user run git status / pytest from the chat box. Add expand_at_mentions plus /git plus /tests.
- DEVIL:
  - Correctness: /git allow list is status log diff show branch remote rev-parse only. log auto bounds to last twenty oneline if user did not pass -n. /tests defaults to -q. expand at mentions silently skips unreadable paths so a typo or sandbox escape leaves the literal token in the prompt rather than blocking the user.
  - Scope: the at expansion only triggers when fs_cfg is provided to chat_turn or chat_turn_stream so unit tests that pass no fs_cfg keep their previous behaviour.
  - Priority: P3 user-requested parity.
- ACT: git allow list dispatcher; tests slash; expand_at_mentions helper that scans for at word path tokens and appends file bodies under fenced blocks. Fifteen new tests covering git status git not allowed git usage tests run pytest and seven at expansion cases plus chat_turn integration. 776 passed.


## Loop 137 — /tokens slash + per-turn telemetry footer
- DECIDE: copilot and ml intern both surface a token and time meter so the user can sense context window pressure and backend latency. Add slash tokens plus a dim telemetry line after every chat reply.
- DEVIL:
  - Correctness: estimate is len divided by four characters per token. Crude but standard. Empty string returns zero, short string returns one, longer strings scale linearly.
  - Scope: real tokenizer would mean pulling tiktoken or transformers just for a status line. Cost is way out of proportion to value here. Document the rule of thumb in the slash response so the user knows it is approximate.
  - Priority: P3 user-visible affordance, no risk of regression because telemetry is purely additive on the App layer and the helper is pure.
- ACT: estimate_tokens helper, slash tokens dispatcher branch, App now records last_turn_tokens, last_turn_seconds, total_tokens, total_turns and writes a dim telemetry line after every chat reply. Five new tests. Seven hundred eighty one passed.


## Loop 138 — /sysprompt and /model slash commands
- DECIDE: claude code lets you swap system prompt mid session and copilot can switch model. Add slash sysprompt and slash model.
- DEVIL:
  - Correctness: Settings is a frozen dataclass so setattr would TypeError. Use object dot dunder setattr to bypass. Tested both show and set paths plus a no-settings client guard.
  - Scope: replacing the system prompt mid-history could confuse the model on the next turn but that is the user's intent. Document by echoing char count and showing the current prompt on bare slash sysprompt.
  - Priority: P3 user-requested parity, no risk to core flow.
- ACT: slash sysprompt with show / replace / insert behavior. slash model with show / set behavior using object dunder setattr to mutate the frozen settings dataclass. Seven new tests. Seven hundred eighty eight passed.


## Loop 139 — /undo and /retry slash commands
- DECIDE: claude code and copilot both expose undo plus retry so users can rewind a bad turn or re-roll an answer without restarting. Ship both.
- DEVIL:
  - Correctness: /undo pops trailing assistant if present then trailing user. Always preserves system at index zero. /retry walks history bottom up to find the most recent user message and trims everything from that index onward. Returns a __RETRY__ sentinel that the App layer detects and replays as a fresh user turn so the dispatcher stays pure.
  - Scope: __RETRY__ sentinel is a string, not a tuple, because the public dispatch_slash signature is fixed at returning tuple of str and bool. Keep that and let the App layer recognize the prefix instead of changing the contract.
  - Priority: P3 user-requested parity, no risk to chat path.
- ACT: undo and retry dispatcher branches plus App layer detection of the retry sentinel. Six new tests covering undo of a full pair, undo of a dangling user, undo with nothing to pop, retry round trip, retry with no prior user, and retry trimming only the last pair when earlier pairs exist. Seven hundred ninety four passed.


## Loop 140 — persistent chat history across runs
- DECIDE: claude code remembers context across sessions. Add jsonl persistence keyed to repo root.
- DEVIL:
  - Correctness: malformed json lines and unknown roles are silently skipped on load. Save and load both swallow OSError so the App can never crash on a read only filesystem or a permission denied. Trailing five hundred message cap prevents unbounded growth.
  - Scope: file lives under the repo root in dot agent slash tui history dot jsonl rather than a global home location so each repo gets its own history and the existing dot agent gitignore patterns can hide it.
  - Priority: P3, low risk because both code paths are wrapped in try except and tests cover round trip plus malformed plus capped tail.
- ACT: history_file_path, save_history_jsonl, load_history_jsonl helpers. App on_mount loads prior history and on_unmount saves on exit. Five new tests. Seven hundred ninety nine passed.


## Loop 141 — slash command tab completion
- DECIDE: claude code and copilot both autocomplete slash names. Wire textual SuggestFromList to the Input widget.
- DEVIL:
  - Correctness: slash completions returns empty list for empty input or non slash prefix so plain chat does not suggest anything. The split slice on the first whitespace head handles users who tab while mid arg without losing their cursor position. Suggester is wrapped in try except ImportError so older textual versions degrade silently.
  - Scope: the SLASH_COMMANDS tuple is the single source of truth for the help text and the suggester. A test asserts every dispatcher branch is in the list so adding a new command without updating the constant fails CI.
  - Priority: P3 user experience polish, no risk to chat path.
- ACT: SLASH_COMMANDS tuple, slash_completions helper, lazy SuggestFromList import inside _build_app, Input widget receives suggester kwarg. Eight new tests. Eight hundred seven passed.


## Loop 142 — /diff <path> against git HEAD
- DECIDE: claude code lets you diff a single file against the most recent commit. Add slash diff with one argument meaning compare to HEAD.
- DEVIL:
  - Correctness: validate path is inside fs root before shelling git so a relative path escape via dot dot is rejected before reaching the subprocess. Use shell tools run shell which inherits the deny list and the timeout. Empty stdout with returncode zero means no working tree differences relative to HEAD. Non zero returncode echoes stderr.
  - Scope: keep the original two argument behavior intact for ad hoc file comparisons that are not under git.
  - Priority: P3 user requested parity, low risk because the new branch only triggers on a single argument.
- ACT: _render_diff_head helper that resolves the path through fs tools and shells git with no pager diff HEAD -- path. Dispatcher branch updated to route one arg to head diff and two args to file diff with a refreshed usage message. Five new tests cover modify diff no changes message two arg passthrough usage on no args and path escape. Eight hundred twelve passed.


## Loop 143 — /sysinfo snapshot
- DECIDE: copilot has slash status that combines model plus health plus context size in one line. Add slash sysinfo as a copy-pasteable bug report block.
- DEVIL:
  - Correctness: getattr chain on client dot settings dot model so a stub client without a Settings instance still renders unknown rather than raising. health check call is wrapped in a try except so a backend that times out cannot crash the slash dispatcher. token estimate uses the existing helper.
  - Scope: pure read only, never mutates history or settings.
  - Priority: P5 polish but free since the helpers exist.
- ACT: _render_sysinfo helper emitting model base url fs root history count tokens and health line. Dispatcher branch and SLASH_COMMANDS entry. Three new tests covering healthy backend, unhealthy backend, and health check raising. Eight hundred fifteen passed. Also fixed two indentation regressions in _render_diff_head and the retry branch caused by an editor merge in this loop.


## Loop 144 — /export markdown transcript
- DECIDE: claude code has Save Conversation as Markdown. Add slash export that writes a properly headed and fenced markdown transcript distinct from slash save's flat log shape.
- DEVIL:
  - Correctness: header lines, fence around bodies, blockquote for system prompt, count of non-system turns in the success message. Path goes through fs_tools.write_file so the sandbox applies.
  - Scope: keep slash save unchanged for users who want the simpler log format.
  - Priority: P5 polish.
- ACT: _render_export helper plus dispatcher branch plus SLASH_COMMANDS entry plus help text. Five new tests cover basic round trip, no args, no history, empty history, and a path escape attempt. Eight hundred twenty passed.


## Loop 145 — /pin and /unpin file-to-system-prompt
- DECIDE: claude code lets you pin a spec or contract file so the model rereads it every turn without the user re-attaching. Add slash pin and slash unpin.
- DEVIL:
  - Correctness: read goes through fs_tools so the sandbox applies and a path escape leaves the system prompt untouched. The pinned files marker is inserted once and subsequent pins append under the same marker so unpin can strip everything in one shot. Files larger than eight kilobytes get the same truncation marker as the at-mention helper.
  - Scope: distinct from at-mention which is a one shot inline expansion. Pin survives across turns by mutating the system prompt.
  - Priority: P3 user requested parity.
- ACT: _render_pin and _render_unpin helpers, dispatcher branches, SLASH_COMMANDS entries, help text. Seven new tests cover pin attaching content pin inserting system message when missing pin appending a second file with one marker pin path escape leaving system unchanged unpin clearing the block unpin when nothing pinned and pin truncating a large file. Eight hundred twenty seven passed.


## Loop 146 — Markdown rendering for assistant replies
- DECIDE: claude code and copilot render fenced code blocks with syntax highlighting and lists with bullets. Add lightweight markdown detection plus rich.markdown.Markdown wrap in the App layer so qwen replies that contain fences headings or lists render as proper markdown in the RichLog.
- DEVIL:
  - Correctness: helper is pure heuristic over substring presence. False negatives are fine because plain text path still works. False positives in the helper still produce valid output because rich.markdown.Markdown handles plain prose. Wrapped the rich import in try except ImportError so the App still works on a stripped install without rich. The plain prefix path keeps short answers on one line because wrapping a one word reply in markdown looks weird.
  - Scope: heuristic not a real markdown parser. Real fix would be to ask the model to declare markdown via a content type tag. This is the stepping stone fix not the architecture rewrite.
  - Priority: P3 user requested parity with claude code copilot ml intern.
- ACT: looks_like_markdown helper plus _MARKDOWN_HINTS tuple. App._post_assistant method called from both streaming success path and AttributeError fallback. Nine new heuristic tests cover fenced code heading bullet list numbered list blockquote bold plain short text plain paragraph and empty input. Eight hundred thirty six passed.


## Loop 147 — /pinned listing pinned files
- DECIDE: pin and unpin landed in loop one forty five but a user with several pins has no way to inspect what is currently attached. Add /pinned to list pinned file paths back from the system prompt.
- DEVIL:
  - Correctness: parses the same hash space heading the pin helper writes so the parse and write stay in sync. No separate state to drift. When the marker is missing or no headings follow it returns nothing pinned.
  - Scope: not a full pin manifest but enough for the user to see which files are attached. P5 utility.
  - Priority: low risk small surface area.
- ACT: _render_pinned helper splits the system message on the marker collects lines starting with hash space and reports them. SLASH_COMMANDS gains pinned. Dispatcher branch added. Help text updated. Three tests cover listing two pins reporting nothing pinned and reporting nothing pinned after unpin. Eight hundred thirty nine passed.


## Loop 148 — /history clear truncates jsonl too
- DECIDE: clear is the missing half of the persistence story. Right now /clear blanks the RichLog only and history grows unbounded across sessions because the jsonl file persists. Add clear sub command.
- DEVIL:
  - Correctness: keeps the system message because dropping the system would lose the pinned files block and the coder system prompt without warning. unlink wrapped in try except OSError so a read only fs or missing parent directory does not crash. only reports deleted persistence file when the file actually existed and was successfully removed so the message stays accurate.
  - Scope: does not also dump the loop_log or next.md because those are agent state not user chat state. tight scope is the right call.
  - Priority: P5 cleanup but high user value since unbounded history bloat across sessions is a real issue.
- ACT: extend /history dispatch to accept clear arg. clears in place via list.clear and extend so the live history reference the App holds stays the same object. unlink jsonl when fs_cfg present. updated usage error string and help text. four tests cover clear keeping system clear deleting the jsonl file clear when no jsonl exists and the existing numeric n form still working.


## Loop 149 — /open launches \$EDITOR on a file
- DECIDE: claude code and copilot let you jump from chat into the editor on a path. Add /open that resolves the path inside the sandbox and execs \$EDITOR.
- DEVIL:
  - Correctness: path goes through fs_tools._resolve_inside_root before the editor sees it so a chat supplied dot dot escape never reaches subprocess. EDITOR string split with shlex so a value like code dash w works. shell free invocation via subprocess.run with a list to defeat command injection through a malicious path that contains shell metacharacters. FileNotFoundError caught for the editor missing case so we return a friendly error not a stack trace. fall back to vi when EDITOR is unset which mirrors POSIX convention.
  - Scope: blocking call. The TUI input is paused while the editor runs which is fine for vim or vscode wait mode. A non blocking variant would need its own slash. P5 utility ships now blocking variant later if requested.
  - Priority: P5 user requested ml intern parity.
- ACT: _render_open helper. Dispatcher branch added with usage error. SLASH_COMMANDS gains open. Help text updated. Four tests cover path escape blocking the launch usage error invoking the editor with a resolved sandbox path and capturing the args via monkeypatch and the FileNotFoundError friendly error path. Eight hundred forty seven passed.


## Loop 150 — vLLM OOM on warmup: smaller KV cache footprint by default
- OBSERVE: tail of .loop/serve.log shows torch.OutOfMemoryError during _allocate_kv_cache_tensors inside profile_cudagraph_memory. 22.97 GiB already in use on the 24 GiB 4090 with only 253 MiB free when warmup tries to allocate another 1.53 GiB. Earlier defaults (max_len 8192 gpu_util 0.85 implicit max_num_seqs 256 default cuda graphs on bf16 KV) leave no headroom for the cuda graph capture step which briefly doubles peak memory. User reports same OOM persists.
- DECIDE: stop assuming a long context single user TUI needs 256 concurrent KV slots. Pick conservative defaults for a single 4090: max_num_seqs 4 max_model_len 4096 gpu_util 0.80 enforce eager kv_cache_dtype fp8 expandable_segments allocator. Each of these alone might not save 1.5 GiB but stacked they save several GiB.
- DEVIL:
  - Correctness: max_num_seqs 4 still allows the TUI to pipeline four parallel chats which is more than a single user needs. enforce eager skips cuda graphs which is the exact step that crashed in the trace so it eliminates the failure mode. fp8 kv cache halves KV memory and is supported on 4090 sm_89 since vllm 0.6. expandable_segments is exactly what the OOM message itself recommended for the 208 MiB reserved but unallocated fragmentation case. Lowering max_model_len from 8192 to 4096 halves the per slot KV reservation.
  - Scope: this is a runtime tuning fix not an architecture rewrite. It preserves the same model and same OpenAI compat surface so the rest of the stack does not see a behavior change. Knobs are still env overridable so a user with more VRAM or a multi user setup can raise them.
  - Priority: P1 user reported. The whole chat path is unusable while the engine cannot warm up.
- ACT: rewrote scripts/serve_qwen.sh defaults max_len 4096 gpu_util 0.80 max_seqs 4 kv_dtype fp8 eager 1. Added PYTORCH_CUDA_ALLOC_CONF expandable_segments True export. Added QWEN_SERVE_MAX_SEQS QWEN_SERVE_KV_DTYPE QWEN_SERVE_EAGER env knobs and updated header comments. Wired new flags max num seqs kv cache dtype enforce eager into the vllm serve invocation. Updated docs LOCAL_SERVE.md troubleshooting entry to reflect the new defaults and document the further dropdown order. bash dash n syntax check passes. Eight hundred forty seven passed.


## Loop 151 — /cd switches fs sandbox root for the session
- DECIDE: claude code lets the user retarget the workspace mid session. ml intern has a /cwd. Add /cd via a sentinel string the App intercepts to swap self.fs_cfg.
- DEVIL:
  - Correctness: the dispatcher returns a __CD__ prefixed string and the App detects and swaps. Mirrors the existing __RETRY__ sentinel pattern so there is one mechanism for tests to assert against without a live App. New FsConfig copies the same byte and entry limits so the per session quotas survive the swap. expanduser handles tilde paths and resolve follows symlinks. exists and is_dir checks fail loudly with a friendly message before the sentinel is emitted.
  - Scope: does not persist the new root across restarts. A user who quits and relaunches gets back the original root. That is the correct call because the launch path passes a root via cli args and a persisted cd would silently override it.
  - Priority: P5.
- ACT: _CD_SENTINEL constant. _render_cd helper. Dispatcher branch with an empty arg case that just shows the current root. App.on_input_submitted handles the new sentinel by rebuilding FsConfig with the same byte and entry limits and writing a cwd status line to the RichLog. SLASH_COMMANDS gains cd. Help text updated. Five tests cover empty arg showing cwd relative subdir returning sentinel absolute path returning sentinel missing path erroring and a file path erroring with not a directory. Eight hundred fifty two passed.


## Loop 152 — /grep --ext suffix filter
- DECIDE: ripgrep style language filter is the most missed grep feature versus claude code and copilot which both ship type filters. Add a slash grep TODO dash dash py form that filters hits to a single language.
- DEVIL:
  - Correctness: filter is applied post search in _render_grep so the underlying shell_tools.grep API stays minimal and other callers (other slash commands the bug finder agent) are unaffected. _split_grep_flags walks args once collecting positionals and the last seen long flag. Unrecognised single dash short flags are dropped so a typo does not silently match every line as a pattern. The empty positional case after flag stripping is caught with the same usage error message.
  - Scope: ripgrep also has a path glob filter and a count only mode. Those are separate slashes if the user asks for them.
  - Priority: P5.
- ACT: _render_grep grew an optional suffix kwarg. _split_grep_flags pure helper. Dispatcher uses the helper to split args. Help text grew the dash dash ext form. Seven new tests cover split extracting suffix split no suffix split with only pattern grep filtering to py grep filtering to md no filter keeping both and a flag without a pattern returning usage error. Eight hundred fifty nine passed.


## Loop 153 — multi-file pin in one slash call
- DECIDE: /pin landed in loop 145 but only accepts a single path. claude code at-mention syntax accepts multiple paths in one breath. Generalise /pin to take many paths.
- DEVIL:
  - Correctness: per file _render_pin call so a single bad path emits its own pin error line and the others still pin successfully. The marker block helper is idempotent so the marker still appears exactly once after multiple calls. Joining with newline keeps output readable in the RichLog.
  - Scope: keeps single arg behaviour identical because cmd.args of length one collapses the join to the same single line.
  - Priority: P5.
- ACT: dispatcher now iterates cmd.args calling _render_pin per path and joins with newline. Help text grew to slash pin path bracket path dot dot dot bracket. Three new tests cover three paths in one call partial failure where one good path and one escape path produce mixed output and an empty arg case still hitting the usage error. Eight hundred sixty two passed.


## Loop 154 - test the serve script and fix the multimodal cache OOM
- OBSERVE: user reran the serve script after loop 150's defaults landed and got `Available KV cache memory: -0.63 GiB` -> `ValueError: No available memory for the cache blocks`. Engine log shows model weights took 17.45 GiB and the encoder cache budget was 16384 image tokens with one image item profiled, even though we never serve images. With gpu_util 0.80 -> 19.2 GiB ceiling on the 24 GiB 4090 the encoder cache + activations pushed KV cache below zero. User's other complaint was that the serve script has no tests so regressions sneak in.
- DECIDE: two-part loop. First add a dry run mode to the script and write tests for the argv defaults so future edits cannot silently regress the OOM mitigations. Then bump defaults: gpu_util 0.92, max_num_seqs 1, max_model_len 2048, and add limit-mm-per-prompt JSON disabling image and video so the multimodal encoder cache stops eating headroom.
- DEVIL:
  - Correctness attack: the brace expansion of the JSON default would clash with bash brace expansion. Workaround: use the `${VAR+set}` indirection to detect set vs unset, then assign the literal JSON string in a separate branch. Verified by running `QWEN_SERVE_DRY_RUN=1 bash scripts/serve_qwen.sh` and grepping for the literal JSON.
  - Scope attack: maybe the real fix is to switch off vLLM's multimodal entirely with `--mm-encoder-tp-mode disabled`. That flag is not in vllm 0.7.x stable; `--limit-mm-per-prompt` is the documented knob and the engine still imports the vision tower but does not size the encoder cache when limits are zero.
  - Priority attack: tests for the serve script vs tests for the python code. The tests are mandatory because the user explicitly called out that we were not testing the script. Without them the gpu_util default could be flipped back to 0.80 by a future loop and we would not notice until the user retries the serve and OOMs again.
- ACT:
  - scripts/serve_qwen.sh: added `QWEN_SERVE_DRY_RUN=1` short-circuit that prints the vllm argv one line per arg and exits zero (skips venv install, pidfile, nohup). Built the argv into a `CMD` array so dry-run and the real exec share one source of truth. Added `QWEN_SERVE_LIMIT_MM` with `${VAR+set}` indirection and the default `{"image":0,"video":0}` so multimodal encoder cache is skipped. Set new defaults max_len 2048, gpu_util 0.92, max_seqs 1. Updated the env knob comment block to match.
  - tests/test_serve_qwen_sh.py: new file. 21 tests across two classes. TestServeScriptDefaults locks the OOM-safe defaults plus the model id, served-model alias, host/port, trust-remote-code, enforce-eager, and the PYTORCH_CUDA_ALLOC_CONF env line. TestServeScriptOverrides verifies each env knob actually flows through to the argv: max_seqs, max_len, gpu_util, kv_dtype, eager=0 dropping the flag, port, model, api_key, limit_mm empty dropping the flag, limit_mm custom JSON, and QWEN_SERVE_EXTRA token splitting. Helper `_run` wipes existing QWEN_SERVE_ env vars before each run so the tests are hermetic.
  - docs/LOCAL_SERVE.md: rewrote the OOM troubleshooting block to describe the new defaults and the multimodal disable mechanism. Removed the now obsolete max_seqs=1/max_len=2048 dropdown advice because those are the new defaults.
- VERIFY: `bash -n scripts/serve_qwen.sh` clean. `pytest tests/test_serve_qwen_sh.py -v` shows all 21 passing. Full suite: eight hundred eighty three passed one skipped.


## Loop 155 - tui and server cli get help and version flags
- OBSERVE: end to end smoke pass after loop 154 revealed that `qwen-coder-tui --help` hangs because Textual swallows argv and the same goes for `qwen-coder-mcp --help` which calls asyncio.run before parsing anything. user said end to end check was not made; this is exactly that gap.
- DECIDE: add argparse to both entry points before any heavy import. P3 because it is a real ux bug a user hits the first time they install the package and want to verify the binary is wired up.
- DEVIL:
  - Correctness: argparse SystemExit is the correct contract for --help; tests catch it explicitly. argparse parses argv before _build_app runs so the textual ImportError path is unaffected.
  - Scope: real symptom is that the user could not probe the binary. Real cause is no argparse layer. Adding argparse fixes both.
  - Priority: writing a streaming RichLog is sexier but the user explicitly called out missing end to end checks. Closing that hole first.
- ACT: tui.main and server.main both grew an argparse layer with --version backed by qwen_coder_mcp.__version__ and a description string. main now accepts an optional argv arg defaulting to sys.argv so tests can inject without monkeypatching sys.argv. seven new tests in tests/test_cli_entry_points.py: tui --help exits zero without calling _build_app (verified by monkeypatching _build_app to raise), tui --version prints the version, tui unknown flag errors. mirror three tests for server plus a help-does-not-call-asyncio.run check via monkeypatch on server.asyncio.run.
- VERIFY: eight hundred ninety passed one skipped. python -m qwen_coder_mcp.tui --version prints `qwen-coder-tui 0.1.0`. python -m qwen_coder_mcp.server --version prints `qwen-coder-mcp 0.1.0`. Both exit immediately.


## Loop 156 - repo hygiene plus health check hint surfacing
- OBSERVE: loop 155 commit accidentally added .agent/tui_history.jsonl (auto generated by the textual app on startup) because gitignore did not list it. Also _render_sysinfo dropped the actionable hint string the qwen client populates on connection refused, so the user just saw `backend unavailable: connection refused` with no suggestion of running scripts/serve_qwen.sh.
- DECIDE: gitignore the auto generated jsonl, untrack it via git rm --cached, surface the hint in /sysinfo, and add regression tests so a future loop cannot recommit the file or drop the hint.
- DEVIL:
  - Correctness: gitignore needs the exact relative path .agent/tui_history.jsonl; tested by reading the file and asserting the literal entry is present. The git ls-files regression test would skip in environments without git rather than fail spuriously. Hint rendering only adds an extra line when the client returns a hint key so the unhealthy-no-hint test stays green.
  - Scope: the real symptom is the user couldn't tell why the backend was unreachable. Real fix is rendering the hint that already exists in qwen_client.health_check. Done.
  - Priority: small but the user explicitly called out missing end to end checks; this closes one. P3.
- ACT:
  - .gitignore: appended `.agent/tui_history.jsonl` and `.agent/*.tmp`.
  - git rm --cached .agent/tui_history.jsonl (file kept on disk for the user, just untracked).
  - tui.py _render_sysinfo: when health check is not ok and a hint key is present, append `\n  hint:     <hint>` to the health line.
  - tests/test_tui.py: new test_health_check_hint_rendered in TestSysinfo class, asserting connection refused error plus hint substring plus literal "hint:" prefix all appear in the sysinfo output.
  - tests/test_repo_hygiene.py: new file. TestGitignore class checks the file exists and contains the tui_history entry and `.loop/` and `.venv-serve/`. TestNotTracked class runs git ls-files on the jsonl and fails if it ever shows up as tracked, with a remediation message in the assert.
- VERIFY: targeted run shows nine passed in zero point one three seconds. Full suite eight hundred ninety six passed one skipped (was eight hundred ninety; +6 = five new tests in this loop and one preexisting suite).


## Loop 157 - health check errors include the base_url
- OBSERVE: previous health_check messages said `connection refused: [Errno 111]` with no indication of which host or port the user's QwenClient was actually probing. A user running with QWEN_BASE_URL pointing at the wrong port would have no signal of the misconfiguration.
- DECIDE: include self.settings.base_url verbatim in every error string and put a copy-paste curl probe in the hint. P3 because it's a real ux improvement on the most common end to end failure mode.
- DEVIL:
  - Correctness: the base_url is set at QwenClient construction and is immutable; safe to read in any except branch. Existing TUI tests assert "connection refused" substring (lowercased) so they still pass.
  - Scope: real symptom is opaque error; real cause is that the error string omits the actionable detail. Fix is at the source.
  - Priority: P3, helps every misconfigured user immediately.
- ACT: qwen_client.health_check now formats the three exception branches with `at {self.settings.base_url}` and the ConnectError hint additionally suggests `curl -fsS {base_url}/models`. Three new tests in TestHealthCheck assert the base_url appears in the error message for ConnectError ConnectTimeout and a generic ReadError plus that the curl probe in the hint references the same url. eight hundred ninety nine passed.


## Loop 158 - fix the vllm validation error caused by the client/server max_tokens drift
- OBSERVE: user's serve log now shows `vllm.exceptions.VLLMValidationError: max_tokens=4096 cannot be greater than max_model_len=max_total_tokens=2048` repeating per chat request. After loop 154 cut the server max-model-len to 2048 to fix the OOM, the client side default QWEN_MAX_TOKENS stayed at 4096, so every request was DOA. Tests did not catch this because nothing asserted that the client default fits inside the server default.
- DECIDE: two fixes. First, lower the client default to 1024 so a fresh install works out of the box. Second, add a server_max_len field to Settings (env QWEN_SERVER_MAX_LEN, falling back to QWEN_SERVE_MAX_LEN) and clamp every chat request's max_tokens at submit time, accounting for the prompt token estimate plus 64 tokens of chat-template headroom. That way a stale .env or a serve-script bump still works.
- DEVIL:
  - Correctness: the clamp uses an estimator (4 chars per token) that mirrors tui.estimate_tokens. It is conservative for english and aggressive for code-heavy prompts; the 64-token headroom protects against off-by-template-overhead errors. Returns at least 1 so the request still goes through. The clamp test sends an 8000-char prompt and asserts the returned budget is below 100, demonstrating the clamp activates on long prompts.
  - Scope: real symptom is 400-from-vllm; real cause is no client-side knowledge of the server cap. Fix at both the default level and the clamp level so neither alone has to be perfect.
  - Priority: P1. This is the live error blocking the user from chatting. Goes ahead of every P3+ candidate.
- ACT:
  - config.py: Settings gains server_max_len. load_settings reads QWEN_SERVER_MAX_LEN with a QWEN_SERVE_MAX_LEN fallback so the server-side env knob doubles as the client cap. QWEN_MAX_TOKENS default dropped from 4096 to 1024.
  - qwen_client.py: new _resolve_max_tokens method on QwenClient. Both chat() and chat_stream() now route max_tokens through it. system_user() unchanged because it forwards max_tokens to chat().
  - tests/test_qwen_client.py: TestResolveMaxTokens with seven cases covering clamp against server cap, short-prompt budget retention, long-prompt eating most of the budget, explicit-request still clamped, server_max_len=0 disabling clamp, dict-message support, and end-to-end via chat() asserting the payload has the clamped value.
  - tests/test_config_settings.py: new file. TestDefaultSettings asserts max_tokens default fits inside server_max_len default and locks the specific values 1024 and 2048. TestSettingsOverrides covers QWEN_SERVER_MAX_LEN, the QWEN_SERVE_MAX_LEN fallback, explicit-overrides-fallback, QWEN_MAX_TOKENS override, and the frozen-dataclass invariant. autouse fixture wipes QWEN_ and LOOP_ env vars per test.
  - tests/test_tui.py, tests/test_chat_stream.py: every Settings(...) test fixture grew a `server_max_len=2048,` line so the constructor still accepts the old kwargs.
  - .env.example: QWEN_MAX_TOKENS=1024 plus a new QWEN_SERVER_MAX_LEN=2048 with comments explaining the relationship.
- VERIFY: nine hundred fourteen passed one skipped (was eight hundred ninety nine; +15 = seven clamp tests, eight config tests, plus mechanical Settings shim adjustments). Smoke: load_settings with no env shows max_tokens=1024 server_max_len=2048; _resolve_max_tokens on a short prompt returns 1024.


## Loop 159 -- live token streaming + CSS polish
**OBSERVE:** User: "no token streaming, TUI looks horrible." Confirmed `on_input_submitted` ran the SSE generator inline on the UI thread, so the screen never repainted until the full reply landed. CSS was four lines.
**DECIDE:** Move chat_turn_stream to a `run_worker(thread=True)` worker; surface progress via a `Static#stream` widget mounted between RichLog and Input; add a status bar; rewrite CSS with theme variables + region styling; add ctrl+l/ctrl+r bindings.
**DEVIL:**
- *Correctness*: race between worker thread and main UI on `self.history`? `_record_turn` only runs inside `_finalize_stream` which is dispatched via `call_from_thread`, so it's main-thread only. Worker only reads the prompt/yields chunks, never mutates history.
- *Scope*: is the *real* problem upstream (chat_turn_stream actually buffering)? No -- chat_turn_stream is a generator that yields per-chunk; the bug was demonstrably the sync handler. Verified by reading the function.
- *Priority*: streaming beats internet access in user-visible impact; missing live tokens is what the user mentioned first. Internet access gets loop 160.
**ACT:** Refactored on_input_submitted to delegate to `_start_streaming_turn` which spawns a worker thread; added `_on_stream_chunk` (per-chunk Static updater w/ tail-truncation) and `_finalize_stream` (clears Static, calls `_post_assistant`, refreshes status). Guarded against re-entry with `_streaming` flag. CSS rewritten (padded borders, accent focus, status dock, theme vars). Status line shows model/msg-count/total-tokens. New tests: `TestStreamingApp` (5), `TestTuiCss` (3).
**RESULT:** 922 passed (+8). Commit pending.

## Loop 160 -- model gets internet access via @web / @search
**OBSERVE:** User: "no internet access." `/search` and `/fetch` slash commands existed but the model itself had no inline way to pull web context, and the system prompt didn't tell the model these capabilities exist.
**DECIDE:** Extend `expand_at_mentions` with `@web:<url>` and `@search:<query>`; advertise both in `CODER_SYSTEM`; keep slash commands for direct user use.
**DEVIL:**
- *Correctness*: would `@web:https://x` regex-collide with the existing file mention regex (`@([\w./\-]+)` does NOT match colons, so `@web:url` is safe). Confirmed via `test_web_token_does_not_leak_into_file_path`.
- *Scope*: real fix is "model can request web access during a turn" -- but Qwen3.6 here is served via vLLM /v1/chat/completions with no tool-calling layer wired up. So inline `@`-attachments are the right scope; tool-calling is a separate, much bigger loop.
- *Priority*: still beats CSS micro-tweaks. User explicitly listed internet access second after streaming.
**ACT:** Added `_AT_WEB_RE`, `_AT_SEARCH_RE`. New keyword args `web_search_fn`/`web_fetch_fn` allow test injection. Body capped at 8KB by default; max 2 web fetches and 2 searches per prompt. Updated `CODER_SYSTEM` to advertise the four entry points. Help text in TUI now lists `@web:` and `@search:`. Header in attached block changed `attached files` -> `attached context`. Existing test updated; 8 new tests covering URL fetch, search render, fetch failure silence, byte cap, max_web cap, mixed file+web, regex non-collision, and prompt advertisement.
**RESULT:** 930 passed (+8). Commit pending.

## Loop 161 -- /search --max + live streaming indicator
**OBSERVE:** Loop 159 added a status footer but it didn't actually change while a turn was streaming -- so the user has no visual confirmation the worker is alive. Also `/search` was hardcoded to 5 results.
**DECIDE:** (1) Add `streaming: bool` kwarg to `_refresh_status` that prefixes a yellow "● streaming…" badge; call with `True` from `_start_streaming_turn`, default `False` everywhere else. (2) Parse `--max <n>` / `--max=<n>` in the search dispatcher, clamp to [1, 20].
**DEVIL:**
- *Correctness*: clamp upper bound? Yes, `min(20, ...)` so DDG isn't hammered.
- *Scope*: streaming indicator should ALSO show during a slow `/search` or `/fetch`? Those are sync and short; not worth the worker dance. Skip for now.
- *Priority*: footer indicator is the smallest leftover from loop 159. /search --max was already in next.md.
**ACT:** dispatcher branch rewritten with `--max` parser + integer/clamp errors. `_refresh_status` extended. `_start_streaming_turn` now refreshes status with streaming=True. 6 new tests covering `--max` (space, equals, clamp, invalid, missing query) + indicator.
**RESULT:** 936 passed (+6). Commit pending.

## Loop 162 -- @@<path> full-file mention + /grep --count
**OBSERVE:** From next.md: 8KB cap on @-mentions silently truncates; users had no count summary for grep.
**DECIDE:** Add `@@<path>` regex that expands BEFORE the regular `@<path>` and skips the truncation cap. Add `--count` / `-c` flag to `_split_grep_flags` (now returns 3-tuple) and a count-summary code path in `_render_grep`.
**DEVIL:**
- *Correctness*: would `@@foo` also match `@foo` due to the existing regex? Original regex used `r"@([\w./\-]+)"` which would match the second `@`. Added `(?<!@)` look-behind so single-`@` no longer matches inside `@@`.
- *Scope*: should `@@<path>` also dodge `FsConfig.max_read_bytes`? No -- that's the actual sandbox limit. "No truncation" means no client-side 8KB cap; the read is still bounded server-side.
- *Priority*: the truncation cap was actively misleading -- the model answered as if it had seen the whole file when it had only the first 8KB. This is correctness-adjacent.
**ACT:** Added `_AT_FULL_FILE_RE`, look-behind on `_AT_FILE_RE`. Process `@@` first, dedupe so `@@x and @x` only inlines once. `_split_grep_flags` now returns `(positionals, suffix, count_only)`; existing callers (1 dispatch site + 3 tests) updated. Help blocks updated. 8 new tests for both features.
**RESULT:** 944 passed (+8). Tree clean.

## Loop 163 -- Ctrl+S manual history save
**OBSERVE:** on_unmount saves history but won't fire if Textual crashes mid-turn -- a real failure mode (we saw it during the OOM debugging streak earlier).
**DECIDE:** Add Ctrl+S binding -> action_save_history that calls save_history_jsonl and surfaces status in the log.
**DEVIL:**
- *Correctness*: catch all exceptions so save failure doesn't crash the App. Done.
- *Scope*: should we also auto-save every N turns? Probably yes as a follow-up. Manual flush is the floor.
- *Priority*: data preservation (priority 1 in the priorities table). Justified.
**ACT:** Added BINDING ctrl+s -> save_history; method calls save_history_jsonl, prints "✓ saved N messages → <path>" or "save failed: ..." in the log. 3 new tests; existing tests untouched.
**RESULT:** 947 passed (+3).

## Loop 164 -- tool-calling agent loop, the model now invokes tools itself
**OBSERVE:** User: "the model still isn't invoking search features. Make it agentic." Loop 160 added user-side @web sugar; the model itself had no protocol.
**DECIDE:** Structured tool-calling layer. Model emits tool_call tags wrapping JSON; runtime parses, executes against a sandboxed registry, feeds tool_result back, repeats up to max_steps. Tools: web_search, web_fetch, fs_read, fs_list, grep, find. New /agent slash + /agent_on toggle. AUTO-FALLBACK: streamed reply containing tool_calls switches to agent transparently.
**DEVIL:**
- Correctness: auto-fallback pops the streamed user+assistant before re-running run_agent so history doesn't double-record. Verified.
- Scope: also handles fenced tool_call blocks; malformed JSON dropped silently.
- Priority: priority-1. Without an executor, model emitting tool_call tags would leak XML to the user.
**ACT:** New module agent_loop.py (zero Textual deps). DEFAULT_TOOLS registry, AgentEvent dataclass, parse/run/format helpers, run_agent generator. CODER_SYSTEM rewritten. New slash commands with sentinels. App _start_agent_turn worker + auto-fallback. 24 + 6 new tests.
**RESULT:** 977 passed (+30 from 947). Tree clean.

## Loop 165 — streaming agent mode + write tools (fs_write, apply_patch)

**Observe**: 977 tests green from loop 164. next.md flagged streaming + write
tools as highest impact. `run_agent` was using blocking `client.chat()` only,
so users saw nothing during agent thinking time. No way for the agent to edit
the workspace.

**Decide**: (1) add `stream=True` path to `run_agent` using `client.chat_stream`
when available, yielding `AgentEvent(kind="chunk", text=...)` events; rewire
`_start_agent_turn` to push chunks through `_on_stream_chunk` with a per-turn
buffer reset. (2) add `fs_write`, `apply_patch` to a new `WRITE_TOOLS` registry
and an `ALL_TOOLS` union; gate them behind a `/agent --write` flag, an
`agent_write_default` toggle, and pass-through to `run_agent(tools=...)`.

**Devil's advocate**:
- Correctness: chunk deltas joined into accumulator → matches non-agent
  streaming. Multi-step turns clear the live buffer at each `assistant`
  boundary so the next turn renders cleanly.
- Scope: write tools opt-in, default registry unchanged. Test confirms
  fs_write returns "unknown tool" when ALL_TOOLS isn't passed.
- Priority: both items were #1 and #2 in next.md and addressed user
  complaint about no streaming during agent invocations.

**Act**:
- agent_loop.py: added `stream` kwarg to `run_agent`, `WRITE_TOOLS`,
  `ALL_TOOLS`, `_tool_fs_write`, `_tool_apply_patch`. Updated
  `TOOL_PROTOCOL_DOC` to advertise the two write tools.
- tui.py: new `_AGENT_WRITE_SENTINEL`, `/agent --write` parser, two new
  slash commands `/agent_write_on` `/agent_write_off`, `agent_write_default`
  flag, chunk dispatch in `_start_agent_turn` (with `_reset_stream_buffer`
  helper), HELP_TEXT updated.
- tests: 7 new tests across streaming (3) + write tools (4).

**Result**: 984 passed, 1 skipped.

## Loop 166 — confirm hook for destructive tools

**Observe**: 984 green from loop 165. Write tools fired silently; users had
no audit trail of what the agent was changing. next.md flagged this #1.

**Decide**: add a `confirm: ConfirmFn | None` callback to `run_tool` and
`run_agent`. Define `DESTRUCTIVE_TOOLS = frozenset(WRITE_TOOLS.keys())`.
For destructive calls, consult the hook; if it returns False, synthesise
a `denied:` tool result the model can read. Read-only tools bypass the
hook entirely. TUI passes a `_confirm_write` hook that logs the call to
the agent status pane and returns True (audit + auto-approve). A future
loop will promote this to a blocking y/n modal.

**Devil's advocate**:
- Correctness: hook is consulted *only* for `DESTRUCTIVE_TOOLS`; test
  confirms a read tool with a denying hook still runs unhindered.
- Scope: real fix is a blocking modal -- but the audit trail addresses
  the user-visible gap today and the infrastructure for a modal lands
  with this commit (caller just needs to wire its own `confirm` to a
  threading.Event).
- Priority: #1 in next.md; nothing higher-impact than guarding a tool
  family that can rewrite the workspace.

**Act**: agent_loop.py +`ConfirmFn`, `always_allow`, `DESTRUCTIVE_TOOLS`,
confirm gate in `run_tool`, kwarg threaded through `run_agent`.
tui.py: `_confirm_write` audit hook in `_start_agent_turn`.
tests/test_agent_loop.py: `TestConfirmHook` with 5 tests covering
default-allow, deny, read-tool bypass, hook propagation through
`run_agent`, and the destructive-set invariant.

**Result**: 989 passed (+5), 1 skipped.

## Loop 167 — blocking y/n modal for destructive tool calls

**Observe**: 989 green from loop 166. confirm hook existed but TUI's
implementation always returned True with just an audit-log line. next.md
flagged the real modal as #1.

**Decide**: define a `_ConfirmScreen(ModalScreen[bool])` with `y`/`n`/`escape`
bindings, push it from the worker thread via `call_from_thread`, and block
on a `threading.Event` until the user dismisses (or 30s timeout fires
default-deny). Add `agent_confirm_writes: bool = True` flag plus
`/confirm_writes_on` / `/confirm_writes_off` slash commands.

**Devil's advocate**:
- Correctness: Three deny paths -- explicit n/escape, push-screen failure
  (handled in `_push_confirm`), and 30s timeout. None of them silently
  approve. Holder uses a list to stay mutable across the callback.
- Scope: Read tools still bypass the modal in `run_tool` (test from
  loop 166 still green). Toggle off path lets long autonomous sessions
  skip prompts -- callers opt into that explicitly.
- Priority: highest item in next.md. The infrastructure for `run_shell`
  (loop 168 candidate) needs this gate already in place.

**Act**:
- tui.py: import threading, ModalScreen; new `_ConfirmScreen` class with
  CSS + bindings; `_push_confirm` helper; `agent_confirm_writes` flag;
  `_confirm_write` reworked to do the blocking round-trip; new toggle
  branches in the sentinel handler; HELP_TEXT + SLASH_COMMANDS updated.
- tests: `TestAgentWriteAndConfirmDispatch` (7 tests) covering the
  --write/-w flags, empty-write usage, both toggle sentinels, the
  default flag values on a fresh App, HELP_TEXT, and completions.

**Result**: 996 passed (+7), 1 skipped.

## Loop 168 — run_shell tool (write-mode + confirm-gated)

**Observe**: 996 green from loop 167. The agent could read, search, fetch
URLs, write files, and apply patches but couldn't actually run anything --
no way for it to verify a fix with pytest/ruff/git diff before claiming
success. next.md flagged this as #1.

**Decide**: add a `run_shell(cmd, timeout=30, cwd=None)` tool that
delegates to the existing `shell_tools.run_shell` (already has denylist
+ sandbox + truncation + timeout). Register it in WRITE_TOOLS so it's
opt-in via `--write` and automatically gated by the confirm modal from
loop 167. TUI's confirm summary shows `$ <cmd>` so the user sees what
will run before approving. Update TOOL_PROTOCOL_DOC.

**Devil's advocate**:
- Correctness: `ShellError` (deny-list trip / cwd escape) is caught and
  becomes a `denied:` tool result. The model sees the failure and can
  adapt. Test confirms `rm -rf /` is blocked.
- Scope: this *is* the symptom and the cause -- the agent needs to run
  commands to be useful. Future loops can extend the denylist or add a
  per-command policy file.
- Priority: confirm modal landed last loop, so the safety story is
  in place. Without it, shipping run_shell would be reckless.

**Act**: agent_loop.py adds `_tool_run_shell` + WRITE_TOOLS entry +
TOOL_PROTOCOL_DOC line. tui.py extends `_confirm_write` summary with a
`$ cmd` branch. tests/test_agent_loop.py adds `TestRunShellTool` (6 tests):
registry membership, basic echo, empty cmd error, denylist trip, deny
through run_tool with confirm, and absence from DEFAULT_TOOLS.

**Result**: 1002 passed (+6), 1 skipped.

## Loop 169 — /agent --max N flag

**Observe**: 1002 green from loop 168. Hard 6-step cap meant any non-trivial
bug-hunt got cut off before the agent could settle. next.md flagged this #1.

**Decide**: extend the slash parser to accept `--max N` (and `--max=N`),
encode the value into the sentinel body as a leading `--max=N\n` line,
decode it in the App handler via a new module-level `_decode_agent_body`
helper, and pass it as the `max_steps` kwarg to `run_agent`. Range-check
1..50 to guard against runaway loops.

**Devil's advocate**:
- Correctness: encoder/decoder are inverses; tested with no-max, max
  only, and both flag orders (--write before --max and after).
- Scope: addresses the cause -- arbitrary cap was too low. Range check
  prevents the inverse mistake (cap too high).
- Priority: #1 in next.md and a precondition for autonomous bug-hunts.

**Act**: tui.py adds flag parser loop in `dispatch_slash`, module-level
`_decode_agent_body` (so tests can import), `max_steps` kwarg through
`_start_agent_turn`, status badge shows the override, HELP_TEXT updated.
9 new tests in `TestAgentMaxFlag`.

**Result**: 1011 passed (+9), 1 skipped.

## Loop 170 — /tools slash command

**Observe**: 1011 green from loop 169. Users had no quick way to see what
the agent could do (had to read source). Toggling `/agent_write_on` was
opaque about what new capability that unlocked.

**Decide**: add a `/tools` slash command that prints read-only vs. write
registries plus the destructive set (which calls hit the modal). Pure
read-only operation, no state changes.

**Devil's advocate**:
- Correctness: pulls names directly from the live `agent_loop.*` dicts,
  so it stays accurate as we add tools.
- Scope: trivial discoverability fix; doesn't address bigger items but
  is a low-risk win that compounds with future tool additions.
- Priority: was #5 in next.md but addressing it now means future loops
  inherit a discoverability path for free.

**Act**: +/tools branch in dispatch_slash, +entry in SLASH_COMMANDS,
+HELP_TEXT line, 3 tests.

**Result**: 1014 passed (+3), 1 skipped.

## Loop 171 — raise default context window to 64k with chunked prefill + CPU swap

**Observe.** User: "There must be some way of getting past that low context limit. The model supports up to 294K. Full GPU utilisation is allowed." Defaults at HEAD `b08ea65`: `MAX_LEN=2048`, `GPU_UTIL=0.92`, no `--swap-space`, no chunked prefill. That left almost the entire usable long-context envelope on the table.

**Orient.** KV-cache math on a 24 GB 4090 / int4 27B / fp8 KV: weights ≈ 14 GiB; per-token KV at fp8 ≈ 128 KiB. 65 536 tokens → ~8 GiB KV → fits with `gpu_util=0.95` and ~2 GiB activation headroom. 131 072 tokens needs ~16 GiB → tight but possible. The real prefill-OOM blocker on long prompts is not steady-state KV but the single giant forward pass at prefill time → fix with `--enable-chunked-prefill --max-num-batched-tokens 4096`. `--swap-space` does NOT extend a single sequence's window, it just lets preempted blocks spill to RAM, but is still cheap insurance.

**Decide.** Bump defaults: `MAX_LEN 2048→65536`, `GPU_UTIL 0.92→0.95`. Always pass `--swap-space 16` (overridable via `QWEN_SERVE_SWAP_SPACE`). Always pass `--enable-chunked-prefill --max-num-batched-tokens 4096` (toggle via `QWEN_SERVE_CHUNKED_PREFILL`, batched-cap via `QWEN_SERVE_MAX_BATCHED`). Mirror the headroom in `config.py`: `server_max_len 2048→65536`, `max_tokens 1024→8192`. Update `tests/test_serve_qwen_sh.py` pin tests + add 5 new ones (swap-space override, chunked-prefill disable, max-batched override, long-context override, default chunked flags). Update `tests/test_config_settings.py`. Update `docs/LOCAL_SERVE.md` troubleshooting section.

**Devil.**
- *Correctness:* Will `--enable-chunked-prefill` break anything? It's been stable in vLLM since 0.6.x, default-on in newer releases. The `--max-num-seqs=1` we already set is compatible. ✅
- *Scope:* Is 64k actually safe with int4-27B on 24 GB? Yes — 14 + 8 + ~1.5 GB activation < 0.95 × 24 GiB ≈ 22.8 GiB. Leaves margin. 128k would be borderline; we leave it as an opt-in via `QWEN_SERVE_MAX_LEN`. ✅
- *Priority:* Is there a higher-impact fix? No — long context is the user's explicit ask and it unblocks every downstream agent feature (multi-file diffs, large repos, long agent transcripts). The token-meter and auto-checkpoint candidates from loop 170's `next.md` are real but smaller in impact. ✅

**Act.** Edited `scripts/serve_qwen.sh` (defaults + 3 new env knobs + always-on `--swap-space` + conditional chunked-prefill flags). Edited `src/qwen_coder_mcp/config.py` (raised both defaults). Edited tests to match new pins; added 5 new tests covering swap-space override, chunked-prefill disable, max-batched override, long-context override, and default chunked flags. Edited `docs/LOCAL_SERVE.md` troubleshooting + added a "Going beyond 64k context" subsection.

**Verify.** `pytest -x -q` → ~1k tests passing, 1 skipped. New defaults are pinned by the serve test harness so any silent regression breaks the suite.

## Loop 172 — align streaming tail to whitespace boundary

**Observe.** At HEAD `4dc3053` (1019 passed) the streaming widget cut the visible tail with raw `accum[-2000:]`. As new chunks arrived the cut point drifted by 1-2 chars per redraw, so the head of the tail flickered mid-word: `…ent loop is a finite state ma|chine that…`. Distracting on long replies.

**Orient.** The other open candidates: token-meter accounting for tool_result body bytes (already covered — feedback gets folded into history as a user msg, so /tokens already counts it), auto-checkpoint to `.agent/agent_state.json` (real but bigger), live vLLM tool_call protocol smoke test (needs a running server, opt-in only). Streaming-tail alignment is small, isolated, and ships a visible UX win.

**Decide.** Extract `render_stream_tail(accum, budget=2000) -> str` as a module-level pure function. When `len(accum) > budget`, snap the cut forward to the next whitespace within the next 64 chars; if no whitespace in window, fall back to the raw cut. Replace the `accum[-2000:]` slice in `_on_stream_chunk` with a call to the new helper.

**Devil.**
- *Correctness:* What if the entire tail is one giant token (e.g. base64)? Test `test_no_whitespace_in_window_falls_back_to_raw_cut` covers that path. ✅
- *Scope:* Is this masking a deeper symptom? No — the existing tail is fundamentally byte-budget-driven; aligning to whitespace is a small visual polish on a working primitive, not a workaround. ✅
- *Priority:* Higher-impact open items? Token meter is already correct (results land in history before /tokens runs). Auto-checkpoint is bigger but strictly opt-in design by nothing's lost without it today. Streaming polish is small and shippable in one loop. ✅ 

**Act.** Added `render_stream_tail` next to `estimate_tokens` in `tui.py`. Updated `_on_stream_chunk` to call it. Added `tests/test_render_stream_tail.py` with 7 cases: short input, zero/negative budget, exact boundary, normal snap, no-whitespace fallback, and bounded-snap-window fallback.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped. New helper has 100% branch coverage from the new tests.

## Loop 173 — auto-checkpoint agent state to .agent/agent_state.json

**Observe.** At HEAD `e05a661` an agent multi-step run that crashed (or got Ctrl-C'd) lost its entire transcript: the TUI only persists history on `/save`. So a 5-step run that completed 4 tool calls just before a network blip wasted the work — and worse, the user has no way to resume from where it died.

**Orient.** The run_agent driver already mutates `history` in place after every tool round-trip. All we need is a callback hook fired right after `history.append(feedback)`, plus pure helpers to (de)serialize history to JSON safely. Writing the checkpoint atomically (tmp + os.replace) means a crash mid-write can't corrupt the file.

**Decide.** Add `serialize_agent_state` / `deserialize_agent_state` (round-trip helpers, version-stamped), `save_agent_checkpoint` (atomic write, parent-dir-creating), `load_agent_checkpoint` (never raises — corrupt file ⇒ empty list). Add `checkpoint: Callable[[list[ChatMessage], int], None] | None` kwarg to `run_agent`; call it after each tool round-trip; swallow exceptions so a flaky disk never aborts an in-flight turn. Wire a default `_agent_checkpoint` in the TUI that writes to `.agent/agent_state.json` under the sandbox root.

**Devil.**
- *Correctness:* Atomic write — the `.tmp` sibling + `os.replace` pattern is portable across POSIX and Windows; tested by `test_save_is_atomic_no_tmp_left_behind`. ✅
- *Scope:* Why no automatic *resume*? Because resume needs a UX decision (which session? merge or replace?) and we shouldn't bake that in without user input. The serialized state is recoverable manually via `load_agent_checkpoint` for now; a future loop can add `/resume`. ✅
- *Priority:* Is checkpointing more important than the live vLLM tool_call smoke test? Yes — the smoke test needs a running server (no CI value, opt-in only) while checkpointing protects every real run. ✅

**Act.** Added 4 new helpers + 1 kwarg in `agent_loop.py`. Wired `_agent_checkpoint` into `_start_agent_turn` in `tui.py`. Created `tests/test_agent_checkpoint.py` with 12 cases: round-trip, version, malformed-skip, save+load, parent-dir creation, atomic-write, missing path, corrupt JSON, non-object JSON, hook fires once per tool step, hook failure doesn't abort, hook silent when no tools called.

**Verify.** `pytest -x -q` → ~1k passing, 1 skipped.

## Loop 174 — /resume slash command rehydrates agent_state.json into history

**Observe.** Loop 173 added auto-checkpointing but no way to load it back. A user whose TUI crashed mid-multi-step run still couldn't resume — the JSON sat there unread. The loop_log explicitly flagged this as the natural follow-up.

**Orient.** dispatch_slash already takes `history` as a mutable list and the existing `/clear` mutates it in place. So `/resume` can do the same: load checkpoint, clear, extend. We must mutate the existing list (not rebind) because the TUI App holds a reference to it. Existing `load_agent_checkpoint` already returns `[]` on missing/corrupt — perfect signal for the "no checkpoint" branch.

**Decide.** Add `/resume` to SLASH_COMMANDS + HELP_TEXT. New branch in dispatch_slash that: reads `fs_cfg.root / .agent / agent_state.json`, replaces history in-place, returns a status string with role counts + last-assistant-snippet (truncated to 200 chars). Add `_role_counts` helper near `_last_assistant_reply`. No confirm modal needed — `/resume` is reversible by `/clear`, and gating it behind a modal would just make recovery slower.

**Devil.**
- *Correctness:* If the checkpoint JSON has no `assistant` role, `next(...)` returns `""` and we omit the snippet line. Tested. ✅
- *Scope:* Should `/resume` also reload the system prompt? It does — `load_agent_checkpoint` includes all roles. The user could overwrite their system prompt this way, but that's the point of "resume the prior session". ✅
- *Priority:* Bigger items? `/diff` and per-step latency are nice-to-haves; resume completes a feature shipped in loop 173 that's currently dead-ended. ✅

**Act.** New `_role_counts` helper. `/resume` registered in SLASH_COMMANDS + HELP_TEXT. Branch added to dispatch_slash. 8 new tests in `tests/test_resume_slash.py` covering: registration, tab completion, parse, missing file, happy-path round-trip, in-place mutation contract, corrupt file recovery, and no-assistant-message snippet handling.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 175 — per-tool latency in agent transcript

**Observe.** At HEAD `b0281e3` the agent transcript showed `→ tool fs_read path='x.py'` then `← fs_read <head line>` with no timing. When a tool call took 4 seconds vs 40 ms users had no signal which one was the bottleneck — the most common debugging question for agent runs.

**Orient.** Calls are sequential in `run_agent` (the inner `for call in calls:` loop blocks until the result lands), so tracking a single "last call started at" timestamp per worker thread is sufficient. Format must be compact: ms for sub-second, decimal seconds up to a minute, mm:ss after.

**Decide.** Add `format_tool_latency(elapsed_s) -> str` next to `render_stream_tail`. Wire it into the `runner` loop: stamp `monotonic()` on `tool_call` events; compute elapsed and prepend to the `← {tool}` status line on `tool_result`. Negative inputs return `(?)` rather than raise so wall-clock weirdness can't crash the UI.

**Devil.**
- *Correctness:* What if an exception fires between `tool_call` and `tool_result`? `tool_started_at` stays set, but `run_agent` would have aborted the iterator entirely so we never hit a stray `tool_result`. ✅
- *Scope:* Should we also track time-to-first-token for the model? That's a different concern (model-side latency vs tool-side latency); ship the tool one first since tools are usually the long pole. ✅
- *Priority:* Higher impact open items? `/diff` and the live vLLM smoke test are both bigger investments. This shipped a visible UX win in one isolated edit. ✅

**Act.** New `format_tool_latency` module-level helper. Updated the `runner` closure in `_start_agent_turn` to stamp+compute. New `tests/test_format_tool_latency.py` with 10 cases covering: zero, sub-second, exactly-1s, decimal-precision, just-under-60s, exactly-60s, padded mm:ss, multi-minute, negative-as-fallback.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 176 — push tool latency into AgentEvent so non-TUI consumers benefit too

**Observe.** Loop 175 added per-tool latency rendering, but the timing logic lived in `tui.py`'s runner closure. Any future non-TUI consumer (the MCP server, a remote agent driver, log replay) would need to re-implement the same wall-clock bookkeeping. That's exactly the kind of "fragile assumption that breaks the next person" item in the priority list.

**Orient.** The natural home for tool latency is on `AgentEvent` itself — it's already the contract between `run_agent` and any consumer. Adding a `latency_s: float | None` field is a backward-compatible additive change (default None) and the timing measurement happens at the only place it can be authoritative: inside `run_tool`'s call site in `run_agent`.

**Decide.** Add `latency_s: float | None = None` to `AgentEvent`. Stamp `time.monotonic()` either side of `run_tool`; populate the field on the emitted `tool_result` event. Update TUI's runner to prefer `ev.latency_s`, falling back to its existing wall-clock bookkeeping when the field is None (keeps the loop-175 helper meaningful for any old client/stub still emitting bare events).

**Devil.**
- *Correctness:* `time.monotonic()` is the right clock — it never goes backwards under NTP. ✅
- *Scope:* Should we time the model turn too? That'd be a bigger change (track first-chunk in `chat_stream`); leave it for the next loop since this one already has tests + commit ready. ✅
- *Priority:* Higher impact than `/diff`? Yes — `/diff` is a one-off polish; this is a contract change that compounds across every future consumer. The MCP server already receives these events via the streaming endpoint, so as soon as a client renders them it gets latency for free. ✅

**Act.** Added `import time` to agent_loop. Extended `AgentEvent` dataclass with `latency_s` field + docstring noting `monotonic`-based semantics. Stamped before/after `run_tool` and threaded into the emitted `tool_result`. Updated TUI runner to prefer `ev.latency_s`. New `tests/test_agent_event_latency.py` with 4 cases: default None, tool_result carries field, other kinds keep None, latency reflects actual sleep duration in a custom slow tool.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 177 — emit a summary event with tool count + total wall time

**Observe.** Per-tool latency (loop 175) and per-event latency on the AgentEvent (loop 176) gave us all the raw timings, but a user watching a long agent run still couldn't see "how much total time did tools eat?" without doing the addition themselves. The loop_log already flagged this as the next low-risk continuation.

**Orient.** The cleanest place to compute the aggregate is inside `run_agent` itself — it's the only point that sees every tool round-trip. Emit a synthetic `AgentEvent(kind="summary", text=..., latency_s=total)` exactly once, right before the terminating `final`/`limit` event. Consumers that don't care can ignore the event; the TUI renders it as a dim status line.

**Decide.** Track `tool_count` and `tool_time_total` across the loop. Emit `summary` in both terminal branches (no-tool path and max-steps path). Update three existing tests in `test_agent_loop.py` whose `kinds == [...]` assertions enumerate the event sequence. Update loop-176's "other kinds leave latency None" test to allow `summary` (alongside `tool_result`) to carry timing data.

**Devil.**
- *Correctness:* What about exception paths? `chat()` raising emits an early `final` event and returns — no summary in that branch. That's fine; the summary is informational and the user already sees the error. ✅
- *Scope:* Should the summary also count model turns / tokens? That'd duplicate the `/tokens` slash command. Keep it focused on tool wall time. ✅
- *Priority:* Higher-impact items? `/checkpoints` listing and the live vLLM smoke test exist but neither moves a metric. The summary completes a three-loop arc on agent observability with one isolated diff. ✅

**Act.** Added `tool_count`, `tool_time_total`, and `_summary_text()` in `run_agent`. Emit `summary` in the no-tool branch and the max-steps branch. Updated the docstring's event-sequence list. Wired the TUI's runner to render `[dim]· {ev.text}[/dim]` on `summary`. Updated 3 existing assertions in `test_agent_loop.py` and 1 in `test_agent_event_latency.py`. Added `tests/test_agent_summary_event.py` with 6 cases covering: position before final, zero-tools text, singular vs plural phrasing, running total across multiple tools, position before limit at max_steps, exactly-once emission.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 178 — emit time-to-first-token event on streaming model turns

**Observe.** With per-tool latency (loop 175/176) and tool aggregate (loop 177) both shipped, the only major timing dimension still hidden was model-side latency. On a long prompt the model can take 3–10 seconds to emit its first token; users had no way to distinguish "model is slow to start replying" from "tool ran for 8 seconds" in transcript review. Initial plan was to surface the loop 177 summary in the MCP server response, but inspection of `serve/` (empty) and `server.py` there's no agent-streaming MCP endpoint to plumb it into  showed that candidate was misframed in `next.md`. Pivoted to the next candidate: TTFT.

**Orient.** Cleanest place for TTFT is right around the existing `chat_stream` loop in `run_agent`: stamp `time.monotonic()` before the iterator opens, emit `AgentEvent(kind="ttft", latency_s=...)` exactly once when the first non-empty chunk arrives. Skipping empty chunks matters — some clients yield `""` keep-alives. Non-streaming path emits no ttft (the round-trip is opaque).

**Decide.** Add `ttft` to the AgentEvent kind doc-list and to the streaming branch only. TUI: render `[dim]· first token in 0.4s[/dim]` on the new event. Tests: 6 cases — emitted before first chunk, exactly one per model turn (so two on a tool round-trip), reflects induced first-chunk delay, blocking client emits none, `stream=False` emits none, all-empty chunks emit none.

**Devil.**
- *Correctness:* What if the model emits a chunk then errors? TTFT still fired before the error; subsequent retry would re-stamp on the next iteration. Fine — TTFT is per-turn, not per-stream-attempt. ✅
- *Scope:* Should we include the *prompt token count* alongside TTFT? That'd require tokenizing the history (heavy) or adding a server hook. Defer; the current crude estimator in `tui.py` already serves /tokens. ✅
- *Priority:* Higher impact than `/checkpoints`? Yes — TTFT closes the timing-coverage triad (tool-side, aggregate, model-side), which is the most-asked perf question. `/checkpoints` is UI sugar on a feature only one user (the dev) has a use case for. ✅

**Act.** New 5-line block in `run_agent`'s streaming branch: ttft sentinel + monotonic stamp + first-non-empty-chunk emit. Doc-list updated. TUI runner gets a `ttft` branch rendering via `format_tool_latency`. New `tests/test_agent_ttft.py` with 6 cases as above. Updated docstring's event-list ordering.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 179 — rotating timestamped checkpoints (keep last N)

**Observe.** Loops 173/174 added single-file `.agent/agent_state.json` checkpointing + `/resume`. Fragility: every save overwrites the only good copy. A buggy run that completes 4 tool calls successfully then commits a corrupt prompt to history just trashed the previous state — there's no way to roll back.

**Orient.** Same pattern unix logrotate solved decades ago: keep the latest at a fixed path, push prior versions into a sibling directory with a sortable timestamp suffix, prune beyond a cap. Microsecond-precision UTC timestamps mean lexicographic sort = chronological sort, and ~1 microsecond resolution is plenty for an agent-step cadence.

**Decide.** Add `rotate_agent_checkpoints(primary, history, *, keep=5)` — writes primary, writes a timestamped sibling under `checkpoints/`, prunes oldest beyond `keep`. Add `list_agent_checkpoints(primary)` for future `/checkpoints` UIs. Wire the TUI's `_agent_checkpoint` to use rotation with `keep=5`. `keep <= 0` retains everything.

**Devil.**
- *Correctness:* Same-millisecond rotations? UTC timestamp uses microsecond precision (`%f` in strftime). Tests sleep 1ms between writes to avoid filesystem-level overlap. ✅
- *Scope:* Should we tag rotations by step number too? No — wall-clock is sufficient for "find the last good state" UX, and step numbers across runs collide. ✅
- *Priority:* Higher impact than `/checkpoints` slash command? Yes — `/checkpoints` is the *consumer*; the rotation infrastructure is a prerequisite. Without rotation there's only ever one snapshot to list. ✅

**Act.** Added `rotate_agent_checkpoints` and `list_agent_checkpoints` next to `save_agent_checkpoint`. Updated TUI's `_agent_checkpoint` closure to call `rotate_agent_checkpoints(target, hist, keep=5)`. New `tests/test_rotate_checkpoints.py` with 9 cases: writes primary+snapshot, primary tracks latest, keeps last N, `keep=0` retains all, prunes oldest first, lexicographic-sort matches chronological, missing-dir returns empty, primary-only returns empty, foreign-stem files filtered out.

**Verify.** `pytest -x -q` → ~1k passed, 1 skipped.

## Loop 180 — `/checkpoints` slash command (list / load / prune)

**Observe.** Loop 179 added rotation infrastructure but no UI surface — `.agent/checkpoints/` quietly fills up and there's no way to inspect or roll back from inside the TUI without dropping to a shell. `/resume` only knows about the primary file.

**Orient.** Three things a user needs from rotated state: see what's there, load any specific snapshot, and prune them down. A single command with subcommands matches the existing `/git`, `/agent_*` style.

**Decide.** Add `/checkpoints` with three forms: bare = list (1-indexed, oldest-first, with mtime + size), `load N` = rehydrate snapshot N into history in-place, `prune K` = delete all but newest K. Pure renderer `_format_checkpoint_listing` so listing is unit-testable without booting the App.

**Devil.**
- *Correctness:* In-place mutation matters — TUI's App holds the same `history` reference. Test `test_load_replaces_history_in_place` pins `id(history)` before/after. ✅
- *Scope:* Why 1-indexed-oldest-first? Matches `_format_checkpoint_listing`'s output users will read with their eyes; reversed indexing would force mental arithmetic. ✅
- *Priority:* `/lat` (timing breakdown) is also queued but doesn't have the gravity of "I just need to roll back to the last good state". ✅

**Act.** New `_format_checkpoint_listing(snapshots) -> str` next to `_role_counts`. Dispatch branch for `name == "checkpoints"` after `/resume`, handles bare/load/prune with explicit error rendering for missing args, non-integer indices, out-of-range, and unknown subcommands. `/checkpoints` registered in `SLASH_COMMANDS` and `HELP_TEXT`.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped. New file `tests/test_checkpoints_slash.py` with 17 cases across 6 test classes.

## Loop 181 — `/resume` falls back to newest rotation when primary missing/corrupt

**Observe.** Loop 179 added rotation, loop 180 surfaced it via `/checkpoints`. But `/resume` still only reads the single primary file. If the primary is truncated, corrupt, or accidentally deleted, `/resume` says "no checkpoint found" and the user has to manually run `/checkpoints load N`.

**Orient.** The recovery path should be transparent: if the primary is unreadable, walk the rotations from newest to oldest and use the first one that deserialises. This matches how unix-y tools handle log rotation — read whatever's most recent and valid.

**Decide.** Add `load_latest_checkpoint(primary) -> (history, source_path | None)` next to `load_agent_checkpoint`. Wire `/resume` to call it and report which file the data actually came from in the status line so the user knows when they've been silently bumped to a rotation.

**Devil.**
- *Correctness:* What if a rotation deserialises but is empty? Empty-list = "nothing useful" — keep falling back. Test `test_empty_primary_falls_back` pins this. ✅
- *Scope:* Should empty rotations be deleted as they're encountered? No — that's policy creep into a read helper. Keep the helper non-destructive; let `/checkpoints prune` handle cleanup. ✅
- *Priority:* `/lat` was the listed priority but `/resume`-while-broken is the recovery path users hit when something goes wrong, which is exactly when they need it most. ✅

**Act.** Added `load_latest_checkpoint` to `agent_loop.py`. Updated `/resume` dispatcher branch to use it; status line reports the source filename so fallbacks are visible. Seven new tests in `test_load_latest_checkpoint.py` covering primary-present, fall-through-to-newest, all-empty, corrupt-primary, corrupt-newest-rotation, all-corrupt, and empty-primary.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped. Existing `test_resume_slash.py` still green.

## Loop 182 — `/lat` slash command (last-turn timing breakdown)

**Observe.** Loops 175-178 emitted `tool_result.latency_s`, `summary`, and `ttft` events. The TUI rendered them as ephemeral status lines that scrolled away the moment the next chunk landed. There was no way after the fact to ask "what just happened, and where did the time go?"

**Orient.** A single command, `/lat`, that prints the most recent turn's profile is a high-leverage observability primitive — it costs ~50 lines of code, makes performance regressions discoverable, and uses data that's already being computed. The natural shape: capture events into a `TurnProfile` dataclass during the runner's event loop, store the latest one on the App, render with a pure formatter.

**Decide.** Add `TurnProfile` (started_at, ended_at, ttft_s, tool_calls, summary_text, summary_total_s) and `format_turn_profile()` next to `format_tool_latency`. App stores `last_turn_profile`. Runner builds the profile alongside the existing status-line rendering. `dispatch_slash` gains an `app=None` kwarg so the `/lat` branch can read the App's attribute; falls back to "no agent turn has run yet" gracefully.

**Devil.**
- *Correctness:* What if a tool_result arrives without a corresponding tool_call (replay/race)? `pending_tool_name` falls back to `ev.tool or "?"` so the row still renders. ✅
- *Scope:* Should `/lat` show *all* recent turns, not just the last? No — that's a different feature (history). Keep `/lat` to one turn, defer the multi-turn view if requested. ✅
- *Priority:* `/agent --resume` is also queued. But `/lat` directly leverages four loops of observability work that's currently invisible after the status line scrolls. Higher leverage. ✅

**Act.** New `TurnProfile` dataclass + `format_turn_profile` in `tui.py`. App `__init__` initialises `self.last_turn_profile = None`. Runner builds the profile in-place: `pending_tool_name` on `tool_call`, latency tuple appended on `tool_result`, summary fields on `summary`, ttft (only the first one per turn) on `ttft`. `app=None` kwarg on `dispatch_slash`; App's submit handler passes `app=self`. `/lat` registered in `SLASH_COMMANDS` and `HELP_TEXT`. Twelve tests in `test_lat_slash.py` covering the renderer (None, total/ttft, no tools, numbered tools, unknown latency, summary, unfinished turn) and the dispatcher (no-app, populated app, app missing attribute, registry wiring).

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped.

## Loop 183 — boot-time checkpoint hint (`render_checkpoint_hint`)

**Observe.** Loop 181 added `load_latest_checkpoint` for `/resume`'s recovery path. But discovery is the issue — after a crash, a user has to *know* `/resume` exists. The TUI boot loads JSONL history and stays silent if it's empty, even when `.agent/agent_state.json` has perfectly good state.

**Orient.** Two storage layers are now in play: chat-history JSONL (every turn, used by both plain chat and agent) and agent checkpoints (per-tool-call, agent only). Auto-loading the checkpoint into chat history would silently cross those layers — wrong. The right answer is to *surface* the checkpoint's existence so `/resume` is one keystroke away.

**Decide.** Add `render_checkpoint_hint(fs_cfg) -> str | None` — pure helper that returns a one-line hint when a usable checkpoint exists, `None` otherwise. Wire it into `on_mount` after the JSONL restore path: when `prior` is empty, log the hint if any.

**Devil.**
- *Correctness:* What if both JSONL and checkpoint exist? The hint only fires when JSONL is empty, so users who already have history aren't confused. ✅
- *Scope:* Should the boot log show recent rotations too? No — that's `/checkpoints`'s job; we don't want to nag at every boot. One discoverability hint, conditional on real recovery value. ✅
- *Priority:* `/agent --resume` is also queued. But this fixes a specific UX hole — silent recovery state — at minimal cost (~30 lines + 5 tests). The helper is reusable for future boot integrations. ✅

**Act.** New pure helper `render_checkpoint_hint` in `tui.py` next to `_role_counts`. Boot path in `on_mount` calls it when `prior` is empty and writes the returned line to the log. Five tests in `test_checkpoint_hint.py` covering: no checkpoint, primary present, rotation-only fallback, empty primary, corrupt primary.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped.

## Loop 184 — `QWEN_AGENT_ROTATION_KEEP` env-var override

**Observe.** Loop 179 hardcoded `keep=5` in the TUI's `_agent_checkpoint` callback. For long sessions or for users who want forensic history of every step, 5 is too low; for users on tight disk budgets it might be too high. Magic number with no escape hatch.

**Orient.** Standard config-knob pattern — read env var at use site, fall back to a documented default constant, defensively parse. The helper is independently testable; the use site is one line.

**Decide.** Add `DEFAULT_ROTATION_KEEP = 5` and `resolve_rotation_keep(env=None)` to `tui.py`. Read `QWEN_AGENT_ROTATION_KEEP` from env. Empty/missing/unparseable → default. Negative → clamped to 0 (which `rotate_agent_checkpoints` already treats as "retain everything"). Wire `_agent_checkpoint` to call `resolve_rotation_keep()` instead of literal `5`.

**Devil.**
- *Correctness:* Should `resolve_rotation_keep` cache the value? No — env vars can change at runtime (testing, hot config). Each call reads fresh; the cost is negligible. ✅
- *Scope:* Why not full config object? Premature — one knob doesn't need a class. If 3+ rotation knobs appear, refactor then. ✅
- *Priority:* Removes a magic number and unblocks a class of users (long-session, audit). Lower-impact than `/agent --resume` but lower-cost too — perfect breather loop after `/lat`'s heavier surface. ✅

**Act.** New helper + constant in `tui.py` between `format_turn_profile` and `render_checkpoint_hint`. Updated TUI `_agent_checkpoint` call. Ten tests in `test_resolve_rotation_keep.py` covering: unset → default, default = 5 pin, empty/whitespace → default, valid int, 0 = retain all, negative clamped, garbage → default, float string → default, env=None reads `os.environ`.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped.

## Loop 185 — `docs/AGENT_CHECKPOINTS.md` covers loops 173, 179-184

**Observe.** Six loops worth of checkpoint work — single-file save (173), `/resume` (174), rotation (179), `/checkpoints` (180), fallback recovery (181), boot hint (183), env-var override (184) — and zero documentation. A user landing on the repo can't discover any of it without reading the source.

**Orient.** The work is feature-complete enough to merit its own doc page. Two storage layers, four slash commands, one env var — that's a half-page table away from being self-explanatory. README needs a one-line link so it's findable.

**Decide.** Create `docs/AGENT_CHECKPOINTS.md` with: storage-layer comparison table, recovery flow numbered list, slash-command table, env-var table, rationale for keeping the two layers separate, file-format note. Add a sentence pointing at it from README.

**Devil.**
- *Correctness:* Did I miss any commands? `/resume`, `/checkpoints`, `/checkpoints load`, `/checkpoints prune`, `/lat`. Cross-checked SLASH_COMMANDS — that's the full set. ✅
- *Scope:* Should this go in LOCAL_SERVE.md instead of a new file? No — LOCAL_SERVE.md is about vLLM/llama.cpp serving; agent checkpoints are an orthogonal concern with their own audience. ✅
- *Priority:* `/agent --resume` is the queue's pick but pure feature work without a doc base means future docs accumulate the entire backlog at once. Document the system *now* while the surface is small enough to fully cover. ✅

**Act.** New `docs/AGENT_CHECKPOINTS.md`. README updated with a single-sentence link. No code changes.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped (unchanged). Markdown changes don't need their own tests but verifying nothing broke is cheap insurance.

## Loop 186 — `/agent --resume` flag

**Observe.** A user mid-multi-step task hits a crash. After restart they want to issue *one more agent turn* that continues exactly where the loop died — but `/resume` only fixes chat history; they still have to launch the agent manually with the original task. Two-step recovery for what should be one keystroke.

**Orient.** The resume mechanism (`load_latest_checkpoint`) and the agent-launch mechanism (`_start_agent_turn`) already exist independently. They just need to compose: when `--resume` is seen, do the load, *then* fire the agent turn against the rehydrated history. Encode the flag through the existing sentinel wire format.

**Decide.** Extend `_decode_agent_body` to a 3-tuple `(task, max_steps, resume)` and parse `--resume` as a leading line in any order. Update the `/agent` command parser to recognise `--resume` alongside `--write` and `--max`. App-side: new `_apply_agent_resume(log)` method that calls `load_latest_checkpoint` and clears+extends `self.history`. Both `_AGENT_SENTINEL` and `_AGENT_WRITE_SENTINEL` paths invoke it before `_start_agent_turn`.

**Devil.**
- *Correctness:* What if `--resume` is set but no checkpoint exists? `_apply_agent_resume` writes a yellow status line and proceeds with the existing history — non-fatal, the user still gets their turn run. ✅
- *Scope:* Should `--resume` overwrite or append to current history? Overwrite, in-place — matches `/resume`'s semantics so the two commands compose predictably. ✅
- *Priority:* This completes the recovery story (boot hint → `/resume` → `/agent --resume`). After this, the next backlog item (`/lat n` ring buffer) is purely additive. ✅

**Act.** Refactored `_decode_agent_body` to handle leading flag lines in any order, returning a 3-tuple. Updated four existing test call sites to unpack the new tuple. Extended `/agent` parser with `--resume`. Both sentinel branches in `on_input_submitted` now decode the 3-tuple and call `self._apply_agent_resume(log)` when set. New `_apply_agent_resume` method. HELP_TEXT updated. Twelve tests in `test_agent_resume_flag.py` covering the decoder matrix (no flags, max only, resume only, both orders, unparseable max), the parser (resume alone, with write, with max, with both), the no-task error path, and HELP_TEXT advertisement.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped.

## Loop 187 — document `/agent --resume` in AGENT_CHECKPOINTS.md

**Observe.** Loop 186 shipped `/agent --resume` but the doc page was written one loop earlier and doesn't mention it.

**Orient.** Tiny doc gap. The recovery flow section already documented `/resume` as step 4; `/agent --resume` is the natural step 5 for users who want to keep working immediately after recovery. The slash-command table needs the new row.

**Decide.** Add row to the slash-command table for `/agent --resume`. Add step 5 to the recovery flow describing the one-shot resume-and-run path and noting that a missing checkpoint is non-fatal.

**Devil.**
- *Correctness:* Did I describe the missing-checkpoint behaviour accurately? `_apply_agent_resume` writes a yellow notice and proceeds; the doc says "reported as a notice, not a fatal". Matches. ✅
- *Scope:* Should this loop also touch the `--resume` README mention? README points users at the doc page; updating the page is enough. ✅
- *Priority:* Doc-only loop right after the feature loop is the right cadence — keeps documentation drift to one loop max. ✅

**Act.** Two edits to `docs/AGENT_CHECKPOINTS.md`: new table row, new step 5. No code changes.

**Verify.** `pytest -x -q` → ~1.1k passed, 1 skipped (unchanged).

## Loop 188 — `/lat N` ring-buffer view of recent turns

**Observe.** `/lat` shows only the most recent turn's profile. For perf debugging — "did this regression appear in the last 3 turns or the 10 before that?" — single-turn view is insufficient. Loop 182 stored just `last_turn_profile`; the historical data was being thrown away after every turn.

**Orient.** A bounded ring buffer is the natural shape — last N turns, oldest evicted. N=20 is enough for a working session without unbounded memory growth; ~3KB per profile, ~60KB total. The renderer needs a multi-turn variant that stacks individual profiles with offset headers (`-1`, `-2`, ...) so users can read top-to-bottom in recency order.

**Decide.** Add `DEFAULT_TURN_PROFILE_HISTORY = 20` and `format_turn_profiles(profiles, n)` next to `format_turn_profile`. App stores `turn_profiles: list[TurnProfile]` alongside `last_turn_profile`; runner appends each completed profile and trims from the front beyond the cap. `/lat` accepts an optional integer arg (default 1); reject non-integer and `<1`. Back-compat: when the App lacks `turn_profiles` (older stubs), fall through to `last_turn_profile` so existing tests keep passing.

**Devil.**
- *Correctness:* What if the buffer overflows mid-render? The runner trims after appending; render reads the buffer atomically (no thread reads during write because the runner is the sole writer). ✅
- *Scope:* Should the cap be env-configurable? Premature — 20 is reasonable, existing `QWEN_AGENT_ROTATION_KEEP` is the precedent if it becomes painful. Defer. ✅
- *Priority:* Audit-log atomic write was also queued. But `/lat N` directly leverages observability work that was previously throwing away data after one turn — that's higher leverage than hardening a write path that hasn't been observed to corrupt. ✅

**Act.** Three edits in `tui.py`: new constant + `format_turn_profiles` between `format_turn_profile` and `format_tool_latency`; `App.__init__` initialises `self.turn_profiles = []`; runner appends + trims after `last_turn_profile = profile`; `/lat` branch parses optional N arg with two error paths (non-integer, <1). HELP_TEXT updated to mention `[N]`. New `tests/test_lat_multi.py` with 14 cases across three test classes covering: empty buffer, single-profile no-header, multi-profile headers, recency ordering, n-clamp to buffer length, n=0 and negative both treated as 1, dispatcher with no arg, valid arg, non-integer, zero, negative, fallback path when buffer missing, default-cap pin.

**Verify.** `pytest -x -q` → ~1.2k passed, 1 skipped.

## Loop 189 — atomic write for `save_history_jsonl`

**Observe.** Searched for persistence layers without atomic writes. `save_agent_checkpoint` uses `.tmp + os.replace + fsync` (loop ~150). `save_history_jsonl` (the durable per-turn JSONL chat history at `<root>/.agent/tui_history.jsonl`) was using a plain `path.open("w", ...)` — truncates the live file before content is fully serialised. A crash mid-write leaves the user with a half-written history file and we'd silently swallow it on next load (load_history_jsonl drops malformed lines).

**Orient.** This is a real integrity gap. The TUI saves history on every assistant turn end and again on shutdown — those are exactly the points where a SIGKILL or OOM is most likely (long generations, high memory pressure under vLLM). The audit-log candidate from `next.md` was misframed — there's no on-disk audit log to harden — but the underlying motivation (atomic writes wherever we persist) is right; just retarget at the actual gap.

**Decide.** Rewrite the body of `save_history_jsonl` to mirror `save_agent_checkpoint`: serialise into `<path>.tmp`, fsync the descriptor, `os.replace(tmp, path)`. On any OSError, unlink the tmp file and return 0 (existing behaviour: silent no-op on save failure). Don't change the function signature — drop-in replacement.

**Devil.**
- *Correctness:* Does `os.replace` cross-device? On the same FS — yes, it's the documented atomic primitive on POSIX and on Windows since Python 3.3. The history lives in the same directory as everything else under `.agent/`, no cross-device move possible. ✅ The `RuntimeError` raised by serialisation now propagates instead of silently returning 0 — but that's *better*: we don't want to lie about save failures, and the original file is still preserved by virtue of not being truncated. The TUI's exception handlers around `save_history_jsonl` calls already catch broadly.
- *Scope:* Should `load_history_jsonl` *also* attempt a `.tmp` recovery? No — if the `os.replace` succeeded the canonical path holds the new data; if it didn't the canonical path holds the old data. The `.tmp` is always either in-flight or stale. Recovering from it would invent ambiguity. ✅
- *Priority:* Live vLLM smoke test still queued. But that requires actual vLLM runtime — environment-dependent, can't be deterministic. Atomic history write is a pure-Python, pure-fs change with deterministic tests. Higher leverage right now. ✅

**Act.** One block edit in `tui.py` replacing the `with path.open("w") as fh:` body with the `.tmp` dance. New `tests/test_history_atomic_write.py` with 8 cases: round-trip, no `.tmp` left after success, original file preserved when `os.replace` fails (monkey-patched to OSError), original file preserved when serialisation raises mid-loop, `.tmp` cleaned up after replace failure, the spy assertion that the live data is in `.tmp` at the moment of replace, max_messages still truncates, each line is independently parseable JSON.

**Verify.** `pytest tests/test_history_atomic_write.py -x -q` → 8 passed. Full suite → ~1.2k passed, 1 skipped.

## Loop 190 — `/lat reset` clears the ring buffer

**Observe.** The turn-profile ring buffer (loop 188) accumulates without a manual escape hatch. Users running mixed workloads — e.g. switching from "exploring the repo" to "benchmarking a specific tool call" — want to scope `/lat` output to the new phase without restarting the TUI.

**Orient.** Smallest possible knob: a `reset` subcommand on `/lat`. Already part of the next.md candidate list. The rest of the queue (`/checkpoints diff N`, TTY-width formatting) is meatier; `/lat reset` keeps the cadence and is the natural completion of the buffer story.

**Decide.** Treat the first arg of `/lat` as polymorphic: integer N (existing) OR the literal token `reset` (case-insensitive). On reset, clear `app.turn_profiles` in place, set `app.last_turn_profile = None`, return how many profiles were cleared. Tolerate older stubs without the buffer attribute. Update HELP_TEXT to `/lat [N|reset]`.

**Devil.**
- *Correctness:* Could a user have a session where they meant to type `5` but typed `reset` and lose data they wanted? Possible but cheap — the only state lost is timing telemetry, no chat history. ✅
- *Scope:* Should this also have a confirmation prompt? Overengineered for this surface — `/lat` data is purely observational. ✅
- *Priority:* `/checkpoints diff N` is more interesting; but it requires designing a meaningful diff format and is a 100+ LoC change. `/lat reset` is the natural smallest next step in the same code area we just touched in loop 188. Keeps the cadence. ✅

**Act.** Edit the `if name == "lat":` branch: before the int parse, peek at `cmd.args[0]` and case-insensitively match `reset`. Update HELP_TEXT. New `tests/test_lat_reset.py` with 7 cases: clears populated buffer (cleared count), no-op on empty (cleared 0), case-insensitive (RESET/Reset/ReSeT), no crash with `app=None`, old-stub fallback (clears `last_turn_profile`), `/lat` after reset shows placeholder, non-exact token like `resetx` still falls through to the int parser and produces the original error.

**Verify.** All `/lat` tests pass (33). Full suite ~1.2k passed.

## Loop 191 — `/help <term>` substring filter

**Observe.** HELP_TEXT has grown to ~50 lines as we keep adding slash commands. Users searching for "how do I do X?" must scroll/scan the whole table. A text filter on the `/help` output would cut the noise.

**Orient.** Smallest possible knob: pure substring match over the help table, case-insensitive, treating regex metacharacters as literals so users don't have to escape. Multi-line entries (where a command's summary wraps) must be kept together — pulling only the first line would orphan the continuation. Bare `/help` continues to print the whole table unchanged.

**Decide.** Extend the `name in {"", "help"}` branch: when `cmd.rest` is non-empty, walk the help table in two-line blocks (entry + optional continuation), lowercase-substring-match the term against the joined block, keep the matching blocks. Print a "no commands match" message on empty result. Update HELP_TEXT row.

**Devil.**
- *Correctness:* The two-line block detection is heuristic — a continuation line is "starts with whitespace, doesn't start with '  /', not blank". The current help table follows that shape; if a future row violates it, the filter will mis-group. Acceptable risk; the help text is small enough that any structural drift will be caught by these tests. ✅
- *Scope:* Should the filter be regex? No — pure substring is more predictable and the tests pin that `.*` is treated literally. Users wanting regex have `grep` for the source. ✅
- *Priority:* `/checkpoints diff N` is meatier but requires a diff-formatting design pass. `/help --search` is a 30-LoC change in a heavily-trafficked command. Higher leverage right now. ✅

**Act.** Edit the `name in {"", "help"}` branch in `dispatch_slash` to add the filter. Update HELP_TEXT row. New `tests/test_help_search.py` with 10 cases: bare `/help` unchanged, command-name match, case-insensitivity, summary-text match (DuckDuckGo → /search), continuation lines preserved (/grep), no-match message, regex metacharacters treated literally, multi-word terms, header preserved on match, unique-command match (one entry only).

**Verify.** All 10 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 192 — `/checkpoints diff N`

**Observe.** `/checkpoints` lists snapshots and lets users `load` or `prune` them, but there's no way to *preview* what loading would change before doing it. Users running `/resume` were taking a leap of faith — "this newer snapshot might cost me messages I want to keep."

**Orient.** A symmetric paired diff between the current chat history and a chosen snapshot is the right shape. By message index: same/changed/role-mismatch/added/dropped, plus a one-line preview of each row. Pure renderer + a thin dispatch branch.

**Decide.** New `format_history_diff(current, snapshot, *, snapshot_label, preview_chars=60)` returning a header line plus one row per index. Five symbols (`=` `~` `≠` `+` `-`). New `diff <N>` subcommand on `/checkpoints` that loads snapshot N (1-based, oldest-first, same indexing as `load`) and renders the diff against the live history.

**Devil.**
- *Correctness:* Index-based pairing assumes histories align positionally. If a user inserts a `/sysprompt` mid-session, every later index shifts by one — the diff would show every later row as "changed". True, but that's the *real* state of the world; trying to be clever (LCS) would hide the shift. Index-pairing is honest. ✅
- *Scope:* No content diff at the line level — just role/equal/preview. Adding `difflib.unified_diff` per-message is the next loop's job if anyone needs it. ✅
- *Priority:* This unblocks the recovery story (`/resume` is now informed); higher leverage than TTY-width formatting which is pure cosmetics. ✅
- *Side effect caught during act:* HELP_TEXT row for `/checkpoints` now spans 4 lines. The `/help <term>` filter from loop 191 only collected ONE continuation line — would have orphaned the diff line. Fixed: filter now greedily collects all continuations. Without the loop-191 work, this would have shipped a silent regression on `/help checkpoints`.

**Act.** Added `format_history_diff` next to `_format_checkpoint_listing`. Added `diff` branch to the `/checkpoints` dispatcher (refused on `history is None`, missing arg, non-int, no snapshots, out-of-range). Updated unknown-subcommand message to list `diff`. Updated HELP_TEXT to multi-line entry. Tightened the loop-191 help filter to greedy continuation collection. New `tests/test_checkpoints_diff.py` with 17 cases: 10 for the renderer (both empty, identical, content changed, role mismatch, added, dropped, preview truncation, newline collapse, snapshot label in header, header counts) and 7 for the dispatcher (no args, no snapshots, OOR, invalid index, success path, no-history, unknown-subcommand-message).

**Verify.** All 17 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 193 — `--inline` per-message unified diff

**Observe.** The loop-192 `/checkpoints diff` shows *which* messages changed but not *how* — users still have to load the snapshot to actually see the difference, which is the very thing they were trying to avoid by running diff in the first place. The renderer's `~` rows have a 60-char preview only.

**Orient.** `difflib.unified_diff` is in the stdlib and is the canonical answer. Two things to be careful about: (1) verbose for long messages (assistant replies can be 4KB+), and (2) it's only meaningful for content-changed rows — role-mismatch and added/dropped rows have no shared "from/to" pair.

**Decide.** Add an `inline_diff: bool = False` kwarg to `format_history_diff` (off by default — back-compat with all 17 existing tests). When on, append a unified-diff fragment under each `~` row, indented with 5 spaces, capped at `inline_diff_max_lines=12` with a "diff truncated" footer. Wire up via a `--inline` flag on the dispatcher; accept it in any position relative to the index.

**Devil.**
- *Correctness:* Did I get the diff direction right? `unified_diff(snapshot, current)` so snapshot is the "from" — removed lines come from snapshot, added lines come from current. Caught it the first time the test ran (asserted `-line2`, actual was `-LINE2` because LINE2 was in snapshot). ✅
- *Scope:* Per-message diff *only* on content-changed rows. Role-mismatch rows don't get one — different role means it's not really "the same message edited". Added/dropped rows don't get one — there's nothing to diff against. ✅
- *Priority:* This finishes the recovery preview story started in 192. Skipping straight to TTY-width formatting would leave users with a half-finished tool. ✅

**Act.** Two kwargs added to `format_history_diff`. `--inline` flag stripped from args before index parsing in the dispatcher (handles before/after the index). HELP_TEXT row updated. Loop-192's `test_diff_no_args` updated to the new usage string. New `tests/test_checkpoints_diff_inline.py` with 12 cases: 7 for the renderer (default off, unified-diff present, only-on-changed, truncation, no-truncate-when-under-cap, snapshot label appears in `---`/`+++` headers, role-mismatch skipped) + 5 for dispatch (flag after index, flag before index, plain mode unchanged, --inline alone errors, sanity).

**Verify.** All 12 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 194 — atomic write for `fs_tools.write_file`

**Observe.** Auditing `apply_patch` for atomicity (next.md candidate). `apply_patch` itself is fine — `git apply` either succeeds or unwinds. But the audit surfaced something worse: `write_file` (the `fs_write` agent tool — the *primary* write surface used by every "write code" agent turn) used `p.write_bytes(encoded)`. That's a single syscall but it does NOT replace atomically — it truncates first, then writes. A crash mid-write leaves a half-written or zero-byte file. Same gap as `save_history_jsonl` had before loop 189.

**Orient.** This is priority-1 territory: silent data corruption on the agent's most-used write tool. The agent runs unattended overnight; an OOM or oom-kill mid-write would silently destroy the file the agent was editing. We caught this *because* we audited — exactly why audits exist.

**Decide.** Same `.tmp + os.replace + fsync` dance as `save_agent_checkpoint` and `save_history_jsonl`. Wrap in `try/except OSError`, unlink the `.tmp` on failure, raise `FsError` so the agent surface keeps its "all errors are FsError" contract.

**Devil.**
- *Correctness:* `os.replace` is atomic on POSIX and on Windows (since 3.3). The `.tmp` is a sibling of the target so it stays on the same filesystem. ✅ The size-cap check still runs *before* the atomic write — we never create a `.tmp` file just to immediately delete it on size rejection. Pinned by `test_oversize_still_rejected_before_tmp_write`. ✅
- *Scope:* Should `write_file` also fsync the parent directory after replace? POSIX-strict durability says yes — the directory entry rename isn't guaranteed durable until the parent is fsynced. But this is the same level of paranoia as `save_agent_checkpoint`, which doesn't do parent-fsync either; we'd be making `write_file` stricter than the rest of the codebase. Defer: if we ever want strict-durability everywhere, do it as a separate cross-cutting loop. ✅
- *Priority:* This is a real integrity gap on the agent's most-used write tool. Higher leverage than TTY-width formatting. ✅

**Act.** Edit `write_file` body to use the atomic dance. New `tests/test_fs_write_atomic.py` with 7 cases: round-trip, no leftover .tmp on success, replace-failure preserves original, .tmp cleaned up after replace failure, the spy assertion that the source is a .tmp at the moment of replace, oversize-rejected-before-tmp-write (no .tmp created on size reject), create_parents still works (no .tmp left in nested dir).

**Verify.** All 7 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 195 — `/checkpoints diff --since-resume`

**Observe.** Loop 192/193 gave users a way to diff against a *specific* snapshot N. But the natural recovery question is "what would `/resume` do to me right now?" — and `/resume` uses `load_latest_checkpoint`, which has its own ordering (primary first, then rotations newest-first). Forcing users to mentally reproduce that ordering to pick the right N is a bug-magnet.

**Orient.** Add a `--since-resume` flag that calls `load_latest_checkpoint` directly and renders the diff against whatever snapshot the agent layer would have chosen. Same flag stripping as `--inline`, fully composable.

**Decide.** Strip `--inline` and `--since-resume` from args before parsing the index. When `--since-resume` is set, skip the index-required path entirely; reach for `agent_loop.load_latest_checkpoint(primary)`. Print a friendly "(no checkpoint that /resume could load)" if the source is None.

**Devil.**
- *Correctness:* The renderer uses `source.name` as the snapshot label, so users always know which file was diffed even though they didn't pick it. ✅
- *Scope:* Should `/resume` itself print a one-line "to preview, run `/checkpoints diff --since-resume`" hint? Tempting, but too noisy on every resume. Leave as-is. ✅
- *Priority:* This finishes the recovery preview triple — list, diff specific, diff "what `/resume` would do". After this, the recovery story is complete enough to move on to other things. ✅

**Act.** Two-line filter against `--since-resume` in the dispatcher; new branch ahead of the index parse. Updated usage string to list both forms. Updated HELP_TEXT entry. Loop-192's `assert out == "usage:..."` softened to substring matches. New `tests/test_checkpoints_diff_since_resume.py` with 7 cases: no checkpoint message, picks primary when present, falls back to rotation when primary missing, --inline composes, no-history, usage string lists --since-resume, flag order doesn't matter (--since-resume --inline == --inline --since-resume).

**Verify.** All 7 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 196 — docs sync for the recovery diff family

**Observe.** `docs/AGENT_CHECKPOINTS.md` was last touched in loop 187 — covers `/resume`, `/checkpoints load/prune`, `/agent --resume`, but NOT the new `diff` / `diff --since-resume` / `/lat reset` / `/lat N` work. New users hitting the docs would never know those features exist.

**Orient.** Pure documentation loop. No code changes; just the slash-command table and the recovery-flow steps.

**Decide.** Add three rows to the slash-command table (`/checkpoints diff N`, `/checkpoints diff --since-resume`, the updated `/lat [N|reset]`). Add a recovery-flow step 5 explicitly recommending `/checkpoints diff --since-resume` *before* `/resume` for non-destructive previews. Update the file-formats note to mention `fs_write` is now atomic too (carry from loop 194).

**Devil.**
- *Correctness:* Does the new step 5 break the numbering for the existing "resume + continue" step (now step 6)? Yes by design — the natural reading order is now "preview, then act". ✅
- *Scope:* Should this also document the env-var and config story? Already in the page; not changing in this loop. ✅
- *Priority:* Doc loops are cheap and unblock onboarding. Worth the cadence. ✅

**Act.** Three edits in `docs/AGENT_CHECKPOINTS.md`. No tests (no code change).

**Verify.** Test suite still green at ~1.2k passed (regression check, no new tests).

## Loop 197 — `/resume --preview` (a.k.a. `--dry-run`)

**Observe.** Users will type `/resume` first because it's the obvious command. They might *then* discover they wanted to preview — but at that point `/resume` has already mutated `history` in place. The `/checkpoints diff --since-resume` from loop 195 is the right operation but lives under a different command name and isn't discoverable from `/resume`.

**Orient.** Easier path: bake the preview into `/resume` itself. `/resume --preview` (alias `--dry-run`) renders the same diff as `/checkpoints diff --since-resume` but never touches `history`. Discoverable via `/help resume`.

**Decide.** Add a flag check at the top of the `/resume` branch; when set, call `load_latest_checkpoint`, render `format_history_diff`, return without mutating. Update HELP_TEXT to a multi-line entry.

**Devil.**
- *Correctness:* The non-preview path still works unchanged; flag is checked before the mutation. Pinned by `test_resume_without_preview_still_loads`. ✅
- *Scope:* Should `--preview` accept `--inline` too? Yes, but we'd be re-implementing flag parsing inside the `/resume` branch. The dedicated `/checkpoints diff --since-resume --inline` already exists for inline. Keep `/resume --preview` simple. ✅
- *Priority:* Closes the discoverability gap on the recovery preview. Small (~25 LoC + 5 tests). ✅

**Act.** Edit `/resume` branch to handle `--preview`/`--dry-run`. Update HELP_TEXT entry. New `tests/test_resume_preview.py` with 5 cases: preview doesn't mutate, --dry-run alias, no-checkpoint message, non-preview /resume still loads, no-history safety.

**Verify.** All 5 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 198 — `/lat --json` for downstream tooling

**Observe.** `/lat` renders human-readable text. Anyone wanting to ship timing data into a dashboard, jq pipeline, or log shipper has to parse the formatted output — and that format is documentation-by-example with no stability guarantees.

**Orient.** A `--json` (alias `--format=json`) flag emits the same data as a stable JSON shape. Indent=2 so it's human-skimmable too.

**Decide.** New `turn_profiles_as_json(profiles) -> str`. Tool-call tuples flatten to `{"name": ..., "elapsed_s": ...}` (more self-documenting than a 2-element array). Each row also includes the derived `total_s()` so consumers don't have to recompute. Strip `--json`/`--format=json` from args wherever they appear; the integer N and `reset` token still parse normally.

**Devil.**
- *Correctness:* Empty buffer + `--json` should return `[]`, not the `format_turn_profile(None)` placeholder string — the latter is unparseable. Pinned by `test_json_empty_buffer`. ✅
- *Scope:* Should `reset` also support JSON? No — it's an admin command with a one-line confirmation; downstream tooling doesn't need it. Left as plain text. ✅
- *Priority:* This unblocks integration without changing the human path. Lowest risk in the next.md list. ✅

**Act.** New `turn_profiles_as_json` next to `format_turn_profiles`. Strip flags before integer/reset parse. JSON path branches in both the empty-buffer and populated-buffer paths. HELP_TEXT entry expanded. New `tests/test_lat_json.py` with 11 cases: 4 for the renderer (empty, single, tool-calls flattened, indent), 7 for dispatch (--json no-N, --json with N, --format=json alias, empty buffer, position-independent, plain-text default still text-not-json).

**Verify.** All 11 new tests pass. Full suite ~1.2k passed, 1 skipped.

## Loop 199 — `/checkpoints export N <path>`

**Observe.** Users could `load`, `prune`, or `diff` a rotated checkpoint, but couldn't archive one before pruning removed it. The atomic-write recipe now sits in three places (save_agent_checkpoint, save_history_jsonl, fs_tools.write_file) — natural to apply it here too.

**Orient.** A read-only-from-snapshot, atomic-write-to-dest copy. Path validated through `fs_tools._resolve_inside_root` so `../escape.json` and absolute paths outside root are rejected. History never mutated — strictly side-effecting on disk.

**Decide.** New `export` subcommand. `/checkpoints export <N> <path>`. Dest path is interpreted relative to the FS root, parent dirs auto-created (`mkdir parents=True`), atomic via `.tmp + fsync + os.replace`. On any OSError, tmp cleaned up and a text error returned (no exception leaks).

**Devil.**
- *Correctness:* What if the destination already exists? `os.replace` overwrites — pinned by `test_export_overwrites_existing`. Symlink shenanigans? `_resolve_inside_root` resolves symlinks before the boundary check, so escape attempts via symlinks are caught. ✅
- *Scope:* Should this support an `--all` flag to archive every snapshot? Out of scope — single-snapshot export is the atomic primitive; a script can loop. ✅
- *Priority:* The atomic recipe means a failed export never half-writes a file users might trust as a backup. That makes this safer than handing them the raw cp command. ✅

**Act.** New `export` branch in `/checkpoints` dispatch, between `diff` and `prune`. HELP_TEXT updated. Unknown-subcommand message lists `export` (pinned by test). 11 new tests in `test_checkpoints_export.py`: usage error, no-snapshots, invalid index, out-of-range, byte-identical copy, path-escape rejection, history non-mutation, parent dir auto-creation, unknown-subcommand listing, no-tmp-leftover, overwrite-existing.

**Verify.** All 11 pass. Full suite ~1.25k passed, 1 skipped.

## Loop 200 — `/sysinfo --json`

**Observe.** `/sysinfo` was a free-form bug-report dump; `/lat --json` (loop 198) proved structured-data export is useful for downstream tooling. `/sysinfo` data is even more dashboard-shaped — health status, model name, history size — than `/lat`'s timing tuples.

**Orient.** Mirror loop 198: a `--json`/`--format=json` flag that swaps the renderer. New `_render_sysinfo_json` next to `_render_sysinfo`. Health failures surface as a structured `{"ok": False, "error": ..., "hint": ...}` rather than a string with an inline newline.

**Devil.**
- *Correctness:* The free-form text encoded the hint via `\n  hint: ...` inside the health line. JSON path keeps the hint as its own field — better, but a regression risk for any user grepping `hint:` from text. Pinned: text path unchanged. ✅
- *Scope:* Should the JSON include a schema_version? Premature — no consumers yet. Adding it later is non-breaking. ✅
- *Priority:* Same justification as loop 198: makes the data programmable without touching the human path. Low-risk, narrow surface. ✅

**Act.** New `_render_sysinfo_json` returning `json.dumps(payload, indent=2)`. Dispatch detects `--json` or `--format=json` in `cmd.args`. HELP_TEXT updated. 6 new tests in `test_sysinfo_json.py`: healthy parses, format=json alias, unhealthy preserves error+hint, exception caught & encoded, no-flag keeps text+header, None history yields zero counts.

**Verify.** All 6 pass. Full suite ~1.26k passed, 1 skipped. Existing TestSysInfoSlash text-path tests still green — no regression.

## Loop 201 — `/tokens --json` (JSON-export trilogy complete)

**Observe.** Loops 198 and 200 added `--json` to `/lat` and `/sysinfo`. `/tokens` is the third introspection command and the most number-shaped of the three — completes the trilogy and gives users a uniform pattern.

**Orient.** Same `--json`/`--format=json` flag, indent=2 JSON. Going beyond simple totals: include a per-message breakdown so users debugging context-window pressure can see *which* message is hot, not just that totals are high.

**Devil.**
- *Correctness:* Per-message tokens must sum to the headline total — pinned by `test_per_message_shape` (asserts `sum(per_message) == tokens_estimated`). The `history is None` guard fires *before* flag parsing, so `--json` users won't get parseable JSON when the history is missing — pinned by `test_no_history_still_text` so this stays an explicit choice, not silent breakage. ✅
- *Scope:* Should the JSON include the message content? No — could be huge, and consumers can re-fetch via `/save`. Just index/role/count keeps the payload bounded. ✅
- *Priority:* Lowest-risk JSON loop yet (no I/O, no network) but the per-message field unlocks real triage value — finds which message is consuming context. ✅

**Act.** Single `if as_json:` branch in the `tokens` handler; flag check via `cmd.args` membership. Per-message list with `{index, role, tokens_estimated}`. HELP_TEXT updated. 6 new tests in `test_tokens_json.py`: parses, per-message shape & sum-invariant, `--format=json` alias, empty history, None history still returns text, no flag keeps human path.

**Verify.** All 6 pass. Full suite ~1.26k passed, 1 skipped.

## Loop 202 — `format_turn_profile` honours TTY width

**Observe.** The `summary:` line was a hard-coded one-liner — long agent summaries (12+ tool calls, error counts, retry tallies) overflowed narrow terminals and broke the visual layout. The tool-name column was capped at 20 with no narrow-terminal escape valve either.

**Orient.** Two related fixes: wrap the summary line via `textwrap.fill` with a hanging indent that lines up under the colon, and trim/ellipsis-truncate over-long tool names when the terminal is too narrow to fit a 20-char column with a latency suffix.

**Decide.** New `width: int | None = None` kwarg on `format_turn_profile` (and forwarded through `format_turn_profiles`). `None` means "look at the actual terminal via shutil.get_terminal_size" with fallback to 80, then floor at 40 so we never emit a waterfall. Tests pin behaviour by passing explicit widths, eliminating env dependency.

**Devil.**
- *Correctness:* Could the wrap break the JSON path (loop 198 `--json`)? No — `turn_profiles_as_json` does its own serialisation and never calls `format_turn_profile`. Width is irrelevant there. ✅ Could `textwrap` mangle words with hyphens or path separators? Disabled both `break_long_words` and `break_on_hyphens` so a long path stays on one line even if it overflows. ✅
- *Scope:* Should this also wrap the per-tool rows? No — those are aligned columns; wrapping would lose the alignment. Truncation with `…` is the correct narrow-terminal compromise. ✅
- *Priority:* This was the one carried-forward UX gap in the candidate pool. Real-world long summaries from the agent loop motivated it. ✅

**Act.** `format_turn_profile` and `format_turn_profiles` both accept `width: int | None`. Width resolution: explicit > terminal > 80, floored at 40. Tool-name col now `min(20, max_name_len, width-budget)` with mid-string ellipsis truncation when a name exceeds the col. Summary wrapped via textwrap with 11-char hanging indent. 8 new tests in `test_turn_profile_width.py`: wide=one-liner, narrow=wraps, indent-aligns-under-colon, long-name-truncated, normal-name-unchanged, default-uses-terminal-size, width-floored-at-40, stacked-profiles-propagate.

**Verify.** All 8 pass. Full suite ~1.27k passed, 1 skipped. Existing 43 /lat tests still green — the kwarg defaults preserve old behaviour.
