"""create_app-level proof that the two history-media bindings connect a REST-accepted
attachment and a native transcript image to the same local asset, through both history and
the search anchor window. Removing either binding must lose the original name and source."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from tests.test_server import BEARER, ORIGIN, _history_v2, _open_v2, authed, running
from viki_agent import protection, storage
from viki_agent.catalog import Catalog, project_dir
from viki_agent.conversations.commands import CommandInput
from viki_agent.conversations.drafts import DraftService, draft_scope
from viki_agent.media import assets as assets_module
from viki_agent.media.assets import AssetService
from viki_agent.turn_input import Attachment, TurnInput

# Bounds a wait on a store lock that normally clears within milliseconds; never real work.
HANG_GUARD_S = 5

SID = "aaaaaaaa-1111-4111-8111-111111111111"
CID = "bbbbbbbb-1111-4111-8111-111111111111"
ASSET = "cccccccc-1111-4111-8111-111111111111"
WRITE = {**BEARER, "origin": ORIGIN}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
# What the user actually attached: a distinctive name and a "paste" source, neither of which
# the generic history-media fallback (source="history") would ever produce.
ORIGINAL = Attachment(ASSET, "image/png", len(PNG), 1, 1, "original-name.png", source="paste")


def _image_block() -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(PNG).decode("ascii"),
        },
    }


def _write_transcript(home: Path, vault: Path) -> None:
    transcript = project_dir(home, vault) / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "user",
        "timestamp": "2026-09-19T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "look"}, _image_block()]},
        "uuid": SID,
        "parentUuid": None,
        "isSidechain": False,
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _assert_original(event: dict[str, Any]) -> None:
    """The one bar both bindings must clear: not just an attachment, but this attachment."""
    attachments = event["data"].get("attachments", [])
    assert len(attachments) == 1, "attachment restore: expected exactly one attachment"
    assert attachments[0]["name"] == ORIGINAL.name, "attachment restore: name was not restored"
    assert attachments[0]["source"] == ORIGINAL.source, (
        "attachment restore: source was not restored"
    )


def _drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assets_module, "require_private_store", lambda _store: None)
    monkeypatch.setattr(protection, "require_private_file", lambda _path: None)
    # The app's own background store work can hold the store's write lock past Windows'
    # 25 ms busy budget while history reads the store; the history-media resolver then
    # reports the image as not_in_local_assets. This file proves the bindings, not that path.
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", HANG_GUARD_S)
    # A short root, not pytest's own deeply nested tmp_path: project_dir() embeds the vault's
    # full path in the transcript folder name, and pytest's own tmp_path pushes that past
    # Windows' 260-character MAX_PATH once the session id and ".jsonl" are appended.
    root = Path(tempfile.mkdtemp())
    try:
        _run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(tmp_path: Path) -> None:
    vault, home = tmp_path / "vault", tmp_path / "home"
    catalog = Catalog(home, vault)
    with running(tmp_path, catalog=catalog) as app:
        scope = draft_scope(vault)
        store = app.manager.app_state.store
        store.put_asset(
            {
                "scope": scope,
                "asset_id": ASSET,
                "media_type": "image/png",
                "byte_size": len(PNG),
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "width": 1,
                "height": 1,
                "created_at": "2026-09-19T00:00:00.000Z",
            }
        )
        blob = AssetService(tmp_path / "data" / "assets", store, scope)._path(ASSET)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(PNG)

        # Accept a command carrying the asset over REST: the durable command row is what lets
        # AssetService.bind_history_metadata later recover the original name and source.
        actor = app.manager.open(SID)
        draft = DraftService(app.manager.app_state, vault).save(
            SID, 0, TurnInput("look", (ORIGINAL,))
        )
        value = CommandInput(CID, SID, actor.generation, draft.revision)
        accepted = app.client.post("/api/v2/commands", headers=WRITE, json=asdict(value))
        assert accepted.status_code == 202

        # A native transcript carrying the same image bytes, as the CLI would have written it.
        _write_transcript(home, vault)

        with authed(app.client) as ws:
            _, target = _open_v2(ws, SID)
            page = _history_v2(ws, target)
        assert "error" not in page["data"]
        events = page["data"]["events"]
        assert len(events) == 1
        _assert_original(events[0])

        search = app.client.get(
            f"/api/v2/sessions/{SID}/conversation-search?q=look&request_id={uuid.uuid4()}",
            headers=WRITE,
        )
        assert search.status_code == 200
        hits = search.json()["hits"]
        assert len(hits) == 1
        anchor = app.client.post(
            f"/api/v2/sessions/{SID}/history-anchor",
            headers=WRITE,
            json={"request_id": str(uuid.uuid4()), "anchor": hits[0]["anchor"]},
        )
        assert anchor.status_code == 200
        anchor_events = anchor.json()["events"]
        image_events = [e for e in anchor_events if e["data"].get("attachments")]
        assert len(image_events) == 1
        _assert_original(image_events[0])


def test_history_media_bindings_restore_the_original_attachment_at_create_app_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drive(monkeypatch)


def test_removing_the_catalog_binding_loses_the_original_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Catalog, "bind_history_media", lambda self, resolver: None)
    with pytest.raises(AssertionError, match="attachment restore:"):
        _drive(monkeypatch)


def test_removing_the_asset_service_binding_loses_the_original_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AssetService, "bind_history_metadata", lambda self, resolver: None)
    with pytest.raises(AssertionError, match="attachment restore:"):
        _drive(monkeypatch)
