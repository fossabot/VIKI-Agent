"""Tests for between_chats.py: the <cross-session-message> parser and the bounded scan that
turns the vault's listed chats into the Between chats wire shape.

Every transcript and registry entry here is invented for the test; none of it is copied from a
real ~/.claude file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from viki_agent import catalog as catalog_module
from viki_agent.catalog import Catalog, _tail_window, project_dir
from viki_agent.conversations import between_chats as between_chats_module
from viki_agent.conversations.between_chats import (
    MAX_MESSAGES,
    NAME_CHARS,
    OPEN_TAG_MAX_CHARS,
    TEXT_CHARS,
    between_chats,
    received_messages,
)
from viki_agent.events import SessionsResult

VAULT = Path("C:/Vault")
R = "11111111-1111-4111-8111-111111111111"  # a receiving chat
S = "22222222-1111-4111-8111-111111111111"  # a listed, non-other chat that sent a message
OTHER_LISTED = "33333333-1111-4111-8111-111111111111"  # listed, but other=True
GHOST = "44444444-1111-4111-8111-111111111111"  # a registry sessionId with no transcript at all
R1 = "55555555-1111-4111-8111-111111111111"
R2 = "66666666-1111-4111-8111-111111111111"


# --- helpers --------------------------------------------------------------------------------------


def _home(tmp_path: Path, name: str = "home") -> tuple[Path, Path]:
    home = tmp_path / name
    folder = project_dir(home, VAULT)
    folder.mkdir(parents=True)
    return home, folder


def _write(folder: Path, session_id: str, lines: list[dict[str, Any]]) -> Path:
    path = folder / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8"
    )
    return path


def _listed_entry(
    *, entrypoint: str = "cli", text: str = "hello", ts: str = "2026-10-20T00:00:00.000Z"
) -> dict[str, Any]:
    """The minimum one transcript needs to become a listed row: a first prompt and an
    entrypoint (MAIN_ENTRYPOINTS gives `other=False`; anything else gives `other=True`)."""
    return {
        "type": "user",
        "isSidechain": False,
        "entrypoint": entrypoint,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _tag(
    *,
    from_: str = "uds:/sock-a",
    name: str = "chat-a",
    ts: str = "2026-10-20T10:00:00.000Z",
    body: str = "hello there",
) -> str:
    return (
        f'<cross-session-message from="{from_}" from-name="{name}" timestamp="{ts}">'
        f"{body}</cross-session-message>"
    )


def _enqueue(tag: str) -> dict[str, Any]:
    return {"type": "queue-operation", "operation": "enqueue", "content": tag}


def _queued_command(tag: str) -> dict[str, Any]:
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": tag}}


def _meta_user(tag: str) -> dict[str, Any]:
    return {
        "type": "user",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": f"Another Claude session sent a message:\n{tag}\nRespond if useful.",
        },
    }


def _lines(*entries: dict[str, Any]) -> bytes:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode("utf-8")


def _registry_entry(home: Path, name: str, *, pid: int, session_id: str, socket: str) -> None:
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / name).write_text(
        json.dumps({"pid": pid, "sessionId": session_id, "messagingSocketPath": socket}),
        encoding="utf-8",
    )


def _messages(result: dict[str, object]) -> list[Any]:
    """`result["messages"]`, typed: `between_chats` answers `dict[str, object]` on the wire's
    behalf, so tests narrow it once here instead of at every call site."""
    messages = result["messages"]
    assert isinstance(messages, list)
    return messages


# --- _tail_window's `window` parameter (catalog.py) -----------------------------------------------


def test_tail_window_takes_a_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"a\nbb\nccc\n")
    size = path.stat().st_size

    assert _tail_window(path, size, window=4) == b"ccc\n"

    # None reads TAIL_WINDOW_BYTES at call time, not a value frozen when the module was imported.
    monkeypatch.setattr(catalog_module, "TAIL_WINDOW_BYTES", 4)
    assert _tail_window(path, size) == b"ccc\n"


# --- one row per message, text is the first non-blank body line -----------------------------------


def test_one_row_per_message_across_carriers() -> None:
    tag = _tag(ts="2026-10-20T10:00:00.000Z", body="hi from the other chat")
    raw = _lines(_enqueue(tag), _meta_user(tag), _queued_command(tag))

    received = received_messages(raw)

    assert len(received) == 1
    assert received[0].at == "2026-10-20T10:00:00.000Z"
    assert received[0].text == "hi from the other chat"


def test_text_is_the_first_body_line_clipped() -> None:
    long_word = "x" * 250
    body = f"\n   \n  hello   world  {long_word}\nsecond line unseen"
    raw = _lines(_enqueue(_tag(body=body)))

    [msg] = received_messages(raw)

    assert msg.text == "hello world " + "x" * 187 + "…"
    assert len(msg.text) == TEXT_CHARS


# --- fail closed ------------------------------------------------------------------------------


def test_quotes_and_malformed_elements_give_no_row() -> None:
    good = _tag(body="valid message")
    duplicate_attr = (
        '<cross-session-message from="uds:/x" from="uds:/y" from-name="dup" '
        'timestamp="2026-10-20T10:00:00.000Z">body</cross-session-message>'
    )
    missing_name = (
        '<cross-session-message from="uds:/x" timestamp="2026-10-20T10:00:00.000Z">'
        "body</cross-session-message>"
    )
    empty_name = (
        '<cross-session-message from="uds:/x" from-name="" '
        'timestamp="2026-10-20T10:00:00.000Z">body</cross-session-message>'
    )
    no_close = (
        '<cross-session-message from="uds:/x" from-name="n" '
        'timestamp="2026-10-20T10:00:00.000Z">body without a close'
    )
    bad_timestamp = (
        '<cross-session-message from="uds:/x" from-name="n" timestamp="not-a-timestamp">'
        "body</cross-session-message>"
    )
    raw = _lines(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": good}]}},
        {
            "type": "user",
            "isMeta": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "content": good}]},
        },
        {"type": "user", "message": {"role": "user", "content": good}},  # plain turn, no isMeta
        _enqueue(duplicate_attr),
        _enqueue(missing_name),
        _enqueue(empty_name),
        _enqueue(no_close),
        _enqueue(bad_timestamp),
        _enqueue(good),  # control: the scan still finds a real one
    )

    received = received_messages(raw)

    assert [r.text for r in received] == ["valid message"]


# --- sender resolution -------------------------------------------------------------------------


def test_sender_resolves_only_to_a_listed_chat(tmp_path: Path) -> None:
    home, folder = _home(tmp_path)
    _write(folder, S, [_listed_entry()])
    _write(folder, OTHER_LISTED, [_listed_entry(entrypoint="sdk-py")])
    _registry_entry(home, "a.json", pid=111, session_id=S, socket="/sock-s")
    _registry_entry(home, "b.json", pid=222, session_id=OTHER_LISTED, socket="/sock-other")
    _registry_entry(home, "c.json", pid=333, session_id=GHOST, socket="/sock-ghost")
    tags = [
        _tag(from_="uds:/sock-s", ts="2026-10-20T10:00:04.000Z", body="from s"),
        _tag(from_="uds:/sock-other", ts="2026-10-20T10:00:03.000Z", body="from other"),
        _tag(from_="uds:/sock-ghost", ts="2026-10-20T10:00:02.000Z", body="from ghost"),
        _tag(from_="not-uds:/sock-s", ts="2026-10-20T10:00:01.000Z", body="not uds"),
    ]
    _write(folder, R, [_listed_entry(), *(_enqueue(t) for t in tags)])

    result = between_chats(Catalog(home, VAULT))

    by_text = {m["text"]: m for m in _messages(result)}
    assert by_text["from s"]["from_session_id"] == S
    assert by_text["from other"]["from_session_id"] is None
    assert by_text["from ghost"]["from_session_id"] is None
    assert by_text["not uds"]["from_session_id"] is None


def test_registry_key_files_are_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, folder = _home(tmp_path)
    _write(folder, R, [_listed_entry()])
    _registry_entry(home, "peer.json", pid=111, session_id=R, socket="/sock")
    (home / "sessions" / "peer.deadbeef.key").write_text("secret", encoding="utf-8")

    opened: list[Path] = []
    real_open, real_read_text, real_read_bytes = Path.open, Path.read_text, Path.read_bytes

    def spy_open(self: Path, *a: Any, **kw: Any) -> Any:
        opened.append(self)
        return real_open(self, *a, **kw)

    def spy_read_text(self: Path, *a: Any, **kw: Any) -> str:
        opened.append(self)
        return real_read_text(self, *a, **kw)

    def spy_read_bytes(self: Path, *a: Any, **kw: Any) -> bytes:
        opened.append(self)
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    between_chats(Catalog(home, VAULT))

    assert not any(p.suffix == ".key" for p in opened)
    assert any(p.name == "peer.json" for p in opened)  # the spy did see the registry read


# --- newest first, capped, bounded, broadcasts --------------------------------------------------


def test_newest_first_and_capped(tmp_path: Path) -> None:
    home, folder = _home(tmp_path)
    total = MAX_MESSAGES + 5
    tags = [_tag(ts=f"2026-10-20T10:00:{n:02d}.000Z", body=f"msg-{n:02d}") for n in range(total)]
    _write(folder, R, [_listed_entry(), *(_enqueue(t) for t in tags)])

    result = between_chats(Catalog(home, VAULT))

    texts = [m["text"] for m in _messages(result)]
    assert len(texts) == MAX_MESSAGES
    assert texts == [f"msg-{n:02d}" for n in range(total - 1, total - 1 - MAX_MESSAGES, -1)]


def test_reads_only_newest_sessions_and_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(between_chats_module, "SCAN_SESSIONS", 3)
    monkeypatch.setattr(between_chats_module, "SCAN_BYTES", 300)

    # (a) session-count bound: 4 non-other sessions, newest first by `ts`; only the oldest
    # (beyond the patched 3-session cutoff) carries a message.
    home_a, folder_a = _home(tmp_path, "home-a")
    ids = [f"{n}0000000-1111-4111-8111-111111111111" for n in range(4)]
    for n, sid in enumerate(ids):
        ts = f"2026-10-20T09:{59 - n:02d}:00.000Z"
        lines = [_listed_entry(ts=ts)]
        if n == 3:
            lines.append(_enqueue(_tag(body="too old to scan")))
        _write(folder_a, sid, lines)
    beyond_cutoff = between_chats(Catalog(home_a, VAULT))
    assert beyond_cutoff["messages"] == []

    # (b) byte bound: one scanned session, an early message pushed outside the tail window by a
    # large filler line, and a later message inside it.
    home_b, folder_b = _home(tmp_path, "home-b")
    filler = {"type": "progress", "pad": "x" * 2000}
    early = _enqueue(_tag(body="too early to see"))
    later = _enqueue(_tag(body="within the tail"))
    _write(folder_b, R, [_listed_entry(), early, filler, later])
    tailed = between_chats(Catalog(home_b, VAULT))
    assert [m["text"] for m in _messages(tailed)] == ["within the tail"]


def test_broadcast_is_one_row_per_receiver(tmp_path: Path) -> None:
    home, folder = _home(tmp_path)
    tag = _tag(body="broadcast to everyone")
    _write(folder, R1, [_listed_entry(), _enqueue(tag)])
    _write(folder, R2, [_listed_entry(), _enqueue(tag)])

    result = between_chats(Catalog(home, VAULT))

    receivers = {
        m["to_session_id"] for m in _messages(result) if m["text"] == "broadcast to everyone"
    }
    assert receivers == {R1, R2}


# --- partial --------------------------------------------------------------------------------------


def test_unreadable_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, folder = _home(tmp_path)
    _write(folder, R, [_listed_entry()])
    catalog = Catalog(home, VAULT)

    real_stat_for_read = between_chats_module._stat_for_read

    def denied(path: Path) -> Any:
        return "unreadable" if path.name == f"{R}.jsonl" else real_stat_for_read(path)

    monkeypatch.setattr(between_chats_module, "_stat_for_read", denied)

    assert between_chats(catalog) == {"messages": [], "partial": True}

    monkeypatch.undo()
    partial_listing = SessionsResult(sessions=[], state="partial", reason="time_budget", omitted=1)
    monkeypatch.setattr(catalog, "list_sessions", lambda **kw: partial_listing)

    assert between_chats(catalog)["partial"] is True


def test_permission_error_after_a_good_stat_marks_partial_and_others_still_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, folder = _home(tmp_path)
    _write(folder, R, [_listed_entry(), _enqueue(_tag(body="blocked"))])
    _write(folder, R1, [_listed_entry(), _enqueue(_tag(body="still answers"))])

    real_tail_window = between_chats_module._tail_window

    def flaky(path: Path, size: int, *, window: int | None = None) -> bytes:
        if path.name == f"{R}.jsonl":
            raise PermissionError("denied")
        return real_tail_window(path, size, window=window)

    monkeypatch.setattr(between_chats_module, "_tail_window", flaky)

    result = between_chats(Catalog(home, VAULT))

    assert result["partial"] is True
    assert [m["text"] for m in _messages(result)] == ["still answers"]


# --- name/text are clipped in UTF-16 units (the client's `.length`), not code points ------------


def test_name_and_text_clip_in_utf16_units_not_code_points() -> None:
    emoji = "\U0001f600"
    long_name = emoji * 64  # 64 code points = 128 UTF-16 units: over NAME_CHARS, must clip
    long_text = "x" * 199 + emoji  # 200 code points = 201 UTF-16 units: over TEXT_CHARS by one
    raw = _lines(_enqueue(_tag(name=long_name, body=long_text)))

    [msg] = received_messages(raw)

    name_units = len(msg.from_name.encode("utf-16-le")) // 2
    text_units = len(msg.text.encode("utf-16-le")) // 2
    assert name_units <= NAME_CHARS
    assert msg.from_name.endswith("…")
    assert text_units <= TEXT_CHARS
    assert msg.text.endswith("…")
    # A split surrogate half would make this raise UnicodeDecodeError.
    msg.from_name.encode("utf-16-le").decode("utf-16-le")
    msg.text.encode("utf-16-le").decode("utf-16-le")


# --- the timestamp and tag regexes are ASCII-only ------------------------------------------------


def test_full_width_digit_timestamp_gives_no_row() -> None:
    full_width_ts = "２０２６-１０-２０T１０:００:００Z"
    raw = _lines(_enqueue(_tag(ts=full_width_ts)))

    assert received_messages(raw) == []


# --- every row the server returns must pass the client's own checks ------------------------------


def test_whitespace_only_from_name_gives_no_row() -> None:
    raw = _lines(
        _enqueue(_tag(name=" ", ts="2026-10-20T10:00:00.000Z")),
        _enqueue(_tag(name="　", ts="2026-10-20T10:00:01.000Z")),
    )

    assert received_messages(raw) == []


def test_non_uuid_listed_sender_resolves_to_none(tmp_path: Path) -> None:
    home, folder = _home(tmp_path)
    _write(folder, "notes", [_listed_entry()])  # listed, but its own id is not a UUID
    _registry_entry(home, "n.json", pid=444, session_id="notes", socket="/sock-n")
    _write(folder, R, [_listed_entry(), _enqueue(_tag(from_="uds:/sock-n", body="from notes"))])

    result = between_chats(Catalog(home, VAULT))

    [msg] = [m for m in _messages(result) if m["text"] == "from notes"]
    assert msg["from_session_id"] is None


def test_non_uuid_listed_receiver_contributes_no_row(tmp_path: Path) -> None:
    home, folder = _home(tmp_path)
    _write(folder, "notes", [_listed_entry(), _enqueue(_tag(body="to notes"))])

    result = between_chats(Catalog(home, VAULT))

    assert _messages(result) == []


# --- bounded cost regardless of content ----------------------------------------------------------


def test_long_open_tag_attribute_run_with_no_close_tag_gives_no_row() -> None:
    # Far more attribute characters than OPEN_TAG_MAX_CHARS, and no '>' anywhere: the bounded,
    # possessive match must fail outright (functional check only; no wall-clock assertion).
    reps = OPEN_TAG_MAX_CHARS // len(' a=""') + 10
    bomb = "<cross-session-message" + ' a=""' * reps + "X"
    raw = _lines(_enqueue(bomb))

    assert received_messages(raw) == []


def test_tail_contributes_only_its_newest_max_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(between_chats_module, "MAX_MESSAGES", 2)
    home, folder = _home(tmp_path)
    tags = [_tag(ts=f"2026-10-20T10:00:{n:02d}.000Z", body=f"msg-{n:02d}") for n in range(5)]
    _write(folder, R, [_listed_entry(), *(_enqueue(t) for t in tags)])

    result = between_chats(Catalog(home, VAULT))

    assert [m["text"] for m in _messages(result)] == ["msg-04", "msg-03"]


# --- a non-meta user turn and a suffixed timestamp give no row ------------------------------------


def test_non_meta_user_turn_with_the_prefix_gives_no_row() -> None:
    tag = _tag(body="should not surface")
    raw = _lines(
        {
            "type": "user",
            "isMeta": False,
            "message": {
                "role": "user",
                "content": f"Another Claude session sent a message:\n{tag}\nRespond if useful.",
            },
        }
    )

    assert received_messages(raw) == []


def test_timestamp_with_a_trailing_suffix_gives_no_row() -> None:
    raw = _lines(_enqueue(_tag(ts="2026-10-20T10:00:00.000Z junk")))

    assert received_messages(raw) == []
