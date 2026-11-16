"""voice_stream: framing, endpointing, segments and revisions."""

from __future__ import annotations

import io
import json
import math
import random
import struct
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from viki_agent.voice.voice_stream import (
    END_SILENCE_FRAMES,
    FRAME_SAMPLES,
    MAX_AUDIO_FRAME_BYTES,
    MAX_CAPTURE_S,
    MAX_PAUSE_FRAMES,
    MAX_SEGMENT_S,
    MAX_TEXT_CHARS,
    ONSET_FRAMES,
    ONSET_WINDOW,
    PRE_ROLL_S,
    REFRESH_S,
    SAMPLE_RATE,
    CaptureCapError,
    Endpointer,
    FrameError,
    Session,
    parse_begin,
    read_frame,
)

CAPTURE = "123e4567-e89b-42d3-a456-426614174000"
OTHER_CAPTURE = "9b2f6a0c-1d3e-4f5a-8b7c-0d1e2f3a4b5c"
LANGUAGES = ("auto", "en", "ja")
FRAME_BYTES = FRAME_SAMPLES * 2
SECOND = SAMPLE_RATE * 2
END_PAUSE_S = END_SILENCE_FRAMES * FRAME_SAMPLES / SAMPLE_RATE + 0.2  # long enough to end a segment


def seconds(value: float) -> int:
    """Bytes of PCM in `value` seconds."""
    return round(value * SAMPLE_RATE) * 2


def pack(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def tone(duration_s: float, dbfs: float = -20.0) -> bytes:
    peak = 32768 * 10 ** (dbfs / 20) * math.sqrt(2)  # RMS at dbfs
    count = round(duration_s * SAMPLE_RATE)
    return pack([round(peak * math.sin(2 * math.pi * 300 * i / SAMPLE_RATE)) for i in range(count)])


def silence(duration_s: float) -> bytes:
    return bytes(seconds(duration_s))


def noise(duration_s: float, dbfs: float = -40.0) -> bytes:
    peak = 32768 * 10 ** (dbfs / 20) * math.sqrt(3)  # uniform noise: RMS = peak / sqrt(3)
    rng = random.Random(7)
    count = round(duration_s * SAMPLE_RATE)
    return pack([round(rng.uniform(-peak, peak)) for _ in range(count)])


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


def wire(kind: bytes, payload: bytes = b"", length: int | None = None) -> io.BytesIO:
    size = len(payload) if length is None else length
    return io.BytesIO(struct.pack(">cI", kind, size) + payload)


# ---- framing -------------------------------------------------------------------------------


def test_read_frame_returns_kind_and_payload_then_none_at_eof() -> None:
    stream = io.BytesIO(
        struct.pack(">cI", b"A", 4) + b"\x01\x00\x02\x00" + struct.pack(">cI", b"E", 0)
    )
    assert read_frame(stream) == (b"A", b"\x01\x00\x02\x00")
    assert read_frame(stream) == (b"E", b"")
    assert read_frame(stream) is None


def test_read_frame_accepts_the_audio_length_limits() -> None:
    assert read_frame(wire(b"A", bytes(2))) == (b"A", bytes(2))
    assert read_frame(wire(b"A", bytes(MAX_AUDIO_FRAME_BYTES))) is not None


@pytest.mark.parametrize(
    "stream",
    [
        io.BytesIO(b"A\x00\x00"),  # short header
        wire(b"A", bytes(4), length=8),  # short payload
        wire(b"A", bytes(3)),  # odd audio
        wire(b"A", b""),  # empty audio
        wire(b"A", bytes(MAX_AUDIO_FRAME_BYTES + 2)),  # oversized audio
        wire(b"A", b"", length=0xFFFFFFFF),  # a length no payload could follow
        wire(b"Z"),  # unknown kind
        wire(b"E", b"x"),  # E and X carry nothing
        wire(b"X", b"x"),
        wire(b"B", bytes(257)),  # begin over 256 bytes
    ],
)
def test_read_frame_refuses_a_bad_frame(stream: io.BytesIO) -> None:
    with pytest.raises(FrameError):
        read_frame(stream)


def begin_payload(**overrides: object) -> bytes:
    body: dict[str, object] = {"capture": CAPTURE, "language": "ja", **overrides}
    return json.dumps(body).encode("ascii")


def test_parse_begin_returns_capture_and_language() -> None:
    assert parse_begin(begin_payload(), LANGUAGES) == (CAPTURE, "ja", END_SILENCE_FRAMES)
    assert parse_begin(begin_payload(language="auto"), LANGUAGES) == (
        CAPTURE,
        "auto",
        END_SILENCE_FRAMES,
    )


def test_parse_begin_accepts_pause_frames() -> None:
    assert parse_begin(begin_payload(pause_frames=END_SILENCE_FRAMES), LANGUAGES) == (
        CAPTURE,
        "ja",
        END_SILENCE_FRAMES,
    )
    assert parse_begin(begin_payload(pause_frames=150), LANGUAGES) == (CAPTURE, "ja", 150)
    assert parse_begin(begin_payload(pause_frames=MAX_PAUSE_FRAMES), LANGUAGES) == (
        CAPTURE,
        "ja",
        MAX_PAUSE_FRAMES,
    )


@pytest.mark.parametrize(
    "payload",
    [
        begin_payload(capture=CAPTURE.upper()),
        begin_payload(capture=CAPTURE.replace("-", "")),
        begin_payload(capture="{" + CAPTURE + "}"),
        begin_payload(capture="urn:uuid:" + CAPTURE),
        begin_payload(capture=7),
        begin_payload(language="xx"),
        begin_payload(language=["en"]),
        begin_payload(language=None),
        begin_payload(extra=1),
        begin_payload(pause_frames=END_SILENCE_FRAMES - 1),
        begin_payload(pause_frames=MAX_PAUSE_FRAMES + 1),
        begin_payload(pause_frames=True),
        begin_payload(pause_frames="150"),
        begin_payload(pause_frames=1.5),
        b'{"capture": "' + CAPTURE.encode() + b'"}',
        b'{"capture": "' + CAPTURE.encode() + b'", "language": "en", "language": "ja"}',
        f'{{"capture": "{CAPTURE}", "capture": "{CAPTURE}", "language": "en"}}'.encode(),
        b'["capture", "language"]',
        b"not json",
        b"",
        '{"capture": "x", "language": "日本語"}'.encode(),  # not ASCII
        begin_payload(language="en" + " " * 250),  # over 256 bytes
    ],
)
def test_parse_begin_refuses(payload: bytes) -> None:
    with pytest.raises(FrameError):
        parse_begin(payload, LANGUAGES)


# ---- endpointer ----------------------------------------------------------------------------


def events(endpointer: Endpointer, pcm: bytes) -> list[tuple[int, str]]:
    """(frame index, event) for every start or end the stream produces."""
    found: list[tuple[int, str]] = []
    for index, frame in enumerate(frames_of(pcm)):
        event = endpointer.feed(frame)
        if event is not None:
            found.append((index, event))
    return found


def test_tone_after_silence_starts_within_the_onset_window() -> None:
    endpointer = Endpointer()
    found = events(endpointer, silence(1.0) + tone(1.0))
    silent_frames = SAMPLE_RATE // FRAME_SAMPLES
    assert found == [(silent_frames + ONSET_FRAMES - 1, "start")]
    assert endpointer.onset_frames == ONSET_FRAMES
    assert ONSET_FRAMES <= ONSET_WINDOW


def test_onset_frames_reaches_back_to_the_earliest_speech_frame_of_the_window() -> None:
    endpointer = Endpointer()
    gap_frames = 2
    burst = ONSET_FRAMES - 3
    pcm = silence(0.5) + tone(3 * 0.02) + silence(gap_frames * 0.02) + tone(burst * 0.02)
    assert [event for _, event in events(endpointer, pcm)] == ["start"]
    assert endpointer.onset_frames == 3 + gap_frames + burst


def test_a_stream_that_opens_with_speech_starts() -> None:
    assert [event for _, event in events(Endpointer(), tone(1.0))] == ["start"]


def test_end_silence_frames_of_silence_end_the_speech() -> None:
    endpointer = Endpointer()
    tone_frames = 50
    found = events(endpointer, silence(0.5) + tone(1.0) + silence(1.5))
    start = 25 + ONSET_FRAMES - 1
    assert found == [(start, "start"), (25 + tone_frames + END_SILENCE_FRAMES - 1, "end")]


def test_a_shorter_pause_does_not_end_the_speech() -> None:
    pause = END_SILENCE_FRAMES - 1
    pcm = silence(0.5) + tone(1.0) + silence(pause * 0.02) + tone(1.0)
    assert [event for _, event in events(Endpointer(), pcm)] == ["start"]


@pytest.mark.parametrize("frames", [150, 60])
def test_end_silence_frames_is_configurable(frames: int) -> None:
    """Ends a segment at its own configured count of silent frames, not one short."""
    endpointer = Endpointer()
    endpointer.end_silence_frames = frames
    assert events(endpointer, silence(0.5) + tone(1.0) + silence((frames - 1) * 0.02)) == [
        (25 + ONSET_FRAMES - 1, "start")
    ]
    assert events(endpointer, silence(0.02)) == [(0, "end")]


def test_steady_noise_starts_nothing() -> None:
    assert events(Endpointer(), noise(5.0)) == []


def test_quiet_speech_above_the_new_floor_starts_a_segment() -> None:
    assert [event for _, event in events(Endpointer(), silence(1.0) + tone(1.0, dbfs=-46.0))] == [
        "start"
    ]


def test_a_one_second_pause_keeps_the_segment() -> None:
    lead = silence(0.5) + tone(1.0)
    start = 25 + ONSET_FRAMES - 1
    through_pause = events(Endpointer(), lead + silence(1.0) + tone(1.0))
    assert through_pause == [(start, "start")]
    # the end comes only after END_SILENCE_FRAMES of silence
    found = events(Endpointer(), lead + silence(END_PAUSE_S))
    assert found == [(start, "start"), (25 + 50 + END_SILENCE_FRAMES - 1, "end")]


def test_steady_noise_that_opens_the_stream_never_starts() -> None:
    # Not pinned after digital zeros: silence drops the floor, which rises 2.5 dB per second, so
    # steady noise right after silence can start a segment (it did at -40 too). The final's VAD
    # keeps such a segment's final empty.
    assert events(Endpointer(), noise(5.0, dbfs=-48.0)) == []


def test_a_100_ms_click_starts_nothing() -> None:
    assert events(Endpointer(), silence(1.0) + tone(0.1) + silence(1.0) + tone(0.1)) == []


def test_speech_below_the_absolute_minimum_starts_nothing() -> None:
    assert events(Endpointer(), silence(1.0) + tone(1.0, dbfs=-60.0)) == []


def test_reset_returns_to_idle() -> None:
    endpointer = Endpointer()
    assert [event for _, event in events(endpointer, silence(0.5) + tone(1.0))] == ["start"]
    endpointer.reset()
    assert endpointer.onset_frames == 0
    assert [event for _, event in events(endpointer, silence(0.5) + tone(1.0))] == ["start"]


def test_reset_keeps_the_configured_end_silence_frames() -> None:
    endpointer = Endpointer()
    endpointer.end_silence_frames = 200
    endpointer.reset()
    assert endpointer.end_silence_frames == 200


# ---- session -------------------------------------------------------------------------------


@dataclass
class Call:
    pcm_bytes: int
    language: str
    partial: bool


class FakeRecognizer:
    """Answers each call from its call number; keeps what it was asked."""

    def __init__(self, reply: Callable[[int], str] | None = None) -> None:
        self.reply = reply or (lambda n: f"text {n}")
        self.calls: list[Call] = []

    def transcribe(self, pcm: bytes, language: str, *, partial: bool) -> str:
        self.calls.append(Call(len(pcm), language, partial))
        return self.reply(len(self.calls) - 1)


def opened(
    reply: Callable[[int], str] | None = None, language: str = "en"
) -> tuple[Session, FakeRecognizer, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    recognizer = FakeRecognizer(reply)
    session = Session(recognizer, results.append, LANGUAGES)
    session.begin(CAPTURE, language)
    return session, recognizer, results


def feed(session: Session, pcm: bytes, chunk: int = SECOND) -> None:
    for start in range(0, len(pcm), chunk):
        session.audio(pcm[start : start + chunk])


def kinds(results: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [r for r in results if r["type"] == kind]


def test_partials_count_up_then_one_final() -> None:
    session, recognizer, results = opened()
    feed(session, tone(3.0) + silence(END_PAUSE_S))
    partials = kinds(results, "partial")
    assert [p["revision"] for p in partials] == [1, 2, 3, 4]
    assert {p["segment"] for p in partials} == {0}
    assert all(p["capture"] == CAPTURE for p in partials)
    # one decode per REFRESH_S of audio, the first after FIRST_PARTIAL_S
    heard = [call.pcm_bytes for call in recognizer.calls if call.partial]
    assert heard[:4] == [seconds(0.6), seconds(1.6), seconds(2.6), seconds(3.6)]
    assert all(b - a >= seconds(REFRESH_S) for a, b in zip(heard, heard[1:], strict=False))
    # the final comes last, once, and nothing more names the segment
    assert [r["type"] for r in results] == ["partial"] * 4 + ["final"]
    assert results[-1] == {"type": "final", "capture": CAPTURE, "segment": 0, "text": "text 4"}
    feed(session, silence(1.0))
    session.end()
    assert results[5:] == [{"type": "done", "capture": CAPTURE}]


def test_end_mid_speech_finals_then_done() -> None:
    session, recognizer, results = opened()
    feed(session, tone(1.5))
    session.end()
    assert [r["type"] for r in results] == ["partial", "final", "done"]
    assert recognizer.calls[-1].partial is False
    assert not session.is_open


def test_end_with_no_speech_sends_done_only() -> None:
    session, recognizer, results = opened()
    feed(session, silence(1.0) + tone(0.1))
    session.end()
    assert results == [{"type": "done", "capture": CAPTURE}]
    assert recognizer.calls == []


def test_cancel_sends_cancelled_and_nothing_later() -> None:
    session, recognizer, results = opened()
    feed(session, tone(1.0))
    seen = len(results)
    decodes = len(recognizer.calls)
    session.cancel()
    assert results[seen:] == [{"type": "cancelled", "capture": CAPTURE}]
    assert not session.is_open
    for late in (lambda: session.audio(bytes(2)), session.end, session.cancel):
        with pytest.raises(FrameError):
            late()
    assert len(results) == seen + 1
    assert len(recognizer.calls) == decodes


def test_two_segments_number_from_zero_and_restart_revisions() -> None:
    session, _, results = opened()
    feed(session, tone(1.0) + silence(END_PAUSE_S) + tone(1.0) + silence(END_PAUSE_S))
    session.end()
    finals = kinds(results, "final")
    assert [f["segment"] for f in finals] == [0, 1]
    for segment in (0, 1):
        revisions = [p["revision"] for p in kinds(results, "partial") if p["segment"] == segment]
        assert revisions[:1] == [1]
        assert revisions == list(range(1, len(revisions) + 1))
    assert results[-1] == {"type": "done", "capture": CAPTURE}


def test_a_second_capture_numbers_its_segments_from_zero() -> None:
    session, _, results = opened()
    feed(session, tone(1.0) + silence(1.0))
    session.end()
    session.begin(OTHER_CAPTURE, "en")
    feed(session, tone(1.0) + silence(1.0))
    session.end()
    assert [(f["capture"], f["segment"]) for f in kinds(results, "final")] == [
        (CAPTURE, 0),
        (OTHER_CAPTURE, 0),
    ]


def test_begin_sets_the_endpointers_end_silence_frames() -> None:
    session, _, _ = opened()
    assert session._endpointer.end_silence_frames == END_SILENCE_FRAMES  # noqa: SLF001
    session.cancel()
    session.begin(OTHER_CAPTURE, "en", pause_frames=100)
    assert session._endpointer.end_silence_frames == 100  # noqa: SLF001


def test_a_segment_ends_at_the_cap_and_a_new_one_starts() -> None:
    session, recognizer, results = opened()
    feed(session, tone(25.0))
    session.end()
    finals = [(c.pcm_bytes) for c in recognizer.calls if not c.partial]
    assert finals == [seconds(MAX_SEGMENT_S), seconds(5)]
    assert [(f["segment"]) for f in kinds(results, "final")] == [0, 1]
    first = [p["revision"] for p in kinds(results, "partial") if p["segment"] == 1]
    assert first[:1] == [1]


def test_a_repeated_text_makes_one_partial() -> None:
    session, recognizer, results = opened(lambda n: "hello")
    feed(session, tone(3.5) + silence(END_PAUSE_S))
    assert len([c for c in recognizer.calls if c.partial]) > 1
    assert [p["text"] for p in kinds(results, "partial")] == ["hello"]
    assert kinds(results, "final")[0]["text"] == "hello"


def test_blank_audio_makes_no_partial_and_an_empty_final() -> None:
    session, _, results = opened(lambda n: "[BLANK_AUDIO]")
    feed(session, tone(2.0) + silence(END_PAUSE_S))
    assert [r["type"] for r in results] == ["final"]
    assert results[0]["text"] == ""


@pytest.mark.parametrize(
    ("heard", "text"),
    [
        ("  hello  ", "hello"),
        ("[ Music ]", ""),
        ("(applause)", ""),
        ("（笑）", ""),
        ("【音楽】", ""),
        ("♪ ♪", ""),
        (" [a] (b) ♪ ", ""),
        ("hello [laughs]", "hello [laughs]"),
        pytest.param("a" * (MAX_TEXT_CHARS + 50), "a" * MAX_TEXT_CHARS, id="capped"),
    ],
)
def test_text_is_stripped_and_annotation_only_text_is_empty(heard: str, text: str) -> None:
    session, _, results = opened(lambda n: heard)
    feed(session, tone(0.5))
    session.end()
    assert kinds(results, "final")[0]["text"] == text


@pytest.mark.parametrize("pause_s", [1.0, 0.5])
def test_the_buffer_stays_bounded_over_a_long_capture(pause_s: float) -> None:
    session, _, _ = opened(lambda n: "")
    limit = seconds(MAX_SEGMENT_S + PRE_ROLL_S + 1)
    longest = 0
    for _ in range(round(110 / (1.0 + pause_s))):
        for part in (tone(1.0), silence(pause_s)):
            feed(session, part)
            longest = max(longest, len(session._buf))  # noqa: SLF001 - the bound is the point
    assert 0 < longest <= limit


def test_idle_audio_keeps_only_the_pre_roll_and_the_onset_window() -> None:
    session, _, _ = opened()
    feed(session, silence(10.0))
    assert len(session._buf) == seconds(PRE_ROLL_S) + ONSET_WINDOW * FRAME_BYTES  # noqa: SLF001


def test_a_segment_starts_pre_roll_before_the_speech() -> None:
    session, recognizer, _ = opened()
    feed(session, silence(5.0) + tone(1.0))
    assert recognizer.calls[0].pcm_bytes == seconds(PRE_ROLL_S + 0.6)


def test_audio_past_the_capture_cap_raises() -> None:
    session, _, _ = opened(lambda n: "")
    feed(session, silence(MAX_CAPTURE_S), chunk=MAX_AUDIO_FRAME_BYTES)
    with pytest.raises(CaptureCapError):
        session.audio(bytes(2))


def test_every_decode_gets_the_captures_language() -> None:
    session, recognizer, _ = opened(language="ja")
    feed(session, tone(2.0) + silence(END_PAUSE_S))
    assert {c.language for c in recognizer.calls} == {"ja"}
    flags = [c.partial for c in recognizer.calls]
    assert flags == [True] * (len(flags) - 1) + [False]


def test_chunk_size_does_not_change_the_results() -> None:
    audio = silence(0.3) + tone(2.0) + silence(1.0) + tone(1.0) + silence(1.0)
    whole_frames = opened()
    odd_chunks = opened()
    feed(whole_frames[0], audio, chunk=FRAME_BYTES)
    feed(odd_chunks[0], audio, chunk=1_002)
    assert whole_frames[2] == odd_chunks[2]
    assert whole_frames[1].calls == odd_chunks[1].calls


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SlowRecognizer(FakeRecognizer):
    """Each decode advances the clock and notes how much audio had been fed by then."""

    def __init__(self, clock: FakeClock, partial_s: float, final_s: float) -> None:
        super().__init__()
        self.clock = clock
        self.durations = {True: partial_s, False: final_s}
        self.fed = 0
        self.fed_at: list[int] = []

    def transcribe(self, pcm: bytes, language: str, *, partial: bool) -> str:
        self.fed_at.append(self.fed)
        self.clock.now += self.durations[partial]
        return super().transcribe(pcm, language, partial=partial)


def timed(partial_s: float, final_s: float = 0.0) -> tuple[Session, SlowRecognizer]:
    clock = FakeClock()
    recognizer = SlowRecognizer(clock, partial_s, final_s)
    session = Session(recognizer, lambda _: None, LANGUAGES, clock=clock)
    session.begin(CAPTURE, "en")
    return session, recognizer


def feed_frames(session: Session, recognizer: SlowRecognizer, pcm: bytes) -> None:
    for frame in frames_of(pcm):
        recognizer.fed += len(frame)
        session.audio(frame)


def partial_heard(recognizer: SlowRecognizer) -> list[int]:
    return [c.pcm_bytes - seconds(PRE_ROLL_S) for c in recognizer.calls if c.partial]


@pytest.mark.parametrize(
    ("decode_s", "heard_s"),
    [(3.0, [0.6, 3.6, 6.6]), (0.2, [0.6, 1.6, 2.6])],
)
def test_a_partial_waits_for_as_much_audio_as_the_previous_decode_took(
    decode_s: float, heard_s: list[float]
) -> None:
    session, recognizer = timed(decode_s)
    feed_frames(session, recognizer, silence(5.0) + tone(heard_s[-1] + 0.1))
    assert partial_heard(recognizer) == [seconds(s) for s in heard_s]


def test_a_slow_final_defers_the_next_segments_first_partial() -> None:
    session, recognizer = timed(0.0, final_s=3.0)
    feed_frames(session, recognizer, tone(1.0) + silence(END_PAUSE_S))
    assert [c.partial for c in recognizer.calls][-1] is False
    decodes = len(recognizer.calls)
    feed_frames(session, recognizer, tone(4.0))
    assert recognizer.fed_at[decodes] - recognizer.fed_at[decodes - 1] == seconds(3.0)


def test_the_slow_decode_state_resets_for_the_next_capture() -> None:
    session, recognizer = timed(3.0)
    feed_frames(session, recognizer, tone(1.0))
    session.cancel()
    session.begin(OTHER_CAPTURE, "en")
    calls = len(recognizer.calls)
    feed_frames(session, recognizer, tone(1.0))
    assert recognizer.calls[calls].pcm_bytes == seconds(0.6)


def test_the_session_refuses_what_the_stream_rules_forbid() -> None:
    results: list[dict[str, object]] = []
    session = Session(FakeRecognizer(), results.append, LANGUAGES)
    for early in (lambda: session.audio(bytes(2)), session.end, session.cancel):
        with pytest.raises(FrameError):
            early()
    with pytest.raises(FrameError):
        session.begin(CAPTURE, "xx")
    session.begin(CAPTURE, "en")
    with pytest.raises(FrameError):
        session.begin(CAPTURE, "en")
    for bad in (b"", bytes(3), bytes(MAX_AUDIO_FRAME_BYTES + 2)):
        with pytest.raises(FrameError):
            session.audio(bad)
    assert session.is_open
    assert results == []
