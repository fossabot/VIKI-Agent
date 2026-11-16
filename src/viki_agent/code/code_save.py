"""Code saves: the editor's text becomes bytes in the file's own encoding and newline.

The editor holds LF breaks and no BOM. `encode_text` puts both back as the read reported them,
so a file that is not edited keeps its bytes. `CodeSaver` refuses everything that needs no write
(ref, workspace, trust, encoding, size) and then hands the bytes to the journalled
publication service, which owns the hash check and the write.
"""

from __future__ import annotations

import codecs
import hashlib
from dataclasses import dataclass
from typing import Literal

from anyio.to_thread import run_sync

from viki_agent.code.code_files import CodeError
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    MutationError,
    code_ref,
)
from viki_agent.vaults.vault_publication import REQUEST_LEASE_WAIT_S, VaultPublicationService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

Encoding = Literal["utf-8", "utf-8-bom", "utf-16-le", "utf-16-be"]
Newline = Literal["lf", "crlf", "cr", "none"]

# encoding -> (byte-order mark, codec of the text after it)
_ENCODINGS = {
    "utf-8": (b"", "utf-8"),
    "utf-8-bom": (codecs.BOM_UTF8, "utf-8"),
    "utf-16-le": (codecs.BOM_UTF16_LE, "utf-16-le"),
    "utf-16-be": (codecs.BOM_UTF16_BE, "utf-16-be"),
}
# A file with no break yet gets LF when the editor adds one.
_BREAKS = {"lf": "\n", "crlf": "\r\n", "cr": "\r", "none": "\n"}


@dataclass(frozen=True)
class SaveResult:
    operation_id: str
    status: Literal["saved", "no_change", "conflict"]
    sha256: str | None
    cleanup_pending: bool


def encode_text(text: str, *, encoding: Encoding, newline: Newline) -> bytes:
    if encoding not in _ENCODINGS or newline not in _BREAKS:
        raise CodeError("invalid_body")
    bom, codec = _ENCODINGS[encoding]
    # CRLF first, so its CR is not read again as a lone CR.
    body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", _BREAKS[newline])
    try:
        return bom + body.encode(codec)  # strict: a lone surrogate has no encoding
    except UnicodeEncodeError:
        raise CodeError("invalid_content") from None


class CodeSaver:
    def __init__(self, workspaces: WorkspaceService, publications: VaultPublicationService) -> None:
        self._workspaces = workspaces
        self._publications = publications

    async def save(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        text: str,
        *,
        encoding: Encoding,
        newline: Newline,
        expected_sha256: str | None,
    ) -> SaveResult:
        """`MutationError` and `PublicationError` from the service pass through unchanged."""
        try:
            code_ref(ref)
        except MutationError as error:
            if error.code == "invalid_ref":
                raise CodeError("invalid_path") from None
            if error.code == "excluded_path":
                raise CodeError("private_path") from None
            raise
        try:
            workspace = await run_sync(lambda: self._workspaces.get(workspace_id, kind="code"))
        except WorkspaceError:
            raise CodeError("unknown_workspace") from None
        if workspace.policy != "ordinary":
            raise CodeError("trust_required")
        content = encode_text(text, encoding=encoding, newline=newline)
        if len(content) > MAX_PUBLICATION_BYTES:
            raise CodeError("too_large")
        outcome = await self._publications.save_code(
            workspace_id,
            policy_revision,
            root_physical_id,
            ref,
            content,
            expected_sha256=expected_sha256,
            # A worktree cleanup or a branch switch holds the folder's lease while Git runs.
            # Unbounded, the save could land after the editor's request had timed out.
            wait_s=REQUEST_LEASE_WAIT_S,
        )
        # `recovered` and `blocked` are what recovery answers, never a save.
        assert outcome.status in ("saved", "no_change", "conflict")
        sha256 = None if outcome.status == "conflict" else hashlib.sha256(content).hexdigest()
        return SaveResult(outcome.operation_id, outcome.status, sha256, outcome.cleanup_pending)
