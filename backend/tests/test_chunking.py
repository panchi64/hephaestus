"""Token-window chunking: window/overlap boundaries, offsets, edge cases."""

from __future__ import annotations

import pytest

from services.chunking import chunk_text


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("a small note", window=512, overlap=64)
    assert len(chunks) == 1
    assert chunks[0].text == "a small note"
    assert chunks[0].offset == 0
    assert chunks[0].ordinal == 0


def test_empty_input_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_window_and_overlap_boundaries():
    # 10 tokens, window 4, overlap 1 → step 3 → windows start at 0,3,6; the start=6
    # window (w6..w9) reaches the end, so there's no trailing partial chunk.
    words = [f"w{i}" for i in range(10)]
    chunks = chunk_text(" ".join(words), window=4, overlap=1)
    assert [c.ordinal for c in chunks] == [0, 1, 2]
    assert chunks[0].text.split() == ["w0", "w1", "w2", "w3"]
    # Overlap: chunk 1 begins where chunk 0's step landed (token w3).
    assert chunks[1].text.split()[0] == "w3"
    # The final window covers the tail and stops (no empty trailing chunk).
    assert chunks[-1].text.split()[-1] == "w9"


def test_offsets_point_into_the_source():
    text = "alpha beta gamma delta"
    chunks = chunk_text(text, window=2, overlap=0)
    # Each chunk's offset is the char index of its first token in the source.
    assert text[chunks[0].offset :].startswith("alpha")
    assert text[chunks[1].offset :].startswith("gamma")


def test_repeated_tokens_get_distinct_offsets():
    text = "the cat sat on the mat"
    chunks = chunk_text(text, window=2, overlap=0)
    # "the" appears twice — offsets must advance, not both resolve to the first.
    offsets = [c.offset for c in chunks]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_overlap_must_be_smaller_than_window():
    with pytest.raises(ValueError):
        _ = chunk_text("a b c", window=4, overlap=4)
