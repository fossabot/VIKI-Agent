"""POSIX proof that every note operation runs for real over HTTP: rename, move
into a new folder, archive, delete to a recovery entry, and restore, chained on one note, end
with the exact original bytes. Windows behaviour is proven by
`tests/vaults/test_vault_ops_refusals.py` and `tests/vaults/test_vault_ops_integration.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.vaults.test_vault_ops_refusals import operate
from tests.vaults.test_vault_write_integration import HEADERS, launch, sha

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX operation round trip")


def test_every_operation_runs_and_restore_returns_the_bytes(tmp_path: Path) -> None:
    with launch(tmp_path) as app:
        workspace = app.create_vault("ops")
        vault = Path(workspace.root.display)
        original = (vault / "Welcome.md").read_bytes()
        loaded = {"expected_sha256": sha(original)}

        renamed = operate(
            app, workspace, "rename", {"ref": "Welcome.md", "to": "Renamed.md", **loaded}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["status"] == "done", renamed.text
        assert (vault / "Renamed.md").read_bytes() == original

        moved = operate(
            app, workspace, "move", {"ref": "Renamed.md", "to": "sub/Renamed.md", **loaded}
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["status"] == "done", moved.text
        assert (vault / "sub" / "Renamed.md").read_bytes() == original

        archived = operate(
            app,
            workspace,
            "archive",
            {"ref": "sub/Renamed.md", "to": "archive/sub/Renamed.md", **loaded},
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "done", archived.text
        assert (vault / "archive" / "sub" / "Renamed.md").read_bytes() == original

        deleted = operate(app, workspace, "delete", {"ref": "archive/sub/Renamed.md", **loaded})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "done", deleted.text
        assert not (vault / "archive" / "sub" / "Renamed.md").exists()

        listed = app.client.get(
            f"/api/v2/vaults/{workspace.workspace_id}/recovery", headers=HEADERS
        )
        assert listed.status_code == 200, listed.text
        [entry] = listed.json()["entries"]
        assert entry["ref"] == "archive/sub/Renamed.md"

        restored = operate(
            app, workspace, "restore", {"recovery_id": entry["recovery_id"], "to": "Welcome.md"}
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "done", restored.text
        assert (vault / "Welcome.md").read_bytes() == original
