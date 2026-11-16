"""Whether a note under one folder of a vault changed after a moment: one zero-content scan
through `reader` (a `VaultService`), comparing each listed note's file time. Model-free and
read-only; nothing here opens a note, logs, or raises for a vault problem. A `VaultError` or a
partial listing leaves the answer unknown (`None`), so the caller runs the slot rather than
skip a change it could not see.

Folder and ref compare by NFC and case folding, so a folder typed in another letter case or
Unicode form still finds its notes; that only ever widens what counts as a change.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime

from viki_agent.vaults.vault import VaultError, VaultService


def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def notes_changed(
    reader: VaultService, workspace_id: str, folder: str, since: datetime
) -> bool | None:
    """True when a listed note at any depth under `folder` has a file time after `since`; False
    only when the listing is complete and none does, including a folder with no notes; None when
    the vault could not be listed whole."""
    try:
        scan = reader.scan(workspace_id)
    except VaultError:
        return None
    if scan.partial:
        return None
    prefix = _key(folder) + "/"
    after = since.timestamp()
    return any(
        entry.modified > after for entry in scan.entries if _key(entry.ref).startswith(prefix)
    )
