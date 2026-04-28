"""Loop 129: filesystem MCP tools (read_file, list_dir, write_file,
apply_patch) for claude-code / ml-intern style filesystem access.

All paths are confined to a configurable repository root via realpath
resolution -- a relative or symlinked path that escapes the root is
rejected. Reads and writes are byte-capped. apply_patch shells out to
`git apply` for unified-diff application.

Pure helpers; the server layer owns the only mutable state (the
configured root).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 200_000
DEFAULT_MAX_WRITE_BYTES = 1_000_000
DEFAULT_MAX_LIST_ENTRIES = 500


@dataclass(frozen=True)
class FsConfig:
    root: Path
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    max_list_entries: int = DEFAULT_MAX_LIST_ENTRIES


class FsError(Exception):
    """Raised on any filesystem-tool error -- caller surfaces as text."""


def _resolve_inside_root(cfg: FsConfig, rel: str) -> Path:
    """Resolve `rel` against `cfg.root` and reject any path that, after
    symlink resolution, escapes the root. The root itself is allowed."""
    if rel is None:
        raise FsError("path must be provided")
    rel = str(rel)
    if not rel:
        raise FsError("path must be non-empty")
    root = cfg.root.resolve(strict=False)
    candidate = (root / rel).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FsError(f"path escapes repo root: {rel}") from exc
    return candidate


def read_file(cfg: FsConfig, rel: str) -> dict[str, object]:
    """Return `{path, size, text, truncated}` for a file inside the root."""
    p = _resolve_inside_root(cfg, rel)
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    raw = p.read_bytes()
    truncated = len(raw) > cfg.max_read_bytes
    body = raw[: cfg.max_read_bytes] if truncated else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FsError(f"binary file: {rel}") from exc
    return {
        "path": rel,
        "size": len(raw),
        "text": text,
        "truncated": truncated,
    }


def list_dir(cfg: FsConfig, rel: str = ".") -> dict[str, object]:
    """Return `{path, entries: [{name, kind, size}], truncated}`."""
    p = _resolve_inside_root(cfg, rel) if rel else cfg.root.resolve(strict=False)
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if not p.is_dir():
        raise FsError(f"not a directory: {rel}")
    entries: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(p))
    except OSError as exc:
        raise FsError(f"listdir failed: {exc}") from exc
    truncated = len(names) > cfg.max_list_entries
    for name in names[: cfg.max_list_entries]:
        child = p / name
        try:
            if child.is_dir():
                entries.append({"name": name, "kind": "dir", "size": 0})
            elif child.is_symlink():
                entries.append({"name": name, "kind": "symlink", "size": 0})
            else:
                entries.append(
                    {"name": name, "kind": "file", "size": child.stat().st_size}
                )
        except OSError:
            entries.append({"name": name, "kind": "unknown", "size": 0})
    return {"path": rel, "entries": entries, "truncated": truncated}


def write_file(cfg: FsConfig, rel: str, content: str, *, create_parents: bool = False) -> dict[str, object]:
    """Write `content` (utf-8) to `rel` inside the root. Returns metadata.

    Refuses to write if `len(content.encode('utf-8')) > max_write_bytes`.
    Creates parent directories only if `create_parents=True`."""
    p = _resolve_inside_root(cfg, rel)
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    encoded = content.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        raise FsError(
            f"content too large ({len(encoded)} > {cfg.max_write_bytes})"
        )
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.parent.exists():
        raise FsError(f"parent does not exist: {p.parent}")
    # Atomic write via .tmp + os.replace so a crash mid-write can never
    # leave the target file half-written. Mirrors save_agent_checkpoint
    # and save_history_jsonl. The .tmp lives next to the target so
    # os.replace stays within one filesystem.
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FsError(f"write failed: {exc}") from exc
    return {"path": rel, "size": len(encoded), "created": True}


def apply_patch(cfg: FsConfig, diff_text: str, *, check_only: bool = False) -> dict[str, object]:
    """Apply a unified diff via `git apply`. Returns `{ok, message}`.

    `check_only=True` runs `git apply --check` -- useful for the TUI to
    preview applicability without mutating the tree.
    """
    if not diff_text or not diff_text.strip():
        raise FsError("diff is empty")
    root = cfg.root.resolve(strict=False)
    cmd = ["git", "apply"]
    if check_only:
        cmd.append("--check")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(diff_text)
        diff_path = fh.name
    try:
        cmd.append(diff_path)
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = proc.returncode == 0
        msg = (proc.stdout + proc.stderr).strip()
        return {"ok": ok, "check_only": check_only, "message": msg}
    finally:
        try:
            os.unlink(diff_path)
        except OSError:
            pass


def format_read(res: dict[str, object]) -> str:
    head = f"# {res['path']} (size={res['size']}"
    if res.get("truncated"):
        head += ", truncated"
    head += ")\n"
    return head + str(res.get("text", ""))


def format_list(res: dict[str, object]) -> str:
    lines = [f"# {res['path']}"]
    for e in res["entries"]:  # type: ignore[index]
        kind = e["kind"]
        size = e.get("size", 0)
        if kind == "dir":
            lines.append(f"  {e['name']}/")
        elif kind == "symlink":
            lines.append(f"  {e['name']} -> (symlink)")
        else:
            lines.append(f"  {e['name']}  {size}")
    if res.get("truncated"):
        lines.append("  ... truncated")
    return "\n".join(lines)
