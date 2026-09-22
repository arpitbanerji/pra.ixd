"""Convert agent Markdown into Telegram-safe HTML and split it into messages.

Telegram messages cap out at 4096 characters and only accept a small HTML
subset. Splitting naively breaks code fences and HTML tags, so the source is
split *before* rendering: each chunk is rendered independently and therefore
always carries balanced tags.
"""

from __future__ import annotations

import re

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Leave room for the tags we add while rendering (bold, links, code wrappers).
_RENDER_HEADROOM = 400

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parser treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """Apply inline Markdown to an already-escaped line."""
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    return text


def to_telegram_html(markdown: str) -> str:
    """Render Markdown to the HTML subset Telegram accepts."""
    blocks: list[str] = []
    spans: list[str] = []

    def _stash_block(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        body = escape_html(match.group(2).rstrip("\n"))
        attr = f' class="language-{escape_html(lang)}"' if lang else ""
        blocks.append(f"<pre><code{attr}>{body}</code></pre>")
        return f"\x00B{len(blocks) - 1}\x00"

    def _stash_span(match: re.Match[str]) -> str:
        spans.append(f"<code>{escape_html(match.group(1))}</code>")
        return f"\x00S{len(spans) - 1}\x00"

    # Code is extracted first so its contents are never touched by Markdown rules.
    text = _FENCE_RE.sub(_stash_block, markdown)
    text = _INLINE_CODE_RE.sub(_stash_span, text)

    out: list[str] = []
    for line in text.split("\n"):
        if _HR_RE.match(line):
            out.append("————————")
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            out.append(f"<blockquote>{_inline(escape_html(quote.group(1)))}</blockquote>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            out.append(f"<b>{_inline(escape_html(heading.group(2)))}</b>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out.append(f"{bullet.group(1)}• {_inline(escape_html(bullet.group(2)))}")
            continue
        ordered = _ORDERED_RE.match(line)
        if ordered:
            out.append(
                f"{ordered.group(1)}{ordered.group(2)}. {_inline(escape_html(ordered.group(3)))}"
            )
            continue
        out.append(_inline(escape_html(line)))
    rendered = "\n".join(out)

    rendered = rendered.replace("&amp;#", "&#")
    return re.sub(
        r"\x00([BS])(\d+)\x00",
        lambda m: (blocks if m.group(1) == "B" else spans)[int(m.group(2))],
        rendered,
    )


def strip_html(text: str) -> str:
    """Best-effort plain-text view, used when Telegram rejects our HTML."""
    import html as _html

    return _html.unescape(re.sub(r"<[^>]+>", "", text))


def split_for_telegram(
    markdown: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split Markdown into chunks whose rendered HTML fits in one Telegram message.

    Code fences are tracked so a fence spanning a boundary is closed at the end of
    one chunk and reopened at the start of the next, keeping every chunk valid.
    """
    if not markdown.strip():
        return []

    budget = max(1, limit - _RENDER_HEADROOM)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence: str | None = None  # language of the open fence; None means "not in one"

    def flush() -> None:
        """Emit the current chunk, closing and reopening a fence if one is open."""
        nonlocal current, size
        if not current:
            return
        body = "\n".join(current)
        if fence is not None:
            body = f"{body}\n```"
        chunks.append(body)
        current = [f"```{fence}"] if fence is not None else []
        size = sum(len(part) + 1 for part in current)

    for raw_line in markdown.split("\n"):
        remaining = raw_line
        while True:
            # -1 accounts for the newline that will join this line to the chunk.
            available = budget - size - 1
            if available <= 0:
                flush()
                available = budget - size - 1
            if not current:
                available = max(available, 1)
            if len(remaining) <= available:
                current.append(remaining)
                size += len(remaining) + 1
                break
            current.append(remaining[:available])
            size += available + 1
            remaining = remaining[available:]
            flush()

        if raw_line.lstrip().startswith("```"):
            fence = None if fence is not None else raw_line.strip()[3:].strip()

    flush()

    # Final guard: a chunk can still overflow if it contains many inline tags.
    safe: list[str] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        if len(to_telegram_html(chunk)) <= limit:
            safe.append(chunk)
            continue
        mid = max(1, len(chunk) // 2)
        cut = chunk.rfind("\n", 0, mid)
        if cut <= 0:
            cut = mid
        safe.extend(split_for_telegram(chunk[:cut], limit))
        safe.extend(split_for_telegram(chunk[cut:], limit))
    return safe
