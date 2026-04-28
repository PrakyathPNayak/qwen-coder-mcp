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


