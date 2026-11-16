"""Keep in vault: copy one verified chat attachment to `attachments/<leaf>`.

The one non-note file the app writes, create-only, through the journalled publication service
under hard rule 1. The bytes come from the asset store, verified against the asset's SHA-256 there
and re-hashed here; nothing records where a kept file came from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from anyio.to_thread import run_sync

from viki_agent.media.assets import AssetError, AssetService
from viki_agent.turn_input import MAX_ATTACHMENT_BYTES
from viki_agent.vaults.vault_mutation import (
    ATTACHMENT_SUFFIXES,
    MutationCoordinator,
    MutationError,
    VaultPublication,
    attachment_ref,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    ResolveRoot,
    VaultPublicationService,
)
from viki_agent.workspaces import WorkspaceError, WorkspaceService

FOLDER = "attachments"
# The suffix is the sniffed type's, whatever the name says.
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_REFUSED_POLICIES = {"protected": "vault_protected", "existing_readonly": "vault_read_only"}
_UNNAMED = "attachment"


class AttachmentError(RuntimeError):
    """A path-free keep refusal; `operation_id` names a publication row left for recovery."""

    def __init__(self, code: str, operation_id: str | None = None) -> None:
        self.code = code
        self.operation_id = operation_id
        super().__init__(code)


@dataclass(frozen=True)
class KeptAttachment:
    ref: str
    reused: bool
    byte_size: int


def _stem(name: str) -> str:
    if not isinstance(name, str):
        raise AttachmentError("invalid_name")
    stem = name.strip()
    head, dot, tail = stem.rpartition(".")
    if dot and f".{tail}".casefold() in ATTACHMENT_SUFFIXES:
        stem = head
    return stem or _UNNAMED


def _candidate_refs(name: str, sha256: str, media_type: str) -> tuple[str, str]:
    """The plain name, then the hash-suffixed one for when another file holds it."""
    stem, suffix = _stem(name), _SUFFIXES[media_type]
    refs = (f"{FOLDER}/{stem}{suffix}", f"{FOLDER}/{stem}-{sha256[:12]}{suffix}")
    try:
        for ref in refs:
            attachment_ref(ref)
    except MutationError:
        raise AttachmentError("invalid_name") from None
    return refs


class VaultAttachmentService:
    """`coordinator` and `resolve_root` must be the ones `publications` holds, so the reuse probe's
    lease excludes the same writers as the save's."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        assets: AssetService,
        publications: VaultPublicationService,
        resolve_root: ResolveRoot,
        coordinator: MutationCoordinator,
        *,
        lease_wait_s: float,
        native: VaultPublication | None = None,
    ) -> None:
        self._workspaces = workspaces
        self._assets = assets
        self._publications = publications
        self._resolve_root = resolve_root
        self._coordinator = coordinator
        self._lease_wait_s = lease_wait_s
        self._native = native or VaultPublication()

    async def keep(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        asset_id: str,
        name: str,
    ) -> KeptAttachment:
        await run_sync(self._require_writable, workspace_id)
        try:
            data, sha256, media_type = await self._assets.read_for_vault(asset_id)
        except AssetError:
            raise AttachmentError("asset_unavailable") from None
        # The asset store verified these bytes against its row; they enter the vault only if
        # they still hash to it here.
        if await run_sync(lambda: hashlib.sha256(data).hexdigest()) != sha256:
            raise AttachmentError("asset_unavailable")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("too_large")
        refs = _candidate_refs(name, sha256, media_type)
        try:
            ref, reused = await self._pick(
                workspace_id, policy_revision, root_physical_id, refs, sha256
            )
            if reused:
                return KeptAttachment(ref, True, len(data))
            outcome = await self._publications.save(
                workspace_id,
                policy_revision,
                root_physical_id,
                ref,
                data,
                expected_sha256=None,
                create_folders=True,
                wait_s=self._lease_wait_s,
            )
        except PublicationError as exc:
            raise AttachmentError(exc.code, exc.operation_id) from None
        except MutationError as exc:
            raise AttachmentError(exc.code) from None
        if outcome.status == "conflict":
            # Another writer created the name between the probe and the save; create-only kept it.
            raise AttachmentError("conflict", outcome.operation_id)
        return KeptAttachment(ref, False, len(data))

    def _require_writable(self, workspace_id: str) -> None:
        try:
            policy = self._workspaces.get(workspace_id).policy
        except WorkspaceError:
            raise AttachmentError("unknown_workspace") from None
        if policy in _REFUSED_POLICIES:
            raise AttachmentError(_REFUSED_POLICIES[policy])

    async def _pick(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        refs: tuple[str, str],
        sha256: str,
    ) -> tuple[str, bool]:
        """The first candidate already holding these bytes (reused) or absent (to create)."""
        root = await run_sync(self._resolve_root, workspace_id, policy_revision, root_physical_id)
        for ref in refs:
            # `save` takes its own lease, so this one is released first; its create-only publish
            # refuses a file another writer makes in between.
            async with self._coordinator.lease(root, (ref,), wait_s=self._lease_wait_s) as lease:
                existing = await run_sync(self._native.attachment_sha256, lease, root, ref)
            if existing == sha256:
                return ref, True
            if existing is None:
                return ref, False
        raise AttachmentError("conflict")
