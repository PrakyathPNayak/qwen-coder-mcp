"""Loop 135 tests: shell_tools (run / grep / find) + safety + sandboxing."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, shell_tools


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


# ---------------------------------------------------------------- run_shell
class TestRunShell:
    def test_basic_echo(self, tmp_path: Path) -> None:
        res = shell_tools.run_shell(_cfg(tmp_path), "echo hi")
        assert res.returncode == 0
        assert "hi" in res.stdout
        assert res.timed_out is False

    def test_nonzero_exit(self, tmp_path: Path) -> None:
        res = shell_tools.run_shell(_cfg(tmp_path), "false")
        assert res.returncode != 0

    def test_cwd_is_repo_root(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("X")
        res = shell_tools.run_shell(_cfg(tmp_path), "ls")
        assert "marker.txt" in res.stdout

    def test_cwd_arg_resolves_inside_root(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "inside.txt").write_text("Y")
        res = shell_tools.run_shell(_cfg(tmp_path), "ls", cwd="sub")
        assert "inside.txt" in res.stdout

    def test_cwd_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError):
            shell_tools.run_shell(_cfg(tmp_path), "ls", cwd="../..")

    def test_empty_command_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(shell_tools.ShellError):
            shell_tools.run_shell(_cfg(tmp_path), "   ")

    def test_timeout_kills(self, tmp_path: Path) -> None:
        res = shell_tools.run_shell(_cfg(tmp_path), "sleep 5", timeout=0.5)
        assert res.timed_out is True
        assert "timeout" in res.stderr.lower()

    def test_output_truncation(self, tmp_path: Path) -> None:
        # Generate a lot of output, force a small cap.
        res = shell_tools.run_shell(
            _cfg(tmp_path),
            "yes hello | head -c 10000",
            output_cap=200,
        )
        assert res.truncated is True
        assert "[truncated" in res.stdout


class TestDenylist:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -rf / --no-preserve-root",
        "sudo apt install foo",
        "mkfs.ext4 /dev/sda",
        "dd of=/dev/sda if=/dev/zero",
        ":(){ :|:& };:",
        "shutdown -h now",
        "reboot",
        "chmod -R 777 /",
        "chown -R root /",
    ])
    def test_rejects(self, tmp_path: Path, cmd: str) -> None:
        with pytest.raises(shell_tools.ShellError):
            shell_tools.run_shell(_cfg(tmp_path), cmd)

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "rm -rf build",  # not / -- allowed
        "git status",
        "pytest -q",
        "python -c 'print(1)'",
    ])
    def test_allows_normal(self, tmp_path: Path, cmd: str) -> None:
        # We don't care about the output here, only that the deny list
        # does not trip. Some commands may exit nonzero; that's fine.
        try:
            shell_tools.run_shell(_cfg(tmp_path), cmd, timeout=2.0)
        except shell_tools.ShellError as exc:  # pragma: no cover
            pytest.fail(f"unexpectedly denied: {exc}")


# ---------------------------------------------------------------- grep
class TestGrep:
    def test_finds_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def foo(): pass\nfoo_count = 1\n")
        (tmp_path / "b.py").write_text("nothing here\n")
        hits = shell_tools.grep(_cfg(tmp_path), r"\bfoo\b")
        paths = {h.path for h in hits}
        assert paths == {"a.py"}

    def test_recursive(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "x.txt").write_text("needle\n")
        hits = shell_tools.grep(_cfg(tmp_path), "needle")
        assert any(h.path.endswith("x.txt") for h in hits)

    def test_path_subtree(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "outside.txt").write_text("needle\n")
        (sub / "inside.txt").write_text("needle\n")
        hits = shell_tools.grep(_cfg(tmp_path), "needle", path="sub")
        assert all("sub" in h.path for h in hits)

    def test_skips_dotgit(self, tmp_path: Path) -> None:
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "obj").write_text("needle\n")
        (tmp_path / "src.py").write_text("needle\n")
        hits = shell_tools.grep(_cfg(tmp_path), "needle")
        assert all(".git" not in h.path for h in hits)
        assert any("src.py" in h.path for h in hits)

    def test_skips_binary(self, tmp_path: Path) -> None:
        (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02needle\x03")
        (tmp_path / "text.txt").write_text("needle\n")
        hits = shell_tools.grep(_cfg(tmp_path), "needle")
        assert {h.path for h in hits} == {"text.txt"}

    def test_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("Hello world\n")
        hits = shell_tools.grep(_cfg(tmp_path), "hello", case_insensitive=True)
        assert hits

    def test_empty_pattern(self, tmp_path: Path) -> None:
        with pytest.raises(shell_tools.ShellError):
            shell_tools.grep(_cfg(tmp_path), "")

    def test_bad_regex(self, tmp_path: Path) -> None:
        with pytest.raises(shell_tools.ShellError):
            shell_tools.grep(_cfg(tmp_path), "[unclosed")

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError):
            shell_tools.grep(_cfg(tmp_path), "x", path="../..")

    def test_max_hits_cap(self, tmp_path: Path) -> None:
        (tmp_path / "many.txt").write_text("\n".join(["needle"] * 500) + "\n")
        hits = shell_tools.grep(_cfg(tmp_path), "needle", max_hits=10)
        assert len(hits) == 10


# ---------------------------------------------------------------- find
class TestFind:
    def test_glob(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        out = shell_tools.find(_cfg(tmp_path), "*.py")
        assert set(out) == {"a.py", "b.py"}

    def test_recursive_glob(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        out = shell_tools.find(_cfg(tmp_path), "**/*.py")
        assert any("deep.py" in p for p in out)

    def test_skips_excluded_dirs(self, tmp_path: Path) -> None:
        d = tmp_path / "node_modules"
        d.mkdir()
        (d / "x.py").write_text("")
        (tmp_path / "real.py").write_text("")
        out = shell_tools.find(_cfg(tmp_path), "**/*.py")
        assert "node_modules/x.py" not in out
        assert "real.py" in out

    def test_empty_pattern(self, tmp_path: Path) -> None:
        with pytest.raises(shell_tools.ShellError):
            shell_tools.find(_cfg(tmp_path), "")

    def test_path_escape(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError):
            shell_tools.find(_cfg(tmp_path), "*", path="../..")

    def test_max_results(self, tmp_path: Path) -> None:
        for i in range(20):
            (tmp_path / f"f{i}.txt").write_text("")
        out = shell_tools.find(_cfg(tmp_path), "*.txt", max_results=5)
        assert len(out) == 5
