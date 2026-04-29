"""Loop 281: extended markdown detection in looks_like_markdown.

Inline backticks, markdown links and table rows now trigger Markdown
rendering so short replies like ``use the `grep` tool`` get monospace
highlighting instead of flat plain text.
"""

from __future__ import annotations

from qwen_coder_mcp import tui


class TestInlineCodeDetection:
    def test_paired_backticks_trigger_markdown(self) -> None:
        assert tui.looks_like_markdown("use the `grep` tool")

    def test_single_backtick_does_not(self) -> None:
        assert not tui.looks_like_markdown("the price was 5` in 1850")

    def test_three_backticks_unfenced(self) -> None:
        # 3 backticks (e.g. shell quote inside text) still triggers.
        assert tui.looks_like_markdown("a `b` c `d`")


class TestLinkDetection:
    def test_markdown_link_triggers(self) -> None:
        assert tui.looks_like_markdown("see [docs](https://example.com)")

    def test_link_without_url_does_not(self) -> None:
        assert not tui.looks_like_markdown("see [docs] for more")

    def test_link_with_text_outside(self) -> None:
        assert tui.looks_like_markdown(
            "Read [the README](README.md) before starting."
        )


class TestTableDetection:
    def test_pipe_table_triggers(self) -> None:
        text = "results:\n| col1 | col2 |\n| --- | --- |\n| a | b |"
        assert tui.looks_like_markdown(text)


class TestNumberedListExtended:
    def test_two_dot_item_triggers(self) -> None:
        # The original detector only had "\n1. ". After loop 281 we also
        # accept "\n2. " so that a list pasted starting from item 2
        # still renders.
        assert tui.looks_like_markdown("intro\n2. second\n3. third")


class TestLooksLikeMarkdownNegativesPreserved:
    def test_short_yes_no(self) -> None:
        assert not tui.looks_like_markdown("yes")

    def test_plain_paragraph(self) -> None:
        assert not tui.looks_like_markdown(
            "the answer is forty two because it is the convention"
        )

    def test_empty(self) -> None:
        assert not tui.looks_like_markdown("")
