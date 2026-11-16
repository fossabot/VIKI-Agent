"""The user's Decision model key: sealed at rest in one row, answered only by `load`, removable,
and read as missing, saved, unreadable or unavailable with the key in no error, repr or log."""

from __future__ import annotations

import logging
import sqlite3
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection
from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.decision_loop.contracts import DecisionLoopError
from viki_agent.decision_loop.key_store import DecisionKeyStore
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
MS = timedelta(milliseconds=1)
CONTEXT = b"VIKI decision key v1\x00"
MARKER = "Marker7f3a"
KEY = "ts_" + MARKER + "-2c4e.91bd~Synthetic+Key/0"
OTHER_KEY = "ts_Second-Marker-4e91"
PRINTABLE = "".join(chr(code) for code in range(0x21, 0x7F))  # "!" to "~"


class Clock:
    """A settable wall clock."""

    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def keys(store: SessionStore, clock: Clock) -> DecisionKeyStore:
    return DecisionKeyStore(store, now=clock)


def rows(store: SessionStore) -> list[dict[str, Any]]:
    with store._connection() as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM decision_key")]


def execute(store: SessionStore, sql: str, values: tuple[object, ...]) -> None:
    with store._connection(write=True) as connection:
        connection.execute(sql, values)


def flip_a_byte(store: SessionStore) -> None:
    body = bytearray(rows(store)[0]["body"])
    body[len(body) // 2] ^= 0x01
    execute(store, "UPDATE decision_key SET body=?", (bytes(body),))


def shown(error: BaseException) -> str:
    """All an error can show: its formatted traceback, and the repr, args and attributes of each
    error in its chain."""
    parts = ["".join(traceback.format_exception(error))]
    pending: list[BaseException | None] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts += [repr(current), repr(current.args), repr(vars(current))]
        pending += [current.__cause__, current.__context__]
    return "\n".join(parts)


def refused(code: str, operation: Callable[..., object], *args: object) -> DecisionLoopError:
    """`operation(*args)` refuses with `code` alone, and nothing the error can show holds a
    marked key."""
    with pytest.raises(DecisionLoopError) as caught:
        operation(*args)
    error = caught.value
    assert (error.code, str(error), error.extra) == (code, code, {})
    assert MARKER not in shown(error)
    return error


def refuse(data: bytes, *, context: bytes) -> bytes:
    """`protection.protect` and `protection.unprotect` when sealing does not work."""
    raise protection.ProtectionError()


# ---- sealing and saving ------------------------------------------------------------------------


def test_the_key_is_sealed_at_rest(keys: DecisionKeyStore, store: SessionStore) -> None:
    assert keys.save(KEY) is None

    (row,) = rows(store)
    assert (row["slot"], row["format"], row["updated_at"]) == ("typesafe", 1, stamp(NOW))
    for raw in (row["body"], store.path.read_bytes()):
        for text in (KEY, MARKER):
            assert text.encode("ascii") not in raw and text.encode("utf-16-le") not in raw
    # The body is the key's ASCII bytes sealed under the key's own context.
    assert protection.unprotect(row["body"], context=CONTEXT) == KEY.encode("ascii")
    assert (keys.load(), keys.status(), keys.saved(), keys.changed_at()) == (
        KEY,
        "saved",
        True,
        stamp(NOW),
    )


@pytest.mark.parametrize(
    "key", [PRINTABLE, "~" * 4096, "!"], ids=["printable", "longest", "shortest"]
)
def test_every_printable_key_round_trips(
    keys: DecisionKeyStore, store: SessionStore, key: str
) -> None:
    keys.save(key)
    assert (keys.load(), keys.status()) == (key, "saved")
    (row,) = rows(store)
    assert protection.unprotect(row["body"], context=CONTEXT) == key.encode("ascii")


def test_remove_deletes_the_key_once(keys: DecisionKeyStore, store: SessionStore) -> None:
    missing = ("missing", None, False, None)
    assert (keys.status(), keys.load(), keys.saved(), keys.changed_at()) == missing
    assert keys.remove() is False

    keys.save(KEY)
    assert keys.remove() is True
    assert rows(store) == []
    assert (keys.status(), keys.load(), keys.saved(), keys.changed_at()) == missing
    assert keys.remove() is False


def test_each_save_replaces_the_row_and_changes_changed_at(
    keys: DecisionKeyStore, store: SessionStore, clock: Clock
) -> None:
    keys.save(KEY)
    assert keys.changed_at() == stamp(NOW)
    # The same key saved again at the same instant still changes it, one millisecond on.
    keys.save(KEY)
    assert keys.changed_at() == stamp(NOW + MS)
    # So does a save while the clock reads earlier than the stored stamp.
    clock.at = NOW - timedelta(hours=1)
    keys.save(KEY)
    assert keys.changed_at() == stamp(NOW + 2 * MS)
    # A later clock is taken as it reads.
    clock.at = NOW + timedelta(minutes=5)
    keys.save(OTHER_KEY)
    assert [row["updated_at"] for row in rows(store)] == [stamp(NOW + timedelta(minutes=5))]
    assert keys.load() == OTHER_KEY
    # A remove forgets the stamp, so the next save starts from the clock again.
    assert keys.remove() is True and keys.changed_at() is None
    clock.at = NOW
    keys.save(KEY)
    assert (keys.changed_at(), keys.load()) == (stamp(NOW), KEY)


@pytest.mark.parametrize(
    "stored", ["9999-12-31T23:59:59.999Z", "9999-99-99T99:99:99.999Z"], ids=["last", "unparsed"]
)
def test_a_stored_stamp_that_cannot_move_on_gives_way_to_the_clock(
    keys: DecisionKeyStore, store: SessionStore, stored: str
) -> None:
    keys.save(KEY)
    execute(store, "UPDATE decision_key SET updated_at=?", (stored,))
    keys.save(KEY)
    assert (keys.changed_at(), keys.load()) == (stamp(NOW), KEY)


def test_the_default_clock_stamps_utc_now(store: SessionStore) -> None:
    before = datetime.now(UTC) - MS  # a stamp keeps whole milliseconds
    DecisionKeyStore(store).save(KEY)
    changed_at = DecisionKeyStore(store).changed_at()
    assert changed_at is not None
    assert before <= parse_stamp(changed_at) <= datetime.now(UTC)


def test_a_failed_save_keeps_the_saved_key(
    keys: DecisionKeyStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    keys.save(KEY)
    before = rows(store)
    clock.at = NOW + timedelta(minutes=1)

    monkeypatch.setattr(protection, "protect", refuse)
    refused("unavailable", keys.save, OTHER_KEY)
    assert (keys.status(), keys.load()) == ("saved", KEY)
    # A sealed body the column cannot hold fails inside the transaction, which rolls back.
    monkeypatch.setattr(protection, "protect", lambda data, *, context: b"x" * 8193)
    refused("unavailable", keys.save, OTHER_KEY)
    monkeypatch.undo()
    assert rows(store) == before and keys.load() == KEY


# ---- unreadable and unavailable ----------------------------------------------------------------


def test_a_flipped_byte_reads_unreadable(
    keys: DecisionKeyStore, store: SessionStore, clock: Clock
) -> None:
    keys.save(KEY)
    flip_a_byte(store)

    assert (keys.status(), keys.load(), keys.saved()) == ("unreadable", None, False)
    # Its stamp still reads, and the next save replaces it.
    assert keys.changed_at() == stamp(NOW)
    clock.at = NOW + timedelta(minutes=1)
    keys.save(KEY)
    assert (keys.status(), keys.load()) == ("saved", KEY)


WRONG_BODIES = {
    "item_context": (KEY.encode("ascii"), b"VIKI decision item v1\x00"),
    "messaging_context": (KEY.encode("ascii"), b"VIKI messaging settings v1\x00slack"),
    "empty": (b"", CONTEXT),
    "space": (b"Marker 7f3a", CONTEXT),
    "newline": (KEY.encode("ascii") + b"\n", CONTEXT),
    "not_ascii": (b"Marker" + bytes([0xC3, 0xA9]), CONTEXT),
    "too_long": (b"x" * 4097, CONTEXT),
}


@pytest.mark.parametrize(("plain", "context"), WRONG_BODIES.values(), ids=WRONG_BODIES.keys())
def test_a_body_that_does_not_open_to_a_key_reads_unreadable(
    keys: DecisionKeyStore, store: SessionStore, plain: bytes, context: bytes
) -> None:
    keys.save(KEY)
    execute(store, "UPDATE decision_key SET body=?", (protection.protect(plain, context=context),))
    assert (keys.status(), keys.load(), keys.saved()) == ("unreadable", None, False)


def test_sealing_that_does_not_work_reads_unavailable(
    keys: DecisionKeyStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys.save(KEY)
    before = rows(store)

    # The body refuses to open and so does the probe: unavailable, not unreadable.
    monkeypatch.setattr(protection, "unprotect", refuse)
    assert (keys.status(), keys.saved()) == ("unavailable", False)
    refused("unavailable", keys.load)
    # Reading the stamp needs no sealing.
    assert keys.changed_at() == stamp(NOW)
    monkeypatch.setattr(protection, "protect", refuse)
    assert keys.status() == "unavailable"
    refused("unavailable", keys.load)
    refused("unavailable", keys.save, OTHER_KEY)
    monkeypatch.undo()
    assert rows(store) == before and keys.load() == KEY


@pytest.mark.parametrize("fault", ["unreadable", "unavailable"])
def test_a_key_that_does_not_open_can_still_be_removed(
    keys: DecisionKeyStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    keys.save(KEY)
    if fault == "unreadable":
        flip_a_byte(store)
    else:
        monkeypatch.setattr(protection, "unprotect", refuse)
        monkeypatch.setattr(protection, "protect", refuse)
    assert keys.status() == fault
    assert keys.remove() is True
    assert rows(store) == [] and keys.status() == "missing"


# Every method that refuses when the store does not work; `status` and `saved` answer instead.
OPERATIONS: dict[str, Callable[[DecisionKeyStore], object]] = {
    "save": lambda keys: keys.save(OTHER_KEY),
    "load": lambda keys: keys.load(),
    "changed_at": lambda keys: keys.changed_at(),
    "remove": lambda keys: keys.remove(),
}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ["private", "store"])
def test_a_store_that_does_not_work_reads_unavailable(
    keys: DecisionKeyStore,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fault: str,
) -> None:
    keys.save(KEY)
    before = rows(store)

    def not_private(store: object) -> None:
        raise protection.ProtectionError()

    def broken(self: SessionStore, **kwargs: object) -> object:
        raise StateError("busy")

    if fault == "private":
        monkeypatch.setattr("viki_agent.decision_loop.key_store.require_private_store", not_private)
    else:
        monkeypatch.setattr(SessionStore, "_connection", broken)
    refused("unavailable", OPERATIONS[operation], keys)
    assert (keys.status(), keys.saved()) == ("unavailable", False)
    monkeypatch.undo()
    assert rows(store) == before


# ---- refusals and leaks ------------------------------------------------------------------------


MALFORMED: dict[str, object] = {
    "empty": "",
    "space": MARKER + " key",
    "newline": MARKER + "\n",
    "tab": "\t" + MARKER,
    "delete": MARKER + chr(0x7F),
    "nul": chr(0) + MARKER,
    "accented": MARKER + chr(0xE9),
    "wide": MARKER + chr(0x9375),
    "too_long": MARKER + "x" * (4097 - len(MARKER)),
    "bytes": MARKER.encode("ascii"),
    "none": None,
    "number": 7,
}


@pytest.mark.parametrize("key", MALFORMED.values(), ids=MALFORMED.keys())
def test_save_refuses_a_malformed_key_without_echoing_it(
    keys: DecisionKeyStore, store: SessionStore, key: object
) -> None:
    keys.save(OTHER_KEY)
    before = rows(store)
    refused("invalid_request", keys.save, key)
    assert rows(store) == before


def test_the_key_shows_in_no_log_repr_error_or_answer_but_load(
    keys: DecisionKeyStore,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    answers: list[object] = []
    errors: list[DecisionLoopError] = []

    def run(operation: Callable[..., object], *args: object) -> None:
        try:
            answers.append(operation(*args))
        except DecisionLoopError as error:
            errors.append(error)

    def read_all() -> None:
        for read in (keys.load, keys.status, keys.saved, keys.changed_at):
            run(read)

    run(keys.save, KEY)
    loaded = keys.load()  # the one answer that holds the key
    for read in (keys.status, keys.saved, keys.changed_at):
        run(read)
    run(keys.save, KEY + " ")
    monkeypatch.setattr(protection, "unprotect", refuse)
    read_all()
    monkeypatch.setattr(protection, "protect", refuse)
    run(keys.save, KEY)
    monkeypatch.undo()
    flip_a_byte(store)
    read_all()
    run(keys.remove)

    assert loaded == KEY
    assert [error.code for error in errors] == ["invalid_request", "unavailable", "unavailable"]
    seen = [
        caplog.text,
        *(record.getMessage() for record in caplog.records),
        repr(keys),
        str(keys),
        repr(vars(keys)),
        repr(answers),
        *(shown(error) for error in errors),
    ]
    assert [text for text in seen if MARKER in text] == []
