from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent.state import AppState, Prefs
from viki_agent.vaults.vault_bookmarks import MAX_BOOKMARKS, BookmarkError, BookmarkService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

WID = str(uuid.uuid4())


class _Workspaces:
    def get(self, workspace_id: str) -> object:
        if workspace_id != WID:
            raise WorkspaceError("unknown_workspace")
        return object()


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    return AppState(tmp_path / "state.json", Prefs())


@pytest.fixture
def service(state: AppState) -> BookmarkService:
    return BookmarkService(state, cast(WorkspaceService, _Workspaces()))


def _code(call: Any, *args: str) -> str:
    with pytest.raises(BookmarkError) as raised:
        call(*args)
    return raised.value.code


def test_add_list_remove_in_casefold_order(service: BookmarkService) -> None:
    for ref in ("b.md", "A/c.md", "a/b.md"):
        service.add(WID, ref)
    assert [mark.ref for mark in service.list(WID)] == ["a/b.md", "A/c.md", "b.md"]
    assert service.remove(WID, "b.md") is True
    assert [mark.ref for mark in service.list(WID)] == ["a/b.md", "A/c.md"]


def test_a_double_add_keeps_the_stored_stamp(service: BookmarkService) -> None:
    first = service.add(WID, "note.md")
    assert service.add(WID, "note.md") == first
    assert len(service.list(WID)) == 1


def test_remove_of_an_absent_ref_is_false(service: BookmarkService) -> None:
    assert service.remove(WID, "absent.md") is False


def test_unknown_workspace(service: BookmarkService) -> None:
    other = str(uuid.uuid4())
    assert _code(service.list, other) == "unknown_workspace"
    assert _code(service.add, other, "note.md") == "unknown_workspace"
    assert _code(service.remove, other, "note.md") == "unknown_workspace"


@pytest.mark.parametrize("ref", ["", "a//b.md", "../x.md", "/x.md", "a\\b.md", "c:x.md"])
def test_invalid_ref(service: BookmarkService, ref: str) -> None:
    assert _code(service.add, WID, ref) == "invalid_ref"
    assert _code(service.remove, WID, ref) == "invalid_ref"


def test_a_non_string_ref_is_refused_not_converted(service: BookmarkService) -> None:
    assert _code(service.add, WID, cast(Any, 1)) == "invalid_ref"
    assert service.list(WID) == ()


@pytest.mark.parametrize(
    "ref", [".git/x.md", "a/b/.Claude/c.md", "a/.env.prod", "a/Credentials.json"]
)
def test_an_excluded_name_at_depth_is_private(service: BookmarkService, ref: str) -> None:
    assert _code(service.add, WID, ref) == "private_path"
    assert service.list(WID) == ()


def test_the_cap_refuses_the_201st(service: BookmarkService) -> None:
    for index in range(MAX_BOOKMARKS):
        service.add(WID, f"n{index}.md")
    assert _code(service.add, WID, "one-more.md") == "bookmarks_full"
    assert len(service.list(WID)) == MAX_BOOKMARKS
    assert service.add(WID, "n0.md").ref == "n0.md"  # An existing ref is not a new row.
    assert service.remove(WID, "n0.md") is True
    service.add(WID, "one-more.md")


def test_the_cap_is_counted_inside_the_insert_transaction(
    state: AppState, service: BookmarkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = state.store._connection
    seen: list[tuple[bool, str]] = []

    @contextlib.contextmanager
    def spy(*, write: bool = False, **kwargs: Any) -> Any:
        with real(write=write, **kwargs) as connection:
            connection.set_trace_callback(lambda sql: seen.append((write, sql)))
            yield connection

    monkeypatch.setattr(state.store, "_connection", spy)
    service.add(WID, "note.md")
    assert any(write and "count(*)" in sql for write, sql in seen)
    assert any(write and sql.startswith("INSERT") for write, sql in seen)
    assert not any(not write and "count(*)" in sql for write, sql in seen)
