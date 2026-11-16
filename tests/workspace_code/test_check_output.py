"""The bounded output buffer of a check run."""

from __future__ import annotations

import tracemalloc

from viki_agent.code.check_output import (
    MAX_ANSWER_BYTES,
    MAX_HELD_BYTES,
    TAIL_BYTES,
    OutputBuffer,
    Slice,
    decode_window,
    read_stored,
)

KANA = "あ".encode()  # three bytes: E3 81 82
CHUNK = 65_536


def test_the_buffer_holds_at_most_two_mib() -> None:
    buffer = OutputBuffer()
    cursor = 0
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        for index in range(800):
            buffer.append(bytes([65 + index % 26]) * CHUNK)
            assert buffer.held() <= MAX_HELD_BYTES
            if index % 8 == 7:
                cursor = buffer.read(cursor).next
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert buffer.end == 800 * CHUNK
    assert buffer.held() == MAX_HELD_BYTES
    assert peak - baseline < 3 * 1024 * 1024


def test_offsets_follow_the_bytes_across_drops() -> None:
    buffer = OutputBuffer()
    buffer.append(b"a" * MAX_HELD_BYTES)
    assert (buffer.start, buffer.end) == (0, MAX_HELD_BYTES)
    first = buffer.read(0)
    assert (first.start, first.next, first.dropped) == (0, MAX_ANSWER_BYTES, False)

    buffer.append(b"b" * 10)
    assert (buffer.start, buffer.end, buffer.held()) == (10, MAX_HELD_BYTES + 10, MAX_HELD_BYTES)
    again = buffer.read(0)
    assert again.dropped is True
    assert (again.start, again.next) == (10, 10 + MAX_ANSWER_BYTES)
    assert buffer.read(10).dropped is False


def test_a_chunk_larger_than_the_buffer_keeps_its_end() -> None:
    buffer = OutputBuffer()
    buffer.append(b"x" * (MAX_HELD_BYTES + 5))
    assert (buffer.start, buffer.held()) == (5, MAX_HELD_BYTES)


def test_a_reader_at_the_end_waits_there() -> None:
    buffer = OutputBuffer()
    buffer.append(b"abc")
    assert buffer.read(3) == Slice(3, 3, "", False)
    assert buffer.read(99) == Slice(3, 3, "", False)
    buffer.append(b"d")
    assert buffer.read(3).text == "d"


def test_small_appends_read_back_in_order() -> None:
    buffer = OutputBuffer()
    for byte in b"hello world":
        buffer.append(bytes([byte]))
    assert buffer.read(0) == Slice(0, 11, "hello world", False)
    assert buffer.read(6).text == "world"


def test_a_character_split_across_appends_is_never_split_while_open() -> None:
    buffer = OutputBuffer()
    buffer.append(b"x" + KANA[:2])
    held_back = buffer.read(0)
    assert (held_back.text, held_back.next) == ("x", 1)
    buffer.append(KANA[2:])
    rest = buffer.read(held_back.next)
    assert (rest.text, rest.next) == ("あ", 4)


def test_a_character_split_by_the_answer_cap_is_never_split_while_open() -> None:
    buffer = OutputBuffer()
    buffer.append(b"x" * (MAX_ANSWER_BYTES - 1) + KANA + b"y")
    first = buffer.read(0)
    assert first.text == "x" * (MAX_ANSWER_BYTES - 1)
    assert first.next == MAX_ANSWER_BYTES - 1
    second = buffer.read(first.next)
    assert second.text == "あy"
    assert second.next == buffer.end


def test_an_incomplete_character_is_replaced_only_when_final() -> None:
    buffer = OutputBuffer()
    buffer.append(b"ok" + KANA[:2])
    assert buffer.read(0).text == "ok"
    buffer.close()
    assert buffer.closed is True
    last = buffer.read(0)
    assert (last.text, last.next) == ("ok�", 4)


def test_a_closed_buffer_not_yet_read_to_the_end_is_not_final() -> None:
    buffer = OutputBuffer()
    buffer.append(b"x" * (MAX_ANSWER_BYTES - 1) + KANA)
    buffer.close()
    first = buffer.read(0)
    assert first.next == MAX_ANSWER_BYTES - 1
    assert buffer.read(first.next).text == "あ"


def test_leading_continuation_bytes_are_skipped_after_a_drop() -> None:
    buffer = OutputBuffer()
    buffer.append("é".encode() * (MAX_HELD_BYTES // 2) + b"a")
    assert buffer.start == 1  # the drop cut the first character in two
    answer = buffer.read(0)
    assert answer.dropped is True
    assert answer.start == 1
    # the window is the skipped byte, 131,071 characters and half of the next one
    assert answer.text == "é" * ((MAX_ANSWER_BYTES - 2) // 2)
    assert answer.next == 1 + 1 + len(answer.text.encode())


def test_tail_is_the_last_bytes() -> None:
    buffer = OutputBuffer()
    assert buffer.tail() == b""
    buffer.append(b"abc")
    assert buffer.tail() == b"abc"
    buffer.append(bytes(range(256)) * (TAIL_BYTES // 256 + 3))
    expected = (b"abc" + bytes(range(256)) * (TAIL_BYTES // 256 + 3))[-TAIL_BYTES:]
    assert buffer.tail() == expected


def test_read_stored_continues_a_live_readers_offset() -> None:
    buffer = OutputBuffer()
    buffer.append("one あ ".encode())
    seen = buffer.read(0)
    buffer.append("two い".encode())
    buffer.close()
    stored = read_stored(buffer.tail(), buffer.end, seen.next)
    assert stored.dropped is False
    assert stored.start == seen.next
    assert seen.text + stored.text == "one あ two い"
    assert stored.next == buffer.end


def test_read_stored_marks_a_drop_and_starts_at_the_tail() -> None:
    tail = KANA[1:] + b"end"  # starts inside a character
    answer = read_stored(tail, 1000, 0)
    assert answer.dropped is True
    assert answer.start == 1000 - len(tail)
    assert answer.text == "end"
    assert answer.next == 1000


def test_read_stored_is_final_so_a_cut_character_is_replaced() -> None:
    answer = read_stored(b"ab" + KANA[:2], 4, 0)
    assert (answer.text, answer.next) == ("ab�", 4)


def test_read_stored_of_nothing() -> None:
    assert read_stored(b"", 0, 0) == Slice(0, 0, "", False)
    assert read_stored(b"", 0, 5) == Slice(0, 0, "", False)


def test_decode_window_skips_up_to_three_leading_continuation_bytes() -> None:
    assert decode_window(b"\x80\x81\x82a", final=False) == ("a", 4)
    # a fourth continuation byte is not a cut character's rest; it decodes as a replacement
    assert decode_window(b"\x80\x80\x80\x80a", final=True) == ("�a", 5)


def test_decode_window_leaves_an_incomplete_sequence_unless_final() -> None:
    for cut in (1, 2):
        assert decode_window(b"a" + KANA[:cut], final=False) == ("a", 1)
        assert decode_window(b"a" + KANA[:cut], final=True) == ("a�", 1 + cut)
    four = "😀".encode()
    for cut in (1, 2, 3):
        assert decode_window(four[:cut], final=False) == ("", 0)
    assert decode_window(four, final=False) == ("😀", 4)
    assert decode_window(b"", final=False) == ("", 0)


def test_decode_window_replaces_an_invalid_byte_even_when_open() -> None:
    assert decode_window(b"a\xffb", final=False) == ("a�b", 3)
    assert decode_window(b"a\xff", final=False) == ("a�", 2)
