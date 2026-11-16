"""Manifest shape validation is independent of filesystem mutation support."""

from __future__ import annotations

import pytest

from viki_agent.vaults import vault_backup, vault_restore


def test_backup_and_restore_keep_the_eight_mebibyte_manifest_bound() -> None:
    assert vault_backup.MAX_MANIFEST_BYTES == vault_restore.MAX_MANIFEST_BYTES == 8 * 1024 * 1024


@pytest.mark.parametrize("policy", [[], {}])
def test_container_policies_are_invalid_backup_errors(policy: object) -> None:
    document = {
        "format": vault_restore.BACKUP_FORMAT,
        "version": vault_restore.BACKUP_VERSION,
        "scope": vault_restore.BACKUP_SCOPE,
        "encrypted": False,
        "source": {"name": "Synthetic", "workspace_id": "synthetic", "policy": policy},
        "files": [],
        "skipped": [],
        "totals": {"files": 0, "bytes": 0, "skipped": 0},
    }
    with pytest.raises(vault_restore.BackupRestoreError) as refused:
        vault_restore._parse(document)
    assert refused.value.code == "invalid_backup"
