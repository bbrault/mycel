from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord_bot import chunk_message


class TestChunkMessage:
    def test_short_message_single_chunk(self) -> None:
        text = "hello world"
        assert chunk_message(text) == ["hello world"]

    def test_exact_limit(self) -> None:
        text = "a" * 1900
        assert chunk_message(text) == [text]

    def test_splits_on_newline(self) -> None:
        line_a = "x" * 1000
        line_b = "y" * 1000
        text = f"{line_a}\n{line_b}"  # 2001 chars → split at newline
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert chunks[0] == line_a
        assert chunks[1] == line_b

    def test_splits_without_newline(self) -> None:
        text = "a" * 3800
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 1900
        assert chunks[1] == "a" * 1900

    def test_empty_message(self) -> None:
        assert chunk_message("") == [""]

    def test_multi_chunk(self) -> None:
        text = ("line\n") * 1000  # ~5000 chars
        chunks = chunk_message(text)
        assert all(len(c) <= 1900 for c in chunks)
        # Reassemble should give back original content (minus stripped newlines)
        reassembled = "\n".join(chunks)
        assert "line" in reassembled
