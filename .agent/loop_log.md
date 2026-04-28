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




