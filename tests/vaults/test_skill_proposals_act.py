"""`SkillProposalService.act`: approve, edit, reject or withdraw one skill proposal at the
revision the user saw, or refuse with one code and change nothing.

The refusal order is the contract, and the ladders below walk it offender by offender. Every case
runs on the pieces of the harness in `test_skill_proposals.py`: a real vault folder, a real
Claude home, the real native inspector, a real store and the real sealing. `act()` and
`refusal()` hash both trees around every call, so an action that wrote a file would show up as a
changed byte or modification time.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from tests.profiles.test_native_assets import _root, _workspace, _write
from tests.vaults.test_proposals import NOON, STAMP, tree
from tests.vaults.test_skill_proposals import (
    BROKEN,
    INSTALLED,
    NAME,
    SCAN_FAILURES,
    TEXT,
    Harness,
    _break_the_scan,
    _raises,
    _Workspaces,
    skill,
    test_the_module_imports_no_writer,  # noqa: F401 (the same static check runs here)
)
from viki_agent import protection
from viki_agent.profiles.native_assets import NativeAssetInspector
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.skill_proposals import (
    ACTIONS,
    MAX_REASON_CHARS,
    MAX_SKILL_BYTES,
    SkillProposalError,
    SkillProposalService,
)
from viki_agent.workspaces import WorkspaceService

LATER = NOON + 60 * 1_000_000_000
LATER_STAMP = "2023-11-14T22:14:20.123Z"
REVISION = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
ELSEWHERE = hashlib.sha256(b"a text the owner never saw").hexdigest()
EDITED_BODY = "EDITED-BODY-3f9b walk the inbox, then every area"
EDITED = skill(body=EDITED_BODY)
# Per action: a request that passes the request checks, and one that fails them.
VALID: dict[str, dict[str, Any]] = {
    "approve": {},
    "edit": {"text": EDITED},
    "reject": {"reason": "Not now"},
    "withdraw": {},
}
MALFORMED: dict[str, dict[str, Any]] = {
    "approve": {"text": EDITED},
    "edit": {},
    "reject": {"reason": " "},
    "withdraw": {"reason": "Not now"},
}


@pytest.fixture
def clock() -> list[int]:
    """The service's wall clock in nanoseconds; a test moves it to tell an action's stamp apart."""
    return [NOON]


@pytest.fixture
def harness(tmp_path: Path, clock: list[int]) -> Harness:
    """The harness of `test_skill_proposals.py`, built the same way, on `clock`."""
    vault, home = tmp_path / "vault", tmp_path / "home"
    (vault / ".claude" / "skills").mkdir(parents=True)
    (home / "skills").mkdir(parents=True)
    spaces = _Workspaces(_workspace(vault))
    inspector = NativeAssetInspector(spaces, claude_home=_root(home))
    store = AppState(tmp_path / "state.json", Prefs()).store
    service = SkillProposalService(
        cast(WorkspaceService, spaces), store, inspector, wall=lambda: clock[0]
    )
    return Harness(service, store, spaces, inspector, vault, home)


def act(harness: Harness, item: dict[str, Any], action: str, **kwargs: Any) -> dict[str, Any]:
    """One action at the item's own revision, with the vault and the Claude home unchanged."""
    result = harness.unchanged(
        functools.partial(harness.service.act, item["id"], action, item["revision"], **kwargs)
    )
    assert result is not None
    return result


def refusal(harness: Harness, *args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    """The refusal `act` raises, with both trees and every stored row unchanged."""
    rows = harness.store.list_skill_proposals(harness.wid)
    with pytest.raises(SkillProposalError) as raised:
        harness.unchanged(functools.partial(harness.service.act, *args, **kwargs))
    assert harness.store.list_skill_proposals(harness.wid) == rows
    return raised.value.code, raised.value.extra


def into(harness: Harness, item: dict[str, Any], state: str) -> None:
    """Move a proposal made over `INSTALLED` into one derived state."""
    if state == "handed":
        harness.mark(item, approved_at=STAMP)
    elif state == "rejected":
        harness.mark(item, rejected_at=STAMP, reason="Not now")
    elif state == "stale":
        harness.install(skill(body="Steps written elsewhere"))
    elif state == "source_missing":
        (harness.vault / ".claude" / "skills" / NAME / "SKILL.md").unlink()
    elif state == "unknown":
        harness.spaces.root_available = False
    elif state == "applied":
        harness.install(TEXT)
    assert harness.states()[NAME][0] == state


# ---- the request and the refusals every action shares ----------------------------------------


@pytest.mark.parametrize(
    ("action", "revision", "kwargs"),
    [
        ("Approve", REVISION, {}),
        ("delete", REVISION, {}),
        (None, REVISION, {}),
        (["approve"], REVISION, {}),
        ("approve", REVISION.upper(), {}),
        ("approve", REVISION[:-1], {}),
        ("approve", REVISION + "0", {}),
        ("approve", REVISION + "\n", {}),
        ("approve", REVISION.encode(), {}),
        ("approve", None, {}),
        ("approve", REVISION, {"text": EDITED}),
        ("reject", REVISION, {"text": EDITED}),
        ("withdraw", REVISION, {"text": ""}),
        ("edit", REVISION, {}),
        ("edit", REVISION, {"text": EDITED.encode()}),
        ("approve", REVISION, {"reason": "Not now"}),
        ("edit", REVISION, {"text": EDITED, "reason": "Not now"}),
        ("withdraw", REVISION, {"reason": "Not now"}),
        ("reject", REVISION, {"reason": ""}),
        ("reject", REVISION, {"reason": "  \u3000 "}),
        ("reject", REVISION, {"reason": "x" * (MAX_REASON_CHARS + 1)}),
        ("reject", REVISION, {"reason": "Not\nnow"}),
        ("reject", REVISION, {"reason": "Not now\n"}),
        ("reject", REVISION, {"reason": "Not\r\nnow"}),
        ("reject", REVISION, {"reason": "Not\tnow"}),
        ("reject", REVISION, {"reason": "Not\x00now"}),
        ("reject", REVISION, {"reason": "Not\x1b[2Jnow"}),
        ("reject", REVISION, {"reason": "Not\x7fnow"}),
        ("reject", REVISION, {"reason": "Not\x85now"}),
        ("reject", REVISION, {"reason": "Not\u2028now"}),
        ("reject", REVISION, {"reason": "Not\ud800now"}),
        ("reject", REVISION, {"reason": 7}),
        ("reject", REVISION, {"reason": b"Not now"}),
    ],
    ids=[
        "action_case",
        "action_unknown",
        "action_none",
        "action_list",
        "revision_uppercase",
        "revision_short",
        "revision_long",
        "revision_newline",
        "revision_bytes",
        "revision_none",
        "text_with_approve",
        "text_with_reject",
        "empty_text_with_withdraw",
        "edit_without_text",
        "edit_text_bytes",
        "reason_with_approve",
        "reason_with_edit",
        "reason_with_withdraw",
        "reason_empty",
        "reason_blank",
        "reason_long",
        "reason_newline",
        "reason_trailing_newline",
        "reason_crlf",
        "reason_tab",
        "reason_nul",
        "reason_escape",
        "reason_delete",
        "reason_next_line",
        "reason_line_separator",
        "reason_surrogate",
        "reason_number",
        "reason_bytes",
    ],
)
def test_act_refuses_a_malformed_request_first(
    harness: Harness, action: Any, revision: Any, kwargs: dict[str, Any]
) -> None:
    """Before the proposal is looked up: the id here names no proposal."""
    harness.create()

    assert refusal(harness, str(uuid.uuid4()), action, revision, **kwargs) == (
        "invalid_request",
        {},
    )


@pytest.mark.parametrize("action", ACTIONS)
def test_the_shared_refusals_come_in_order(harness: Harness, action: str) -> None:
    """A malformed request, then an unknown id, a vault that is not open, a moved revision, and
    only then the action's own checks: here, a file that already holds the proposal."""
    item = harness.create()
    harness.install(TEXT)
    unknown = str(uuid.uuid4())
    harness.spaces.active = dataclasses.replace(
        harness.spaces.workspace, workspace_id=str(uuid.uuid4())
    )

    assert refusal(harness, unknown, action, ELSEWHERE, **MALFORMED[action]) == (
        "invalid_request",
        {},
    )
    assert refusal(harness, unknown, action, ELSEWHERE, **VALID[action]) == (
        "unknown_proposal",
        {},
    )
    assert refusal(harness, item["id"], action, ELSEWHERE, **VALID[action]) == ("not_active", {})
    harness.spaces.active = harness.spaces.workspace
    assert refusal(harness, item["id"], action, ELSEWHERE, **VALID[action]) == (
        "stale_revision",
        {"current_revision": item["revision"]},
    )
    if action != "withdraw":
        assert refusal(harness, item["id"], action, item["revision"], **VALID[action]) == (
            "already_applied",
            {},
        )
        (harness.vault / ".claude" / "skills" / NAME / "SKILL.md").unlink()
    harness.service.act(item["id"], action, item["revision"], **VALID[action])


@pytest.mark.parametrize("proposal_id", [str(uuid.UUID(int=1)), None, 7])
def test_act_refuses_an_unknown_proposal(harness: Harness, proposal_id: Any) -> None:
    harness.create()

    assert refusal(harness, proposal_id, "approve", REVISION) == ("unknown_proposal", {})


@pytest.mark.parametrize("change", ["other_vault", "chat_only"])
@pytest.mark.parametrize("action", ACTIONS)
def test_act_refuses_while_its_vault_is_not_open(
    harness: Harness, action: str, change: str
) -> None:
    item = harness.create()
    if change == "chat_only":
        harness.spaces.chat_only = True
    else:
        harness.spaces.active = dataclasses.replace(
            harness.spaces.workspace, workspace_id=str(uuid.uuid4())
        )

    assert refusal(harness, item["id"], action, item["revision"], **VALID[action]) == (
        "not_active",
        {},
    )


# ---- approve -------------------------------------------------------------------------------


def test_approve_records_the_approval_at_the_revision_the_owner_saw(
    harness: Harness, clock: list[int]
) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    clock[0] = LATER

    approved = act(harness, item, "approve")

    assert approved == {
        **item,
        "state": "handed",
        "attention": False,
        "approved_at": LATER_STAMP,
        "updated_at": LATER_STAMP,
    }
    assert harness.service.inbox_items() == [approved]
    # A handed proposal is still at the revision the user saw, so it can be approved again.
    assert act(harness, approved, "approve") == approved


@pytest.mark.parametrize("written", [TEXT, TEXT.replace("\n", "\r\n")], ids=["lf", "crlf"])
def test_approve_hands_off_and_the_file_a_chat_writes_applies_it(
    harness: Harness, written: str
) -> None:
    """The approval writes no file. Once a chat writes the text, the next read says applied and
    only a withdrawal is left."""
    harness.install(INSTALLED)
    item = harness.create()
    assert act(harness, item, "approve")["state"] == "handed"

    harness.install(written)

    assert harness.states() == {NAME: ("applied", False)}
    for action in ("approve", "edit", "reject"):
        assert refusal(harness, item["id"], action, item["revision"], **VALID[action]) == (
            "already_applied",
            {},
        )
    assert harness.row(item)["approved_at"] == STAMP
    assert harness.service.act(item["id"], "withdraw", item["revision"]) is None


@pytest.mark.parametrize("state", ["rejected", "stale", "source_missing", "unknown"])
def test_approve_refuses_a_proposal_neither_open_nor_handed(harness: Harness, state: str) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    into(harness, item, state)

    assert refusal(harness, item["id"], "approve", item["revision"]) == (
        "not_approvable",
        {"state": state},
    )


def test_approve_is_stale_after_a_rejection_made_at_its_revision_after_the_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection leaves the revision where it was, so the approval's compare-and-set still
    matches the row; the store's CHECK of one decision per row refuses the write instead, and
    the rejection stands with its reason."""
    item = harness.create()
    read = harness.store.read_skill_proposal

    def read_then_reject(proposal_id: str) -> dict[str, Any] | None:
        row = read(proposal_id)
        monkeypatch.setattr(harness.store, "read_skill_proposal", read)
        harness.service.act(item["id"], "reject", item["revision"], reason="Not now")
        return row

    monkeypatch.setattr(harness.store, "read_skill_proposal", read_then_reject)
    before = (tree(harness.vault), tree(harness.home))

    with pytest.raises(SkillProposalError) as raised:
        harness.service.act(item["id"], "approve", item["revision"])

    assert (raised.value.code, raised.value.extra) == (
        "stale_revision",
        {"current_revision": item["revision"]},
    )
    row = harness.row(item)
    assert (row["approved_at"], row["rejected_at"], row["reason"]) == (None, STAMP, "Not now")
    assert harness.states() == {NAME: ("rejected", False)}
    assert (tree(harness.vault), tree(harness.home)) == before


# ---- reject --------------------------------------------------------------------------------


def test_reject_keeps_the_record_with_its_reason_stripped(
    harness: Harness, clock: list[int]
) -> None:
    item = harness.create()
    act(harness, item, "approve")
    clock[0] = LATER

    rejected = act(harness, item, "reject", reason="  " + "x" * MAX_REASON_CHARS + " ")

    assert rejected == {
        **item,
        "state": "rejected",
        "attention": False,
        "rejected_at": LATER_STAMP,
        "reason": "x" * MAX_REASON_CHARS,
        "updated_at": LATER_STAMP,
    }
    assert harness.service.inbox_items() == [rejected]
    # A second rejection without a reason drops the first one's.
    again = act(harness, item, "reject")
    assert (again["state"], again["reason"]) == ("rejected", None)
    assert harness.row(item)["reason"] is None


def test_reject_bounds_its_reason_in_utf16_code_units(harness: Harness) -> None:
    """The browser measures a reason in UTF-16 code units, where an emoji outside the BMP takes
    two, and refuses the whole decisions answer over one reason past the limit."""
    item = harness.create()
    emoji = "\U0001f600"

    for reason in (emoji * 150, emoji * (MAX_REASON_CHARS // 2 + 1)):
        assert refusal(harness, item["id"], "reject", item["revision"], reason=reason) == (
            "invalid_request",
            {},
        )
    for reason in ("\u3042" * MAX_REASON_CHARS, emoji * (MAX_REASON_CHARS // 2)):
        assert act(harness, item, "reject", reason=reason)["reason"] == reason


@pytest.mark.parametrize("state", ["open", "handed", "stale", "source_missing", "unknown"])
def test_reject_takes_any_state_but_applied(harness: Harness, state: str) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    into(harness, item, state)

    rejected = act(harness, item, "reject", reason="Not now")

    assert (rejected["approved_at"], rejected["rejected_at"], rejected["reason"]) == (
        None,
        STAMP,
        "Not now",
    )
    # A scan that fails still says nothing about the file, so the state stays unknown.
    assert rejected["state"] == ("unknown" if state == "unknown" else "rejected")


# ---- edit ----------------------------------------------------------------------------------


def test_edit_rebases_onto_a_changed_skill(harness: Harness, clock: list[int]) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    act(harness, item, "approve")
    current = harness.install(skill(body="Steps written elsewhere"))
    assert harness.states() == {NAME: ("stale", True)}
    clock[0] = LATER

    edited = act(harness, item, "edit", text=EDITED)

    data = EDITED.encode("utf-8")
    assert edited == {
        **item,
        "revision": hashlib.sha256(data).hexdigest(),
        "state": "open",
        "attention": True,
        "base_revision": current,
        "text_bytes": len(data),
        "updated_at": LATER_STAMP,
    }
    assert harness.service.inbox_items() == [edited]
    detail = harness.service.detail(item["id"])
    assert detail["text"] == EDITED
    diff = cast(str, detail["diff"]).splitlines()
    assert "-\tSteps written elsewhere" in diff and f"+\t{EDITED_BODY}" in diff
    # The revision the user saw before the edit has moved on.
    assert refusal(harness, item["id"], "approve", item["revision"]) == (
        "stale_revision",
        {"current_revision": edited["revision"]},
    )


def test_edit_clears_a_rejection_and_its_reason(harness: Harness) -> None:
    item = harness.create()
    act(harness, item, "reject", reason="Not now")

    edited = act(harness, item, "edit", text=EDITED)

    assert (edited["state"], edited["approved_at"], edited["rejected_at"], edited["reason"]) == (
        "open",
        None,
        None,
        None,
    )
    assert (edited["base_revision"], edited["title"]) == (None, f"New skill: {NAME}")


def test_edit_of_a_removed_skill_proposes_it_as_new(harness: Harness) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    into(harness, item, "source_missing")

    edited = act(harness, item, "edit", text=EDITED)

    assert (edited["state"], edited["base_revision"], edited["title"]) == (
        "open",
        None,
        f"New skill: {NAME}",
    )


def test_edit_refusals_come_in_order(harness: Harness) -> None:
    """Applied first; then create's checks on the text, the name, the text the proposal holds;
    then the rebase: a partial scan without the target, or a file that holds the edit already,
    with LF or CRLF line ends."""
    item = harness.create()
    harness.install(TEXT)
    harness.install(BROKEN, name="broken")
    edit = functools.partial(refusal, harness, item["id"], "edit", item["revision"])
    renamed = skill("other-skill", body=EDITED_BODY)

    assert edit(text=renamed + "\r") == ("already_applied", {})
    (harness.vault / ".claude" / "skills" / NAME / "SKILL.md").unlink()
    assert edit(text=renamed + "\r") == ("invalid_skill", {"reason": "carriage_return"})
    assert edit(text=renamed) == ("name_changed", {})
    assert edit(text=TEXT) == ("no_change", {})
    assert edit(text=EDITED) == ("catalog_partial", {})
    harness.install(EDITED)
    assert edit(text=EDITED) == ("no_change", {})
    harness.install(EDITED.replace("\n", "\r\n"))
    assert edit(text=EDITED) == ("no_change", {})
    # The same partial scan holding the target still says what the edit changes.
    base = harness.install(INSTALLED)
    assert act(harness, item, "edit", text=EDITED)["base_revision"] == base


@pytest.mark.parametrize(
    ("text", "code", "extra"),
    [
        (EDITED.replace("\n", "\r\n"), "invalid_skill", {"reason": "carriage_return"}),
        (EDITED + "\x00", "invalid_skill", {"reason": "control_character"}),
        (EDITED + "\x85", "invalid_skill", {"reason": "control_character"}),
        (EDITED + "\ud800", "invalid_skill", {"reason": "invalid_encoding"}),
        (EDITED + "x" * MAX_SKILL_BYTES, "too_large", {"limit": MAX_SKILL_BYTES}),
        ("No frontmatter here\n", "invalid_skill", {"reason": "missing_frontmatter"}),
        (BROKEN, "invalid_skill", {"reason": "malformed_metadata"}),
        (skill(description="a" * 1537), "invalid_skill", {"reason": "long_description"}),
    ],
    ids=[
        "crlf",
        "nul",
        "next_line",
        "surrogate",
        "too_large",
        "missing_frontmatter",
        "malformed_metadata",
        "long_description",
    ],
)
def test_edit_applies_the_checks_create_applies(
    harness: Harness, text: str, code: str, extra: dict[str, Any]
) -> None:
    item = harness.create()

    assert refusal(harness, item["id"], "edit", item["revision"], text=text) == (code, extra)


@SCAN_FAILURES
def test_edit_refuses_when_the_skills_cannot_be_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, error: Exception | None
) -> None:
    item = harness.create()
    _break_the_scan(harness, monkeypatch, error)

    assert refusal(harness, item["id"], "edit", item["revision"], text=EDITED) == (
        "unavailable",
        {},
    )


def test_edit_refuses_when_sealing_is_unavailable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = harness.create()
    monkeypatch.setattr(protection, "protect", _raises(protection.ProtectionError()))

    assert refusal(harness, item["id"], "edit", item["revision"], text=EDITED) == (
        "unavailable",
        {},
    )


# ---- withdraw ------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["open", "handed", "rejected", "applied", "unknown"])
def test_withdraw_removes_the_record_in_any_state(harness: Harness, state: str) -> None:
    harness.install(INSTALLED)
    item = harness.create()
    into(harness, item, state)

    withdrawn = harness.unchanged(
        functools.partial(harness.service.act, item["id"], "withdraw", item["revision"])
    )

    assert withdrawn is None
    assert harness.store.read_skill_proposal(item["id"]) is None
    assert harness.service.inbox_items() == []
    assert refusal(harness, item["id"], "withdraw", item["revision"]) == ("unknown_proposal", {})


# ---- a row that moves on between the read and the write --------------------------------------


@pytest.mark.parametrize("how", ["edited", "withdrawn"])
@pytest.mark.parametrize("action", ACTIONS)
def test_a_row_moved_on_between_the_read_and_the_write_is_refused_as_stale(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, action: str, how: str
) -> None:
    """Another window edits or withdraws the proposal after `act` reads it and before it writes:
    the compare-and-set at the revision the user saw matches no row, so nothing changes."""
    item = harness.create()
    read = harness.store.read_skill_proposal
    moved: list[dict[str, Any] | None] = []

    def read_then_move(proposal_id: str) -> dict[str, Any] | None:
        row = read(proposal_id)
        monkeypatch.setattr(harness.store, "read_skill_proposal", read)
        if how == "edited":
            harness.service.act(
                item["id"], "edit", item["revision"], text=skill(body="Steps edited elsewhere")
            )
        else:
            harness.service.act(item["id"], "withdraw", item["revision"])
        moved.append(read(proposal_id))
        return row

    monkeypatch.setattr(harness.store, "read_skill_proposal", read_then_move)
    before = (tree(harness.vault), tree(harness.home))

    with pytest.raises(SkillProposalError) as raised:
        harness.service.act(item["id"], action, item["revision"], **VALID[action])

    (now,) = moved
    current = now["proposed_sha256"] if now is not None else None
    assert (raised.value.code, raised.value.extra) == (
        "stale_revision",
        {"current_revision": current},
    )
    assert (current is None) == (how == "withdrawn")
    assert harness.store.read_skill_proposal(item["id"]) == now
    assert (tree(harness.vault), tree(harness.home)) == before


# ---- what no action does -------------------------------------------------------------------


def test_the_trees_are_byte_identical_after_every_action(harness: Harness) -> None:
    """No action writes the vault or the Claude home: an approval hands the change to a chat."""
    harness.install(INSTALLED)
    _write(harness.home / "skills" / NAME / "SKILL.md", skill(body="Mine"))
    change = harness.create()
    fresh = harness.create(skill("fresh-skill"))
    # A digest that matches neither hash: the CRLF check reads this file.
    harness.install(
        skill("fresh-skill", body="Made elsewhere").replace("\n", "\r\n"), name="fresh-skill"
    )
    before = (tree(harness.vault), tree(harness.home))

    approved = act(harness, change, "approve")
    rejected = act(harness, approved, "reject", reason="Not now")
    edited = act(harness, rejected, "edit", text=EDITED)
    act(harness, edited, "approve")
    rebased = act(harness, fresh, "edit", text=skill("fresh-skill", body="Rebased steps"))
    assert harness.service.act(rebased["id"], "withdraw", rebased["revision"]) is None
    for args, kwargs in (
        ((change["id"], "approve", change["revision"]), {}),
        ((edited["id"], "edit", edited["revision"]), {"text": EDITED}),
        ((edited["id"], "approve", "not-a-revision"), {}),
        ((rebased["id"], "withdraw", rebased["revision"]), {}),
    ):
        refusal(harness, *args, **kwargs)

    assert (tree(harness.vault), tree(harness.home)) == before


def test_the_text_is_absent_from_answers_errors_and_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    item = harness.create()
    edited = act(harness, item, "edit", text=EDITED)
    errors: list[SkillProposalError] = []
    for revision, text in (
        (item["revision"], EDITED),
        (edited["revision"], EDITED),
        (edited["revision"], EDITED + "\r"),
        (edited["revision"], EDITED.replace(f"name: {NAME}", "name: other-skill")),
        (edited["revision"], EDITED + "x" * MAX_SKILL_BYTES),
    ):
        with pytest.raises(SkillProposalError) as raised:
            harness.service.act(item["id"], "edit", revision, text=text)
        errors.append(raised.value)

    assert [error.code for error in errors] == [
        "stale_revision",
        "no_change",
        "invalid_skill",
        "name_changed",
        "too_large",
    ]
    shown = repr((edited, harness.service.inbox_items())) + repr(
        [(error.code, error.extra, error.args, error.__cause__) for error in errors]
    )
    assert EDITED_BODY not in shown
    assert EDITED_BODY not in caplog.text
    assert EDITED_BODY in cast(str, harness.service.detail(item["id"])["text"])
