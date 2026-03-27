import asyncio

import pytest

from penta.services.stream_parser import async_lines


async def _lines_from_bytes(chunks: list[bytes]) -> list[str]:
    """Helper: feed byte chunks into a StreamReader, collect parsed lines."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()

    result = []
    async for line in async_lines(reader):
        result.append(line)
    return result


class TestAsyncLines:
    @pytest.mark.asyncio
    async def test_single_complete_line(self):
        lines = await _lines_from_bytes([b'{"type": "init"}\n'])
        assert lines == ['{"type": "init"}']

    @pytest.mark.asyncio
    async def test_multiple_lines(self):
        lines = await _lines_from_bytes([b'line1\nline2\nline3\n'])
        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_partial_chunks(self):
        lines = await _lines_from_bytes([b'hel', b'lo wo', b'rld\n'])
        assert lines == ["hello world"]

    @pytest.mark.asyncio
    async def test_line_split_across_chunks(self):
        lines = await _lines_from_bytes([b'first\nsec', b'ond\nthird\n'])
        assert lines == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_trailing_data_without_newline(self):
        lines = await _lines_from_bytes([b'complete\npartial'])
        assert lines == ["complete", "partial"]

    @pytest.mark.asyncio
    async def test_empty_lines_skipped(self):
        lines = await _lines_from_bytes([b'a\n\n\nb\n'])
        assert lines == ["a", "b"]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        lines = await _lines_from_bytes([])
        assert lines == []

    @pytest.mark.asyncio
    async def test_whitespace_only_lines_skipped(self):
        lines = await _lines_from_bytes([b'data\n   \n  \nmore\n'])
        assert lines == ["data", "more"]
