"""saved_checks.py: the commit-checks items, their keys and what the model is sent."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.decision_loop.fakes import settings_values
from viki_agent.decision_loop import contracts, saved_checks, words
from viki_agent.decision_loop.contracts import (
    CheckFolder,
    CheckLook,
    DecisionSettings,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.detectors import Observation, detect
from viki_agent.decision_loop.state import History, Now, Owner, build_state

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SCOPE = "scope-x"
HEAD = "a" * 40
CHECK_ID = "3f2c9a1e-5447-4a8d-9d58-b68385409b99"
FOLDER_ID = "7d1e4b20-5447-4a8d-9d58-b68385409b99"


def _folder(
    workspace_id: str = FOLDER_ID,
    phase: str = "waiting",
    head: str = HEAD,
    pending: tuple[str, ...] = (CHECK_ID,),
) -> CheckFolder:
    return CheckFolder(workspace_id, "garden-app", head, phase, pending)


def _look(*folders: CheckFolder) -> CheckLook:
    return CheckLook(folders)


def test_a_waiting_folder_becomes_a_suggestion_with_run_work() -> None:
    (item,) = saved_checks.candidates(_look(_folder()), SCOPE)
    assert item.type == "commit_checks"
    assert item.item_key == item_key("commit_checks", FOLDER_ID, HEAD)
    assert item.title == words.type_words("commit_checks")
    assert dict(item.facts) == {
        "workspace_id": SCOPE,
        "folder_id": FOLDER_ID,
        "folder": "garden-app",
        "head": HEAD,
        "phase": "waiting",
        "pending": (CHECK_ID,),
    }
    assert dict(item.words) == {}
    assert item.evidence == ()
    assert item.related_chat is None
    assert item.related_ref is None
    assert item.deadline is None
    assert item.shown_elsewhere == ()
    assert dict(item.rules_evidence) == {}
    assert item.origin_session is None
    assert item.work_allowed == ("run",)


@pytest.mark.parametrize("phase", [p for p in contracts.CHECK_PHASES if p != "waiting"])
def test_running_and_ended_folders_carry_no_work(phase: str) -> None:
    (item,) = saved_checks.candidates(_look(_folder(phase=phase, pending=())), SCOPE)
    assert item.facts["phase"] == phase
    assert item.work_allowed == ()


def test_keys_name_the_folder_and_commit_not_the_scope() -> None:
    (item,) = saved_checks.candidates(_look(_folder()), SCOPE)
    assert saved_checks.candidates(_look(_folder()), "other")[0].item_key == item.item_key
    # The same commit keeps its key through the phases.
    assert (
        saved_checks.candidates(_look(_folder(phase="passed")), SCOPE)[0].item_key == item.item_key
    )
    assert (
        saved_checks.candidates(_look(_folder(head="b" * 40)), SCOPE)[0].item_key != item.item_key
    )
    assert saved_checks.candidates(_look(_folder("other-id")), SCOPE)[0].item_key != item.item_key


def test_at_most_three_folders_are_listed() -> None:
    assert contracts.MAX_CHECK_FOLDERS == 3
    look = _look(*(_folder(f"folder-{n}") for n in (4, 2, 5, 1, 3)))
    found = saved_checks.candidates(look, SCOPE)
    assert [c.facts["folder_id"] for c in found] == ["folder-1", "folder-2", "folder-3"]


def test_an_unread_check_look_lists_nothing() -> None:
    assert saved_checks.candidates(CheckLook(None, ("checks_unknown",)), SCOPE) == []
    assert saved_checks.candidates(CheckLook(()), SCOPE) == []


def test_a_saved_check_item_sends_only_its_kind_to_the_model() -> None:
    (item,) = saved_checks.candidates(_look(_folder()), SCOPE)
    now = Now(None, False, False, NOW, 0)
    sent = canonical_json(build_state(item, Owner(), now, History(), DecisionSettings()).state)
    assert "saved checks on a code folder's new commit" in sent
    # The type words hold "folder" and "commit", so the values and the fact keys are checked.
    for private in (
        "garden-app",
        FOLDER_ID,
        HEAD,
        CHECK_ID,
        "waiting",
        SCOPE,
        "workspace_id",
        "folder_id",
        "pending",
    ):
        assert private not in sent, private


def _detected(obs: Observation, **overrides: Any) -> list[contracts.Candidate]:
    settings = contracts.parse_settings(settings_values(**overrides))
    return [c for c in detect(obs, settings, (), NOW) if c.type == "commit_checks"]


def test_saved_checks_need_the_grant_and_an_ordinary_vault() -> None:
    obs = replace(
        Observation(SCOPE, "vault", None, (), (), None, ()),
        checks=_look(_folder()),
        policy="ordinary",
    )
    for grant in ("ask", "allowed"):
        (item,) = _detected(obs, **{"decision.grant.check": grant})
        assert item.work_allowed == ("run",), grant
    assert _detected(obs, **{"decision.grant.check": "off"}) == []
    assert _detected(replace(obs, checks=None)) == []  # the cycle did not read the folders
    # `detect` keeps the item under every policy; the cut takes only what the policy forbids.
    assert _detected(replace(obs, policy="protected"))[0].work_allowed == ()
    assert _detected(replace(obs, policy="existing_readonly"))[0].work_allowed == ()
    assert _detected(replace(obs, policy=None))[0].work_allowed == ()
