"""Tests for `_read_file` symlink-escape guard.

A symlink committed in the repo pointing at `/etc/passwd` (or any
absolute path outside `_REPO`) must NOT leak its content into the
model prompt — `_read_file` resolves the path and refuses anything
not under `_REPO`.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    import agent.loop as L
    monkeypatch.setattr(L, "_REPO", tmp_path)
    return tmp_path, L


def test_reads_normal_file(repo):
    root, L = repo
    f = root / "ok.py"
    f.write_text("x = 1\n", "utf-8")
    assert L._read_file(f, max_bytes=1000) == "x = 1\n"


def test_refuses_symlink_pointing_outside_repo(repo, tmp_path_factory):
    root, L = repo
    outside_root = tmp_path_factory.mktemp("outside")
    secret = outside_root / "secret.txt"
    secret.write_text("TOPSECRET\n", "utf-8")

    link = root / "leak.py"
    os.symlink(secret, link)

    assert L._read_file(link, max_bytes=1000) is None


def test_allows_symlink_pointing_inside_repo(repo):
    root, L = repo
    target = root / "target.py"
    target.write_text("y = 2\n", "utf-8")
    link = root / "alias.py"
    os.symlink(target, link)
    assert L._read_file(link, max_bytes=1000) == "y = 2\n"


def test_refuses_too_large_file(repo):
    root, L = repo
    f = root / "big.py"
    f.write_text("x" * 100, "utf-8")
    assert L._read_file(f, max_bytes=10) is None


def test_refuses_invalid_utf8(repo):
    root, L = repo
    f = root / "bad.py"
    f.write_bytes(b"\xff\xfe not utf8\n")
    assert L._read_file(f, max_bytes=1000) is None


def test_missing_file_returns_none(repo):
    root, L = repo
    assert L._read_file(root / "nope.py", max_bytes=1000) is None


def test_refuses_dangling_symlink(repo):
    root, L = repo
    link = root / "dangling.py"
    os.symlink(root / "does_not_exist", link)
    # resolve(strict=True) raises FileNotFoundError -> None.
    assert L._read_file(link, max_bytes=1000) is None
