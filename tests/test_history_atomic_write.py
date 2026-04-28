"""Loop 189 — atomic write semantics for save_history_jsonl.

The on-disk JSONL history was written with a plain ``path.open("w")``
which truncates the file before the new content is fully serialised,
so a crash (OOM, SIGKILL, power loss) mid-write would leave the user
with a partial history. Loop 189 made the write atomic via the same
``.tmp + os.replace`` dance used by ``save_agent_checkpoint``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from qwen_coder_mcp.tui import (
    ChatMessage,
    load_history_jsonl,
    save_history_jsonl,
)


def _msgs() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="world"),
    ]


def test_round_trip_preserves_messages(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    n = save_history_jsonl(_msgs(), p)
    assert n == 2
    out = load_history_jsonl(p)
    assert [(m.role, m.content) for m in out] == [
        ("user", "hello"),
        ("assistant", "world"),
    ]


def test_does_not_leave_tmp_sibling_on_success(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    save_history_jsonl(_msgs(), p)
    siblings = sorted(x.name for x in tmp_path.iterdir())
    assert siblings == ["h.jsonl"]
    assert not (tmp_path / "h.jsonl.tmp").exists()


def test_replace_preserves_old_file_when_write_fails(
    tmp_path: Path,
) -> None:
    """If the os.replace step blows up after the .tmp is written,
    the *original* file must still be readable. (We simulate the
    failure by monkey-patching os.replace.)"""
    p = tmp_path / "h.jsonl"
    save_history_jsonl(
        [ChatMessage(role="user", content="original")], p
    )
    assert load_history_jsonl(p)[0].content == "original"

    with patch("qwen_coder_mcp.tui.os.replace", side_effect=OSError):
        n = save_history_jsonl(
            [ChatMessage(role="user", content="new")], p
        )
    assert n == 0
    # The original file is intact.
    assert load_history_jsonl(p)[0].content == "original"


def test_partial_write_does_not_corrupt_existing_file(
    tmp_path: Path,
) -> None:
    """If serialisation itself raises mid-loop, the original file is
    untouched because the bad data only ever lived in .tmp."""
    p = tmp_path / "h.jsonl"
    save_history_jsonl(
        [ChatMessage(role="user", content="original")], p
    )

    class _Boom:
        role = "user"

        @property
        def content(self) -> str:  # noqa: D401
            raise RuntimeError("serialisation failure")

    with pytest.raises(RuntimeError):
        save_history_jsonl([_Boom()], p)  # type: ignore[list-item]

    # Original survives because we never truncated the real file.
    assert load_history_jsonl(p)[0].content == "original"


def test_tmp_sibling_cleaned_up_after_replace_failure(
    tmp_path: Path,
) -> None:
    p = tmp_path / "h.jsonl"
    save_history_jsonl(_msgs(), p)
    with patch("qwen_coder_mcp.tui.os.replace", side_effect=OSError):
        save_history_jsonl(_msgs(), p)
    # The .tmp sibling should have been unlinked after the failed replace.
    assert not (tmp_path / "h.jsonl.tmp").exists()


def test_writes_under_atomic_tmp_path_during_serialisation(
    tmp_path: Path,
) -> None:
    """While save_history_jsonl is running its serialisation loop,
    the live data lives in <path>.tmp, not in <path>. We assert this
    by hooking os.replace to inspect filesystem state at the moment
    of replacement."""
    p = tmp_path / "h.jsonl"
    seen: dict[str, bool] = {}

    real_replace = os.replace

    def _spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen["tmp_existed"] = Path(src).exists()
        seen["src_was_tmp"] = str(src).endswith(".tmp")
        real_replace(src, dst)

    with patch("qwen_coder_mcp.tui.os.replace", side_effect=_spy):
        save_history_jsonl(_msgs(), p)

    assert seen == {"tmp_existed": True, "src_was_tmp": True}


def test_truncates_to_max_messages(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    msgs = [ChatMessage(role="user", content=str(i)) for i in range(10)]
    n = save_history_jsonl(msgs, p, max_messages=3)
    assert n == 3
    loaded = load_history_jsonl(p)
    assert [m.content for m in loaded] == ["7", "8", "9"]


def test_each_line_is_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    save_history_jsonl(_msgs(), p)
    raw = p.read_text(encoding="utf-8").splitlines()
    decoded = [json.loads(line) for line in raw]
    assert decoded == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
