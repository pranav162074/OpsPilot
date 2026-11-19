"""Splits documents into chunks for retrieval.

Every chunk is an exact slice of the original text (text[char_start:char_end]), so an answer
can quote and point at the passage it came from. Markdown is split along its headings first,
then by paragraph, and fenced code blocks are never cut in half unless a single block is
bigger than a whole chunk.
"""

import math
import re
from dataclasses import dataclass, field

# A rough count that is close for English prose and code. Branch 4 swaps in the embedding
# model's own tokenizer.
CHARS_PER_TOKEN = 4

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

Span = tuple[int, int]


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    heading: str
    content: str
    token_count: int
    char_start: int
    char_end: int


@dataclass
class _Section:
    heading: str
    spans: list[Span] = field(default_factory=list)


def _sections(text: str, markdown: bool) -> list[_Section]:
    """Groups the text into sections (one per heading) made of paragraph spans."""
    sections: list[_Section] = []
    path: list[tuple[int, str]] = []
    current = _Section(heading="")
    has_body = False
    para: Span | None = None
    fence: str | None = None
    pos = 0

    def end_paragraph() -> None:
        nonlocal para, has_body
        if para is not None:
            current.spans.append(para)
            has_body = True
            para = None

    for line in text.splitlines(keepends=True):
        start, pos = pos, pos + len(line)
        body = line.rstrip("\r\n")
        end = start + len(body)

        if fence is not None:
            # Inside a code block: everything belongs to it until the closing fence
            para = (para[0] if para else start, end)
            stripped = body.strip()
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = None
            continue

        if markdown and (opening := _FENCE.match(body)):
            para = (para[0] if para else start, end)
            fence = opening.group(1)
            continue

        if markdown and (heading := _HEADING.match(body)):
            end_paragraph()
            # A heading with no text under it (say "# Database" straight before "## Failover")
            # is carried into the next section rather than becoming a chunk of its own
            if has_body:
                sections.append(current)
                current = _Section(heading="")
                has_body = False
            level = len(heading.group(1))
            path = [(lvl, title) for lvl, title in path if lvl < level]
            path.append((level, heading.group(2).strip()))
            current.heading = " > ".join(title for _, title in path)
            current.spans.append((start, end))
            continue

        if not body.strip():
            end_paragraph()
            continue

        para = (para[0] if para else start, end)

    end_paragraph()
    if current.spans:
        sections.append(current)
    return sections


def _split_long(text: str, span: Span, max_tokens: int) -> list[Span]:
    """Breaks a paragraph that is too big for one chunk: at line ends, then mid-line."""
    start, end = span
    if estimate_tokens(text[start:end]) <= max_tokens:
        return [span]

    max_chars = max_tokens * CHARS_PER_TOKEN
    pieces: list[Span] = []
    line_start = start
    for line in text[start:end].splitlines(keepends=True):
        line_end = line_start + len(line.rstrip("\r\n"))
        for piece_start in range(line_start, line_end, max_chars):
            pieces.append((piece_start, min(piece_start + max_chars, line_end)))
        line_start += len(line)

    # Join neighbouring lines back up as far as the limit allows
    merged: list[Span] = []
    for piece in pieces:
        if merged and piece[1] - merged[-1][0] <= max_chars:
            merged[-1] = (merged[-1][0], piece[1])
        else:
            merged.append(piece)
    return merged


def chunk_text(
    text: str, *, markdown: bool, max_tokens: int, overlap_tokens: int
) -> list[ChunkDraft]:
    if max_tokens < 1 or not 0 <= overlap_tokens < max_tokens:
        raise ValueError("Need max_tokens >= 1 and 0 <= overlap_tokens < max_tokens")

    drafts: list[ChunkDraft] = []

    def emit(heading: str, units: list[Span]) -> None:
        start, end = units[0][0], units[-1][1]
        content = text[start:end]
        drafts.append(
            ChunkDraft(
                ordinal=len(drafts),
                heading=heading,
                content=content,
                token_count=estimate_tokens(content),
                char_start=start,
                char_end=end,
            )
        )

    def fits(units: list[Span], extra: Span) -> bool:
        return estimate_tokens(text[units[0][0] : extra[1]]) <= max_tokens

    for section in _sections(text, markdown):
        units = [piece for span in section.spans for piece in _split_long(text, span, max_tokens)]
        current: list[Span] = []
        for unit in units:
            if current and not fits(current, unit):
                emit(section.heading, current)
                # Start the next chunk with the end of this one, so a fact that straddles
                # the boundary is still whole in at least one chunk
                carry: list[Span] = []
                for previous in reversed(current):
                    if estimate_tokens(text[previous[0] : (carry or [previous])[-1][1]]) > (
                        overlap_tokens
                    ):
                        break
                    carry.insert(0, previous)
                current = carry if carry and fits(carry, unit) else []
            current.append(unit)
        if current:
            emit(section.heading, current)

    return drafts
