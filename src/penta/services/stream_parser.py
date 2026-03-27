from __future__ import annotations

from typing import AsyncIterator

import asyncio


async def async_lines(reader: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield complete newline-delimited lines from an asyncio StreamReader.

    Handles partial reads and buffering. Yields stripped non-empty lines.
    """
    buffer = b""
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            # EOF — flush remaining buffer
            if buffer:
                line = buffer.decode("utf-8", errors="replace").strip()
                if line:
                    yield line
            break
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                yield line
