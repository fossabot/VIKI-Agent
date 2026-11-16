"""The code save route over real services in temporary folders.

Writing tests run on any OS whose volume `vault_mutation._supported_volume` admits. Every
refusal that stops before a write runs everywhere, with a stub behind the saver that records
any call it gets.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.workspace_code.test_code_api import ORIGIN, ROOT_NAME
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code import code_api
from viki_agent.code.code_api import (
    MAX_CODE_REQUESTS,
    MAX_SAVE_BYTES,
    code_routes,
    code_save_routes,
)
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.code_save import CodeSaver
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    MutationCoordinator,
    MutationError,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    VaultPublicationService,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver

AUTH = {"Authorization": "Bearer secret", "Origin": ORIGIN}
JSON = {**AUTH, "Content-Type": "application/json"}
Entry = tuple[str, bool, int, str]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: Path) -> list[Entry]:
    """Every entry under `root` with its size and hash, so a stage or a changed byte shows."""
    entries: list[Entry] = []
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as listing:
            for item in listing:
                relative = os.path.relpath(item.path, root)
                if item.is_dir(follow_symlinks=False):
                    entries.append((relative, True, 0, ""))
                    pending.append(Path(item.path))
                else:
                    data = Path(item.path).read_bytes()
                    entries.append((relative, False, len(data), _sha(data)))
    return sorted(entries)


class StubPublications:
    """Records `save_code` calls; answers `saved`, or raises what it was given. With `gate` it
    waits there, so a test can hold requests in flight."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.raises = raises
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    async def save_code(self, *args: Any, **kwargs: Any) -> PublicationOutcome:
        self.calls.append((args, kwargs))
        if self.entered is not None and len(self.calls) == MAX_CODE_REQUESTS:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return PublicationOutcome("operation", "saved", cleanup_pending=False)


@dataclass
class Env:
    service: Any
    journal: PublicationJournal
    publications: StubPublications | VaultPublicationService
    saver: CodeSaver
    workspace_id: str
    revision: int
    physical_id: str
    root: Path
    parent: Path

    def app(self, workers: set[asyncio.Task[object]] | None = None) -> Starlette:
        files = CodeFiles(self.service)
        authorized = lambda request: request.headers.get("authorization") == "Bearer secret"  # noqa: E731
        shared: dict[str, Any] = {
            "authorized": authorized,
            "origins": frozenset({ORIGIN}),
            "workers": set() if workers is None else workers,
        }
        return Starlette(
            routes=[
                *code_routes(
                    files, CodeGit(self.service, files, git_available=lambda: False), **shared
                ),
                *code_save_routes(self.saver, **shared),
            ]
        )

    def url(self, workspace_id: str | None = None) -> str:
        return f"/api/v2/code/{workspace_id or self.workspace_id}/file"

    def fields(self, content: str = "x\n", **changes: Any) -> dict[str, Any]:
        fields = {
            "policy_revision": self.revision,
            "root_physical_id": self.physical_id,
            "content": content,
            "expected_sha256": None,
            "encoding": "utf-8",
            "newline": "lf",
            **changes,
        }
        return {name: value for name, value in fields.items() if value is not DROP}

    def put(
        self,
        client: TestClient,
        ref: str = "a.py",
        fields: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:  # TestClient's response and httpx's are two unrelated types to pyright
        body = self.fields() if fields is None else fields
        return client.put(
            self.url(),
            params={"path": ref},
            content=json.dumps(body),
            headers={**JSON, **(headers or {})},
        )

    def state(self) -> tuple[list[Entry], list[Any]]:
        return _snapshot(self.root), self.journal.list_pending()

    def stub(self) -> StubPublications:
        assert isinstance(self.publications, StubPublications)
        return self.publications


DROP = object()


def _env(tmp_path: Path, *, trust: bool = True, stub: StubPublications | None = None) -> Env:
    store = store_at(tmp_path)
    service = service_at(tmp_path, store)
    root = tmp_path / ROOT_NAME
    root.mkdir()
    if trust:
        workspace_id = trusted(service, root)
    else:
        workspace_id = service.register_existing(COMMAND, str(root), kind="code")[0].workspace_id
    code = service.get(workspace_id, kind="code")
    journal = PublicationJournal(store)
    publications: StubPublications | VaultPublicationService
    if stub is not None:
        publications = stub
    else:
        resolver = WorkspaceMutationResolver(service, git_available=lambda: False)
        publications = VaultPublicationService(
            journal, functools.partial(resolver, kind=None), MutationCoordinator()
        )
    saver = CodeSaver(service, cast(Any, publications))
    return Env(
        service,
        journal,
        publications,
        saver,
        workspace_id,
        code.policy_revision,
        code.root.physical_id,
        root,
        tmp_path,
    )


def _stubbed(tmp_path: Path, raises: Exception | None = None, **options: bool) -> Env:
    env = _env(tmp_path, stub=StubPublications(raises), **options)
    (env.root / "a.py").write_bytes(b"old\n")
    return env


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _refused(
    response: Any, status: int, code: str, env: Env, operation_id: str | None = None
) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert list(body) == ["error"]
    error = body["error"]
    assert set(error) == ({"code", "message"} | ({"operation_id"} if operation_id else set()))
    assert error["code"] == code and error["message"]
    assert error.get("operation_id") == operation_id
    # Compare decoded strings: JSON escapes a Windows path's backslashes in the raw text.
    for text in _strings(body):
        assert ROOT_NAME not in text and str(env.parent) not in text
    assert response.content.isascii()
    return error


def _untouched(env: Env, before: tuple[list[Entry], list[Any]]) -> None:
    assert env.state() == before
    if isinstance(env.publications, StubPublications):
        assert env.publications.calls == []


# --- a save over the real journalled service ------------------------------------------


def test_put_saves_a_trusted_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"old\n")
    fields = env.fields("new\n", expected_sha256=_sha(b"old\n"))
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", fields)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"operation_id", "status", "sha256", "cleanup_pending"}
    assert (body["status"], body["sha256"], body["cleanup_pending"]) == (
        "saved",
        _sha(b"new\n"),
        False,
    )
    assert (env.root / "a.py").read_bytes() == b"new\n"
    assert env.journal.list_pending() == []
    assert [entry[0] for entry in _snapshot(env.root)] == ["a.py"]


def test_put_creates_a_new_file_in_an_existing_folder_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "src").mkdir()
    with TestClient(env.app()) as client:
        response = env.put(client, "src/new.py", env.fields("print(1)\n"))
        assert (response.status_code, response.json()["status"]) == (200, "saved")
        assert (env.root / "src" / "new.py").read_bytes() == b"print(1)\n"
        before = env.state()
        missing = env.put(client, "lib/new.py", env.fields("print(1)\n"))
    _refused(missing, 409, "unsafe_parent", env)
    assert env.state() == before


def test_put_unchanged_text_is_no_change(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"same\n")
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("same\n", expected_sha256=_sha(b"same\n")))
    assert response.status_code == 200
    assert response.json()["status"] == "no_change"
    assert response.json()["sha256"] == _sha(b"same\n")
    assert (env.root / "a.py").read_bytes() == b"same\n"


def test_put_writes_the_files_own_encoding_and_newline(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.txt").write_bytes(b"\xff\xfe" + "one\r\ntwo\r\n".encode("utf-16-le"))
    with TestClient(env.app()) as client:
        fields = env.fields(
            "one\nTWO\n",
            expected_sha256=_sha((env.root / "a.txt").read_bytes()),
            encoding="utf-16-le",
            newline="crlf",
        )
        response = env.put(client, "a.txt", fields)
    assert response.json()["status"] == "saved"
    assert (env.root / "a.txt").read_bytes() == b"\xff\xfe" + "one\r\nTWO\r\n".encode("utf-16-le")


def test_a_script_body_is_saved_as_bytes_and_no_answer_holds_a_path(tmp_path: Path) -> None:
    env = _env(tmp_path)
    text = "<script>alert('x')</script>\f\x1b\n"
    with TestClient(env.app()) as client:
        response = env.put(client, "page.html", env.fields(text))
    assert response.status_code == 200
    assert (env.root / "page.html").read_bytes() == text.encode()
    for body in (response.json(),):
        for string in _strings(body):
            assert ROOT_NAME not in string and str(env.parent) not in string
    assert response.content.isascii()


def test_a_changed_file_is_409_with_status_conflict(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"on disk\n")
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("mine\n", expected_sha256=_sha(b"read\n")))
    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"operation_id", "status", "sha256", "cleanup_pending"}
    assert (body["status"], body["sha256"]) == ("conflict", None)
    assert env.state() == before


def test_an_existing_name_with_no_hash_is_a_conflict(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"on disk\n")
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("mine\n"))
    assert (response.status_code, response.json()["status"]) == (409, "conflict")
    assert env.state() == before


@pytest.mark.parametrize("change", [{"policy_revision": 999}, {"root_physical_id": "1:2"}])
def test_a_stale_revision_or_root_is_409_workspace_changed(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"old\n")
    before = env.state()
    fields = env.fields("new\n", expected_sha256=_sha(b"old\n"), **change)
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", fields)
    _refused(response, 409, "workspace_changed", env)
    assert env.state() == before


def test_a_read_only_file_is_409_read_only_target(tmp_path: Path) -> None:
    env = _env(tmp_path)
    target = env.root / "a.py"
    target.write_bytes(b"old\n")
    target.chmod(stat.S_IREAD)
    before = env.state()
    try:
        with TestClient(env.app()) as client:
            response = env.put(client, "a.py", env.fields("new\n", expected_sha256=_sha(b"old\n")))
    finally:
        target.chmod(stat.S_IWRITE | stat.S_IREAD)
    _refused(response, 409, "read_only_target", env)
    assert env.state() == before


def test_a_hash_for_a_missing_file_is_409_missing_target(tmp_path: Path) -> None:
    env = _env(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "gone.py", env.fields("x\n", expected_sha256=_sha(b"old\n")))
    _refused(response, 409, "missing_target", env)
    assert env.state() == before


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS has no mode bits")
def test_a_save_keeps_the_files_mode(tmp_path: Path) -> None:
    env = _env(tmp_path)
    target = env.root / "run.sh"
    before = b"#!/bin/sh\necho old\n"
    target.write_bytes(before)
    target.chmod(0o755)
    with TestClient(env.app()) as client:
        fields = env.fields("#!/bin/sh\necho new\n", expected_sha256=_sha(before))
        response = env.put(client, "run.sh", fields)
    assert response.status_code == 200
    assert target.read_bytes() == b"#!/bin/sh\necho new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


# --- Bearer, Origin, workspace id, query ----------------------------------------------------------


def test_put_needs_bearer(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        for headers in ({"Origin": ORIGIN}, {"Authorization": "Bearer other", "Origin": ORIGIN}):
            response = client.put(
                env.url(),
                params={"path": "a.py"},
                content=json.dumps(env.fields()),
                headers={**headers, "Content-Type": "application/json"},
            )
            _refused(response, 401, "unauthorized", env)
    _untouched(env, before)


def test_a_missing_or_foreign_origin_is_403_and_a_listed_one_passes(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        absent = client.put(
            env.url(),
            params={"path": "a.py"},
            content=json.dumps(env.fields()),
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        _refused(absent, 403, "forbidden_origin", env)
        foreign = env.put(client, headers={"Origin": "https://evil.invalid"})
        _refused(foreign, 403, "forbidden_origin", env)
        _untouched(env, before)
        assert env.put(client).status_code == 200
    assert len(env.stub().calls) == 1


def test_a_non_canonical_workspace_id_is_404_before_any_service_call(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    real = env.workspace_id
    with TestClient(env.app()) as client:
        for raw in (real.upper(), f"{{{real}}}", real.replace("-", ""), f"urn:uuid:{real}", "w"):
            response = client.put(
                env.url(raw),
                params={"path": "a.py"},
                content=json.dumps(env.fields()),
                headers=JSON,
            )
            _refused(response, 404, "unknown_workspace", env)
    _untouched(env, before)


@pytest.mark.parametrize(
    "query",
    ["", "?x=1", "?path", "?path=a.py&path=b.py", "?path=a.py&x=1", "?path=%ZZ", "?path=%FF"],
)
def test_a_query_other_than_one_path_is_400_invalid_query(tmp_path: Path, query: str) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = client.put(env.url() + query, content=json.dumps(env.fields()), headers=JSON)
    _refused(response, 400, "invalid_query", env)
    _untouched(env, before)


def test_an_over_size_query_is_400(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = client.put(
            env.url() + "?path=" + "a" * code_api.MAX_QUERY_BYTES,
            content=json.dumps(env.fields()),
            headers=JSON,
        )
    _refused(response, 400, "invalid_query", env)
    _untouched(env, before)


# --- the body -------------------------------------------------------------------------------------

BAD_FIELDS = [
    pytest.param({"policy_revision": 0}, id="revision-zero"),
    pytest.param({"policy_revision": 9_007_199_254_740_992}, id="revision-over-2-53"),
    pytest.param({"policy_revision": True}, id="revision-bool"),
    pytest.param({"policy_revision": "1"}, id="revision-string"),
    pytest.param({"policy_revision": 1.0}, id="revision-float"),
    pytest.param({"policy_revision": DROP}, id="revision-missing"),
    pytest.param({"root_physical_id": "1"}, id="physical-id-no-colon"),
    pytest.param({"root_physical_id": "1:"}, id="physical-id-no-serial"),
    pytest.param({"root_physical_id": "a:1"}, id="physical-id-letter"),
    pytest.param({"root_physical_id": "1:" + "2" * 80}, id="physical-id-long"),
    pytest.param({"root_physical_id": 12}, id="physical-id-int"),
    pytest.param({"root_physical_id": DROP}, id="physical-id-missing"),
    pytest.param({"content": 5}, id="content-int"),
    pytest.param({"content": None}, id="content-null"),
    pytest.param({"content": DROP}, id="content-missing"),
    pytest.param({"expected_sha256": "A" * 64}, id="hash-uppercase"),
    pytest.param({"expected_sha256": "a" * 63}, id="hash-short"),
    pytest.param({"expected_sha256": 5}, id="hash-int"),
    pytest.param({"expected_sha256": DROP}, id="hash-missing"),
    pytest.param({"encoding": "utf-32"}, id="encoding-utf-32"),
    pytest.param({"encoding": "latin-1"}, id="encoding-latin-1"),
    pytest.param({"encoding": "utf-8-sig"}, id="encoding-utf-8-sig"),
    pytest.param({"encoding": ["utf-8"]}, id="encoding-list"),
    pytest.param({"encoding": None}, id="encoding-null"),
    pytest.param({"encoding": DROP}, id="encoding-missing"),
    pytest.param({"newline": "mixed"}, id="newline-mixed"),
    pytest.param({"newline": "LF"}, id="newline-upper"),
    pytest.param({"newline": None}, id="newline-null"),
    pytest.param({"newline": DROP}, id="newline-missing"),
    pytest.param({"extra": 1}, id="extra-key"),
]


@pytest.mark.parametrize("changes", BAD_FIELDS)
def test_a_body_of_any_other_shape_is_400_invalid_body(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields(**changes))
    _refused(response, 400, "invalid_body", env)
    _untouched(env, before)


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '"x"', "5", b"\xff\xfe"])
def test_a_body_that_is_not_one_json_object_is_400_invalid_body(
    tmp_path: Path, raw: str | bytes
) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = client.put(env.url(), params={"path": "a.py"}, content=raw, headers=JSON)
    _refused(response, 400, "invalid_body", env)
    _untouched(env, before)


def test_a_duplicate_key_is_400_invalid_body(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    raw = json.dumps(env.fields())[:-1] + ',"newline":"crlf"}'
    with TestClient(env.app()) as client:
        response = client.put(env.url(), params={"path": "a.py"}, content=raw, headers=JSON)
    _refused(response, 400, "invalid_body", env)
    _untouched(env, before)


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
def test_another_content_type_or_encoding_is_400_invalid_body(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = client.put(
            env.url(),
            params={"path": "a.py"},
            content=json.dumps(env.fields()),
            headers={**AUTH, **headers},
        )
    _refused(response, 400, "invalid_body", env)
    _untouched(env, before)


def test_any_string_is_content_so_a_control_character_is_accepted(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("a\x00b\x0c\x1bc\n"))
    assert response.status_code == 200
    assert env.stub().calls[0][0][4] == b"a\x00b\x0c\x1bc\n"


def test_a_lone_surrogate_is_400_invalid_content(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("a\ud800b"))
    _refused(response, 400, "invalid_content", env)
    _untouched(env, before)


def test_a_request_over_the_intake_bound_is_413_too_large(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    assert MAX_SAVE_BYTES == 6 * MAX_PUBLICATION_BYTES + 4096
    with TestClient(env.app()) as client:
        response = client.put(
            env.url(),
            params={"path": "a.py"},
            content=b" " * (MAX_SAVE_BYTES + 1),
            headers=JSON,
        )
    _refused(response, 413, "too_large", env)
    _untouched(env, before)


def test_content_over_2_mib_once_encoded_is_413_too_large(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, "a.py", env.fields("a" * (MAX_PUBLICATION_BYTES + 1)))
    _refused(response, 413, "too_large", env)
    _untouched(env, before)


# --- refusals by the code service -----------------------------------------------------------------


def test_an_unsupported_drive_is_409_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    (env.root / "a.py").write_bytes(b"old\n")
    before = env.state()
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)
    with TestClient(env.app()) as client:
        response = env.put(client)
    _refused(response, 409, "unsupported_filesystem", env)
    assert env.state() == before


@pytest.mark.parametrize(
    ("ref", "status", "code"),
    [
        ("a//b", 400, "invalid_path"),
        ("", 400, "invalid_path"),
        ("../x.py", 400, "invalid_path"),
        (".env", 403, "private_path"),
        (".git/config", 403, "private_path"),
        ("src/id_rsa", 403, "private_path"),
        (".viki-stage-x", 403, "private_path"),
    ],
)
def test_a_malformed_or_private_path_is_refused_and_nothing_is_written(
    tmp_path: Path, ref: str, status: int, code: str
) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client, ref)
    _refused(response, status, code, env)
    _untouched(env, before)


def test_an_untrusted_folder_is_409_trust_required(tmp_path: Path) -> None:
    env = _stubbed(tmp_path, trust=False)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client)
    _refused(response, 409, "trust_required", env)
    _untouched(env, before)


def test_a_vault_or_unknown_id_is_404_unknown_workspace(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    vault_root = tmp_path / "notes"
    vault_root.mkdir()
    vault, _ = env.service.register_existing(COMMAND, str(vault_root))
    before = env.state()
    with TestClient(env.app()) as client:
        for workspace_id in (vault.workspace_id, "00000000-0000-4000-8000-000000000000"):
            response = client.put(
                env.url(workspace_id),
                params={"path": "a.py"},
                content=json.dumps(env.fields()),
                headers=JSON,
            )
            _refused(response, 404, "unknown_workspace", env)
    _untouched(env, before)


# Every code the saver passes through, with the status and the code the route answers.
STATUS_TABLE: list[tuple[Exception, int, str]] = [
    (CodeError("invalid_content"), 400, "invalid_content"),
    (MutationError("invalid_ref"), 400, "invalid_path"),
    (MutationError("excluded_path"), 403, "private_path"),
    (MutationError("ordinary_workspace_required"), 409, "trust_required"),
    (MutationError("stale_root"), 409, "root_unavailable"),
    (MutationError("workspace_changed"), 409, "workspace_changed"),
    (MutationError("unsupported_filesystem"), 409, "unsupported_filesystem"),
    (MutationError("read_only_target"), 409, "read_only_target"),
    (MutationError("unsafe_target"), 409, "unsafe_target"),
    (MutationError("unsafe_parent"), 409, "unsafe_parent"),
    (MutationError("missing_target"), 409, "missing_target"),
    (PublicationError("publication_busy"), 409, "publication_busy"),
    (PublicationError("vault_recovery_required"), 409, "vault_recovery_required"),
    (MutationError("target_busy"), 423, "target_busy"),
    (MutationError("parent_busy"), 423, "parent_busy"),
    (MutationError("stage_busy"), 423, "stage_busy"),
    (MutationError("vault_locked"), 423, "vault_locked"),
    (PublicationError("publication_limit"), 429, "publication_limit"),
    (PublicationError("journal_unavailable"), 503, "journal_unavailable"),
    (MutationError("a_code_nobody_mapped"), 503, "a_code_nobody_mapped"),
    (OSError("C:\\secret\\place"), 503, "unavailable"),
]


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    STATUS_TABLE,
    ids=[f"{error.__class__.__name__}-{code}" for error, _, code in STATUS_TABLE],
)
def test_each_service_refusal_has_its_status(
    tmp_path: Path, raised: Exception, status: int, code: str
) -> None:
    env = _stubbed(tmp_path, raised)
    before = env.state()
    with TestClient(env.app()) as client:
        response = env.put(client)
    error = _refused(response, status, code, env)
    assert str(raised) not in error["message"]
    assert env.state() == before


def test_a_recovery_required_answer_carries_its_operation_id(tmp_path: Path) -> None:
    for code in ("recovery_required", "vault_recovery_required"):
        (tmp_path / code).mkdir()
        env = _stubbed(tmp_path / code, PublicationError(code, "op-1"))
        with TestClient(env.app()) as client:
            response = env.put(client)
        _refused(response, 409 if code == "vault_recovery_required" else 503, code, env, "op-1")


# --- intake clock, disconnect, capacity -----------------------------------------------------------


async def _drive(
    env: Env, receive: Callable[[], Awaitable[dict[str, Any]]]
) -> tuple[int, dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": env.url(),
        "raw_path": env.url().encode(),
        "root_path": "",
        "query_string": b"path=a.py",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"authorization", b"Bearer secret"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    await env.app()(scope, receive, send)  # type: ignore[arg-type]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body)


async def test_a_body_that_never_arrives_is_408_intake_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    real_fail_after = anyio.fail_after
    monkeypatch.setattr(code_api.anyio, "fail_after", lambda _seconds: real_fail_after(0.02))

    async def never() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    status, body = await _drive(env, never)
    assert (status, body["error"]["code"]) == (408, "intake_timeout")
    _untouched(env, before)


async def test_a_client_that_leaves_mid_body_is_400_disconnected(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    before = env.state()
    sent_part = False

    async def leaves() -> dict[str, Any]:
        nonlocal sent_part
        if not sent_part:
            sent_part = True
            return {"type": "http.request", "body": b'{"policy', "more_body": True}
        return {"type": "http.disconnect"}

    status, body = await _drive(env, leaves)
    assert (status, body["error"]["code"]) == (400, "disconnected")
    _untouched(env, before)


async def test_a_fifth_save_is_429_overloaded_and_the_permits_come_back(tmp_path: Path) -> None:
    stub = StubPublications()
    stub.gate, stub.entered = asyncio.Event(), asyncio.Event()
    env = _env(tmp_path, stub=stub)
    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=env.app(workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def save() -> httpx.Response:
            return await client.put(
                env.url(),
                params={"path": "a.py"},
                content=json.dumps(env.fields()),
                headers=JSON,
            )

        calls = [asyncio.create_task(save()) for _ in range(MAX_CODE_REQUESTS)]
        await asyncio.wait_for(stub.entered.wait(), 2)  # a guard on an event that fires at once
        assert len(workers) == MAX_CODE_REQUESTS
        _refused(await save(), 429, "overloaded", env)
        stub.gate.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        assert (await save()).status_code == 200


# --- the read route beside it ---------------------------------------------------------------------


def test_get_file_still_answers_through_the_same_app(tmp_path: Path) -> None:
    env = _stubbed(tmp_path)
    with TestClient(env.app()) as client:
        response = client.get(env.url(), params={"path": "a.py"}, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["text"] == "old\n"
        # Neither verb answers the other's path with a different method.
        assert client.post(env.url(), params={"path": "a.py"}, headers=AUTH).status_code == 405
