"""Keep in vault and memory writes on a volume with no supported filesystem, through the routes.

`vault_mutation._supported_volume` is the one refusal point every root shares: it decides, for any
root on any OS, whether its filesystem type is one the publication service backs. Patching it to
answer False makes an admitted vault's volume look unsupported, so the real routes, resolver and
coordinator run unchanged and only the volume answer differs.

Each request must answer 409 `unsupported_filesystem`, leave every tree byte-identical (`snapshot`)
and add no `memory_reviews` row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.media.test_assets import PNG
from tests.vaults.test_vault_knowledge_integration import (
    HEADERS,
    Harness,
    launch,
    reviews,
    trees,
)
from viki_agent.domain import Workspace
from viki_agent.vaults import vault_mutation


def patch_memory(
    app: Harness, workspace: Workspace, memory_id: str, sha256: str, **change: Any
) -> Any:
    return app.client.patch(
        f"/api/v2/vaults/{workspace.workspace_id}/memories/{memory_id}",
        headers=HEADERS,
        json={**app.admission(workspace), "sha256": sha256, **change},
    )


def test_keep_and_memory_writes_refuse_unsupported_filesystem_and_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with launch(tmp_path, monkeypatch) as app:
        workspace = app.create_vault("backendless")
        # `personal` and written while the volume is still supported: the record every PATCH names.
        seed = app.create_memory(workspace, scope="personal").json()
        asset_id = app.upload(PNG)
        monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)
        requests = {
            "keep": lambda: app.keep(workspace, asset_id),
            "create": lambda: app.create_memory(workspace),
            "approve": lambda: patch_memory(
                app, workspace, seed["memory_id"], seed["sha256"], action="approve"
            ),
            "retire": lambda: patch_memory(
                app, workspace, seed["memory_id"], seed["sha256"], action="retire", retired_by=None
            ),
        }
        before = trees(tmp_path)
        reviewed = reviews(tmp_path)

        for name, send in requests.items():
            answer = send()

            assert answer.status_code == 409, (name, answer.text)
            assert answer.json()["error"]["code"] == "unsupported_filesystem", (name, answer.text)
            assert trees(tmp_path) == before, name
            assert reviews(tmp_path) == reviewed, name
