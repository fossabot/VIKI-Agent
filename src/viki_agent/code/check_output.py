"""The bounded output buffer of a live check run.

A run holds at most `MAX_HELD_BYTES` of its output, dropping the oldest bytes. Every byte has an
absolute offset: the first byte ever seen is 0. A reader asks for the bytes after an offset and
gets back the offset to ask from next. Offsets are bytes, so a reader never has to know where the
buffer dropped; text is cut only where a UTF-8 sequence ends while the run is live.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

MAX_HELD_BYTES = 2_097_152
MAX_ANSWER_BYTES = 262_144
TAIL_BYTES = 65_536
# A reader thread may append one byte at a time; joining small chunks keeps a deque entry's own
# overhead from dwarfing the bytes it holds.
_MERGE_BELOW = 4096
_CONTINUATION = range(0x80, 0xC0)


@dataclass(frozen=True, slots=True)
class Slice:
    start: int
    next: int
    text: str
    dropped: bool


def _sequence_length(lead: int) -> int:
    """The length of the UTF-8 sequence a lead byte starts; 1 for a byte that starts none."""
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def _complete_length(data: bytes) -> int:
    """`len(data)` less an incomplete sequence at its end."""
    size = len(data)
    for back in range(1, min(3, size) + 1):
        byte = data[size - back]
        if byte in _CONTINUATION:
            continue
        return size - back if byte >= 0xC0 and back < _sequence_length(byte) else size
    return size


def decode_window(data: bytes, *, final: bool) -> tuple[str, int]:
    """Decode a window cut at an arbitrary byte; the int is how many bytes it consumed.

    Leading continuation bytes belong to a character the window cut, so they are skipped (and
    counted as consumed). Unless `final`, an incomplete sequence at the end is left for the next
    window.
    """
    skip = 0
    while skip < min(3, len(data)) and data[skip] in _CONTINUATION:
        skip += 1
    end = len(data) if final else skip + _complete_length(data[skip:])
    return data[skip:end].decode("utf-8", errors="replace"), end


class OutputBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: deque[bytes] = deque()
        self._start = 0
        self._held = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def start(self) -> int:
        with self._lock:
            return self._start

    @property
    def end(self) -> int:
        with self._lock:
            return self._start + self._held

    def held(self) -> int:
        with self._lock:
            return self._held

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            if self._chunks and len(self._chunks[-1]) < _MERGE_BELOW:
                self._chunks[-1] += chunk
            else:
                self._chunks.append(bytes(chunk))
            self._held += len(chunk)
            self._drop_oldest()

    def _drop_oldest(self) -> None:
        excess = self._held - MAX_HELD_BYTES
        while excess > 0:
            first = self._chunks[0]
            cut = min(excess, len(first))
            if cut == len(first):
                self._chunks.popleft()
            else:
                self._chunks[0] = first[cut:]
            self._start += cut
            self._held -= cut
            excess -= cut

    def read(self, after: int) -> Slice:
        with self._lock:
            # An offset past the end is a reader that is ahead of us; it waits at the end.
            begin = min(max(after, self._start), self._start + self._held)
            window = self._collect(begin, MAX_ANSWER_BYTES)
            final = self._closed and begin + len(window) == self._start + self._held
        text, consumed = decode_window(window, final=final)
        return Slice(begin, begin + consumed, text, after < begin)

    def _collect(self, begin: int, limit: int) -> bytes:
        """Up to `limit` held bytes from the absolute offset `begin`; the caller holds the lock."""
        parts: list[bytes] = []
        position = self._start
        need = limit
        for chunk in self._chunks:
            if need <= 0:
                break
            if position + len(chunk) > begin:
                piece = chunk[max(begin - position, 0) :][:need]
                parts.append(piece)
                need -= len(piece)
            position += len(chunk)
        return b"".join(parts)

    def tail(self) -> bytes:
        with self._lock:
            begin = max(self._start, self._start + self._held - TAIL_BYTES)
            return self._collect(begin, TAIL_BYTES)


def read_stored(tail: bytes, output_bytes: int, after: int) -> Slice:
    """Answer from a finished run's stored tail.

    Its first byte sits at `output_bytes - len(tail)`. The offsets are the live buffer's, so a
    reader that followed the run continues where it was.
    """
    base = output_bytes - len(tail)
    begin = min(max(after, base), output_bytes)
    window = tail[begin - base :][:MAX_ANSWER_BYTES]
    text, consumed = decode_window(window, final=True)
    return Slice(begin, begin + consumed, text, after < begin)
