"""Read-only correlation of native transcript images to existing local assets."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent import protection
from viki_agent.catalog import Catalog, project_dir
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import UserMessage
from viki_agent.media import assets
from viki_agent.media.assets import AssetService
from viki_agent.state import AppState, Prefs
from viki_agent.storage import AssetRow, SessionStore
from viki_agent.turn_input import Attachment

SCOPE = "a" * 64
SID = "aaaaaaaa-1111-4111-8111-111111111111"
ASSET = "bbbbbbbb-1111-4111-8111-111111111111"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
DATA = base64.b64encode(PNG).decode("ascii")


def _image(data: str = DATA, **source: Any) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data, **source},
    }


def _entry(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2026-09-19T00:00:00.000Z",
        "message": {"role": "user", "content": blocks},
    }


def _attachment(asset_id: str = ASSET) -> Attachment:
    return Attachment(asset_id, "image/png", len(PNG), 1, 1, "history-image.png", source="history")


def _message(mapper: Mapper, blocks: list[dict[str, Any]]) -> UserMessage:
    events = mapper.entry(_entry(blocks))
    assert len(events) == 1 and isinstance(events[0], UserMessage)
    return events[0]


def test_unbound_mapper_keeps_the_legacy_image_placeholder_without_media_fields() -> None:
    message = _message(Mapper(vault="C:/Vault", mode="default"), [_image()])
    assert message.text == "[image]"
    assert message.data() == {"text": "[image]", "command": "", "at": "2026-09-19T00:00:00.000Z"}


def test_history_mapper_hashes_strict_native_base64_and_dedupes_repeated_image_ids() -> None:
    calls: list[tuple[str, str, int, str]] = []

    def resolve(*value: object) -> Attachment:
        calls.append(cast(tuple[str, str, int, str], value))
        return _attachment()

    message = _message(
        Mapper(vault="C:/Vault", mode="default", history_media=resolve),
        [{"type": "text", "text": "look"}, _image(), _image()],
    )
    assert message.text == "look"
    assert message.attachments == (_attachment(),)
    assert (
        calls == [(hashlib.sha256(PNG).hexdigest(), "image/png", len(PNG), "history-image.png")] * 2
    )
    wire = message.data()
    assert "data" not in json.dumps(wire) and wire["attachments"][0]["source"] == "history"


def test_history_mapper_refuses_invalid_or_unmatched_media_without_aborting_the_turn() -> None:
    mapper = Mapper(vault="C:/Vault", mode="default", history_media=lambda *_: None)
    message = _message(mapper, [{"type": "text", "text": "keep"}, _image("not base64")])
    assert message.text == "keep"
    assert message.unavailable_attachments[0].reason == "invalid_native_media"

    unmatched = _message(
        Mapper(vault="C:/Vault", mode="default", history_media=lambda *_: None), [_image()]
    )
    assert unmatched.text == ""
    assert unmatched.unavailable_attachments[0].reason == "not_in_local_assets"
    assert unmatched.unavailable_attachments[0].byte_size == len(PNG)


def test_history_mapper_keeps_empty_text_with_entries_instead_of_the_image_placeholder() -> None:
    message = _message(
        Mapper(vault="C:/Vault", mode="default", history_media=lambda *_: _attachment()),
        [_image()],
    )
    assert message.text == ""
    assert message.attachments == (_attachment(),)


def test_history_mapper_limits_native_image_work_and_reports_at_most_ten_cards() -> None:
    unavailable = _message(
        Mapper(vault="C:/Vault", mode="default", history_media=lambda *_: None),
        [_image() for _ in range(11)],
    )
    assert len(unavailable.unavailable_attachments) == 10
    assert all(item.reason == "not_in_local_assets" for item in unavailable.unavailable_attachments)
    ids = iter(f"{i:08x}-1111-4111-8111-111111111111" for i in range(11))
    limited = _message(
        Mapper(vault="C:/Vault", mode="default", history_media=lambda *_: _attachment(next(ids))),
        [_image() for _ in range(11)],
    )
    assert len(limited.attachments) == 10
    assert [item.reason for item in limited.unavailable_attachments] == ["limit"]


class _Store:
    def __init__(self, row: AssetRow) -> None:
        self.path = Path("state.sqlite3")
        self.row = row
        self.calls = 0

    def find_assets_by_digest(
        self, scope: str, sha256: str, media_type: str, byte_size: int
    ) -> list[AssetRow]:
        self.calls += 1
        if (scope, sha256, media_type, byte_size) != (
            SCOPE,
            self.row["sha256"],
            self.row["media_type"],
            self.row["byte_size"],
        ):
            return []
        return [dict(self.row)]


def test_asset_digest_lookup_revalidates_stored_file_and_exposes_expired_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assets, "require_private_store", lambda _store: None)
    monkeypatch.setattr(protection, "require_private_file", lambda _path: None)
    row: AssetRow = {
        "scope": SCOPE,
        "asset_id": ASSET,
        "media_type": "image/png",
        "byte_size": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "width": 1,
        "height": 1,
        "created_at": "2026-09-19T00:00:00.000Z",
        "local_state": "stored",
        "local_removed_at": None,
    }
    store = _Store(row)
    service = AssetService(tmp_path / "assets", cast(SessionStore, store), SCOPE)
    path = service._path(ASSET)
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG)
    found = service.find_by_digest(row["sha256"], "image/png", len(PNG), "history-image.png")
    assert found == _attachment() and store.calls == 1
    path.write_bytes(b"changed")
    assert service.find_by_digest(row["sha256"], "image/png", len(PNG), "history-image.png") is None
    row.update(local_state="expired", local_removed_at="2026-09-20T00:00:00.000Z")
    assert (
        service.find_by_digest(row["sha256"], "image/png", len(PNG), "history-image.png")
        == _attachment()
    )
    original = Attachment(
        ASSET,
        "image/png",
        len(PNG),
        1,
        1,
        "original.png",
        source="paste",
        modified_at="2026-09-18T00:00:00.000Z",
    )
    service.bind_history_metadata(lambda _: original)
    assert service.find_by_digest(row["sha256"], "image/png", len(PNG), "fallback.png") == original


def test_store_digest_query_is_scope_isolated_and_prefers_stored_then_newest(
    tmp_path: Path,
) -> None:
    state = AppState(tmp_path / "state.json", Prefs())
    digest = hashlib.sha256(PNG).hexdigest()
    rows = []
    for suffix, scope, local_state, created_at in (
        ("1", SCOPE, "expired", "2026-09-18T00:00:00.000Z"),
        ("2", SCOPE, "stored", "2026-09-17T00:00:00.000Z"),
        ("3", "b" * 64, "stored", "2026-09-19T00:00:00.000Z"),
    ):
        row = {
            "scope": scope,
            "asset_id": f"00000000-0000-4000-8000-00000000000{suffix}",
            "media_type": "image/png",
            "byte_size": len(PNG),
            "sha256": digest,
            "width": 1,
            "height": 1,
            "created_at": created_at,
        }
        state.store.put_asset(row)
        if local_state == "expired":
            with state.store._connection(write=True) as connection:
                connection.execute(
                    "UPDATE assets SET local_state='expired',local_removed_at=? WHERE asset_id=?",
                    ("2026-09-20T00:00:00.000Z", row["asset_id"]),
                )
        rows.append(row)
    found = state.store.find_assets_by_digest(SCOPE, digest, "image/png", len(PNG))
    assert [row["asset_id"] for row in found] == [rows[1]["asset_id"], rows[0]["asset_id"]]


def test_catalog_history_uses_only_an_explicitly_bound_asset_resolver(tmp_path: Path) -> None:
    vault, home = Path("C:/Vault"), tmp_path / "home"
    transcript = project_dir(home, vault) / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                **_entry([_image()]),
                "uuid": SID,
                "parentUuid": None,
                "isSidechain": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    catalog = Catalog(home, vault)
    plain = catalog.history(SID).events[0]
    assert isinstance(plain, UserMessage) and plain.attachments == ()
    catalog.bind_history_media(lambda *_: _attachment())
    restored = catalog.history(SID).events[0]
    assert isinstance(restored, UserMessage) and restored.attachments == (_attachment(),)
