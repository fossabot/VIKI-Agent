from __future__ import annotations

import json
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.catalog import Catalog, project_dir
from viki_agent.conversations.conversation_search import ConversationSearch
from viki_agent.conversations.conversation_search_api import routes
from viki_agent.conversations.tool_sources import ToolSources, _is_root, _item

SID = str(uuid.UUID(int=1000))
RID = str(uuid.UUID(int=2000))
BASE = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer synthetic", "Origin": BASE}


def _tool_use(tool_use_id: str, name: str, **input_fields: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": input_fields}


def _write_transcript(path: Path, blocks: list[dict[str, Any]]) -> None:
    """One assistant entry per tool_use block, chained into a single linear main chain."""
    rows: list[dict[str, Any]] = []
    parent: str | None = None
    for i, block in enumerate(blocks):
        uid = str(uuid.UUID(int=i + 1))
        rows.append(
            {
                "type": "assistant",
                "uuid": uid,
                "parentUuid": parent,
                "message": {"id": f"m{i + 1}", "content": [block]},
            }
        )
        parent = uid
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _catalog(
    tmp_path: Path, blocks: list[dict[str, Any]], vault: Path = Path("C:/SyntheticVault")
) -> Catalog:
    _write_transcript(project_dir(tmp_path, vault) / f"{SID}.jsonl", blocks)
    return Catalog(tmp_path, vault)


# --- direct lookup() behaviour ------------------------------------------------------------


def test_a_session_with_no_transcript_is_missing(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path, Path("C:/SyntheticVault"))
    service = ToolSources(catalog, lambda sid: (sid, None))
    result = service.lookup(SID, ["w1"])
    assert (result["state"], result["reason"], result["items"]) == (
        "missing",
        "transcript_missing",
        [],
    )


def test_an_https_url_is_returned(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_tool_use("w1", "WebFetch", url="https://example.com/x")])
    service = ToolSources(catalog, lambda sid: (sid, None))
    result = service.lookup(SID, ["w1"])
    assert result["state"] == "ready"
    assert result["items"] == [
        {"tool_use_id": "w1", "tool": "WebFetch", "url": "https://example.com/x"}
    ]


def test_javascript_and_spaced_urls_are_dropped(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        [
            _tool_use("w1", "WebFetch", url="javascript:alert(1)"),
            _tool_use("w2", "WebFetch", url="http://example.com/a b"),
        ],
    )
    service = ToolSources(catalog, lambda sid: (sid, None))
    result = service.lookup(SID, ["w1", "w2"])
    assert result["items"] == []
    assert result["state"] == "ready"


def test_a_backslash_in_the_url_is_dropped(tmp_path: Path) -> None:
    """A backslash before the `@` reads as `docs.python.org` to `urlsplit` but as `evil.example`
    to a browser's WHATWG parser, so the URL is refused outright, exactly as another refused URL
    (above) yields no web item."""
    catalog = _catalog(
        tmp_path,
        [_tool_use("w1", "WebFetch", url="https://evil.example\\@docs.python.org/3/")],
    )
    service = ToolSources(catalog, lambda sid: (sid, None))
    result = service.lookup(SID, ["w1"])
    assert result["items"] == []
    assert result["state"] == "ready"


def test_a_read_id_is_not_answered(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_tool_use("r1", "Read", file_path="n.md")])
    service = ToolSources(catalog, lambda sid: (sid, None))
    result = service.lookup(SID, ["r1"])
    assert result["items"] == []
    assert result["state"] == "ready"


def test_an_absolute_path_under_the_root_is_made_relative(tmp_path: Path) -> None:
    """`root` is `tmp_path` (absolute on every OS), not a Windows-only `C:/...` literal, so this
    proves out on a POSIX CI leg as well as here. Calls `_item` directly instead of routing a
    transcript through a project folder named after `tmp_path` itself, which would double the
    path and risk MAX_PATH on Windows when the test run's own base temp dir is already long."""
    item = _item(
        "g1", "Grep", {"pattern": "needle", "path": f"{tmp_path.as_posix()}/notes/a.md"}, tmp_path
    )
    assert item is not None
    assert (item["path"], item["path_state"]) == ("notes/a.md", "workspace_relative")


def test_a_dot_git_path_is_unknown(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        [_tool_use("g1", "Grep", pattern="needle", path="C:/SyntheticVault/.git/config")],
    )
    service = ToolSources(catalog, lambda sid: (sid, None))
    item = service.lookup(SID, ["g1"])["items"][0]
    assert (item["path"], item["path_state"]) == ("", "unknown")


def test_no_path_is_the_root(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_tool_use("g1", "Glob", pattern="*.md")])
    service = ToolSources(catalog, lambda sid: (sid, None))
    item = service.lookup(SID, ["g1"])["items"][0]
    assert (item["path"], item["path_state"]) == ("", "root")


def test_a_dot_path_is_the_root(tmp_path: Path) -> None:
    item = _item("g1", "Grep", {"pattern": "needle", "path": "."}, tmp_path)
    assert item is not None
    assert (item["path"], item["path_state"]) == ("", "root")


def test_the_absolute_root_path_is_the_root(tmp_path: Path) -> None:
    item = _item("g1", "Grep", {"pattern": "needle", "path": tmp_path.as_posix()}, tmp_path)
    assert item is not None
    assert (item["path"], item["path_state"]) == ("", "root")


def test_the_absolute_root_path_with_a_trailing_separator_is_the_root(tmp_path: Path) -> None:
    item = _item("g1", "Grep", {"pattern": "needle", "path": f"{tmp_path.as_posix()}/"}, tmp_path)
    assert item is not None
    assert (item["path"], item["path_state"]) == ("", "root")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path spellings")
def test_the_absolute_root_path_written_with_backslashes_is_the_root(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    item = _item("g1", "Grep", {"pattern": "needle", "path": str(tmp_path)}, tmp_path)
    assert item is not None
    assert (item["path"], item["path_state"]) == ("", "root")


def test_macos_root_matching_folds_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _is_root("/users/t/vault", "/Users/t/Vault")


def test_macos_root_matching_normalizes_nfc_against_nfd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    nfc = unicodedata.normalize("NFC", "/Vault-café")
    nfd = unicodedata.normalize("NFD", "/Vault-café")
    assert nfc != nfd  # sanity: genuinely different byte sequences
    assert _is_root(nfc, nfd)


def test_a_path_outside_the_root_is_unknown(tmp_path: Path) -> None:
    outside = f"{(tmp_path.parent / 'elsewhere').as_posix()}/x.md"
    item = _item("g1", "Grep", {"pattern": "needle", "path": outside}, tmp_path)
    assert item is not None
    assert (item["path"], item["path_state"]) == ("", "unknown")


def test_a_long_pattern_is_cut_with_an_ellipsis(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_tool_use("g1", "Grep", pattern="n" * 200)])
    service = ToolSources(catalog, lambda sid: (sid, None))
    item = service.lookup(SID, ["g1"])["items"][0]
    assert item["pattern"] == "n" * 159 + "…"


# --- the /tool-sources route --------------------------------------------------------------


def _app(catalog: Catalog) -> Starlette:
    service = ConversationSearch(catalog, lambda sid: (sid, None))
    return Starlette(
        routes=routes(
            service,
            authorized=lambda req: req.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({BASE}),
            workers=set(),
            tool_sources=ToolSources(catalog, service.resolve),
        )
    )


def test_tool_sources_route_returns_matched_items(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_tool_use("w1", "WebFetch", url="https://example.com/x")])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers=HEADERS,
            json={"request_id": RID, "tool_use_ids": ["w1"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == RID
    assert body["state"] == "ready"
    assert body["items"] == [
        {"tool_use_id": "w1", "tool": "WebFetch", "url": "https://example.com/x"}
    ]
    assert response.headers["cache-control"] == "no-store"


def test_tool_sources_route_rejects_seventeen_ids(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [])
    ids = [str(uuid.UUID(int=i)) for i in range(17)]
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers=HEADERS,
            json={"request_id": RID, "tool_use_ids": ids},
        )
    assert response.status_code == 400


def test_tool_sources_route_rejects_a_duplicate_id(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers=HEADERS,
            json={"request_id": RID, "tool_use_ids": ["dup", "dup"]},
        )
    assert response.status_code == 400


def test_tool_sources_route_rejects_a_161_character_id(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers=HEADERS,
            json={"request_id": RID, "tool_use_ids": ["a" * 161]},
        )
    assert response.status_code == 400


def test_tool_sources_route_rejects_a_129_emoji_id(tmp_path: Path) -> None:
    """129 emoji is 129 code points (within the 160-char bound) but 516 UTF-8 bytes, over the
    512-byte bound."""
    catalog = _catalog(tmp_path, [])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers=HEADERS,
            json={"request_id": RID, "tool_use_ids": ["\U0001f600" * 129]},
        )
    assert response.status_code == 400


def test_tool_sources_route_without_bearer_is_unauthorized(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers={"Origin": BASE},
            json={"request_id": RID, "tool_use_ids": ["w1"]},
        )
    assert response.status_code == 401


def test_tool_sources_route_with_a_foreign_origin_is_forbidden(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [])
    with TestClient(_app(catalog), base_url=BASE) as client:
        response = client.post(
            f"/api/v2/sessions/{SID}/tool-sources",
            headers={"Authorization": "Bearer synthetic", "Origin": "https://evil.test"},
            json={"request_id": RID, "tool_use_ids": ["w1"]},
        )
    assert response.status_code == 403
