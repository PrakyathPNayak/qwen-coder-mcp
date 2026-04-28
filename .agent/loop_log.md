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
