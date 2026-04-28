"""Self-improving agentic loop for qwen-coder-mcp.

For each iteration:
  1. Pick a candidate file (rotating cursor across the repo).
  2. Ask Qwen to find issues.
  3. Pick the top issue and ask Qwen to produce a unified diff fix.
  4. Run a "devil's advocate" pass to challenge the diff.
  5. If ACCEPT and `git apply --check` passes (and Python syntax check passes
     for *.py), apply, commit, and push.
  6. Append to STATE.md and .loop/history/<timestamp>.md.
  7. Sleep and repeat. Errors never break the loop.
"""
from __future__ import annotations

import codecs
import datetime as _dt
import json
import os
import re
import stat as _stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Iterable

# Allow `python -m agent.loop` from repo root.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from qwen_coder_mcp import prompts  # noqa: E402
from qwen_coder_mcp.qwen_client import QwenClient, QwenError  # noqa: E402

LOOP_DIR = _REPO / ".loop"
HISTORY_DIR = LOOP_DIR / "history"
STATE_ARCHIVE_DIR = LOOP_DIR / "state_archive"
CURSOR_FILE = LOOP_DIR / "cursor.json"
LOG_FILE = LOOP_DIR / "runtime.log"
STATE_FILE = _REPO / "STATE.md"
STATE_MAX_BYTES = 256 * 1024  # rotate STATE.md when it exceeds this

# Paths excluded from candidate file selection.
EXCLUDE_DIRS = {".git", ".loop", ".venv", "venv", "__pycache__", "dist", "build"}
EXCLUDE_FILES = {"STATE.md", "LICENSE"}
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini",
    ".txt", ".sh", ".js", ".ts",
}


# ---------------------------------------------------------------- utilities
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_GIT_CMD_TIMEOUT_SECONDS = 60


_LOOP_ITER_BUDGET_DEFAULT = 600.0
_LOOP_ITER_BUDGET_MAX = 24 * 60 * 60.0  # 24 hours


def _iteration_budget_seconds() -> float:
    """Wall-clock ceiling for one `_iteration` call. Three Qwen calls can
    each retry several times with backoff (~120s timeout × ~3 attempts +
    sleeps), so a single iteration could otherwise burn ~20 minutes if
    the backend is flapping. The budget is checked *between* phases so
    one in-flight network call may still complete after the deadline.

    Values are clamped to (0, 24h]. Bad input or non-positive values
    fall back to the default. Absurdly large values are capped so a
    typo (`6000000` instead of `600`) cannot effectively disable the
    budget.
    """
    raw = os.environ.get("QWEN_LOOP_ITER_BUDGET_S", str(_LOOP_ITER_BUDGET_DEFAULT))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _LOOP_ITER_BUDGET_DEFAULT
    if v <= 0:
        return _LOOP_ITER_BUDGET_DEFAULT
    if v > _LOOP_ITER_BUDGET_MAX:
        return _LOOP_ITER_BUDGET_MAX
    return v


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` with a hard timeout.

    Raises `subprocess.TimeoutExpired` only when callers passed
    `check=True`; with `check=False` (the cleanup paths) we synthesise
    a non-zero `CompletedProcess` so the caller can keep going.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=_REPO,
            check=check,
            text=True,
            capture_output=True,
            timeout=_GIT_CMD_TIMEOUT_SECONDS,
            errors="surrogateescape",
        )
    except subprocess.TimeoutExpired:
        if check:
            raise
        _log(f"_run_git timeout: git {' '.join(args)} exceeded {_GIT_CMD_TIMEOUT_SECONDS}s")
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=124,
            stdout="",
            stderr=f"timed_out_after_{_GIT_CMD_TIMEOUT_SECONDS}s",
        )


def _candidate_files() -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(_REPO):
        rel_root = Path(root).relative_to(_REPO)
        # prune excluded dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in files:
            if name in EXCLUDE_FILES or name.startswith("."):
                continue
            p = Path(root) / name
            if p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                # `lstat` so symlinks are detected as themselves rather
                # than their target. Symlinks are skipped: in-repo links
                # are redundant (the target is also a candidate) and
                # out-of-repo links are blocked by `_read_file` anyway,
                # so picking one wastes an iteration slot on a
                # guaranteed "unreadable_or_too_large".
                st = p.lstat()
                if _stat.S_ISLNK(st.st_mode):
                    continue
                if st.st_size == 0:
                    continue
            except OSError:
                continue
            out.append(p.relative_to(_REPO))
        # don't recurse into excluded
    out.sort()
    return out


def _load_cursor() -> int:
    try:
        return int(json.loads(CURSOR_FILE.read_text("utf-8")).get("idx", 0))
    except Exception:
        return 0


def _save_cursor(idx: int) -> None:
    """Persist the cursor atomically.

    A naive `Path.write_text` is non-atomic: if the process is killed
    while the file is being truncated/written, the next `_load_cursor`
    sees an empty/corrupt file and (per its own try/except) falls back
    to ``0`` — silently re-scanning files that were already covered.
    Write to a sibling tempfile then `os.replace`, which is atomic on
    POSIX (and on Windows for files on the same volume).

    On rename failure (disk full, permissions, etc.), the previous
    cursor value is preserved and the failure is logged. We do NOT
    re-raise: the outer loop catches all exceptions, but if we raise
    here we abort the current iteration *after* the model round-trip
    has already cost time/tokens, and the next iteration loads the
    OLD cursor again — so the same file is re-scanned indefinitely
    until disk recovers. Logging-and-continuing means we lose progress
    for one iteration, not unbounded compute.
    """
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(CURSOR_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps({"idx": idx}), "utf-8")
        os.replace(tmp, CURSOR_FILE)
    except OSError as exc:
        # Drop the half-written tmp; previous CURSOR_FILE (if any) is
        # untouched because we never opened it for writing.
        try:
            tmp.unlink()
        except OSError:
            pass
        try:
            _log(f"cursor save failed (idx={idx}): {exc}")
        except Exception:
            pass


def _read_file(path: Path, max_bytes: int) -> str | None:
    """Read a candidate file from the repo. Refuses to read content
    whose resolved target is outside `_REPO`, so an in-repo symlink
    pointing at e.g. `/etc/passwd` cannot leak host content into a
    model prompt."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    repo_resolved = _REPO.resolve()
    try:
        if not resolved.is_relative_to(repo_resolved):
            return None
    except AttributeError:
        # Python < 3.9 fallback (we target 3.11+, but be defensive).
        try:
            resolved.relative_to(repo_resolved)
        except ValueError:
            return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ------------------------------------------------------------- model output
_INNER_FENCE_RE = re.compile(
    r"```[a-zA-Z0-9_+\-]*\s*\n(.*?)\n```", re.DOTALL
)


_OPEN_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+\-]*\s*\n", re.MULTILINE)
_CLOSE_FENCE_RE = re.compile(r"\n```\s*$")


def _strip_fence(text: str) -> str:
    """Extract the payload from a model response.

    The model is prompted to emit a fenced block, but in practice it
    sometimes wraps the fence in prose ("Here is the diff:\n```diff…```")
    or omits the fence entirely and returns a raw unified diff. Handle
    all three:

    1. Pure raw diff (starts with ``diff --git`` or ``--- ``) → return as-is.
    2. Otherwise return the inner text of the *first* properly-closed
       fenced block.
    3. Unclosed fence (model dropped the trailing ```): strip the
       leading ```lang\\n and any trailing ```; the remainder is best-
       effort payload (downstream `_apply_diff` validates).
    4. No fence at all → return the stripped original.

    When the model emits multiple fences we return the first; the prompt
    is contractually one diff in one fence, so multiple fences indicate
    a misformatted response which the downstream `_apply_diff` will
    reject if the first fence isn't actually a diff.
    """
    text = text.strip()
    if not text:
        return text
    if text.startswith(("diff --git", "--- ")):
        return text
    m = _INNER_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # Unclosed-fence salvage: model produced ```lang\n<body> with no closer.
    om = _OPEN_FENCE_RE.match(text)
    if om:
        body = text[om.end():]
        body = _CLOSE_FENCE_RE.sub("", body)
        return body.strip()
    return text


# Phrases that signal "no real findings" when they form the entire reply.
# Matched only when the response has no bullet or numbered-list markers,
# so a longer reply that happens to *contain* "no issues" inside one of
# its bullets is still parsed for that bullet.
_NO_ISSUE_RE = re.compile(
    r"""^\s*(?:
        no\s+(?:issues?|bugs?|problems?|errors?|findings?|defects?|concerns?)
            (?:\s+found)?
            (?:\s+(?:in|with)\s+(?:this|the)\s+(?:file|code))?\s*[.!]?
        |
        (?:(?:everything|this(?:\s+code)?|the\s+code)\s+)?
            looks?\s+(?:good|fine|clean|ok|okay|correct)
            (?:\s+to\s+me)?\s*[.!]?
        |
        lgtm\s*[.!]?
        |
        nothing\s+(?:to\s+(?:fix|change|do|report)|wrong|broken)\s*[.!]?
        |
        clean\s*[.!]?
        |
        all\s+good\s*[.!]?
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_first_issue(text: str) -> str | None:
    text = text.strip()
    if not text or text.upper().startswith("NO_ISSUES"):
        return None
    # If the model returned a single benign "no findings" sentence with
    # no list markers, treat it as clean.
    if "\n" not in text and _NO_ISSUE_RE.match(text):
        return None
    # numbered list: "1. ...". Capture first item.
    m = re.search(r"(?ms)^\s*1[.)]\s+(.+?)(?=^\s*2[.)]\s+|\Z)", text)
    if m:
        return m.group(1).strip()
    # bullet list fallback.
    m = re.search(r"(?ms)^\s*[-*]\s+(.+?)(?=^\s*[-*]\s+|\Z)", text)
    if m:
        return m.group(1).strip()
    # Last-chance fallback: first line, but reject benign one-liners.
    first = text.splitlines()[0].strip()
    if not first or _NO_ISSUE_RE.match(first):
        return None
    return first


_VERDICT_ACCEPT_RE = re.compile(r"VERDICT\s*:\s*ACCEPT\b", re.IGNORECASE)
_VERDICT_REJECT_RE = re.compile(
    r"VERDICT\s*:\s*REJECT\b\s*(.*)", re.IGNORECASE | re.DOTALL
)


def _verdict_accepts(text: str) -> tuple[bool, str]:
    if _VERDICT_ACCEPT_RE.search(text):
        return True, "accept"
    m = _VERDICT_REJECT_RE.search(text)
    if m:
        # Trim only the immediate reject reason (first line), so a long
        # post-verdict ramble doesn't bloat the log.
        tail = m.group(1).strip()
        if tail:
            tail = tail.splitlines()[0].strip()
        return False, tail or "reject"
    # No clear verdict -> reject conservatively.
    return False, "no_verdict"


# ------------------------------------------------------------- diff handling
_DIFF_PATH_HEADER_RE = re.compile(
    r"^(?:"
    # `diff --git` form: a-path then b-path, each either unquoted or
    # quoted (git wraps paths in C-string quotes when they contain
    # spaces, special chars, or non-ASCII bytes with quotePath=true).
    r"diff --git\s+(?P<a1>(?:\"a/(?:\\.|[^\"\\])*\"|a/\S+))"
    r"\s+(?P<b1>(?:\"b/(?:\\.|[^\"\\])*\"|b/\S+))"
    r"|---\s+(?P<a2>(?:\"a/(?:\\.|[^\"\\])*\"|a/\S+))"
    r"|\+\+\+\s+(?P<b2>(?:\"b/(?:\\.|[^\"\\])*\"|b/\S+))"
    r")",
    re.MULTILINE,
)

# `rename from <path>` / `rename to <path>` / `copy from <path>` /
# `copy to <path>` — git-apply consumes these and writes to the named
# destination path. They are NOT prefixed with `a/` or `b/`. Without
# this, a rename-to traversal path could slip through `_has_unsafe_path`.
# Path may be quoted (contains spaces / non-ASCII with quotePath=true).
_DIFF_RENAME_COPY_RE = re.compile(
    r"^(?:rename from|rename to|copy from|copy to)\s+"
    r"(?P<p>(?:\"(?:\\.|[^\"\\])*\"|\S.*?))\s*$",
    re.MULTILINE,
)


def _unquote_diff_path(raw: str) -> str:
    """Strip the `a/` or `b/` prefix and decode C-string quoting that
    git uses for paths containing spaces, control bytes, or non-ASCII
    octets when `core.quotePath=true`. Returns the repo-relative path.
    """
    s = raw
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        try:
            decoded = codecs.escape_decode(body.encode("utf-8"))[0]
            assert isinstance(decoded, bytes)
            s = decoded.decode("utf-8", errors="surrogateescape")
        except (ValueError, UnicodeDecodeError):
            # Decoding failed — return the literal body without quotes
            # so downstream checks still see *some* path string and can
            # reject it via traversal/absolute checks.
            s = body
    if s.startswith("a/") or s.startswith("b/"):
        s = s[2:]
    return s


def _diff_paths(diff: str) -> list[str]:
    """Return every repo-relative path mentioned in diff headers.

    Covers `--- a/`, `+++ b/`, `diff --git a/X b/Y`, plus
    `rename from`/`rename to`/`copy from`/`copy to`. Paths quoted in
    git's C-string format (used when paths contain spaces or non-ASCII
    bytes with `core.quotePath=true`) are decoded before being
    returned, so safety checks see the real destination path.
    """
    paths: list[str] = []
    for m in _DIFF_PATH_HEADER_RE.finditer(diff):
        for g in ("a1", "b1", "a2", "b2"):
            v = m.group(g)
            if not v:
                continue
            decoded = _unquote_diff_path(v)
            if decoded and decoded != "/dev/null":
                paths.append(decoded)
    for m in _DIFF_RENAME_COPY_RE.finditer(diff):
        v = m.group("p")
        if not v:
            continue
        decoded = _unquote_diff_path(v)
        if decoded and decoded != "/dev/null":
            paths.append(decoded)
    return paths


def _has_unsafe_path(diff: str) -> str | None:
    """Return an error message if any header path is unsafe, else None.

    Refuses absolute paths, `..` traversal segments, and Windows-drive paths.
    Defence in depth: `git apply` already refuses to write outside the tree,
    but we want the loop to log this distinctly (not as a generic apply
    failure) and to abort before the working tree is touched.
    """
    for raw in _diff_paths(diff):
        # Strip a single trailing tab+timestamp some diffs emit.
        path = raw.split("\t", 1)[0]
        if not path:
            return "empty_path"
        # NUL byte in a path is always hostile — UNIX path APIs split
        # at NUL silently; a quoted decoded `\0` could rewrite the
        # destination. Newlines/CR likewise have no place in a real
        # filename and break log parsing.
        if "\x00" in path:
            return f"nul_in_path:{path!r}"
        if "\n" in path or "\r" in path:
            return f"newline_in_path:{path!r}"
        if path.startswith("/"):
            return f"absolute_path:{path}"
        # Windows drive prefix: one ASCII letter followed by ':'. POSIX
        # filenames legitimately contain `:` (e.g. `a:b.py`), so we must
        # not flag every 2nd-char-colon path as absolute.
        if (
            len(path) >= 2
            and path[1] == ":"
            and "A" <= path[0].upper() <= "Z"
        ):
            return f"absolute_path:{path}"
        # Normalise forward slashes only; backslashes shouldn't appear.
        if "\\" in path:
            return f"backslash_in_path:{path}"
        parts = path.split("/")
        if any(p == ".." for p in parts):
            return f"path_traversal:{path}"
    return None


def _has_binary_patch(diff: str) -> str | None:
    """Reject diffs containing binary patch markers.

    Two formats matter:
      1. `Binary files a/X and b/Y differ` — git-diff's textual marker
         that a binary diff was suppressed; applying it is a no-op but
         indicates the model misunderstood the task.
      2. `GIT binary patch` — actual binary delta in base85 blocks. A
         coding loop never legitimately emits these; the corpus this
         model edits is text-only.

    Only header lines (everything before the first `@@` hunk header)
    are scanned. Inside a hunk, content lines are data — a markdown
    file documenting "Binary files differ" is not a binary patch.
    """
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            # New file headers reset the in_hunk state.
            if line.startswith("diff --git ") or line.startswith("--- "):
                in_hunk = False
            else:
                continue
        s = line.strip()
        if s.startswith("GIT binary patch"):
            return "git_binary_patch"
        if s.startswith("Binary files ") and s.endswith(" differ"):
            return "binary_files_marker"
    return None


def _has_unsafe_mode(diff: str) -> str | None:
    """Reject diffs that create or change to dangerous file modes.

    `120000` is git's symlink mode. A model-emitted diff that drops a
    symlink into the worktree could point at `/etc/passwd` or
    `~/.ssh/authorized_keys`, and `git apply` would happily honour it
    inside the repo (the symlink target is just bytes to git). We
    refuse symlinks blanket-style: a code-fix loop never legitimately
    needs to introduce one. `160000` (gitlinks / submodules) is
    similarly out of scope.
    """
    for line in diff.splitlines():
        s = line.strip()
        # `new file mode 120000` / `new mode 120000` / `old mode 120000`
        # any mode-line with the symlink or gitlink mode is suspicious.
        if s.startswith(("new file mode ", "new mode ", "old mode ", "deleted file mode ")):
            mode = s.rsplit(" ", 1)[-1]
            if mode == "120000":
                return f"symlink_mode:{s}"
            if mode == "160000":
                return f"gitlink_mode:{s}"
        # `index <sha>..<sha> <mode>` — git encodes the file mode on the
        # index line for new files / mode-only changes when no explicit
        # `new file mode` header is emitted. A concise diff for a new
        # symlink can be just `diff --git ...\nindex 0..abc 120000\n...`.
        if s.startswith("index "):
            parts = s.split()
            if len(parts) == 3 and parts[2] in ("120000", "160000"):
                kind = "symlink_mode" if parts[2] == "120000" else "gitlink_mode"
                return f"{kind}:{s}"
    return None


def _has_dir_path_conflict(diff: str) -> str | None:
    """Reject diffs whose `+++ b/<path>` target is an existing directory
    in the working tree. `git apply --check` does NOT catch this — it
    succeeds, then `git apply` fails late with a generic
    "Directory not empty" error. Catching it up front gives a clear
    diagnostic and avoids partial applies.

    Only the destination side (`+++` / `b/`-side rename/copy targets)
    is checked. The source side is allowed to overlap with directories
    only via deletes, which we detect via `--- a/<path>` paired with
    `+++ /dev/null`; for now we conservatively check every destination
    path emitted by `_diff_paths`.
    """
    for raw in _diff_paths(diff):
        path = raw.split("\t", 1)[0]
        if not path:
            continue
        # `_diff_paths` returns repo-relative; absolute/traversal paths
        # were already rejected by `_has_unsafe_path` in the apply
        # pipeline before this helper runs.
        target = (_REPO / path)
        try:
            if target.is_dir() and not target.is_symlink():
                return f"dir_path_conflict:{path}"
        except OSError:
            # Permission or filesystem issue — let `git apply` surface it.
            continue
    return None


def _has_structural_defect(diff: str) -> str | None:
    """Return an error if the diff is malformed, else None.

    A unified diff body must contain at least one `+++ ` header and at
    least one `@@ ` hunk marker. Some malformed model outputs include
    only `--- a/PATH` (a half-diff) or include `--- ` and `+++ ` but no
    hunks at all. These would be rejected by `git apply` but with an
    opaque message; we want a distinct outcome so the log is readable.
    """
    has_minus = False
    has_plus = False
    has_hunk = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            has_minus = True
        elif line.startswith("+++ "):
            has_plus = True
        elif line.startswith("@@ ") or line.startswith("@@\t"):
            has_hunk = True
    if has_minus and not has_plus:
        return "missing_plus_header"
    if has_plus and not has_minus:
        return "missing_minus_header"
    if not has_hunk:
        # `diff --git` alone with rename-only metadata is rare from a
        # coding model and we never want to apply a rename-without-hunks.
        return "no_hunks"
    return None


_MAX_DIFF_BYTES = 256 * 1024
_MAX_DIFF_LINES = 5000


def _has_oversized_diff(diff: str) -> str | None:
    """Reject pathologically large diffs.

    A single coding fix should be a focused patch — usually <100 lines,
    rarely above ~1000. A 5000-line / 256 KB diff signals one of:
      - the model dumped the entire file rewritten as a diff,
      - context window leaked partial corpus into the response,
      - model is hallucinating mass-edits across many files.

    None of these are recoverable patches; reject before we waste
    `git apply` and a commit on them. Limits are deliberately well
    above any legitimate fix to avoid false positives.
    """
    n = len(diff)
    if n > _MAX_DIFF_BYTES:
        return f"size_bytes:{n}>{_MAX_DIFF_BYTES}"
    lines = diff.count("\n") + (0 if diff.endswith("\n") else 1)
    if lines > _MAX_DIFF_LINES:
        return f"size_lines:{lines}>{_MAX_DIFF_LINES}"
    return None


_GIT_APPLY_TIMEOUT_SECONDS = 30


def _run_git_apply(args: list[str], diff: str) -> tuple[int, str]:
    """Run `git <args>` feeding `diff` on stdin with a hard timeout.

    Returns (returncode, stderr-text). On TimeoutExpired the process is
    killed and (124, "timed_out_after_<N>s") is returned.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO,
            input=diff,
            text=True,
            capture_output=True,
            timeout=_GIT_APPLY_TIMEOUT_SECONDS,
            errors="surrogateescape",
        )
        return proc.returncode, proc.stderr.strip()[:300]
    except subprocess.TimeoutExpired:
        return 124, f"timed_out_after_{_GIT_APPLY_TIMEOUT_SECONDS}s"


def _apply_diff(diff_text: str) -> tuple[bool, str]:
    """Try `git apply --check` then `git apply`. Returns (ok, message)."""
    diff = _strip_fence(diff_text)
    if not diff.lstrip().startswith(("diff --git", "--- ")):
        return False, "not_a_unified_diff"
    # Normalise line endings. Some models emit CRLF, which `git apply`
    # rejects with "patch with CRLF line endings" by default. We never
    # want CRs in unified-diff metadata lines, and content-line CRs in
    # the working tree are independently encoded by the patch hunks.
    diff = diff.replace("\r\n", "\n").replace("\r", "\n")
    if not diff.endswith("\n"):
        diff += "\n"
    oversized = _has_oversized_diff(diff)
    if oversized is not None:
        return False, f"oversized_diff: {oversized}"
    unsafe = _has_unsafe_path(diff)
    if unsafe is not None:
        return False, f"unsafe_path: {unsafe}"
    binary = _has_binary_patch(diff)
    if binary is not None:
        return False, f"binary_patch: {binary}"
    unsafe_mode = _has_unsafe_mode(diff)
    if unsafe_mode is not None:
        return False, f"unsafe_mode: {unsafe_mode}"
    structural = _has_structural_defect(diff)
    if structural is not None:
        return False, f"malformed_diff: {structural}"
    dir_conflict = _has_dir_path_conflict(diff)
    if dir_conflict is not None:
        return False, f"dir_conflict: {dir_conflict}"
    rc, err = _run_git_apply(["apply", "--check", "-"], diff)
    if rc != 0:
        return False, f"apply_check_failed: {err}"
    rc, err = _run_git_apply(["apply", "-"], diff)
    if rc != 0:
        return False, f"apply_failed: {err}"
    return True, "applied"


_VALIDATE_TIMEOUT_SECONDS = 30


def _validate_changed_files(paths: Iterable[Path]) -> tuple[bool, str]:
    """Validate touched files by extension. Rejects diffs that produce
    syntactically invalid Python, JSON, TOML, or YAML.

    Returns (ok, message). Unknown extensions are skipped silently.
    """
    paths = [Path(p) for p in paths]
    py = [str(_REPO / p) for p in paths if p.suffix == ".py"]
    if py:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", *py],
                cwd=_REPO,
                text=True,
                capture_output=True,
                timeout=_VALIDATE_TIMEOUT_SECONDS,
                errors="surrogateescape",
            )
        except subprocess.TimeoutExpired:
            return False, f"py_invalid: timed_out_after_{_VALIDATE_TIMEOUT_SECONDS}s"
        if proc.returncode != 0:
            return False, f"py_invalid: {(proc.stdout + proc.stderr).strip()[:300]}"
        # compileall returncode=0 even when SyntaxWarning fires (e.g.
        # invalid escape sequences, "is" with a literal). These are
        # almost always real bugs we don't want to commit. Surface
        # them as a validation failure.
        if "SyntaxWarning" in proc.stderr:
            return False, f"py_syntax_warning: {proc.stderr.strip()[:300]}"

    for p in paths:
        full = _REPO / p
        if not full.is_file():
            continue
        suffix = p.suffix.lower()
        try:
            if suffix == ".json":
                import json
                json.loads(full.read_text(encoding="utf-8"))
            elif suffix == ".toml":
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore[no-redef]
                with full.open("rb") as fh:
                    tomllib.load(fh)
            elif suffix in {".yml", ".yaml"}:
                try:
                    import yaml  # type: ignore[import-not-found]
                except ModuleNotFoundError:
                    continue
                yaml.safe_load(full.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — surface any parse error
            return False, f"{suffix.lstrip('.')}_invalid:{p}: {str(exc)[:200]}"

    return True, "ok"


def _python_syntax_ok(paths: Iterable[Path]) -> tuple[bool, str]:
    """Backwards-compatible alias kept for any external callers / tests."""
    return _validate_changed_files(paths)


def _diff_in_scope(changed: Iterable[Path], target: Path) -> tuple[bool, str]:
    """The loop asks the model to fix one specific file. Reject diffs that
    touch any file other than `target` so a misbehaving model cannot
    silently rewrite the rest of the repo.

    An empty `changed` list is intentionally NOT rejected here: the
    empty-diff case is filtered later in `_commit_and_push` via its
    `git status --porcelain` check, so callers get a single
    canonical "nothing to commit" outcome there."""
    target_norm = Path(target).as_posix()
    out_of_scope = [
        Path(p).as_posix() for p in changed
        if Path(p).as_posix() != target_norm
    ]
    if out_of_scope:
        return False, f"out_of_scope:{','.join(out_of_scope[:3])}"
    return True, "ok"


# Paths the loop owns and writes to during an iteration. They must
# never count as "changes" for scope/validation purposes, otherwise a
# missing `.gitignore` or a misconfigured worktree would make every
# iteration look out-of-scope and the loop would silently never commit
# anything useful.
_INTERNAL_PATHS = {
    Path(".loop"),
    Path("STATE.md"),
}


def _is_internal_path(p: Path) -> bool:
    parts = p.parts
    if not parts:
        return False
    if Path(parts[0]) in _INTERNAL_PATHS:
        return True
    return p in _INTERNAL_PATHS


def _changed_paths() -> list[Path]:
    """Return every path in the working tree that differs from HEAD.

    Uses ``git status --porcelain=v1 -z -uall`` so the result includes
    modified *and* untracked files (`git diff` alone misses untracked
    additions, which would let an out-of-scope diff that creates a new
    file slip past `_diff_in_scope`). NUL-separated output is parsed so
    paths containing whitespace are handled correctly. Loop-internal
    artefacts (`.loop/...`, `STATE.md`) are filtered out so that a
    missing or stale `.gitignore` cannot cause every iteration to
    misclassify itself as out-of-scope.
    """
    proc = _run_git(
        "status", "--porcelain=v1", "-z", "-uall", check=False
    )
    out: list[Path] = []
    raw = proc.stdout
    i = 0
    n = len(raw)
    while i < n:
        # Each record: 2 status chars + ' ' + path + '\0'
        # Renames (R/C) look like 'R  newpath\0oldpath\0'.
        if i + 3 > n:
            break
        code = raw[i : i + 2]
        i += 3  # skip 'XY '
        end = raw.find("\0", i)
        if end < 0:
            break
        path = raw[i:end]
        i = end + 1
        if code[0] in ("R", "C"):
            # Rename/copy: a second NUL-terminated path follows (the source).
            end2 = raw.find("\0", i)
            if end2 < 0:
                break
            src = raw[i:end2]
            i = end2 + 1
            if src and not _is_internal_path(Path(src)):
                out.append(Path(src))
        if path and not _is_internal_path(Path(path)):
            out.append(Path(path))
    return out


def _revert_changes() -> None:
    """Discard every working-tree change *and* untracked files.

    `git checkout -- .` only restores tracked files; untracked additions
    (e.g. a brand-new file produced by a model diff) survive it. We
    follow up with `git clean -fd` so the tree is identical to HEAD.
    """
    _run_git("checkout", "--", ".", check=False)
    _run_git("clean", "-fd", check=False)


# ------------------------------------------------------------------- state
def _rotate_state_if_needed() -> Path | None:
    """If STATE.md is over the threshold, archive it to
    `.loop/state_archive/STATE.<UTC>.md` and leave a fresh header in
    place. Returns the archive path, or None if no rotation occurred.

    Rotation is best-effort: any error returns None and leaves
    STATE.md untouched. Archive directory is under `.loop/`, which is
    `.gitignore`d AND in `_INTERNAL_PATHS`, so the archive never
    enters a commit nor trips scope/internal-path filtering.
    """
    try:
        size = STATE_FILE.stat().st_size
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if size <= STATE_MAX_BYTES:
        return None
    try:
        STATE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = STATE_ARCHIVE_DIR / f"STATE.{ts}.md"
        # If a same-second archive already exists (clock skew, fast loop),
        # de-duplicate with a counter suffix rather than overwriting.
        if archive.exists():
            i = 1
            while (STATE_ARCHIVE_DIR / f"STATE.{ts}.{i}.md").exists():
                i += 1
            archive = STATE_ARCHIVE_DIR / f"STATE.{ts}.{i}.md"
        # Move the body atomically; replace it with a small header that
        # references the archive so future readers know history exists.
        os.replace(STATE_FILE, archive)
        header = (
            "# qwen-coder-mcp — Rolling State\n\n"
            f"_Previous entries archived to `{archive.relative_to(_REPO).as_posix()}`._\n\n"
        )
        STATE_FILE.write_text(header, "utf-8")
        return archive
    except OSError:
        return None


def _append_state(entry: str) -> None:
    _rotate_state_if_needed()
    header = "# qwen-coder-mcp — Rolling State\n\n"
    if not STATE_FILE.exists():
        STATE_FILE.write_text(header, "utf-8")
    with STATE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(entry)


def _write_history(name: str, body: str) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / name
    path.write_text(body, "utf-8")
    return path


# -------------------------------------------------------------------- core
def _abort_rebase_if_any() -> None:
    """Best-effort: if a rebase is in progress, abort it. Then if the tree
    is still dirty, hard-reset to the current HEAD to guarantee a clean
    working tree before the next iteration."""
    rebase_dir_a = _REPO / ".git" / "rebase-merge"
    rebase_dir_b = _REPO / ".git" / "rebase-apply"
    if rebase_dir_a.exists() or rebase_dir_b.exists():
        _run_git("rebase", "--abort", check=False)
    status = _run_git("status", "--porcelain", check=False).stdout
    if status.strip():
        _run_git("reset", "--hard", "HEAD", check=False)
        _run_git("clean", "-fd", check=False)


def _commit_and_push(message: str, push: bool) -> bool:
    add = _run_git("add", "-A", check=False)
    if add.returncode != 0:
        _log(f"git add failed: {add.stderr.strip()}")
        return False
    status = _run_git("status", "--porcelain", check=False).stdout
    if not status.strip():
        return False
    commit = _run_git("commit", "-m", message, check=False)
    if commit.returncode != 0:
        _log(f"git commit failed: {commit.stderr.strip()}")
        return False
    if not push:
        return True
    # Best-effort sync with remote. On rebase conflict, abort cleanly so
    # the next iteration starts from a known-good tree instead of wedging.
    pull = _run_git("pull", "--rebase", "--autostash", "origin", "main", check=False)
    if pull.returncode != 0:
        _log(f"git pull --rebase failed: {pull.stderr.strip()[:300]}")
        _abort_rebase_if_any()
        return False
    push_proc = _run_git("push", "origin", "HEAD:main", check=False)
    if push_proc.returncode != 0:
        _log(f"git push failed: {push_proc.stderr.strip()}")
        return False
    return True


def _iteration(client: QwenClient, max_bytes: int, push: bool) -> str:
    files = _candidate_files()
    if not files:
        return "no_candidate_files"
    idx = _load_cursor() % len(files)
    rel = files[idx]
    _save_cursor((idx + 1) % len(files))

    code = _read_file(_REPO / rel, max_bytes)
    if code is None:
        return f"skip:{rel} (unreadable_or_too_large)"

    deadline = time.monotonic() + _iteration_budget_seconds()

    def _over_budget() -> bool:
        return time.monotonic() > deadline

    _log(f"scanning {rel}")
    try:
        issues = client.system_user(
            prompts.REVIEWER_SYSTEM,
            prompts.find_bugs_user(str(rel), code),
            temperature=0.1,
        )
    except QwenError as exc:
        return f"qwen_error_find_bugs:{exc}"

    if _over_budget():
        return f"budget_exceeded:{rel}:after_find_bugs"

    issue = _parse_first_issue(issues)
    if not issue:
        return f"clean:{rel}"

    try:
        diff = client.system_user(
            prompts.CODER_SYSTEM,
            prompts.propose_fix_user(str(rel), code, issue),
            temperature=0.1,
        )
    except QwenError as exc:
        return f"qwen_error_propose_fix:{exc}"

    if _over_budget():
        return f"budget_exceeded:{rel}:after_propose_fix"

    diff_clean = _strip_fence(diff)

    try:
        critique = client.system_user(
            prompts.DEVILS_ADVOCATE_SYSTEM,
            prompts.devils_advocate_user(str(rel), code, diff_clean, issue),
            temperature=0.0,
        )
    except QwenError as exc:
        return f"qwen_error_devils_advocate:{exc}"

    if _over_budget():
        return f"budget_exceeded:{rel}:after_devils_advocate"

    accept, reason = _verdict_accepts(critique)
    history_body = (
        f"# {_now()} — {rel}\n\n"
        f"## Issue\n{issue}\n\n## Proposed diff\n```diff\n{diff_clean}\n```\n\n"
        f"## Devil's advocate\n{critique}\n\n## Outcome\n"
    )

    if not accept:
        _write_history(
            f"{int(time.time())}-rejected.md",
            history_body + f"REJECTED ({reason})\n",
        )
        _append_state(
            f"- {_now()} `{rel}` — rejected fix ({reason[:80]})\n"
        )
        return f"rejected:{rel}:{reason[:80]}"

    ok, msg = _apply_diff(diff_clean)
    if not ok:
        _write_history(
            f"{int(time.time())}-apply-failed.md",
            history_body + f"APPLY FAILED ({msg})\n",
        )
        _append_state(f"- {_now()} `{rel}` — apply failed ({msg[:80]})\n")
        return f"apply_failed:{rel}:{msg[:80]}"

    changed = _changed_paths()
    scope_ok, scope_msg = _diff_in_scope(changed, rel)
    if not scope_ok:
        _revert_changes()
        _write_history(
            f"{int(time.time())}-out-of-scope.md",
            history_body + f"OUT OF SCOPE ({scope_msg})\n",
        )
        _append_state(f"- {_now()} `{rel}` — reverted ({scope_msg[:60]})\n")
        return f"out_of_scope:{rel}:{scope_msg[:80]}"

    syn_ok, syn_msg = _validate_changed_files(changed)
    if not syn_ok:
        _revert_changes()
        _write_history(
            f"{int(time.time())}-syntax-failed.md",
            history_body + f"VALIDATION FAILED:\n```\n{syn_msg}\n```\n",
        )
        _append_state(f"- {_now()} `{rel}` — reverted ({syn_msg[:60]})\n")
        return f"validation_failed:{rel}"

    summary_line = issue.splitlines()[0][:72]
    commit_msg = f"fix({rel.as_posix()}): {summary_line}"
    if _commit_and_push(commit_msg, push):
        _write_history(
            f"{int(time.time())}-applied.md",
            history_body + "APPLIED + COMMITTED\n",
        )
        _append_state(f"- {_now()} `{rel}` — applied: {summary_line}\n")
        return f"applied:{rel}"

    _revert_changes()
    _append_state(f"- {_now()} `{rel}` — commit/push failed, reverted\n")
    return f"commit_failed:{rel}"


def main() -> None:
    from qwen_coder_mcp.config import load_settings  # local import
    settings = load_settings()
    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _log(
        f"loop starting | model={settings.model} base={settings.base_url} "
        f"interval={settings.loop_interval_seconds}s push={settings.loop_push}"
    )
    client = QwenClient(settings)
    try:
        while True:
            try:
                outcome = _iteration(
                    client, settings.loop_max_file_bytes, settings.loop_push
                )
                _log(f"iteration -> {outcome}")
            except Exception:  # never break the loop
                _log("iteration crashed:\n" + traceback.format_exc())
            time.sleep(max(1, settings.loop_interval_seconds))
    finally:
        client.close()


if __name__ == "__main__":
    main()
