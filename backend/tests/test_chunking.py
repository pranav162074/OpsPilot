import re

import pytest

from opspilot.ingestion.chunking import ChunkDraft, chunk_text, estimate_tokens


def chunk(
    text: str, markdown: bool = True, max_tokens: int = 400, overlap: int = 0
) -> list[ChunkDraft]:
    return chunk_text(text, markdown=markdown, max_tokens=max_tokens, overlap_tokens=overlap)


def assert_exact_slices(text: str, chunks: list[ChunkDraft]) -> None:
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.content
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def assert_covers_all_text(text: str, chunks: list[ChunkDraft]) -> None:
    covered = [False] * len(text)
    for c in chunks:
        covered[c.char_start : c.char_end] = [True] * (c.char_end - c.char_start)
    missing = "".join(ch for ch, hit in zip(text, covered, strict=True) if not hit)
    assert not missing.strip(), f"text left out of every chunk: {missing!r}"


def test_splits_on_headings_and_keeps_the_heading_path() -> None:
    text = "# Database\n\n## Failover\n\nPromote the replica.\n\n## Backups\n\nNightly at 02:00.\n"

    chunks = chunk(text)

    assert [c.heading for c in chunks] == ["Database > Failover", "Database > Backups"]
    # The empty "# Database" section is folded into the next one, not a chunk of its own
    assert chunks[0].content == "# Database\n\n## Failover\n\nPromote the replica."
    assert chunks[1].content == "## Backups\n\nNightly at 02:00."
    assert_exact_slices(text, chunks)


def test_text_before_the_first_heading_has_no_heading() -> None:
    chunks = chunk("Intro line.\n\n# Steps\n\nDo it.\n")

    assert [(c.heading, c.content) for c in chunks] == [
        ("", "Intro line."),
        ("Steps", "# Steps\n\nDo it."),
    ]


def test_code_blocks_are_not_split_or_read_as_headings() -> None:
    text = (
        "# Deploy\n\n```bash\n# not a heading\n\nkubectl rollout restart deploy/api\n```\n\nDone.\n"
    )

    chunks = chunk(text)

    assert len(chunks) == 1
    assert chunks[0].heading == "Deploy"
    assert "# not a heading\n\nkubectl" in chunks[0].content


def test_plain_text_has_no_headings() -> None:
    chunks = chunk("# just a line\n\nmore\n", markdown=False)

    assert [c.heading for c in chunks] == [""]


def test_chunks_respect_the_size_limit_and_cover_everything() -> None:
    paragraphs = [
        f"Step {i}: " + "check the logs and the dashboards. " * (i % 7 + 1) for i in range(60)
    ]
    text = "# Runbook\n\n" + "\n\n".join(paragraphs) + "\n"

    chunks = chunk(text, max_tokens=60, overlap=15)

    assert len(chunks) > 5
    assert all(c.token_count <= 60 for c in chunks)
    assert all(c.token_count == estimate_tokens(c.content) for c in chunks)
    assert_exact_slices(text, chunks)
    assert_covers_all_text(text, chunks)


def test_a_single_huge_line_is_cut_into_pieces() -> None:
    text = "x" * 1000

    chunks = chunk(text, markdown=False, max_tokens=50)

    assert [len(c.content) for c in chunks] == [200] * 5
    assert_exact_slices(text, chunks)


def test_neighbouring_chunks_overlap() -> None:
    text = "\n\n".join(f"Fact number {i} is here." for i in range(20))

    chunks = chunk(text, max_tokens=30, overlap=8)

    for before, after in zip(chunks, chunks[1:], strict=False):
        assert after.char_start < before.char_end, (
            "expected the next chunk to repeat the last paragraph"
        )
        assert after.char_start > before.char_start


def test_no_overlap_when_disabled() -> None:
    text = "\n\n".join(f"Fact number {i} is here." for i in range(20))

    chunks = chunk(text, max_tokens=30, overlap=0)

    for before, after in zip(chunks, chunks[1:], strict=False):
        assert after.char_start >= before.char_end


def test_windows_line_endings() -> None:
    text = "# A\r\n\r\nOne.\r\n\r\n# B\r\n\r\nTwo.\r\n"

    chunks = chunk(text)

    assert [c.heading for c in chunks] == ["A", "B"]
    assert_exact_slices(text, chunks)
    assert all(not re.search(r"\r\n$", c.content) for c in chunks)


@pytest.mark.parametrize("text", ["", "   \n\n\t\n"])
def test_blank_text_has_no_chunks(text: str) -> None:
    assert chunk(text) == []


@pytest.mark.parametrize(("max_tokens", "overlap"), [(0, 0), (10, 10), (10, -1)])
def test_rejects_bad_settings(max_tokens: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        chunk("text", max_tokens=max_tokens, overlap=overlap)
