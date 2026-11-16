"""Durable user choices for permission, plan and question cards.

A row here records that the user chose something and how far the app got in handing that
choice to the local driver future. It is not native acknowledgment, not a delivery queue and
not a retry ledger: nothing in this module ever calls a driver or replays a recorded choice.
The chosen value and the displayed request live only inside the DPAPI body; the plain columns
carry identifiers, state and timestamps. No plaintext length is stored and the document is
padded before encryption, because an allow and a deny differ by a single byte: a size anywhere
in the clear would publish the decision the body exists to protect.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from viki_agent import protection
from viki_agent.conversations.drafts import (
    MAX_PAGE_SIZE,
    DraftError,
    draft_scope,
    require_private_store,
)
from viki_agent.state import AppState
from viki_agent.storage import ApprovalRow, StateError

MAX_APPROVALS = 4096
MAX_SCOPE_BYTES = 64 << 20  # ciphertext per scope; no plaintext size exists to count
MAX_CHOICE_BYTES = 64 << 10  # the choice JSON alone
MAX_RECORD_BYTES = (1 << 20) + (128 << 10)  # the padded document: card, choice, framing, padding
MAX_CIPHERTEXT_BYTES = MAX_RECORD_BYTES + (64 << 10)
MAX_TOOL_USE_CHARS = 256
PAD_TO = 4096
ApprovalState = Literal["recorded", "delivering", "delivered_locally", "not_delivered", "unknown"]
TerminalState = Literal["delivered_locally", "not_delivered", "unknown"]
# The reasons each state is allowed to
# carry. A pair outside this table is refused on write and read as a corrupt row, so no
# transition can claim evidence the state does not have.
_REASONS_FOR: dict[str, frozenset[str]] = {
    "recorded": frozenset({""}),
    "delivering": frozenset({""}),
    "delivered_locally": frozenset({""}),
    "not_delivered": frozenset(
        {"stale_request", "runtime_changed", "cancelled", "storage", "interrupted", "owner_changed"}
    ),
    "unknown": frozenset({"interrupted", "owner_changed", "internal"}),
}
# The frozen public name, derived so that adding a reason to one of them cannot leave the
# other behind: only the table above decides what a state may carry.
REASONS = frozenset().union(*_REASONS_FOR.values()) - {""}
_NONTERMINAL = frozenset({"recorded", "delivering"})
_IDENTITY = (
    "choice_id",
    "session_id",
    "generation",
    "runtime_id",
    "request_id",
    "turn_id",
    "tool_use_id",
)
_MESSAGES = {
    "invalid_input": "The choice given isn't valid.",
    "invalid_choice": "The choice isn't in a format that can be recorded.",
    "invalid_id": "The choice record's ID isn't valid.",
    "invalid_page": "The paging for the choice list isn't valid.",
    "not_found": "Choice record not found.",
    "already_decided": "This confirmation already has a recorded choice. Check the record.",
    "conflict": "This choice record can't be changed. Check its current state.",
    # The relief this names is a restart first: the reclaim never takes a row this launch wrote,
    # and neither does the reset, so a scope filled inside one launch only frees after it exits.
    "capacity": (
        "The choice record storage limit has been reached. Open VIKI Agent again, then reset "
        '"Choice records" on the data screen.'
    ),
    "too_large": "The choice record is too large to save.",
    "protection": (
        "Couldn't safely read or write the choice. Check the save location's access rights and "
        + protection.ACCOUNT_HINT
        + "."
    ),
    "corrupt": (
        "Couldn't read the choice record. Keep the original file and check the recovery steps."
    ),
    "unavailable": "Couldn't read or write the choice record. Check the save location.",
    "internal": "Couldn't finish processing the choice record.",
    "unsafe_path": "Couldn't check the choice record's save location.",
}

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """A safe service error; the chosen value and storage exceptions never reach its text."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message if message is not None else _MESSAGES[code]
        super().__init__(self.message)


class ApprovalConflict(ApprovalError):
    def __init__(self) -> None:
        super().__init__("conflict")


@dataclass(frozen=True)
class NativeRequest:
    runtime_id: str
    request_id: str
    turn_id: str


@dataclass(frozen=True)
class PermissionChoice:
    allow: bool


@dataclass(frozen=True)
class QuestionChoice:
    answers: dict[str, str]
    response: str


Choice = PermissionChoice | QuestionChoice


@dataclass(frozen=True)
class ChoiceInput:
    choice_id: str
    session_id: str
    generation: str
    tool_use_id: str
    native: NativeRequest
    choice: Choice


@dataclass(frozen=True)
class ChoiceSummary:
    choice_id: str
    session_id: str
    generation: str
    runtime_id: str
    request_id: str
    turn_id: str
    tool_use_id: str
    state: ApprovalState
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChoiceRecord(ChoiceSummary):
    choice: Choice
    request: dict[str, Any]  # the displayed projection item {type, data, native_request}


@dataclass(frozen=True)
class ChoicePage:
    approvals: list[ChoiceSummary]
    next_after: str | None


def choice_kind(choice: Choice) -> Literal["permission", "question"]:
    if isinstance(choice, PermissionChoice):
        return "permission"
    if isinstance(choice, QuestionChoice):
        return "question"
    raise ApprovalError("invalid_choice")


def choice_to_json(choice: Choice) -> dict[str, Any]:
    if isinstance(choice, PermissionChoice) and isinstance(choice.allow, bool):
        return {"kind": "permission", "allow": choice.allow}
    if (
        isinstance(choice, QuestionChoice)
        and isinstance(choice.answers, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in choice.answers.items())
        and isinstance(choice.response, str)
    ):
        return {"kind": "question", "answers": dict(choice.answers), "response": choice.response}
    raise ApprovalError("invalid_choice")


def choice_from_json(value: Any) -> Choice:
    if isinstance(value, dict):
        kind = value.get("kind")
        if (
            kind == "permission"
            and set(value) == {"kind", "allow"}
            and isinstance(value["allow"], bool)
        ):
            return PermissionChoice(value["allow"])
        if (
            kind == "question"
            and set(value) == {"kind", "answers", "response"}
            and isinstance(value["answers"], dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value["answers"].items())
            and isinstance(value["response"], str)
        ):
            return QuestionChoice(dict(value["answers"]), value["response"])
    raise ApprovalError("invalid_choice")


def _uuid(value: Any, code: str = "invalid_input") -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ApprovalError(code)


def _tool_use_id(value: Any, code: str = "invalid_input") -> str:
    """The one variable-length identifier, kept to printable ASCII.

    It is the last field of the NUL-delimited DPAPI context, so a control character or a
    character the context cannot encode would break the binding rather than the request.
    The empty string is admitted: the driver emits plan and permission cards without a native
    tool-use ID, and those cards must stay answerable.
    """
    if (
        isinstance(value, str)
        and len(value) <= MAX_TOOL_USE_CHARS
        and all(" " <= character <= "~" for character in value)
    ):
        return value
    raise ApprovalError(code)


def _blocks(value: bytes) -> bytes:
    """Pad to whole PAD_TO blocks with JSON whitespace, so a length reveals only the block."""
    return value.ljust(-(-len(value) // PAD_TO) * PAD_TO, b" ")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _input(value: ChoiceInput) -> ChoiceInput:
    if not isinstance(value, ChoiceInput) or not isinstance(value.native, NativeRequest):
        raise ApprovalError("invalid_input")
    for part in (
        value.choice_id,
        value.session_id,
        value.generation,
        value.native.runtime_id,
        value.native.request_id,
        value.native.turn_id,
    ):
        _uuid(part)
    _tool_use_id(value.tool_use_id)
    if len(_canonical(choice_to_json(value.choice))) > MAX_CHOICE_BYTES:
        raise ApprovalError("invalid_choice")
    return value


def _identity(scope: str, value: ChoiceInput) -> dict[str, str]:
    return {
        "scope": scope,
        "choice_id": value.choice_id,
        "session_id": value.session_id,
        "generation": value.generation,
        "runtime_id": value.native.runtime_id,
        "request_id": value.native.request_id,
        "turn_id": value.native.turn_id,
        "tool_use_id": value.tool_use_id,
    }


def _row_identity(scope: str, row: ApprovalRow) -> dict[str, str]:
    return {"scope": scope, **{name: row[name] for name in _IDENTITY}}


def _context(identity: dict[str, str]) -> bytes:
    joined = "\0".join(identity[name] for name in ("scope", *_IDENTITY))
    return f"VIKI approval v1\0{joined}".encode("ascii")


def _request_hash(identity: dict[str, str]) -> str:
    # Identifiers only. The allow/deny value, the answers and the free text are never hashed:
    # a stable digest of a two-valued choice would publish it in a plain column.
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _document(choice: Choice, request: dict[str, Any]) -> bytes:
    """The protected document, padded so that its size carries nothing about the choice.

    Trailing spaces are JSON whitespace, so the reader needs no stored length and strips
    nothing. The serialised choice is quantised to its own block first: rounding only the
    finished document up leaves the one-byte difference visible whenever the allow form lands
    exactly on a block edge, and `length(body)` is a plain column an offline reader of a
    backup or a synced copy can see without the DPAPI key.
    """
    if not isinstance(request, dict):
        raise ApprovalError("invalid_input")
    try:
        chosen = _blocks(_canonical(choice_to_json(choice)))
        document = _blocks(b'{"choice":' + chosen + b',"request":' + _canonical(request) + b"}")
    except TypeError, ValueError, RecursionError:
        raise ApprovalError("invalid_input") from None
    if len(document) > MAX_RECORD_BYTES:
        raise ApprovalError("too_large")
    return document


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 24:
        raise ApprovalError("corrupt")
    try:
        canonical = datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    except ValueError:
        raise ApprovalError("corrupt") from None
    if canonical.replace("+00:00", "Z") != value:
        raise ApprovalError("corrupt")
    return value


class ApprovalService:
    def __init__(self, state: AppState, vault: Path, *, owner: str) -> None:
        self._store = state.store
        self._owner = _uuid(owner)
        self._reclaimed = False
        self._reclaim_refused = False
        try:
            self._scope = draft_scope(vault)
        except DraftError as exc:
            raise ApprovalError(exc.code) from None

    @contextlib.contextmanager
    def _access(self) -> Iterator[None]:
        try:
            require_private_store(self._store)
            yield
        except protection.ProtectionError:
            raise ApprovalError("protection") from None
        except StateError as exc:
            raise ApprovalError(exc.code, exc.message) from None

    def _summary(self, row: ApprovalRow) -> ChoiceSummary:
        try:
            for name in _IDENTITY[:-1]:
                _uuid(row[name], "corrupt")
            _tool_use_id(row["tool_use_id"], "corrupt")
            _uuid(row["owner"], "corrupt")
        except ApprovalError:
            raise ApprovalError("corrupt") from None
        state, reason = row["state"], row["reason"]
        if (
            row["request_hash"] != _request_hash(_row_identity(self._scope, row))
            or state not in _REASONS_FOR
            or reason not in _REASONS_FOR[state]
        ):
            raise ApprovalError("corrupt")
        if row["owner"] != self._owner and state in _NONTERMINAL:
            # Intent commits strictly before the one local callback, so a previous launch's
            # `recorded` row proves the callback never ran; `delivering` stays honest.
            state, reason = (
                ("not_delivered", "owner_changed")
                if state == "recorded"
                else ("unknown", "owner_changed")
            )
        return ChoiceSummary(
            **{name: row[name] for name in _IDENTITY},
            state=cast(ApprovalState, state),
            reason=reason,
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
        )

    def _decode(self, row: ApprovalRow) -> ChoiceRecord:
        summary = self._summary(row)
        cipher = row["body"]
        if not isinstance(cipher, bytes) or not 0 < len(cipher) <= MAX_CIPHERTEXT_BYTES:
            raise ApprovalError("corrupt")
        plaintext = protection.unprotect(cipher, context=_context(_row_identity(self._scope, row)))
        try:
            payload = json.loads(plaintext.decode("ascii"))
        except UnicodeError, ValueError:
            raise ApprovalError("corrupt") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"choice", "request"}
            or not isinstance(payload["request"], dict)
        ):
            raise ApprovalError("corrupt")
        try:
            choice = choice_from_json(payload["choice"])
        except ApprovalError:
            raise ApprovalError("corrupt") from None
        return ChoiceRecord(**asdict(summary), choice=choice, request=payload["request"])

    def _same(self, row: ApprovalRow, value: ChoiceInput) -> ChoiceRecord:
        record = self._decode(row)
        identity = _identity(self._scope, value)
        if any(row[name] != identity[name] for name in _IDENTITY) or record.choice != value.choice:
            raise ApprovalConflict()
        return record

    def lookup(self, value: ChoiceInput) -> ChoiceRecord | None:
        value = _input(value)
        with self._access():
            row = self._store.get_approval(self._scope, value.choice_id)
            return self._same(row, value) if row is not None else None

    def find_request(self, session_id: str, runtime_id: str, request_id: str) -> ChoiceSummary:
        session_id, runtime_id, request_id = (
            _uuid(part) for part in (session_id, runtime_id, request_id)
        )
        with self._access():
            row = self._store.find_request_approval(self._scope, session_id, runtime_id, request_id)
            if row is None:
                raise ApprovalError("not_found")
            return self._summary(row)

    def record(self, value: ChoiceInput, request: dict[str, Any]) -> tuple[ChoiceRecord, bool]:
        value = _input(value)
        identity = _identity(self._scope, value)
        document = _document(value.choice, request)

        def changed(
            current: ApprovalRow | None,
            prior: ApprovalRow | None,
            usage: tuple[int, int],
        ) -> ApprovalRow:
            if current is not None:
                self._same(current, value)
                return current
            if prior is not None:
                self._summary(prior)
                # One recorded choice per original native request, whatever its state.
                raise ApprovalError("already_decided")
            # The quota counts ciphertext: the store holds no plaintext size to count.
            if usage[0] >= MAX_APPROVALS or usage[1] + len(cipher) > MAX_SCOPE_BYTES:
                raise ApprovalError("capacity")
            stamp = _now()
            return {
                **{name: identity[name] for name in _IDENTITY},
                "request_hash": _request_hash(identity),
                "owner": self._owner,
                "state": "recorded",
                "reason": "",
                "body": cipher,
                "created_at": stamp,
                "updated_at": stamp,
            }

        with self._access():
            # DPAPI on a 1 MiB card costs tens of milliseconds, more than the whole busy
            # budget every other writer has. Neither of its inputs comes from the transaction,
            # so it runs before the reservation and leaves only the INSERT inside it.
            cipher = protection.protect(document, context=_context(identity))
            if not isinstance(cipher, bytes) or not 0 < len(cipher) <= MAX_CIPHERTEXT_BYTES:
                raise ApprovalError("protection")
            try:
                row, created = self._store.record_approval(
                    self._scope,
                    value.choice_id,
                    value.session_id,
                    value.native.runtime_id,
                    value.native.request_id,
                    changed,
                )
            except ApprovalError as exc:
                if exc.code != "capacity":
                    raise
                # The quota is full. Make room only from the rows nothing live can reach: a
                # previous launch's terminal rows. One retry, then the honest refusal.
                try:
                    freed = self._store.reclaim_approvals(
                        self._scope,
                        self._owner,
                        max_rows=MAX_APPROVALS,
                        max_bytes=MAX_SCOPE_BYTES,
                        incoming=len(cipher),
                    )
                except StateError as refusal:
                    # The reclaim opens its own BEGIN IMMEDIATE, so a lock collision here raises
                    # `busy`. Letting it out would replace a definite refusal with a code the
                    # browser cannot classify: the card would go `ambiguous` — "possibly
                    # recorded", which is false — and get back the buttons a full quota must take
                    # out of play. The original capacity is the true answer.
                    if not self._reclaim_refused:
                        # Once per launch, same as the reclaimed-count line below: a persistently
                        # contended store must not grow the rotating log by one warning per
                        # answered card either.
                        self._reclaim_refused = True
                        logger.warning("approvals reclaim refused: %s", refusal.code)
                    raise exc from None
                if not freed:
                    raise
                if not self._reclaimed:
                    # Once per launch. From the moment a scope is full every answered card
                    # reclaims, and a warning per turn is what the rotating log must not grow by;
                    # the first line is the record that it began, and the reset result's
                    # count/kept is where totals belong.
                    self._reclaimed = True
                    logger.warning("approvals reclaimed: %d", freed)
                row, created = self._store.record_approval(
                    self._scope,
                    value.choice_id,
                    value.session_id,
                    value.native.runtime_id,
                    value.native.request_id,
                    changed,
                )
            return self._decode(row), created

    def get(self, choice_id: str) -> ChoiceRecord:
        choice_id = _uuid(choice_id, "invalid_id")
        # Decode inside the guard: a failed DPAPI recovery must become "protection", never a
        # native protection exception escaping to the caller.
        with self._access():
            return self._decode(self._existing(choice_id))

    def summary(self, choice_id: str) -> ChoiceSummary:
        choice_id = _uuid(choice_id, "invalid_id")
        with self._access():
            return self._summary(self._existing(choice_id))

    def _existing(self, choice_id: str) -> ApprovalRow:
        row = self._store.get_approval(self._scope, choice_id)
        if row is None:
            raise ApprovalError("not_found")
        return row

    def list(self, *, after: str | None = None, limit: int = 50) -> ChoicePage:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ApprovalError("invalid_page")
        cursor = _uuid(after, "invalid_page") if after is not None else ""
        with self._access():
            rows = self._store.list_approvals(self._scope, cursor, limit + 1)
            summaries = [self._summary(row) for row in rows[:limit]]
            return ChoicePage(summaries, summaries[-1].choice_id if len(rows) > limit else None)

    def mark_delivering(self, choice_id: str) -> ChoiceRecord:
        return self._transition(choice_id, "delivering", "")

    def finish(self, choice_id: str, state: TerminalState, reason: str) -> ChoiceRecord:
        if (
            not isinstance(state, str)
            or state not in {"delivered_locally", "not_delivered", "unknown"}
            or not isinstance(reason, str)
            or reason not in _REASONS_FOR[state]
        ):
            raise ApprovalError("invalid_input")
        return self._transition(choice_id, state, reason)

    def _transition(self, choice_id: str, state: ApprovalState, reason: str) -> ChoiceRecord:
        choice_id = _uuid(choice_id, "invalid_id")

        def changed(row: ApprovalRow | None) -> ApprovalRow:
            if row is None:
                raise ApprovalError("not_found")
            # Everything this transition validates lives in plain columns. Decrypting the body
            # here would hold the writer reservation for tens of milliseconds on a large card,
            # more than the whole busy budget every other writer has; the same row is decoded
            # after the commit anyway. Only the cheap shape of the body stays inside.
            self._summary(row)
            body = row["body"]
            if not isinstance(body, bytes) or not 0 < len(body) <= MAX_CIPHERTEXT_BYTES:
                raise ApprovalError("corrupt")
            if row["owner"] != self._owner:
                raise ApprovalConflict()
            if state not in _NONTERMINAL and (row["state"], row["reason"]) == (state, reason):
                return row  # A repeated identical terminal write is a no-op, not a conflict.
            if row["state"] not in _NONTERMINAL or (
                state == "delivering" and row["state"] != "recorded"
            ):
                raise ApprovalConflict()
            return {**row, "state": state, "reason": reason, "updated_at": _now()}

        with self._access():
            return self._decode(self._store.update_approval(self._scope, choice_id, changed))
