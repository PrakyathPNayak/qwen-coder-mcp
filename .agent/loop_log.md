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
