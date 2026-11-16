"""Framing, endpointing, segments and revisions for the voice worker.

The worker imports this module under ``python -I -B``, so it uses only the standard library.
The partial cadence is counted in audio samples. An optional clock only defers a partial until
as much audio has arrived since the previous decode returned as that decode took, so a slow
decode never queues another behind a backlog.
"""

from __future__ import annotations

import json
import math
import re
import struct
import uuid
from collections import deque
from collections.abc import Callable, Collection
from typing import BinaryIO, Literal, Protocol

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 320
MAX_AUDIO_FRAME_BYTES = 65_536
# -40 dropped quiet passages of real recordings (FLEURS zh: 7 of 25 clips lost over 0.2 s of
# speech); the floor margin below still refuses steady noise that opens the stream.
SPEECH_MIN_DBFS = -50
SPEECH_OVER_FLOOR_DB = 12
FLOOR_RISE_DB = 0.05
ONSET_FRAMES = 10
ONSET_WINDOW = 15
# 0.7 s ended segments at ordinary pauses mid-sentence (FLEURS vi: split clips 25.9 % error, one-
# segment clips 10.4 %); 60 frames of 20 ms is 1.2 s.
END_SILENCE_FRAMES = 60
MAX_PAUSE_FRAMES = 250  # 250 frames of 20 ms is 5 s; the ceiling on a configured pause.
PRE_ROLL_S = 0.3
FIRST_PARTIAL_S = 0.6
REFRESH_S = 1.0
MAX_SEGMENT_S = 20
MAX_CAPTURE_S = 120
MAX_TEXT_CHARS = 4_096

_FRAME_BYTES = FRAME_SAMPLES * 2
_MAX_BEGIN_BYTES = 256
_HEADER = struct.Struct(">cI")
_KINDS = (b"B", b"A", b"E", b"X")
# Text made only of bracketed annotations (and spaces) carries no speech.
_ANNOTATION_ONLY = re.compile(r"(?:\s|♪|\[[^\]]*\]|\([^)]*\)|（[^）]*）|【[^】]*】)*")


class FrameError(ValueError):
    """The input breaks the stdin wire or the stream rules."""


class CaptureCapError(ValueError):
    """A capture holds more than MAX_CAPTURE_S of audio."""


class Recognizer(Protocol):
    def transcribe(self, pcm: bytes, language: str, *, partial: bool) -> str: ...


def _seconds_to_bytes(seconds: float) -> int:
    return round(seconds * SAMPLE_RATE) * 2


def _audio_length_ok(length: int) -> bool:
    return 2 <= length <= MAX_AUDIO_FRAME_BYTES and length % 2 == 0


def _length_ok(kind: bytes, length: int) -> bool:
    if kind == b"B":
        return length <= _MAX_BEGIN_BYTES
    if kind == b"A":
        return _audio_length_ok(length)
    return length == 0


def read_frame(stream: BinaryIO) -> tuple[bytes, bytes] | None:
    """Read one (kind, payload) frame; None at EOF before a header byte."""
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) < _HEADER.size:
        raise FrameError("short header")
    kind, length = _HEADER.unpack(header)
    if kind not in _KINDS:
        raise FrameError("unknown frame kind")
    if not _length_ok(kind, length):
        raise FrameError("frame length out of range")
    payload = stream.read(length)
    if len(payload) < length:
        raise FrameError("short payload")
    return kind, payload


def _refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise FrameError("duplicate key")
    return dict(pairs)


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def parse_begin(payload: bytes, languages: Collection[str]) -> tuple[str, str, int]:
    """Return (capture, language, pause_frames) from a `B` payload, or raise FrameError."""
    if len(payload) > _MAX_BEGIN_BYTES:
        raise FrameError("begin payload too long")
    try:
        data = json.loads(payload.decode("ascii"), object_pairs_hook=_refuse_duplicates)
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError and FrameError
        raise FrameError("begin payload is not valid JSON") from exc
    allowed_keys = ({"capture", "language"}, {"capture", "language", "pause_frames"})
    if not isinstance(data, dict) or set(data) not in allowed_keys:
        raise FrameError("begin payload keys")
    capture, language = data["capture"], data["language"]
    if not _is_canonical_uuid(capture):
        raise FrameError("capture is not a canonical UUID")
    if not isinstance(language, str) or language not in languages:
        raise FrameError("language not offered")
    pause_frames = data.get("pause_frames", END_SILENCE_FRAMES)
    if isinstance(pause_frames, bool) or not isinstance(pause_frames, int):
        raise FrameError("pause_frames is not an int")
    if not END_SILENCE_FRAMES <= pause_frames <= MAX_PAUSE_FRAMES:
        raise FrameError("pause_frames out of range")
    return capture, language, pause_frames


def _level_dbfs(frame: bytes) -> float:
    samples = struct.unpack(f"<{len(frame) // 2}h", frame)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(max(rms, 1.0) / 32768)


class Endpointer:
    """Finds where speech starts and ends in a stream of 20 ms frames."""

    def __init__(self) -> None:
        self.end_silence_frames = END_SILENCE_FRAMES
        self.reset()

    def reset(self) -> None:
        # The floor starts at the absolute minimum, so a stream that opens with speech still
        # starts and steady noise below the margin never does. end_silence_frames is a configured
        # setting, not detection state, so reset() never touches it.
        self._floor = float(SPEECH_MIN_DBFS)
        self._recent: deque[bool] = deque(maxlen=ONSET_WINDOW)
        self._in_speech = False
        self._silent_run = 0
        self.onset_frames = 0

    def feed(self, frame: bytes) -> Literal["start", "end"] | None:
        """Take one frame of FRAME_SAMPLES samples; report a start or an end when it comes."""
        speech = self._is_speech(frame)
        self._recent.append(speech)
        if not self._in_speech:
            if sum(self._recent) < ONSET_FRAMES:
                return None
            self._in_speech = True
            self._silent_run = 0
            # Frames from the earliest speech frame of the window to this one, inclusive.
            self.onset_frames = len(self._recent) - self._recent.index(True)
            return "start"
        self._silent_run = 0 if speech else self._silent_run + 1
        if self._silent_run < self.end_silence_frames:
            return None
        self._in_speech = False
        self._recent.clear()
        return "end"

    def _is_speech(self, frame: bytes) -> bool:
        level = _level_dbfs(frame)
        speech = level > SPEECH_MIN_DBFS and level > self._floor + SPEECH_OVER_FLOOR_DB
        # Speech must not lift the floor: a steady tone would else end itself.
        if not speech:
            self._floor = min(level, self._floor + FLOOR_RISE_DB)
        return speech


def _clean(text: str) -> str:
    text = text.strip()
    if _ANNOTATION_ONLY.fullmatch(text):
        return ""
    return text[:MAX_TEXT_CHARS]


class Session:
    """One capture's stream rules: segments, partial revisions, finals, done and cancelled."""

    def __init__(
        self,
        recognizer: Recognizer,
        send: Callable[[dict[str, object]], None],
        languages: Collection[str],
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._recognizer = recognizer
        self._send = send
        self._languages = languages
        self._clock = clock
        self._endpointer = Endpointer()
        self._capture: str | None = None
        self._language = ""
        self._clear()

    @property
    def is_open(self) -> bool:
        return self._capture is not None

    def begin(self, capture: str, language: str, pause_frames: int = END_SILENCE_FRAMES) -> None:
        if self.is_open:
            raise FrameError("a capture is already open")
        if language not in self._languages:
            raise FrameError("language not offered")
        self._clear()
        self._capture = capture
        self._language = language
        self._endpointer.end_silence_frames = pause_frames

    def audio(self, pcm: bytes) -> None:
        self._require_open()
        if not _audio_length_ok(len(pcm)):
            raise FrameError("audio length out of range")
        if self._received + len(pcm) > _seconds_to_bytes(MAX_CAPTURE_S):
            raise CaptureCapError("capture is too long")
        self._received += len(pcm)
        self._carry += pcm
        whole = len(self._carry) - len(self._carry) % _FRAME_BYTES
        frames = bytes(self._carry[:whole])
        del self._carry[:whole]
        for start in range(0, whole, _FRAME_BYTES):
            self._on_frame(frames[start : start + _FRAME_BYTES])

    def end(self) -> None:
        self._require_open()
        if self._active:
            self._finish_segment()
        self._emit("done")
        self._close()

    def cancel(self) -> None:
        self._require_open()
        self._emit("cancelled")
        self._close()

    def _require_open(self) -> None:
        if not self.is_open:
            raise FrameError("no capture is open")

    def _clear(self) -> None:
        self._endpointer.reset()
        self._buf = bytearray()  # the open segment, or the idle tail
        self._carry = bytearray()  # a frame not yet whole
        self._received = 0
        self._segment = 0
        self._active = False
        self._onset_offset = 0  # where the earliest speech frame starts in _buf
        self._next_partial = 0  # bytes since onset at which the next partial is due
        self._revision = 0
        self._last_partial = ""
        self._decode_s = 0.0  # how long the previous decode took, when a clock is set
        self._taken = 0  # audio bytes taken since the previous decode returned

    def _close(self) -> None:
        self._capture = None
        self._clear()

    def _emit(self, kind: str, **fields: object) -> None:
        self._send({"type": kind, "capture": self._capture, **fields})

    def _on_frame(self, frame: bytes) -> None:
        self._taken += len(frame)
        self._buf += frame
        event = self._endpointer.feed(frame)
        if not self._active:
            if event != "start":
                self._trim_idle()
                return
            self._start_segment_at_onset()
        elif event == "end":
            self._finish_segment()
            return
        elif len(self._buf) >= _seconds_to_bytes(MAX_SEGMENT_S):
            # Speech goes on, so the next segment starts at once.
            self._finish_segment()
            self._open_segment(onset_offset=0)
            return
        self._maybe_partial()

    def _trim_idle(self) -> None:
        # The onset is seen ONSET_WINDOW frames late at most, and the segment reaches back
        # PRE_ROLL_S before the earliest speech frame.
        keep = _seconds_to_bytes(PRE_ROLL_S) + ONSET_WINDOW * _FRAME_BYTES
        del self._buf[:-keep]

    def _start_segment_at_onset(self) -> None:
        onset = self._endpointer.onset_frames * _FRAME_BYTES
        del self._buf[: -(onset + _seconds_to_bytes(PRE_ROLL_S))]
        self._open_segment(onset_offset=len(self._buf) - onset)

    def _open_segment(self, onset_offset: int) -> None:
        self._active = True
        self._onset_offset = onset_offset
        self._next_partial = _seconds_to_bytes(FIRST_PARTIAL_S)

    def _maybe_partial(self) -> None:
        heard = len(self._buf) - self._onset_offset
        if heard < self._next_partial:
            return
        # While a slow decode's worth of audio has not yet arrived, the frames read back from
        # the pipe would each trigger another slow decode: wait, and keep _next_partial.
        if self._taken < _seconds_to_bytes(self._decode_s):
            return
        self._next_partial = heard + _seconds_to_bytes(REFRESH_S)
        text = self._transcribe(partial=True)
        if text and text != self._last_partial:
            self._revision += 1
            self._last_partial = text
            self._emit("partial", segment=self._segment, revision=self._revision, text=text)

    def _finish_segment(self) -> None:
        text = self._transcribe(partial=False)
        self._emit("final", segment=self._segment, text=text)
        self._segment += 1
        self._revision = 0
        self._last_partial = ""
        self._active = False
        # Nothing before this end belongs to the next segment's pre-roll.
        self._buf.clear()

    def _transcribe(self, *, partial: bool) -> str:
        pcm = bytes(self._buf)
        started = self._clock() if self._clock else 0.0
        text = self._recognizer.transcribe(pcm, self._language, partial=partial)
        if self._clock:
            self._decode_s = self._clock() - started
        self._taken = 0
        return _clean(text)
