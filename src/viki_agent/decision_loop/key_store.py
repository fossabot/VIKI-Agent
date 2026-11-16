"""The user's Decision model key, sealed at rest in the store's one `decision_key` row.

The row (slot "typesafe", format 1) keeps the key's ASCII bytes sealed by `protection.protect`
under the key's own context, and `updated_at`, the stamp of its last save. A key is 1 to 4096
characters from "!" to "~"; `save` refuses anything else as `invalid_request`, with no part of
the value in the refusal. `load` is the one method that answers the key.

`status` reads the row as "missing" when there is none, "saved" when its body opens to a key,
"unreadable" when it does not, and "unavailable" when sealing or the store does not work. A bad
body and a protection that cannot work at all refuse alike; as in `messaging_store`, a probe
sealed and opened under the same context tells them apart. `load` answers None for "missing"
and "unreadable" and refuses for "unavailable"; `saved` answers whether the status is "saved".
Each save stamps the row later than the stamp it replaces, so `changed_at` changes with every
save, the same key's included, and shows that the key changed without reading it.

Each method runs one transaction through the store's private connection; `save` seals before
its write transaction begins, and `load` and `status` open the body after their read one ends.
Refusals are `DecisionLoopError` codes: `invalid_request` for a malformed key and `unavailable`
when the store or sealing does not work. Nothing here logs.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

from viki_agent import protection
from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.contracts import DecisionLoopError
from viki_agent.storage import SessionStore, StateError

KeyStatus = Literal["missing", "saved", "unreadable", "unavailable"]

_SLOT = "typesafe"  # the table's one slot
_FORMAT = 1
_CONTEXT = b"VIKI decision key v1\x00"
_KEY = re.compile(r"[\x21-\x7e]{1,4096}")  # printable ASCII without the space
_MILLISECOND = timedelta(milliseconds=1)


def _is_key(value: object) -> bool:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def _after(previous: str | None, current: str) -> str:
    """The stamp a save gives the row: `current`, or one millisecond after `previous` when
    `current` is not later, so a save changes the stamp it replaces. A stored stamp that does
    not parse, or the last one there is, gives way to `current`."""
    if previous is None or current > previous:
        return current
    try:
        return stamp(parse_stamp(previous) + _MILLISECOND)
    except ValueError, OverflowError:
        return current


def _open(sealed: bytes) -> bytes | None:
    """The plain bytes of the stored body, or None when it does not open.

    A bad body and a protection that cannot work at all refuse alike. As in `messaging_store`,
    a probe sealed and opened under the same context tells them apart: when the probe fails
    too, sealing is unavailable.
    """
    try:
        return protection.unprotect(sealed, context=_CONTEXT)
    except protection.ProtectionError:
        pass
    try:
        protection.unprotect(protection.protect(b"{}", context=_CONTEXT), context=_CONTEXT)
    except protection.ProtectionError:
        raise DecisionLoopError("unavailable") from None
    return None


def _key(plain: bytes | None) -> str | None:
    """The key `plain` holds, or None when it holds none."""
    key = plain.decode("ascii") if plain is not None and plain.isascii() else None
    return key if _is_key(key) else None


class DecisionKeyStore:
    """The user's Decision model key in the store's `decision_key` row."""

    def __init__(
        self, store: SessionStore, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.store = store
        self.now = now

    @contextlib.contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        try:
            require_private_store(self.store)
            with self.store._connection(write=write) as connection:
                yield connection
        except StateError, protection.ProtectionError:
            raise DecisionLoopError("unavailable") from None

    def _body(self) -> bytes | None:
        """The row's sealed body, or None when there is no row."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT body FROM decision_key WHERE slot=?", (_SLOT,)
            ).fetchone()
        return None if row is None else row[0]

    def save(self, key: str) -> None:
        """Seal `key` and store it in place of any saved key, in one write transaction.

        A key is 1 to 4096 characters from "!" to "~"; anything else is `invalid_request`.
        """
        if not _is_key(key):
            raise DecisionLoopError("invalid_request")
        current = stamp(self.now())
        try:
            sealed = protection.protect(key.encode("ascii"), context=_CONTEXT)
        except protection.ProtectionError:
            raise DecisionLoopError("unavailable") from None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT updated_at FROM decision_key WHERE slot=?", (_SLOT,)
            ).fetchone()
            connection.execute(
                "INSERT OR REPLACE INTO decision_key (slot,format,body,updated_at) "
                "VALUES (?,?,?,?)",
                (_SLOT, _FORMAT, sealed, _after(None if row is None else row[0], current)),
            )

    def load(self) -> str | None:
        """The saved key: None when there is none or its body does not open to one, and
        `unavailable` when the store or sealing does not work. The one method that answers the
        key."""
        sealed = self._body()
        return None if sealed is None else _key(_open(sealed))

    def saved(self) -> bool:
        """Whether `status` reads "saved"."""
        return self.status() == "saved"

    def status(self) -> KeyStatus:
        """How the saved key reads: "missing", "saved", "unreadable" or "unavailable". It
        answers rather than refusing."""
        try:
            sealed = self._body()
            if sealed is None:
                return "missing"
            return "saved" if _key(_open(sealed)) is not None else "unreadable"
        except DecisionLoopError:
            return "unavailable"

    def changed_at(self) -> str | None:
        """The row's `updated_at`, the stamp of the last save, or None when no key is saved. It
        reads no body, so an unreadable key has one too."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT updated_at FROM decision_key WHERE slot=?", (_SLOT,)
            ).fetchone()
        return None if row is None else row[0]

    def remove(self) -> bool:
        """Delete the saved key, readable or not; answer whether there was one."""
        with self._transaction() as connection:
            deleted = connection.execute("DELETE FROM decision_key WHERE slot=?", (_SLOT,)).rowcount
        return deleted == 1
