from __future__ import annotations

from dataclasses import asdict

import pytest
from starlette.testclient import TestClient

from tests.vaults.test_vault_api import AUTH, ORIGIN, _app
from viki_agent.vaults.note_metadata import extract_display
from viki_agent.vaults.vault import MetadataBatch, MetadataItem

METADATA = "/api/v2/vaults/w/metadata"


class MetadataService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def metadata(self, workspace_id: str, refs: list[str]) -> MetadataBatch:
        self.calls.append((workspace_id, list(refs)))
        raw = "# 日本語\n".encode()
        projection = extract_display(raw, complete=True)
        items = tuple(MetadataItem(ref, "ready", None, len(raw), projection) for ref in refs)
        return MetadataBatch(items, False, (), len(raw) * len(refs), 0)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"null",
        b"{",
        b'{"other":[]}',
        b'{"refs":[]}',
        b'{"refs":"a.md"}',
        b'{"refs":[1]}',
        b'{"refs":["../a.md"]}',
        b'{"refs":["a.txt"]}',
        b'{"refs":["a.md","a.md"]}',
        b'{"refs":["a.md"],"refs":["b.md"]}',
        b'{"refs":["a.md"],"extra":null}',
        b'{"refs":["x\\ud800.md"]}',
        b'{"refs":["x\\u007f.md"]}',
        b'{"refs":[".git/../n.md"]}',
        b'{"refs":["a.md"]}\xff',
    ],
)
def test_malformed_metadata_bodies_never_reach_service_io(body: bytes) -> None:
    service = MetadataService()
    with TestClient(_app(service)) as client:
        response = client.post(METADATA, headers=AUTH, content=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_metadata_request"
    assert service.calls == []


def test_auth_origin_query_and_actual_body_overflow_precede_service_io() -> None:
    service = MetadataService()
    with TestClient(_app(service)) as client:
        assert client.post(METADATA, json={"refs": ["a.md"]}).status_code == 401
        foreign = client.post(
            METADATA, json={"refs": ["a.md"]}, headers={**AUTH, "Origin": "https://evil.invalid"}
        )
        assert foreign.status_code == 403
        query = client.post(METADATA + "?q=a", json={"refs": ["a.md"]}, headers=AUTH)
        assert query.status_code == 400
        assert query.json()["error"]["code"] == "invalid_metadata_request"
        oversized = client.post(METADATA, headers=AUTH, content=b"x" * (128 * 1024 + 1))
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "metadata_request_too_large"
    assert service.calls == []


def test_metadata_post_preserves_exact_order_fields_and_ascii_wire() -> None:
    service = MetadataService()
    refs = ["notes/名前.md", "other.markdown"]
    with TestClient(_app(service)) as client:
        response = client.post(METADATA, json={"refs": refs}, headers={**AUTH, "Origin": ORIGIN})
        assert client.get(METADATA, headers=AUTH).status_code == 405
    assert response.status_code == 200 and response.content.isascii()
    assert service.calls == [("w", refs)]
    body = response.json()
    assert set(body) == {"items", "partial", "partial_reasons", "read_bytes", "omitted"}
    assert [item["ref"] for item in body["items"]] == refs
    assert body["partial"] is False and body["partial_reasons"] == [] and body["omitted"] == 0
    assert body["read_bytes"] == 24 == sum(item["read_bytes"] for item in body["items"])
    projection = asdict(extract_display("# 日本語\n".encode(), complete=True))
    assert body["items"] == [
        {
            "ref": ref,
            "state": "ready",
            "reason": None,
            "read_bytes": 12,
            "display_metadata": projection,
        }
        for ref in refs
    ]


@pytest.mark.parametrize(
    "refs",
    [
        None,
        {},
        True,
        [None],
        [False],
        [{}],
        [[]],
        [1.5],
        [f"{index}.md" for index in range(33)],
        ["/a.md"],
        ["a//b.md"],
        ["a/./b.md"],
        ["a/../b.md"],
        ["a\\b.md"],
        ["C:a.md"],
        ["a.md/"],
        ["x" * 510 + ".md"],
    ],
)
def test_metadata_ref_types_count_and_lexical_failures_precede_io(refs: object) -> None:
    service = MetadataService()
    with TestClient(_app(service)) as client:
        response = client.post(METADATA, json={"refs": refs}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_metadata_request"
    assert service.calls == []


@pytest.mark.parametrize("point", [*range(32), *range(127, 160), 0xD800, 0xDBFF, 0xDC00, 0xDFFF])
def test_metadata_rejects_c0_c1_del_and_surrogates_before_io(point: int) -> None:
    import json

    service = MetadataService()
    encoded = json.dumps({"refs": [f"x{chr(point)}.md"]}).encode("ascii")
    with TestClient(_app(service)) as client:
        response = client.post(METADATA, content=encoded, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_metadata_request"
    assert service.calls == []


def test_metadata_accepts_32_distinct_refs_and_the_exact_512_character_boundary() -> None:
    service = MetadataService()
    refs = ["x" * 509 + ".MD", *[f"{index}.markdown" for index in range(31)]]
    with TestClient(_app(service)) as client:
        response = client.post(METADATA, json={"refs": refs}, headers=AUTH)
    assert response.status_code == 200
    assert service.calls == [("w", refs)]
    assert [item["ref"] for item in response.json()["items"]] == refs


async def test_actual_streamed_body_boundary_without_content_length() -> None:
    import httpx

    service = MetadataService()
    payload = b'{"refs":["a.md"]}'
    body = payload + b" " * (128 * 1024 - len(payload))

    async def chunks(extra: bytes):
        yield body[:65536]
        yield body[65536:]
        yield extra

    transport = httpx.ASGITransport(app=_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.post(METADATA, content=chunks(b""), headers=AUTH)
        refused = await client.post(METADATA, content=chunks(b" "), headers=AUTH)
    assert "content-length" not in allowed.request.headers
    assert "content-length" not in refused.request.headers
    assert allowed.status_code == 200
    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == "metadata_request_too_large"
    assert service.calls == [("w", ["a.md"])]


def test_duplicate_nested_keys_and_excessive_json_depth_precede_io() -> None:
    service = MetadataService()
    bodies = [b'{"refs":[{"x":1,"x":2}]}', b'{"refs":' + b"[" * 3000 + b"]" * 3000 + b"}"]
    with TestClient(_app(service)) as client:
        for body in bodies:
            response = client.post(METADATA, content=body, headers=AUTH)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_metadata_request"
    assert service.calls == []


def test_private_refs_are_per_item_zero_read_refusals(tmp_path, monkeypatch) -> None:
    import viki_agent.vaults.vault as module
    from tests.vaults.test_vault_api import _service

    def forbidden(*args, **kwargs):
        pytest.fail("Private metadata refs must not open or read a prefix")

    monkeypatch.setattr(module.VaultService, "_read_prefix", forbidden)
    refs = [".git/a.md", ".viki/a.md", ".env.local/a.md", ".claude/CLAUDE.md"]
    with TestClient(_app(_service(tmp_path))) as client:
        response = client.post(METADATA, json={"refs": refs}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "ref": ref,
                "state": "unreadable",
                "reason": "private_path",
                "read_bytes": 0,
                "display_metadata": None,
            }
            for ref in refs
        ],
        "partial": True,
        "partial_reasons": ["unreadable"],
        "read_bytes": 0,
        "omitted": 0,
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [("unknown_workspace", 404), ("root_changed", 503), ("root_unavailable", 503)],
)
def test_metadata_root_failures_preserve_mapping_and_hide_exception_details(code, status) -> None:
    from viki_agent.vaults.vault import VaultError

    class RefusedService:
        def metadata(self, workspace_id, refs):
            raise VaultError(code, private_detail="do not expose", refs=refs)

    with TestClient(_app(RefusedService())) as client:
        response = client.post(METADATA, json={"refs": ["n.md"]}, headers=AUTH)
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["error"]["code"] == code
    assert "do not expose" not in response.text and "n.md" not in response.text


@pytest.mark.parametrize("missing_workspace", [True, False])
def test_metadata_uses_current_workspace_root_admission(missing_workspace: bool) -> None:
    from types import SimpleNamespace
    from typing import cast

    from viki_agent.vaults.vault import VaultService
    from viki_agent.workspaces import WorkspaceError, WorkspaceService

    def lookup(_workspace_id):
        if missing_workspace:
            raise WorkspaceError("unknown_workspace")
        return SimpleNamespace(root=None)

    owner = SimpleNamespace(get=lookup, current_root=lambda _workspace: None)
    with TestClient(_app(VaultService(cast(WorkspaceService, owner)))) as client:
        response = client.post(METADATA, json={"refs": ["n.md"]}, headers=AUTH)
    assert response.status_code == (404 if missing_workspace else 503)
    assert response.json()["error"]["code"] == (
        "unknown_workspace" if missing_workspace else "root_unavailable"
    )


# Guards real thread entry/completion; these are not metadata's cooperative work deadline.
HANG_GUARD_S = 5


@pytest.mark.parametrize("disconnect", [False, True])
async def test_metadata_sublimit_and_disconnect_hold_permits_until_actual_work_ends(
    disconnect: bool,
) -> None:
    import asyncio
    import threading

    import httpx

    from viki_agent.vaults.vault import ScanResult

    started, release, entries_release = threading.Event(), threading.Event(), threading.Event()
    entries_started = threading.Semaphore(0)
    block_entries = threading.Event()

    class BlockingMetadata(MetadataService):
        def metadata(self, workspace_id, refs):
            started.set()
            assert release.wait(HANG_GUARD_S)
            return super().metadata(workspace_id, refs)

        def scan(self, workspace_id):
            if block_entries.is_set():
                entries_started.release()
                assert entries_release.wait(HANG_GUARD_S)
            return ScanResult((), False, (), 0, 0, 0)

    service = BlockingMetadata()
    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(service, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = asyncio.create_task(client.post(METADATA, json={"refs": ["a.md"]}, headers=AUTH))
        entries: list[asyncio.Task[httpx.Response]] = []
        try:
            assert await asyncio.to_thread(started.wait, HANG_GUARD_S)
            assert len(workers) == 1
            held = next(iter(workers))
            finished = asyncio.Event()
            held.add_done_callback(lambda _task: finished.set())
            refused = [
                await client.post(METADATA, json={"refs": ["b.md"]}, headers=AUTH) for _ in range(3)
            ]
            assert [response.status_code for response in refused] == [429, 429, 429]
            assert {response.json()["error"]["code"] for response in refused} == {"overloaded"}
            assert (await client.get("/api/v2/vaults/w/entries", headers=AUTH)).status_code == 200
            if disconnect:
                slow.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await slow
                assert held in workers and not held.done()
                assert (
                    await client.post(METADATA, json={"refs": ["b.md"]}, headers=AUTH)
                ).status_code == 429
                block_entries.set()
                entries = [
                    asyncio.create_task(client.get("/api/v2/vaults/w/entries", headers=AUTH))
                    for _ in range(3)
                ]
                for _ in entries:
                    assert await asyncio.to_thread(entries_started.acquire, True, HANG_GUARD_S)
                assert len(workers) == 4 and held in workers
                assert (
                    await client.get("/api/v2/vaults/w/entries", headers=AUTH)
                ).status_code == 429
                entries_finished = []
                for worker in workers - {held}:
                    event = asyncio.Event()
                    worker.add_done_callback(lambda _task, event=event: event.set())
                    entries_finished.append(event)
                entries_release.set()
                assert [response.status_code for response in await asyncio.gather(*entries)] == [
                    200
                ] * 3
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in entries_finished)), HANG_GUARD_S
                )
                assert workers == {held} and not finished.is_set()
        finally:
            entries_release.set()
            release.set()
            await asyncio.gather(*entries, return_exceptions=True)
        if not disconnect:
            assert (await slow).status_code == 200
        await asyncio.wait_for(finished.wait(), HANG_GUARD_S)
        assert not workers
        assert (
            await client.post(METADATA, json={"refs": ["c.md"]}, headers=AUTH)
        ).status_code == 200
    assert service.calls == [("w", ["a.md"]), ("w", ["c.md"])]


async def test_metadata_refused_by_four_shared_workers_returns_its_own_permit() -> None:
    import asyncio
    import threading

    import httpx

    from viki_agent.vaults.vault import ScanResult

    entered, release = threading.Semaphore(0), threading.Event()

    class FullSharedPool(MetadataService):
        def scan(self, workspace_id):
            entered.release()
            assert release.wait(HANG_GUARD_S)
            return ScanResult((), False, (), 0, 0, 0)

    service = FullSharedPool()
    workers: set[asyncio.Task[object]] = set()
    transport = httpx.ASGITransport(app=_app(service, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.get("/api/v2/vaults/w/entries", headers=AUTH))
            for _ in range(4)
        ]
        try:
            for _ in calls:
                assert await asyncio.to_thread(entered.acquire, True, HANG_GUARD_S)
            assert len(workers) == 4
            completed = []
            for worker in workers:
                event = asyncio.Event()
                worker.add_done_callback(lambda _task, event=event: event.set())
                completed.append(event)
            refused = [
                await client.post(METADATA, json={"refs": ["a.md"]}, headers=AUTH) for _ in range(3)
            ]
            assert [response.status_code for response in refused] == [429, 429, 429]
            assert {response.json()["error"]["code"] for response in refused} == {"overloaded"}
            assert service.calls == []
        finally:
            release.set()
        assert [response.status_code for response in await asyncio.gather(*calls)] == [200] * 4
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in completed)), HANG_GUARD_S)
        assert not workers
        for _ in range(2):
            assert (
                await client.post(METADATA, json={"refs": ["a.md"]}, headers=AUTH)
            ).status_code == 200
    assert service.calls == [("w", ["a.md"])] * 2


def _large_projection_fields(ascii_tail: int = 0):
    return {
        "title": "🧠" * (512 - ascii_tail) + "A" * ascii_tail,
        "tags": ["🧠" * 63 + chr(0x1F600 + index) for index in range(16)],
        "type": "🧠" * 64,
        "status": "🧠" * 64,
    }


def _project_fields(fields):
    import json

    raw = (
        "---\n"
        + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()
        )
        + "\n---\n# A\n"
    ).encode()
    assert len(raw) < 32768
    projection = extract_display(raw + b" " * (32768 - len(raw)), complete=False)
    assert projection.state == "ready"
    return projection


def _encoded_batch(items):
    import json

    return json.dumps(
        asdict(MetadataBatch(tuple(items), False, (), sum(item.read_bytes for item in items), 0)),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


class FixedMetadataService:
    def __init__(self, batch):
        self.batch = batch

    def metadata(self, workspace_id, refs):
        assert workspace_id == "w"
        assert refs == [item.ref for item in self.batch.items]
        return self.batch


def test_exact_512k_encoded_envelope_keeps_all_supported_projections() -> None:
    import json

    refs = [f"{index:02d}-" + "🧠" * 506 + ".md" for index in range(32)]
    assert all(len(ref) == 512 for ref in refs)
    small, large = (
        extract_display(b"# A\n", complete=True),
        _project_fields(_large_projection_fields(20)),
    )
    items = [MetadataItem(ref, "ready", None, 32768, small) for ref in refs]
    for index, ref in enumerate(refs):
        candidate = MetadataItem(ref, "ready", None, 32768, large)
        trial = [*items[:index], candidate, *items[index + 1 :]]
        excess = len(_encoded_batch(trial)) - 512 * 1024
        if excess <= 0:
            items = trial
            continue
        fields = _large_projection_fields(20)
        ascii_trim, brain_trim = excess % 12, excess // 12
        fields["title"] = "🧠" * 492 + "A" * (20 - ascii_trim)
        title, tags = fields["title"], fields["tags"]
        assert isinstance(title, str) and isinstance(tags, list)
        assert brain_trim < 492 + 16 * 63
        take = min(brain_trim, 492)
        fields["title"] = title[take:]
        brain_trim -= take
        for tag_index in range(16):
            take = min(brain_trim, 63)
            tags[tag_index] = tags[tag_index][take:]
            brain_trim -= take
        assert brain_trim == 0
        items[index] = MetadataItem(ref, "ready", None, 32768, _project_fields(fields))
        break
    expected = _encoded_batch(items)
    assert len(expected) == 512 * 1024
    batch = MetadataBatch(tuple(items), False, (), 1024 * 1024, 0)
    with TestClient(_app(FixedMetadataService(batch))) as client:
        response = client.post(
            METADATA, content=json.dumps({"refs": refs}, ensure_ascii=False).encode(), headers=AUTH
        )
    assert response.status_code == 200 and response.content == expected
    assert response.content.isascii()
    assert response.json()["omitted"] == 0 and response.json()["partial"] is False


def test_worst_escaped_unicode_reserves_every_ref_and_actual_charge_before_projections() -> None:
    import json

    refs = [f"{index:02d}-" + "🧠" * 506 + ".md" for index in range(32)]
    projection = _project_fields(_large_projection_fields())
    assert projection.display_title is not None and projection.tags is not None
    assert projection.type is not None and projection.status is not None
    assert len(projection.display_title) == 512
    assert len(projection.tags) == 16 and all(len(tag) == 64 for tag in projection.tags)
    assert len(projection.type) == len(projection.status) == 64
    items = tuple(MetadataItem(ref, "ready", None, 32768, projection) for ref in refs)
    assert len(_encoded_batch(items)) > 512 * 1024
    with TestClient(
        _app(FixedMetadataService(MetadataBatch(items, False, (), 1024 * 1024, 0)))
    ) as client:
        response = client.post(
            METADATA, content=json.dumps({"refs": refs}, ensure_ascii=False).encode(), headers=AUTH
        )
    assert response.status_code == 200 and response.content.isascii()
    assert len(response.content) <= 512 * 1024
    body = response.json()
    assert [item["ref"] for item in body["items"]] == refs
    assert body["read_bytes"] == 1024 * 1024 == sum(item["read_bytes"] for item in body["items"])
    kept = [item for item in body["items"] if item["state"] == "ready"]
    dropped = [item for item in body["items"] if item["state"] == "unknown"]
    assert kept and dropped and body["omitted"] == len(dropped)
    assert body["partial"] is True and body["partial_reasons"] == ["response_limit"]
    assert all(
        item["display_metadata"] == asdict(projection) and item["reason"] is None for item in kept
    )
    assert all(
        item["reason"] == "response_limit" and item["display_metadata"] is None for item in dropped
    )
    assert all(
        set(item) == {"ref", "state", "reason", "read_bytes", "display_metadata"}
        for item in body["items"]
    )


def test_encoded_one_byte_boundary_keeps_diagnostics_and_fixed_reason_order(monkeypatch) -> None:
    import json
    from typing import Any

    import viki_agent.vaults.vault_api as module

    projection = _project_fields(_large_projection_fields())
    unsupported = extract_display(b"---\ntitle: [bad]\n---\n", complete=True)
    truncated = extract_display(b"---\ntitle: partial\n", complete=False)
    items = (
        MetadataItem("u.md", "unsupported", "unsupported_metadata", 21, unsupported),
        MetadataItem("t.md", "truncated", "prefix_limit", 19, truncated),
        MetadataItem("c.md", "changed", "changed", 4096, None),
        MetadataItem("e.md", "unreadable", "read_failed", 4096, None),
        MetadataItem("d.md", "unknown", "deadline", 0, None),
        MetadataItem("b.md", "unknown", "byte_limit", 0, None),
        MetadataItem("n.md", "ready", None, 32768, projection),
    )
    reasons = ("deadline", "byte_limit", "unsupported", "truncated", "changed", "unreadable")
    charged = sum(item.read_bytes for item in items)
    original: dict[str, Any] = {
        "items": [asdict(item) for item in items],
        "partial": True,
        "partial_reasons": list(reasons),
        "read_bytes": charged,
        "omitted": 2,
    }
    encoded = json.dumps(original, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    batch = MetadataBatch(items, True, reasons, charged, 2)
    with TestClient(_app(FixedMetadataService(batch))) as client:
        monkeypatch.setattr(module, "MAX_METADATA_RESPONSE", len(encoded))
        exact = client.post(METADATA, json={"refs": [item.ref for item in items]}, headers=AUTH)
        monkeypatch.setattr(module, "MAX_METADATA_RESPONSE", len(encoded) - 1)
        short = client.post(METADATA, json={"refs": [item.ref for item in items]}, headers=AUTH)
    assert exact.status_code == short.status_code == 200
    assert exact.content == encoded
    body = short.json()
    assert len(short.content) <= len(encoded) - 1
    assert body["items"][-1] == {
        "ref": "n.md",
        "state": "unknown",
        "reason": "response_limit",
        "read_bytes": 32768,
        "display_metadata": None,
    }
    assert body["items"][:-1] == original["items"][:-1]
    assert body["partial_reasons"] == [
        "deadline",
        "byte_limit",
        "response_limit",
        "unsupported",
        "truncated",
        "changed",
        "unreadable",
    ]
    assert body["partial"] is True and body["omitted"] == 3
    assert (
        body["read_bytes"]
        == original["read_bytes"]
        == sum(item["read_bytes"] for item in body["items"])
    )


def test_metadata_serializes_one_final_whole_envelope(monkeypatch) -> None:
    import json

    original = json.dumps
    whole_sizes = []

    def observed(value, *args, **kwargs):
        if isinstance(value, dict) and "items" in value:
            whole_sizes.append(len(value["items"]))
        return original(value, *args, **kwargs)

    refs = [f"{index}.md" for index in range(32)]
    monkeypatch.setattr(json, "dumps", observed)
    with TestClient(_app(MetadataService())) as client:
        response = client.post(METADATA, json={"refs": refs}, headers=AUTH)
    assert response.status_code == 200
    assert whole_sizes == [0, 32]
