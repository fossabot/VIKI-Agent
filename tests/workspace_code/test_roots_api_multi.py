"""Additive multi-folder root requests and their bounded, path-free wire projection."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.workspace_code.test_roots_api import (
    ALPHA,
    AUTH,
    BEARER,
    ORIGIN,
    POSTS,
    ROOTS,
    SESSION,
    WS,
    WT,
    Spy,
    _no_store,
    _refused,
    _rig,
    _spied,
    post,
)
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code.roots_api import MAX_BODY_BYTES, MAX_ROOT_ROWS

_EXTRAS = [str(uuid.UUID(int=index)) for index in range(10, 17)]


def test_multi_root_roundtrip_and_final_wire_budget(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    extras = []
    for index in range(7):
        folder = tmp_path / f"extra-{index}"
        folder.mkdir()
        extras.append(trusted(rig.workspaces, folder))
    extras.reverse()
    fields = rig.root_fields(additional_workspace_ids=extras)
    with TestClient(rig.app()) as client:
        created = post(client, ROOTS, fields)
        again = post(client, ROOTS, fields)
        listed = client.get(ROOTS, headers=AUTH)
        assert _no_store(created, 201) == {"root": fields}
        assert _no_store(again, 200) == {"root": fields}
        assert _no_store(listed, 200) == {"roots": [fields], "truncated": False}
        assert created.content.isascii() and listed.content.isascii()

        row = rig.store.get_session_root(SESSION)
        assert row is not None
        assert row["display_root"] == rig.workspace().root.display
        assert [extra["workspace_id"] for extra in row["additional_roots"]] == extras
        assert all(
            set(extra) == {"workspace_id", "display_root", "physical_id"}
            for extra in row["additional_roots"]
        )
        encoded = json.dumps(
            row["additional_roots"], ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        assert MAX_ROOT_ROWS == 4096
        with rig.store._connection(write=True) as connection:
            connection.executemany(
                "INSERT INTO session_roots "
                "(session_id,workspace_id,worktree_id,display_root,created_at,additional_roots) "
                "VALUES (?,?,?,?,?,?)",
                (
                    (
                        str(uuid.UUID(int=index)),
                        rig.workspace_id,
                        None,
                        row["display_root"],
                        row["created_at"],
                        encoded,
                    )
                    for index in range(MAX_ROOT_ROWS - 1)
                ),
            )
        maximum = client.get(ROOTS, headers=AUTH)
    body = _no_store(maximum, 200)
    assert body["truncated"] is False and len(body["roots"]) == MAX_ROOT_ROWS
    assert all(
        set(root) == {"session_id", "workspace_id", "worktree_id", "additional_workspace_ids"}
        and root["workspace_id"] == rig.workspace_id
        and root["worktree_id"] is None
        and root["additional_workspace_ids"] == extras
        for root in body["roots"]
    )
    assert maximum.content.isascii()
    assert len(maximum.content) == 1_757_213
    assert len(maximum.content) <= 2 * 1024 * 1024


@pytest.mark.parametrize("with_empty", [False, True])
@pytest.mark.parametrize("worktree_id", [None, WT])
def test_legacy_root_requests_keep_three_prepare_arguments_and_exact_wire(
    with_empty: bool, worktree_id: str | None
) -> None:
    fields = {"session_id": SESSION, "workspace_id": WS, "worktree_id": worktree_id}
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        response = post(
            client,
            ROOTS,
            {**fields, **({"additional_workspace_ids": []} if with_empty else {})},
        )
    assert _no_store(response, 201) == {"root": fields}
    assert spy.calls == [("prepare", (SESSION, WS, worktree_id))]


@pytest.mark.parametrize("count", [1, 7])
def test_nonempty_extras_reach_prepare_as_an_ordered_tuple(count: int) -> None:
    extras = _EXTRAS[:count]
    fields = {
        "session_id": SESSION,
        "workspace_id": WS,
        "worktree_id": None,
        "additional_workspace_ids": extras,
    }
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        response = post(client, ROOTS, fields)
    assert _no_store(response, 201) == {"root": fields}
    assert spy.calls == [("prepare", (SESSION, WS, None, tuple(extras)))]


@pytest.mark.parametrize(
    "changes",
    [
        {"additional_workspace_ids": None},
        {"additional_workspace_ids": ALPHA},
        {"additional_workspace_ids": {"workspace_id": ALPHA}},
        {"additional_workspace_ids": [1]},
        {"additional_workspace_ids": [True]},
        {"additional_workspace_ids": [None]},
        {"additional_workspace_ids": [[]]},
        {"additional_workspace_ids": [{"workspace_id": ALPHA}]},
        {"additional_workspace_ids": ["<folder>/other"]},
        {"additional_workspace_ids": [ALPHA.upper()]},
        {"additional_workspace_ids": [f"{{{ALPHA}}}"]},
        {"additional_workspace_ids": [ALPHA.replace("-", "")]},
        {"additional_workspace_ids": [f"urn:uuid:{ALPHA}"]},
        {"additional_workspace_ids": [ALPHA, ALPHA]},
        {"additional_workspace_ids": [WS]},
        {"additional_workspace_ids": [*_EXTRAS, ALPHA]},
        {"additional_workspace_ids": [ALPHA], "worktree_id": WT},
        {"additional_workspace_ids": [ALPHA], "additional_roots": []},
        {"additional_workspace_ids": [ALPHA], "display_root": "<folder>"},
    ],
)
def test_invalid_additional_selection_is_invalid_body_before_service(
    changes: dict[str, Any],
) -> None:
    fields = {"session_id": SESSION, "workspace_id": WS, "worktree_id": None, **changes}
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, ROOTS, fields), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("missing", ["session_id", "workspace_id", "worktree_id"])
def test_optional_selection_does_not_replace_a_required_key(missing: str) -> None:
    fields = {
        "session_id": SESSION,
        "workspace_id": WS,
        "worktree_id": None,
        "additional_workspace_ids": _EXTRAS,
    }
    del fields[missing]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, ROOTS, fields), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize("route", ["create", "remove"])
def test_additional_selection_is_not_admitted_by_worktree_bodies(route: str) -> None:
    url, fields = POSTS[route]
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        _refused(post(client, url, {**fields, "additional_workspace_ids": []}), 400, "invalid_body")
    assert spy.calls == []


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Origin": ORIGIN}, 401, "unauthorized"),
        ({"Origin": ORIGIN, "Authorization": "Bearer other"}, 401, "unauthorized"),
        (BEARER, 403, "forbidden_origin"),
        ({**BEARER, "Origin": "https://evil.invalid"}, 403, "forbidden_origin"),
    ],
)
def test_multi_root_authentication_precedes_body_and_service(
    headers: dict[str, str], status: int, code: str
) -> None:
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        for extras in (_EXTRAS, None):
            fields = {
                "session_id": SESSION,
                "workspace_id": WS,
                "worktree_id": None,
                "additional_workspace_ids": extras,
            }
            response = client.post(
                ROOTS,
                content=json.dumps(fields),
                headers={**headers, "Content-Type": "application/json"},
            )
            _refused(response, status, code)
    assert spy.calls == []


def test_multi_root_intake_keeps_duplicate_key_and_four_kib_limits() -> None:
    fields = {
        "session_id": SESSION,
        "workspace_id": WS,
        "worktree_id": None,
        "additional_workspace_ids": _EXTRAS,
    }
    raw = json.dumps(fields)
    spy = Spy()
    with TestClient(_spied(spy)) as client:
        duplicate = raw[:-1] + ',"additional_workspace_ids":[]}'
        for invalid in (duplicate, "{", "[]"):
            _refused(post(client, ROOTS, content=invalid), 400, "invalid_body")
        assert MAX_BODY_BYTES == 4096
        _refused(post(client, ROOTS, content=raw.ljust(MAX_BODY_BYTES + 1)), 413, "too_large")
        assert spy.calls == []
        assert post(client, ROOTS, content=raw.ljust(MAX_BODY_BYTES)).status_code == 201
    assert spy.calls == [("prepare", (SESSION, WS, None, tuple(_EXTRAS)))]
