"""Draft service integration with synthetic app data; never starts a provider or a server port."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Scope

from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.conversations.drafts import draft_scope
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.server import SECURITY_HEADERS, create_app
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

BASE = "http://127.0.0.1:8765"
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ASSET = "00000000-0000-4000-8000-000000000030"
AUTH = {"authorization": "Bearer draft-test-token"}
MUTATION = {**AUTH, "origin": BASE}
PATH = "/api/v2/drafts"
HANG_GUARD_S = 30  # a guard on an event that normally fires in milliseconds; only a hang reaches it


def app_at(tmp_path: Path, *, dev: bool = False):
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    home = tmp_path / "home"
    manager = SessionManager(
        config=DriverConfig(cli=("must-not-run",), vault=vault, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("draft operations must never start a provider"),
    )
    config = Config(
        vault=vault, claude_home=home, cli=Path("absent"), edge=None, data_dir=tmp_path / "data"
    )
    app = create_app(
        config=config,
        token="draft-test-token",
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
        dev=dev,
    )
    return app, state, manager


def test_draft_roundtrip_restart_and_clear_do_not_create_native_ownership(tmp_path: Path) -> None:
    app, state, manager = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    missing = client.get(f"{PATH}/{SID}", headers=AUTH)
    assert missing.status_code == 200
    assert missing.json() == {"key": SID, "revision": 0, "text": "", "saved_at": None}
    text = "Draft\n- naïve input\n😀\n"
    saved = client.put(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": text}
    )
    assert saved.status_code == 200
    receipt = saved.json()
    assert receipt == {"key": SID, "revision": 1, "text": text, "saved_at": receipt["saved_at"]}
    assert receipt["saved_at"]
    assert not state.ours(SID)
    assert manager.get(SID) is None
    assert state.database_path.read_bytes().find(text.encode()) == -1
    assert (
        client.put(
            f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": text}
        ).json()
        == receipt
    )
    conflict = client.put(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": "conflict"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_revision"] == 1
    assert text not in conflict.text
    again, restarted, _ = app_at(tmp_path)
    other = TestClient(again, base_url=BASE)
    assert other.get(f"{PATH}/{SID}", headers=AUTH).json() == receipt
    listed = other.get(PATH, headers=AUTH).json()
    assert listed == {
        "drafts": [
            {
                "key": SID,
                "revision": 1,
                "saved_at": receipt["saved_at"],
                "bytes": len(text.encode()),
            }
        ],
        "next_after": None,
    }
    cleared = other.put(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 1, "text": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["revision"] == 2
    assert other.get(PATH, headers=AUTH).json() == {"drafts": [], "next_after": None}
    assert (
        other.put(
            f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": text}
        ).status_code
        == 409
    )
    assert not restarted.ours(SID)


def test_unavailable_attachment_answers_409_and_keeps_the_draft_unchanged(
    tmp_path: Path,
) -> None:
    app, state, _ = app_at(tmp_path)
    scope = draft_scope(tmp_path / "vault")
    state.store.put_asset(
        {
            "scope": scope,
            "asset_id": ASSET,
            "media_type": "image/png",
            "byte_size": 4,
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-09-19T00:00:00.000Z",
        }
    )
    client = TestClient(app, base_url=BASE)
    body = {
        "expected_revision": 0,
        "text": "keep",
        "attachments": [
            {
                "asset_id": ASSET,
                "media_type": "image/png",
                "byte_size": 4,
                "width": 1,
                "height": 1,
                "name": "synthetic.png",
            }
        ],
    }
    saved = client.put(f"{PATH}/{SID}", headers=MUTATION, json=body)
    assert saved.status_code == 200
    receipt = saved.json()
    assert receipt["revision"] == 1
    with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
        connection.execute("DELETE FROM assets WHERE asset_id=?", (ASSET,))
        connection.commit()
    refused = client.put(f"{PATH}/{SID}", headers=MUTATION, json={**body, "expected_revision": 1})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "attachment_unavailable"
    assert client.get(f"{PATH}/{SID}", headers=AUTH).json() == receipt
    with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
        refs = connection.execute(
            "SELECT asset_id,role FROM asset_refs WHERE owner_kind='draft'"
        ).fetchall()
    assert refs == [(ASSET, "attachment.0")]


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/new"), ("PUT", "/new")])
def test_draft_routes_authenticate_and_keep_security_headers(
    tmp_path: Path,
    method: str,
    suffix: str,
) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    response = client.request(method, PATH + suffix, content=b"invalid body")
    assert response.status_code == 401
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    foreign = client.request(method, PATH + suffix, headers={**AUTH, "origin": "https://evil.test"})
    assert foreign.status_code == 403
    host = client.request(method, PATH + suffix, headers={**MUTATION, "host": "evil.test"})
    assert host.status_code == 400


def test_mutation_requires_origin_and_dev_origin_is_explicit(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    body = {"expected_revision": 0, "text": "draft"}
    assert client.put(PATH + "/new", headers=AUTH, json=body).status_code == 403
    vite = {**AUTH, "origin": "http://127.0.0.1:5173"}
    assert client.put(PATH + "/new", headers=vite, json=body).status_code == 403
    dev_app, _, _ = app_at(tmp_path, dev=True)
    assert (
        TestClient(dev_app, base_url=BASE).put(PATH + "/new", headers=vite, json=body).status_code
        == 200
    )


def test_implicit_head_is_read_only_even_with_a_valid_save_body(tmp_path: Path) -> None:
    app, state, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    before = state.database_path.read_bytes()
    response = client.request(
        "HEAD",
        PATH + "/new",
        headers=AUTH,
        json={"expected_revision": 0, "text": "synthetic HEAD write"},
    )
    assert response.status_code == 200
    assert response.content == b""
    assert client.get(PATH + "/new", headers=AUTH).json()["revision"] == 0
    assert state.database_path.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"expected_revision": True, "text": "x"},
        {"expected_revision": -1, "text": "x"},
        {"expected_revision": 0, "text": 4},
        {"expected_revision": 0, "text": "x", "scope": "elsewhere"},
        {"expected_revision": 0, "text": "x", "command_id": SID},
        {"expected_revision": 0, "text": "\ud800"},
    ],
)
def test_invalid_draft_inputs_never_write(tmp_path: Path, body: object) -> None:
    app, state, _ = app_at(tmp_path)
    original = state.database_path.read_bytes()
    response = TestClient(app, base_url=BASE).put(
        PATH + "/new",
        headers={**MUTATION, "content-type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 400
    assert state.database_path.read_bytes() == original


@pytest.mark.parametrize(
    "suffix",
    [
        "/NEW",
        "/not-a-uuid",
        "/" + SID.upper(),
        "?limit=0",
        "?limit=101",
        "?limit=true",
        "?limit=2&limit=3",
        "?scope=elsewhere",
        "?after=bad",
        "?after=" + "x" * 300,
        "/new?scope=elsewhere",
    ],
)
def test_invalid_keys_and_paging(tmp_path: Path, suffix: str) -> None:
    app, _, _ = app_at(tmp_path)
    assert TestClient(app, base_url=BASE).get(PATH + suffix, headers=AUTH).status_code == 400


@pytest.mark.parametrize(
    "body", [b'{"text":"a","text":"b","expected_revision":0}', b"\xff", b"[[[[", b"null"]
)
def test_malformed_json_is_safe(tmp_path: Path, body: bytes) -> None:
    app, _, _ = app_at(tmp_path)
    result = TestClient(app, base_url=BASE).put(
        PATH + "/new",
        headers={**MUTATION, "content-type": "application/json"},
        content=body,
    )
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "invalid_body"


async def raw_request(
    app: ASGIApp,
    *,
    headers: dict[str, str],
    chunks: list[bytes],
    slow: bool = False,
) -> tuple[int, bytes, int]:
    """Exercise actual ASGI intake, including clients lying about Content-Length."""
    calls = 0
    output: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": PATH + "/new",
        "raw_path": b"",
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 43210),
        "headers": [
            (k.encode(), v.encode()) for k, v in {"host": "127.0.0.1:8765", **headers}.items()
        ],
    }

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if slow:
            await anyio.sleep_forever()
        if not chunks:
            pytest.fail("body was consumed before authentication/Origin/intake admission")
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        output.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in output if m["type"] == "http.response.body")
    return status, body, calls


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({**AUTH}, 403),
        ({**AUTH, "origin": "https://evil.test"}, 403),
        ({**MUTATION}, 415),
        ({**MUTATION, "content-type": "application/json", "content-encoding": "gzip"}, 415),
        ({**MUTATION, "content-type": "application/json", "content-length": "999999999"}, 413),
        ({**MUTATION, "content-type": "application/json", "content-length": "-1"}, 400),
    ],
)
async def test_rejected_request_never_consumes_body(
    tmp_path: Path,
    headers: dict[str, str],
    status: int,
) -> None:
    app, _, _ = app_at(tmp_path)
    actual, _, calls = await raw_request(app, headers=headers, chunks=[])
    assert (actual, calls) == (status, 0)


async def test_stream_size_and_deadline_hold_without_trusting_content_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viki_agent.conversations import draft_api

    app, state, _ = app_at(tmp_path)
    original = state.database_path.read_bytes()
    headers = {**MUTATION, "content-type": "application/json", "content-length": "1"}
    status, _, _ = await raw_request(
        app, headers=headers, chunks=[b"{", b"x" * draft_api.MAX_DRAFT_BODY_BYTES]
    )
    assert status == 413
    monkeypatch.setattr(draft_api, "DRAFT_BODY_TIMEOUT_S", 0.005)
    status, _, calls = await raw_request(app, headers=headers, chunks=[], slow=True)
    assert (status, calls) == (408, 1)
    assert state.database_path.read_bytes() == original


def test_plaintext_limit_is_independent_of_json_escape_size(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    # U+0001 needs six wire bytes but is one UTF-8 byte. The full admitted draft still fits.
    body = json.dumps({"expected_revision": 0, "text": "\x01" * (1 << 20)})
    headers = {**MUTATION, "content-type": "application/json"}
    accepted = client.put(PATH + "/new", headers=headers, content=body)
    assert accepted.status_code == 200
    too_large = client.put(
        PATH + "/new",
        headers=MUTATION,
        json={"expected_revision": 1, "text": "x" * ((1 << 20) + 1)},
    )
    assert too_large.status_code == 413
    assert client.get(PATH + "/new", headers=AUTH).json()["revision"] == 1


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_draft_pressure_is_bounded_and_does_not_block_other_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_request: bool,
) -> None:
    from viki_agent.conversations import draft_api
    from viki_agent.conversations.drafts import DraftService

    app, _, _ = app_at(tmp_path)
    entered: asyncio.Queue[None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    original = DraftService.get

    def blocked(self: DraftService, key: str):
        loop.call_soon_threadsafe(entered.put_nowait, None)
        release.wait()
        return original(self, key)

    monkeypatch.setattr(DraftService, "get", blocked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        active = [
            asyncio.create_task(client.get(PATH + "/new", headers=AUTH))
            for _ in range(draft_api.MAX_DRAFT_REQUESTS)
        ]
        try:
            for _ in active:
                await asyncio.wait_for(entered.get(), 0.05)
            if cancel_request:
                active[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await active[0]
            rejected = await client.put(PATH + "/new", headers=MUTATION, content=b"bad")
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "overloaded"
            unrelated = await client.get("/api/sessions")
            assert unrelated.status_code == 401
        finally:
            release.set()
            results = await asyncio.gather(*active, return_exceptions=True)
        assert all(
            isinstance(response, asyncio.CancelledError)
            or (isinstance(response, httpx.Response) and response.status_code == 200)
            for response in results
        )
        assert (await client.get(PATH, headers=AUTH)).status_code == 200


def test_service_failure_does_not_expose_private_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viki_agent.conversations.drafts import DraftError, DraftService

    app, _, _ = app_at(tmp_path)

    def unavailable(*args: Any, **kwargs: Any):
        raise DraftError("unavailable")

    monkeypatch.setattr(DraftService, "get", unavailable)
    result = TestClient(app, base_url=BASE).get(PATH + "/new", headers=AUTH)
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "unavailable"
    assert "Traceback" not in result.text


@pytest.mark.parametrize("code", ["busy", "incompatible", "recovery_required"])
def test_storage_state_errors_refuse_with_their_own_code_and_log_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: str
) -> None:
    """DraftService._access passes StateError codes through untouched. The approvals route
    answers this same passthrough `busy` with 409; here every one of them is 503, and the
    editor reads a draft 409 as a revision conflict. The divergence is pinned, not endorsed.
    """
    from viki_agent.conversations import drafts as drafts_module
    from viki_agent.storage import StateError

    app, _, _ = app_at(tmp_path)

    def unavailable(*_args: Any) -> None:
        raise StateError(code)

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(drafts_module, "require_private_store", unavailable)
    result = TestClient(app, base_url=BASE).put(
        PATH + f"/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": "draft café"}
    )
    assert result.status_code == 503 and result.json()["error"]["code"] == code
    assert code in caplog.text
    assert "draft café" not in caplog.text and StateError(code).message not in caplog.text


async def test_shutdown_drains_a_save_after_its_http_caller_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from viki_agent.conversations.drafts import DraftService

    app, _, _ = app_at(tmp_path)
    entered = asyncio.Event()
    exiting = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    original = DraftService.save

    def blocked(self: DraftService, key: str, revision: int, text: str):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return original(self, key, revision, text)

    monkeypatch.setattr(DraftService, "save", blocked)

    async def lifecycle() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                caller = asyncio.create_task(
                    client.put(
                        PATH + "/new",
                        headers=MUTATION,
                        json={"expected_revision": 0, "text": "keep after disconnect"},
                    )
                )
                await entered.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            exiting.set()

    shutdown = asyncio.create_task(lifecycle())
    try:
        await asyncio.wait_for(exiting.wait(), HANG_GUARD_S)
        assert not shutdown.done()
    finally:
        release.set()
        await shutdown
    reopened, _, _ = app_at(tmp_path)
    client = TestClient(reopened, base_url=BASE)
    receipt = client.get(PATH + "/new", headers=AUTH).json()
    assert receipt["revision"] == 1 and receipt["text"] == "keep after disconnect"
    retry = client.put(
        PATH + "/new",
        headers=MUTATION,
        json={"expected_revision": 0, "text": "keep after disconnect"},
    )
    assert retry.status_code == 200 and retry.json() == receipt


def _part(start: int) -> dict[str, Any]:
    return {
        "kind": "vault_excerpt",
        "vault_id": "00000000-0000-4000-8000-000000000001",
        "ref": "notes/a.md",
        "start_line": start,
        "end_line": start + 1,
        "sha256": "a" * 64,
    }


def test_put_with_two_parts_reloads_them(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    body = {"expected_revision": 0, "text": "quote", "parts": [_part(1), _part(5)]}
    saved = client.put(f"{PATH}/{SID}", headers=MUTATION, json=body)
    assert saved.status_code == 200 and saved.json()["parts"] == [_part(1), _part(5)]
    assert client.get(f"{PATH}/{SID}", headers=AUTH).json() == saved.json()


def test_put_with_empty_parts_clears_them_and_omits_the_key(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    body = {"expected_revision": 0, "text": "quote", "parts": [_part(1)]}
    assert client.put(f"{PATH}/{SID}", headers=MUTATION, json=body).status_code == 200
    cleared = client.put(
        f"{PATH}/{SID}", headers=MUTATION, json={**body, "parts": [], "expected_revision": 1}
    )
    assert cleared.status_code == 200 and "parts" not in cleared.json()
    assert "parts" not in client.get(f"{PATH}/{SID}", headers=AUTH).json()


def test_put_without_parts_key_behaves_as_before(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    saved = client.put(
        f"{PATH}/{SID}", headers=MUTATION, json={"expected_revision": 0, "text": "plain"}
    )
    assert saved.status_code == 200
    assert set(saved.json()) == {"key", "revision", "text", "saved_at"}


def test_put_with_nine_parts_is_refused(tmp_path: Path) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    body = {"expected_revision": 0, "text": "q", "parts": [_part(i) for i in range(1, 10)]}
    refused = client.put(f"{PATH}/{SID}", headers=MUTATION, json=body)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_body"


@pytest.mark.parametrize(
    "parts", [{"a": 1}, [1], [{**_part(1), "extra": 1}], [{**_part(1), "kind": "other"}]]
)
def test_put_with_a_malformed_part_is_refused(tmp_path: Path, parts: object) -> None:
    app, _, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    body = {"expected_revision": 0, "text": "q", "parts": parts}
    refused = client.put(f"{PATH}/{SID}", headers=MUTATION, json=body)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_body"
