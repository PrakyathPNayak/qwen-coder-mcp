"""Loop 256: regex pattern slicing for fs_read.

Tests cover ``fs_tools.read_file(pattern=...)`` plus the
``_tool_fs_read`` agent wrapper. The pattern path returns matched
lines with ``before``/``after`` context, merges overlapping windows,
and separates non-contiguous groups with ``--`` lines.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _write(cfg: fs_tools.FsConfig, name: str, body: str) -> str:
    p = cfg.root / name
    p.write_text(body, encoding="utf-8")
    return name


class TestFsReadPattern:
    def test_basic_match_returns_only_matching_lines(self, cfg):
        body = "alpha\nbeta\ngamma\nbeta-two\nepsilon\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"^beta")
        assert res["match_lines"] == [2, 4]
        text = res["text"]
        assert "2 | beta\n" in text
        assert "4 | beta-two\n" in text
        assert "alpha" not in text
        assert "gamma" not in text  # gap > before+after

    def test_before_after_context(self, cfg):
        body = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"line5", before=2, after=2)
        assert res["match_lines"] == [5]
        text = res["text"]
        for i in range(3, 8):
            assert f"line{i}" in text
        assert "line2" not in text
        assert "line8" not in text

    def test_overlapping_windows_merge(self, cfg):
        body = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(
            cfg, rel, pattern=r"line(5|7)", before=1, after=1
        )
        assert res["match_lines"] == [5, 7]
        # Windows: [4,6] and [6,8] -> merged to [4,8]. No "--" separator.
        assert "--" not in res["text"]
        for i in range(4, 9):
            assert f"line{i}" in res["text"]

    def test_separator_between_non_contiguous_groups(self, cfg):
        body = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(
            cfg, rel, pattern=r"^line(2|15)$", before=0, after=0
        )
        assert res["match_lines"] == [2, 15]
        assert "--\n" in res["text"]
        assert "2 | line2" in res["text"]
        assert "15 | line15" in res["text"]

    def test_ignore_case(self, cfg):
        body = "Alpha\nALPHA-2\nbeta\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"alpha", ignore_case=True)
        assert res["match_lines"] == [1, 2]

    def test_no_matches_returns_empty_text(self, cfg):
        body = "alpha\nbeta\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"NOTHING_HERE")
        assert res["match_lines"] == []
        assert res["text"] == ""
        assert res["truncated"] is False

    def test_invalid_regex_raises(self, cfg):
        rel = _write(cfg, "f.txt", "x\n")
        with pytest.raises(fs_tools.FsError, match="invalid regex"):
            fs_tools.read_file(cfg, rel, pattern=r"(unclosed")

    def test_negative_before_or_after_raises(self, cfg):
        rel = _write(cfg, "f.txt", "x\n")
        with pytest.raises(fs_tools.FsError):
            fs_tools.read_file(cfg, rel, pattern="x", before=-1)
        with pytest.raises(fs_tools.FsError):
            fs_tools.read_file(cfg, rel, pattern="x", after=-1)

    def test_max_matches_caps_results(self, cfg):
        body = "hit\n" * 10
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"hit", max_matches=3)
        assert len(res["match_lines"]) == 3

    def test_pattern_composes_with_range(self, cfg):
        body = "match\n" + "\n".join(f"line{i}" for i in range(1, 6)) + "\nmatch\n"
        # Layout: 1=match, 2..6=line1..line5, 7=match
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(
            cfg, rel, pattern=r"^match$", start_line=2, end_line=6
        )
        assert res["match_lines"] == []
        # Now restrict to a slice that includes match #2 only.
        res2 = fs_tools.read_file(
            cfg, rel, pattern=r"^match$", start_line=5, end_line=8
        )
        assert res2["match_lines"] == [7]

    def test_line_numbers_always_present_in_pattern_output(self, cfg):
        body = "alpha\nbeta\n"
        rel = _write(cfg, "f.txt", body)
        res = fs_tools.read_file(cfg, rel, pattern=r"alpha")
        assert "1 | alpha" in res["text"]


class TestAgentToolFsReadPattern:
    def test_agent_tool_emits_pattern_header(self, cfg, tmp_path):
        body = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
        _write(cfg, "f.txt", body)
        out = agent_loop._tool_fs_read(
            {"path": "f.txt", "pattern": r"line5", "before": 1, "after": 1}, cfg
        )
        assert out.startswith("# f.txt pattern=")
        assert "matches=1" in out
        assert "4 | line4" in out
        assert "5 | line5" in out
        assert "6 | line6" in out

    def test_agent_tool_no_match_says_so(self, cfg):
        _write(cfg, "f.txt", "alpha\nbeta\n")
        out = agent_loop._tool_fs_read(
            {"path": "f.txt", "pattern": r"zzz"}, cfg
        )
        assert "matches=0" in out
        assert "(no matches)" in out

    def test_agent_tool_pattern_must_be_string(self, cfg):
        _write(cfg, "f.txt", "x\n")
        out = agent_loop._tool_fs_read(
            {"path": "f.txt", "pattern": 123}, cfg
        )
        assert out.startswith("error:")

    def test_agent_tool_back_compat_no_pattern(self, cfg):
        _write(cfg, "f.txt", "alpha\nbeta\n")
        out = agent_loop._tool_fs_read({"path": "f.txt"}, cfg)
        assert "alpha" in out and "beta" in out
        # No pattern header on plain reads.
        assert "pattern=" not in out

    def test_protocol_doc_mentions_pattern(self):
        assert "pattern" in agent_loop.TOOL_PROTOCOL_DOC
        assert "before" in agent_loop.TOOL_PROTOCOL_DOC
        assert "after" in agent_loop.TOOL_PROTOCOL_DOC
