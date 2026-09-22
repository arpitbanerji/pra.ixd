"""Tests for Markdown -> Telegram HTML rendering and chunking."""

from ohbot.render import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    escape_html,
    split_for_telegram,
    strip_html,
    to_telegram_html,
)


def test_escape_html_covers_telegram_markup_chars():
    assert escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_inline_formatting():
    assert to_telegram_html("**bold**") == "<b>bold</b>"
    assert to_telegram_html("__bold__") == "<b>bold</b>"
    assert to_telegram_html("~~gone~~") == "<s>gone</s>"
    assert to_telegram_html("a *slanted* b") == "a <i>slanted</i> b"


def test_links_escape_ampersands_in_href():
    assert to_telegram_html("[docs](https://example.com/a?b=1&c=2)") == (
        '<a href="https://example.com/a?b=1&amp;c=2">docs</a>'
    )


def test_html_in_agent_text_is_neutralised():
    assert to_telegram_html("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_inline_code_is_not_treated_as_markdown():
    assert to_telegram_html("`a *b* c`") == "<code>a *b* c</code>"


def test_fenced_code_is_escaped_and_labelled():
    assert to_telegram_html("```python\nx = 1 < 2\n```") == (
        '<pre><code class="language-python">x = 1 &lt; 2</code></pre>'
    )


def test_headings_bullets_quotes_and_rules():
    html = to_telegram_html("# Title\n- one\n1. first\n> quoted\n---")
    assert "<b>Title</b>" in html
    assert "• one" in html
    assert "1. first" in html
    assert "<blockquote>quoted</blockquote>" in html
    assert "————————" in html


def test_split_returns_nothing_for_blank_input():
    assert split_for_telegram("   \n ") == []


def test_split_reopens_code_fences_across_chunks():
    body = "\n".join(f"value_{i} = {i} * 2" for i in range(500))
    chunks = split_for_telegram(f"```python\n{body}\n```")
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(to_telegram_html(chunk)) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert chunk.count("```") % 2 == 0


def test_split_hard_splits_a_single_giant_line():
    chunks = split_for_telegram("x" * 20000)
    assert len(chunks) >= 5
    for chunk in chunks:
        assert len(to_telegram_html(chunk)) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH


def test_split_hard_splits_giant_line_inside_fence():
    chunks = split_for_telegram("```json\n" + "a" * 15000 + "\n```")
    for chunk in chunks:
        assert len(to_telegram_html(chunk)) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert chunk.count("```") % 2 == 0


def test_strip_html_returns_readable_text():
    assert strip_html("<b>hi</b> &amp; bye") == "hi & bye"
