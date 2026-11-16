"""Private image intake, immutable blob reads and reference-aware collection."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import stat
import sys
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from anyio import to_thread

from viki_agent import compiled, osfs, protection, storage, winproc
from viki_agent.conversations.drafts import require_private_store
from viki_agent.media import media_worker
from viki_agent.storage import AssetRow, SessionStore, StateError
from viki_agent.turn_input import IMAGE_MEDIA_TYPES, MAX_ATTACHMENT_BYTES, MAX_PIXELS, Attachment

logger = logging.getLogger(__name__)

MAX_SCOPE_BYTES = 256 << 20
MAX_SCOPE_ASSETS = 4096
MAX_WORKER_OUTPUT = 2048
WORKER_TIMEOUT_S = 12.0
WORKER_MEMORY_BYTES = 768 << 20
READ_CHUNK_BYTES = 64 << 10
UNREFERENCED_LIMIT = 100
MAX_SCOPES_PER_PASS = 16
ORPHAN_TTL = timedelta(hours=24)
SENT_LOCAL_COPY_TTL = timedelta(days=30)


class AssetError(RuntimeError):
    """A typed refusal that never contains a path, filename or decoder exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UploadedAsset:
    asset_id: str
    media_type: str
    byte_size: int
    width: int
    height: int
    name: str


@dataclass(frozen=True)
class DecodedImage:
    media_type: str
    width: int
    height: int


@dataclass(frozen=True)
class AssetDownload:
    media_type: str
    byte_size: int
    body: AsyncIterator[bytes]


@dataclass(frozen=True)
class AssetCollection:
    orphan_rows: int
    expired_rows: int
    files_removed: int


Decoder = Callable[[Path], Awaitable[DecodedImage]]


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _asset_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise AssetError("invalid_asset")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise AssetError("invalid_asset") from None
    if parsed != value:
        raise AssetError("invalid_asset")
    return value


def _display_name(value: str) -> str:
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeError:
        raise AssetError("invalid_name") from None
    if (
        not isinstance(value, str)
        or not value
        or len(encoded) > 512
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise AssetError("invalid_name")
    return value


def _worker_answer(stdout: bytes) -> dict[str, Any] | None:
    """The worker's small JSON reply, or None when it is missing, oversized or malformed."""
    if len(stdout) > MAX_WORKER_OUTPUT:
        return None
    try:
        answer = json.loads(stdout.decode("ascii"))
    except UnicodeError, ValueError, RecursionError:
        return None
    return answer if isinstance(answer, dict) else None


async def _drop(stream: asyncio.StreamReader) -> None:
    while await stream.read(READ_CHUNK_BYTES):
        pass


async def read_worker_output(child: winproc.Child, limit: int) -> bytes:
    """A worker's stdout, at most `limit + 1` bytes, once the worker has been reaped.

    A worker that writes past the bound is killed rather than read whole, and its stderr is
    drained and dropped so it can never block on a full pipe."""
    process = child.process
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    stdin.close()
    draining = asyncio.create_task(_drop(stderr))
    try:
        output = bytearray()
        while len(output) <= limit and (chunk := await stdout.read(limit + 1 - len(output))):
            output += chunk
        if len(output) > limit:
            child.kill_tree()
            # A full reader pauses its transport, which then sees EOF only once drained, and
            # wait() returns only after every pipe closes. The tree is dead: the rest is bounded.
            await _drop(stdout)
        await process.wait()
        await draining
        return bytes(output)
    finally:
        draining.cancel()


def _scope(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AssetError("invalid_scope")
    return value


def _row(row: Mapping[str, Any]) -> AssetRow:
    fields = {
        "scope",
        "asset_id",
        "media_type",
        "byte_size",
        "sha256",
        "width",
        "height",
        "created_at",
        "local_state",
        "local_removed_at",
    }
    if set(row) != fields:
        raise AssetError("corrupt")
    if (
        row["media_type"] not in IMAGE_MEDIA_TYPES
        or type(row["byte_size"]) is not int
        or not 1 <= row["byte_size"] <= MAX_ATTACHMENT_BYTES
        or type(row["width"]) is not int
        or type(row["height"]) is not int
        or row["width"] < 1
        or row["height"] < 1
        or row["width"] * row["height"] > MAX_PIXELS
        or not isinstance(row["sha256"], str)
        or len(row["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in row["sha256"])
        or row["local_state"] not in {"stored", "expired"}
        or (row["local_state"] == "stored" and row["local_removed_at"] is not None)
        or (
            row["local_state"] == "expired"
            and (not isinstance(row["local_removed_at"], str) or len(row["local_removed_at"]) != 24)
        )
    ):
        raise AssetError("corrupt")
    _asset_id(row["asset_id"])
    return dict(row)


class AssetService:
    """One launch's asset boundary, fixed to the active vault's draft scope."""

    def __init__(
        self,
        root: Path,
        store: SessionStore,
        scope: str,
        *,
        decoder: Decoder | None = None,
    ) -> None:
        self.root = Path(root)
        self._store = store
        self._scope = _scope(scope)
        self._decoder = decoder or self._decode_in_worker
        self._incoming = self.root / "incoming"
        self._blobs = self.root / "blobs" / self._scope
        self._prepare_lock = asyncio.Lock()
        self._prepared = False
        self._reservation_lock = asyncio.Lock()
        self._reserved_bytes = 0
        self._reserved_count = 0
        self._relinking: set[str] = set()
        self._sweep_cursor = str(uuid.uuid4())
        self._history_metadata: Callable[[str], Attachment | None] | None = None

    def bind_history_metadata(self, resolver: Callable[[str], Attachment | None]) -> None:
        self._history_metadata = resolver

    def _prepare(self) -> None:
        if not self.root.parent.is_dir():
            raise AssetError("unavailable")
        try:
            protection.require_private_directory(self.root.parent)
            for directory in (self.root, self._incoming, self.root / "blobs", self._blobs):
                if not directory.exists():
                    protection.create_private_directory(directory)
                protection.require_private_directory(directory)
                storage.check_file(directory / ".viki-asset-boundary")
        except OSError, StateError, protection.ProtectionError:
            raise AssetError("protection") from None

    async def _ready(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await to_thread.run_sync(self._prepare)
            self._prepared = True

    def _private_store(self) -> None:
        try:
            require_private_store(self._store)
        except protection.ProtectionError:
            raise AssetError("protection") from None

    def _path(self, asset_id: str) -> Path:
        return self._blobs / _asset_id(asset_id)

    async def _store_call[T](self, call: Callable[[], T]) -> T:
        def checked() -> T:
            self._private_store()
            try:
                return call()
            except StateError as exc:
                if exc.code == "not_found":
                    raise AssetError("not_found") from None
                raise AssetError(exc.code) from None

        return await to_thread.run_sync(checked)

    async def _reserve(self, requested: int | None, limit: int) -> int:
        if requested is not None and not 1 <= requested <= limit:
            raise AssetError("too_large" if requested > limit else "empty")
        # Content-Length is caller-controlled. Reserve the maximum possible body so concurrent
        # uploads cannot exceed the scope budget by declaring fewer bytes than they stream.
        amount = limit
        async with self._reservation_lock:
            count, used = await self._store_call(lambda: self._store.asset_usage(self._scope))
            if (
                count + self._reserved_count >= MAX_SCOPE_ASSETS
                or used + self._reserved_bytes + amount > MAX_SCOPE_BYTES
            ):
                raise AssetError("capacity")
            self._reserved_count += 1
            self._reserved_bytes += amount
        return amount

    async def _release(self, amount: int) -> None:
        async with self._reservation_lock:
            self._reserved_count -= 1
            self._reserved_bytes -= amount

    async def upload(
        self,
        chunks: AsyncIterable[bytes],
        *,
        name: str,
        declared_bytes: int | None,
        max_bytes: int | None = None,
    ) -> UploadedAsset:
        """Store one image of at most `max_bytes` (the attachment cap when None)."""
        await self._ready()
        display_name = _display_name(name)

        async def put(row: AssetRow) -> None:
            await self._store_call(lambda: self._store.put_asset(row))

        row = await self._intake(
            chunks,
            declared_bytes=declared_bytes,
            limit=MAX_ATTACHMENT_BYTES if max_bytes is None else max_bytes,
            asset_id=str(uuid.uuid4()),
            publish=put,
        )
        return UploadedAsset(
            asset_id=row["asset_id"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            width=row["width"],
            height=row["height"],
            name=display_name,
        )

    async def relink(
        self, asset_id: str, chunks: AsyncIterable[bytes], *, declared_bytes: int | None
    ) -> None:
        """Bring back an expired asset's bytes, but only from the very file that was sent."""
        asset_id = _asset_id(asset_id)
        if asset_id in self._relinking:
            raise AssetError("busy")
        # One relink per id in this process, so a matching blob `_intake` finds is left by a dead
        # relink, a cleanup that could not unlink, or a failed expiry unlink, never a running move.
        self._relinking.add(asset_id)
        try:
            row = await self._metadata(asset_id)
            if row["local_state"] == "stored":
                # Already restored (a retry after a lost response): nothing to read or reserve.
                return
            await self._ready()

            async def restore(fresh: AssetRow) -> None:
                # The new file's own identity goes to the store, whose WHERE checks it again.
                restored = await self._store_call(
                    lambda: self._store.restore_expired_asset(fresh, fresh["created_at"])
                )
                if not restored:
                    raise AssetError("asset_changed")

            await self._intake(
                chunks,
                declared_bytes=declared_bytes,
                limit=MAX_ATTACHMENT_BYTES,
                asset_id=asset_id,
                publish=restore,
                expected=row,
            )
        finally:
            self._relinking.discard(asset_id)

    async def _intake(
        self,
        chunks: AsyncIterable[bytes],
        *,
        declared_bytes: int | None,
        limit: int,
        asset_id: str,
        publish: Callable[[AssetRow], Awaitable[None]],
        expected: AssetRow | None = None,
    ) -> AssetRow:
        """Reserve, stage, check and decode one image, move it to `asset_id`'s blob, then publish.

        `limit` bounds the declared and the streamed size. `expected` refuses any file whose size
        or digest (before the decoder runs), type or dimensions differ from it, and adopts a blob
        already at the path that matches it exactly instead of moving the stage. The stage, the
        reservation and, unless `publish` succeeded, the moved blob are always cleaned up; a
        cancel lets a started move and `publish` finish first.
        """
        reserved = await self._reserve(declared_bytes, limit)
        stage = self._incoming / str(uuid.uuid4())
        final: Path | None = None
        publication: asyncio.Future[None] | None = None
        handle: Any = None
        size = 0
        digest = hashlib.sha256()

        async def cleanup() -> None:
            if handle is not None:
                await to_thread.run_sync(handle.close)
            published = False
            if publication is not None:
                # A cancel ends only our await: the store's thread still commits. The outcome
                # of the publication, not the cancel, decides whether the row owns the blob.
                await asyncio.wait({publication})
                published = not publication.cancelled() and publication.exception() is None
            # A blob at `final` is ours only when our stage is gone, that is, when our rename
            # moved it there; a relink that found another file in place never removes it.
            moved = final is not None and not await to_thread.run_sync(stage.exists)
            for leftover in (stage, final):
                # The published row is the publication point. Once it exists, final belongs to
                # collect; otherwise this call owns the file it moved and removes it.
                if (
                    leftover is not None
                    and (leftover != final or (moved and not published))
                    and await to_thread.run_sync(leftover.exists)
                ):
                    with contextlib.suppress(OSError):
                        await to_thread.run_sync(leftover.unlink)
            await self._release(reserved)

        try:
            handle = await to_thread.run_sync(osfs.open_new_private, stage)
            try:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes | bytearray | memoryview):
                        raise AssetError("invalid_body")
                    if len(chunk) > limit - size:
                        raise AssetError("too_large")
                    data = bytes(chunk)
                    if not data:
                        continue
                    await to_thread.run_sync(handle.write, data)
                    digest.update(data)
                    size += len(data)
                if size == 0:
                    raise AssetError("empty")
                if declared_bytes is not None and size != declared_bytes:
                    raise AssetError("incomplete")
                await to_thread.run_sync(handle.flush)
                await to_thread.run_sync(os.fsync, handle.fileno())
            finally:
                await to_thread.run_sync(handle.close)
                handle = None
            if expected is not None and (
                size != expected["byte_size"] or digest.hexdigest() != expected["sha256"]
            ):
                # Settled by the bytes alone: another file never reaches the decoder.
                raise AssetError("asset_mismatch")
            await to_thread.run_sync(protection.require_private_file, stage)
            decoded = await self._decoder(stage)
            if (
                decoded.media_type not in IMAGE_MEDIA_TYPES
                or type(decoded.width) is not int
                or type(decoded.height) is not int
                or decoded.width < 1
                or decoded.height < 1
                or decoded.width * decoded.height > MAX_PIXELS
            ):
                raise AssetError("decode_failed")
            row: AssetRow = {
                "scope": self._scope,
                "asset_id": asset_id,
                "media_type": decoded.media_type,
                "byte_size": size,
                "sha256": digest.hexdigest(),
                "width": decoded.width,
                "height": decoded.height,
                "created_at": _stamp(),
            }
            if expected is not None and any(
                row[field] != expected[field] for field in ("media_type", "width", "height")
            ):
                raise AssetError("asset_mismatch")
            blob = self._path(asset_id)
            if await to_thread.run_sync(storage.check_file, blob):
                if expected is None:
                    raise AssetError("unavailable")
                try:
                    await to_thread.run_sync(self._read_checked, expected)
                except AssetError:
                    # A different file is never replaced.
                    raise AssetError("unavailable") from None
                # The sent bytes, left by a dead relink or an expiry whose unlink failed: publish
                # them in place; cleanup waits for it, and `final` stays unset, so the file stays.
                publication = asyncio.ensure_future(publish(row))
            else:
                final = blob

                def move(blob: Path) -> None:
                    # A blob that appeared after the check above is refused, never replaced.
                    osfs.rename_no_replace(stage, blob)
                    osfs.fsync_dir(blob.parent)

                async def move_then_publish(blob: Path) -> None:
                    # Part of the publication, so a cancel's cleanup waits for the rename too and
                    # reads `moved` only once the stage is either still in place or gone for good.
                    await to_thread.run_sync(move, blob)
                    await to_thread.run_sync(protection.require_private_file, blob)
                    await publish(row)

                publication = asyncio.ensure_future(move_then_publish(final))
            # Not `asyncio.shield`: once its await is cancelled, Python 3.14 hands a later
            # failure of the publication to the loop's exception handler as well.
            await asyncio.wait({publication})
            publication.result()
            return row
        except AssetError:
            raise
        except OSError, StateError, protection.ProtectionError:
            raise AssetError("unavailable") from None
        finally:
            # The route deadline delivers one cancel wherever the upload is, this cleanup
            # included; shielded, it still releases the reservation and removes the stage.
            cleaning = asyncio.ensure_future(cleanup())
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                await asyncio.shield(cleaning)
                raise

    async def _decode_in_worker(self, stage: Path) -> DecodedImage:
        worker = Path(__file__).with_name("media_worker.py")
        env = media_worker._worker_env(sys.platform, os.environ)
        try:
            child = await winproc.spawn(
                compiled.worker_argv(
                    "media",
                    [
                        *media_worker._worker_argv_prefix(sys.platform, WORKER_MEMORY_BYTES),
                        stage.name,
                    ],
                    python=sys.executable,
                    script=worker,
                ),
                cwd=self._incoming,
                env=env,
                memory_limit_bytes=WORKER_MEMORY_BYTES,
            )
        except OSError:
            raise AssetError("worker_unavailable") from None
        communication = asyncio.create_task(read_worker_output(child, MAX_WORKER_OUTPUT))
        try:
            try:
                async with asyncio.timeout(WORKER_TIMEOUT_S):
                    stdout = await asyncio.shield(communication)
            except TimeoutError:
                child.kill_tree()
                await communication
                raise AssetError("worker_timeout") from None
            except asyncio.CancelledError:
                # Releasing a live kill-on-close Job terminates it, but explicitly terminate and
                # reap the pipe transport first so shutdown never leaves an unobserved process.
                child.kill_tree()
                await asyncio.shield(communication)
                raise
            code = child.process.returncode
            if code == media_worker.DECODER_UNAVAILABLE_EXIT_CODE:
                # A broken install, an interrupted upgrade or an antivirus quarantine: a
                # storage problem, never a verdict on the image, so it must not read as
                # "choose a different picture".
                logger.warning("asset worker exited: %s", code)
                raise AssetError("decoder_unavailable")
            answer = _worker_answer(stdout)
            if code == 0:
                if (
                    answer is not None
                    and set(answer) == {"ok", "media_type", "width", "height"}
                    and answer["ok"] is True
                ):
                    return DecodedImage(
                        media_type=answer["media_type"],
                        width=answer["width"],
                        height=answer["height"],
                    )
                raise AssetError("decode_failed")
            if (
                answer is not None
                and set(answer) == {"ok", "code"}
                and answer["ok"] is False
                and answer["code"] == "decode_failed"
            ):
                raise AssetError("decode_failed")
            # A killed worker (an external kill: antivirus, Task Manager; or a native crash;
            # our own timeout kill already returned above) or any other unexpected exit with no
            # valid JSON body is a worker problem, not a verdict on the image.
            logger.warning("asset worker exited: %s", code)
            raise AssetError("unavailable")
        finally:
            child.release()

    async def inspect_bytes(self, data: bytes) -> DecodedImage:
        """Validate an embedded image without publishing it as a resolvable local asset."""
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_ATTACHMENT_BYTES:
            raise AssetError("too_large" if data else "empty")
        await self._ready()
        stage = self._incoming / str(uuid.uuid4())

        def write() -> None:
            with osfs.open_new_private(stage) as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            protection.require_private_file(stage)

        try:
            await to_thread.run_sync(write)
            return await self._decoder(stage)
        except AssetError:
            raise
        except OSError, protection.ProtectionError:
            raise AssetError("unavailable") from None
        finally:
            with contextlib.suppress(OSError):
                await to_thread.run_sync(stage.unlink, True)

    async def _metadata(self, asset_id: str) -> AssetRow:
        rows = await self._store_call(
            lambda: self._store.asset_metadata(self._scope, (_asset_id(asset_id),))
        )
        if len(rows) != 1:
            raise AssetError("not_found")
        row = _row(rows[0])
        if row["scope"] != self._scope:
            raise AssetError("corrupt")
        return row

    def find_by_digest(
        self, sha256: str, media_type: str, byte_size: int, display_name: str
    ) -> Attachment | None:
        """Return one verified local asset for history, without publishing or retaining anything."""
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or media_type not in IMAGE_MEDIA_TYPES
            or type(byte_size) is not int
            or not 1 <= byte_size <= MAX_ATTACHMENT_BYTES
        ):
            return None
        try:
            name = _display_name(display_name)
            self._private_store()
            rows = self._store.find_assets_by_digest(self._scope, sha256, media_type, byte_size)
        except StateError as exc:
            raise AssetError(exc.code) from None
        except protection.ProtectionError:
            raise AssetError("protection") from None
        for raw in rows:
            row = _row(raw)
            if row["scope"] != self._scope:
                raise AssetError("corrupt")
            if row["local_state"] == "stored":
                try:
                    self._read_checked(row)
                except AssetError:
                    continue
            original = self._history_metadata(row["asset_id"]) if self._history_metadata else None
            if original is not None and (
                original.asset_id == row["asset_id"]
                and original.media_type == row["media_type"]
                and original.byte_size == row["byte_size"]
                and original.width == row["width"]
                and original.height == row["height"]
            ):
                return original
            return Attachment(
                asset_id=row["asset_id"],
                media_type=row["media_type"],
                byte_size=row["byte_size"],
                width=row["width"],
                height=row["height"],
                name=name,
                source="history",
            )
        return None

    def _read_checked(self, row: AssetRow) -> bytes:
        path = self._path(row["asset_id"])
        try:
            if not storage.check_file(path):
                raise AssetError("missing")
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != row["byte_size"]
            ):
                raise AssetError("corrupt")
            protection.require_private_file(path)
            with path.open("rb") as source:
                data = source.read(MAX_ATTACHMENT_BYTES + 1)
        except FileNotFoundError:
            raise AssetError("missing") from None
        except OSError, StateError, protection.ProtectionError:
            raise AssetError("unavailable") from None
        if len(data) != row["byte_size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise AssetError("corrupt")
        return data

    async def resolve(self, attachment: Attachment) -> bytes:
        row = await self._metadata(attachment.asset_id)
        if row["local_state"] != "stored":
            raise AssetError("expired")
        if (
            row["media_type"] != attachment.media_type
            or row["byte_size"] != attachment.byte_size
            or row["width"] != attachment.width
            or row["height"] != attachment.height
        ):
            raise AssetError("asset_changed")
        await self._ready()
        return await to_thread.run_sync(self._read_checked, row)

    async def read_for_vault(self, asset_id: str) -> tuple[bytes, str, str]:
        """A stored asset's bytes, verified against its row, with the row's digest and type.

        An id's bytes never change (a relink adopts only the row's own digest), so the id alone
        names them. Every refusal is `unavailable`: keep-in-vault copies nothing it cannot verify.
        """
        try:
            row = await self._metadata(asset_id)
            if row["local_state"] != "stored":
                raise AssetError("unavailable")
            await self._ready()
            data = await to_thread.run_sync(self._read_checked, row)
        except AssetError:
            raise AssetError("unavailable") from None
        return data, row["sha256"], row["media_type"]

    async def download(self, asset_id: str) -> AssetDownload:
        row = await self._metadata(asset_id)
        if row["local_state"] != "stored":
            raise AssetError("expired")
        await self._ready()
        # Validate the digest before sending response headers. The following stream reopens the
        # same immutable app-owned ID; same-user replacement is outside the confidentiality
        # boundary, and the size remains bounded on every read.
        await to_thread.run_sync(self._read_checked, row)

        async def body() -> AsyncIterator[bytes]:
            path = self._path(row["asset_id"])
            try:
                handle = await to_thread.run_sync(lambda: path.open("rb"))
            except OSError:
                raise AssetError("unavailable") from None
            sent = 0
            try:
                while sent < row["byte_size"]:
                    try:
                        chunk = await to_thread.run_sync(handle.read, READ_CHUNK_BYTES)
                    except OSError:
                        raise AssetError("unavailable") from None
                    if not chunk:
                        raise AssetError("corrupt")
                    sent += len(chunk)
                    if sent > row["byte_size"]:
                        raise AssetError("corrupt")
                    yield chunk
            finally:
                await to_thread.run_sync(handle.close)

        return AssetDownload(row["media_type"], row["byte_size"], body())

    async def remove(self, asset_id: str) -> bool:
        await self._ready()
        asset_id = _asset_id(asset_id)
        deleted = await self._store_call(
            lambda: self._store.delete_asset_if_unreferenced(self._scope, asset_id)
        )
        if not deleted:
            # Distinguish an existing referenced asset from a missing id without exposing refs.
            try:
                await self._metadata(asset_id)
            except AssetError as exc:
                if exc.code == "not_found":
                    return False
                raise
            raise AssetError("in_use")
        try:
            await to_thread.run_sync(self._path(asset_id).unlink, True)
        except OSError:
            raise AssetError("unavailable") from None
        return True

    async def collect_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = UNREFERENCED_LIMIT,
    ) -> AssetCollection:
        """Run one bounded retention pass; callers schedule startup and periodic passes.

        Unreferenced uploads lose both metadata and bytes after 24 hours. Assets referenced
        only by old sent commands keep their immutable metadata but become unavailable after
        30 days. Any draft, queued work or future non-command owner blocks that transition.
        """
        if type(limit) is not int or not 1 <= limit <= UNREFERENCED_LIMIT:
            raise AssetError("invalid_retention")
        instant = datetime.now(UTC) if now is None else now
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise AssetError("invalid_retention")
        instant = instant.astimezone(UTC)
        orphan_before = (
            (instant - ORPHAN_TTL).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        sent_before = (
            (instant - SENT_LOCAL_COPY_TTL)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        removed_at = instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        await self._ready()
        orphan_rows = 0
        files_removed = 0
        orphans = await self._store_call(
            lambda: self._store.list_unreferenced_assets(self._scope, orphan_before, limit)
        )
        for raw in orphans:
            row = _row(raw)
            deleted = await self._store_call(
                lambda row=row: self._store.delete_asset_if_unreferenced(
                    self._scope, row["asset_id"], created_before=orphan_before
                )
            )
            if not deleted:
                continue
            orphan_rows += 1
            if await self._unlink(row["asset_id"]):
                files_removed += 1

        candidates = await self._store_call(
            lambda: self._store.list_stored_command_assets(self._scope, sent_before, limit)
        )
        expired_rows = 0
        for raw in candidates:
            row = _row(raw)
            expired = await self._store_call(
                lambda row=row: self._store.expire_asset_if_due(
                    self._scope, row["asset_id"], sent_before, removed_at
                )
            )
            if expired:
                expired_rows += 1
                # The tombstone commits first, so a concurrent resolver refuses it before the
                # blob is gone. Unlinking each row as soon as it tombstones (rather than
                # re-listing tombstones afterwards) keeps a scope with more expired rows than
                # one pass's limit from leaving later expiries' blobs on disk forever.
                if await self._unlink(row["asset_id"]):
                    files_removed += 1

        # If a prior process committed an orphan-row delete, a tombstone, or died before
        # unlinking, no DB query can name that file again by row state alone. Only old regular
        # UUID blobs are considered, which excludes every new publication; metadata is rechecked
        # before treating an id as an untracked orphan. A relink may adopt an old leftover blob
        # of an expired row in place, so an id this process is relinking is skipped, and the pass
        # claims each id it checks: a relink started between its row read and its unlink would
        # commit a stored row whose blob this pass then removes, so that relink answers busy.
        stale_ids = await to_thread.run_sync(self._stale_untracked_ids, instant - ORPHAN_TTL, limit)
        for asset_id in stale_ids:
            if asset_id in self._relinking:
                continue
            self._relinking.add(asset_id)
            try:
                try:
                    row = await self._metadata(asset_id)
                except AssetError as exc:
                    if exc.code != "not_found":
                        # The cursor already moved past this id (_stale_untracked_ids). Skip it
                        # rather than abort the pass, so rotation revisits it instead of pinning
                        # the whole pass on one row that keeps failing.
                        continue
                    if await self._unlink(asset_id):
                        files_removed += 1
                    continue
                if row["local_state"] == "expired" and await self._unlink(asset_id):
                    files_removed += 1
            finally:
                self._relinking.discard(asset_id)

        files_removed += await to_thread.run_sync(
            self._sweep_incoming_stages, instant - ORPHAN_TTL, limit
        )
        return AssetCollection(orphan_rows, expired_rows, files_removed)

    def _stale_untracked_ids(self, before: datetime, limit: int) -> list[str]:
        try:
            # Path.iterdir() lists every entry before it yields the first one (CPython 3.14
            # Lib/pathlib/__init__.py: entries = list(scandir_it)), so this is already one
            # scandir per pass, bounded by the directory's size; islice below only caps the
            # names examined per pass, not the directory read.
            names = sorted(entry.name for entry in self._blobs.iterdir())
        except OSError:
            return []
        # Resume after the last name examined and wrap around. Stored blobs are examined and
        # kept, so a fixed first page of them would hide every later lost unlink; rotating
        # rechecks each blob within ceil(N / limit) passes within one launch. Across launches
        # the cursor starts at a random name (see __init__), so no fixed page is ever pinned.
        cursor = self._sweep_cursor
        rotated = [name for name in names if name > cursor] + [
            name for name in names if name <= cursor
        ]
        found: list[str] = []
        for name in itertools.islice(rotated, MAX_SCOPE_ASSETS + limit):
            if len(found) >= limit:
                break
            self._sweep_cursor = name
            try:
                asset_id = _asset_id(name)
                info = (self._blobs / name).lstat()
            except AssetError, OSError:
                continue
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                and info.st_mtime < before.timestamp()
            ):
                found.append(asset_id)
        return found

    def _sweep_incoming_stages(self, before: datetime, limit: int) -> int:
        """Remove stale upload stages in one call; a live upload never lingers past its request.

        A stage with two links is a crash between rename_no_replace's fallback hard link and
        its source unlink: the blob already lives at its final name, so unlinking the stage
        name only drops the stray extra link. Three or more links belong to something else and
        are kept.
        """
        removed = 0
        try:
            entries = self._incoming.iterdir()
            for scanned, entry in enumerate(entries):
                if removed >= limit or scanned >= MAX_SCOPE_ASSETS + limit:
                    break
                try:
                    _asset_id(entry.name)
                    info = entry.lstat()
                except AssetError, OSError:
                    continue
                if not (
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink in (1, 2)
                    and not (
                        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    )
                    and info.st_mtime < before.timestamp()
                ):
                    continue
                try:
                    protection.require_private_file(entry)
                    entry.unlink()
                except OSError, protection.ProtectionError:
                    continue
                removed += 1
        except OSError:
            return removed
        return removed

    async def _unlink(self, asset_id: str) -> bool:
        def remove() -> bool:
            try:
                self._path(asset_id).unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError:
                return False

        return await to_thread.run_sync(remove)


async def collect_scopes(
    root: Path,
    store: SessionStore,
    services: dict[str, AssetService],
    after: str,
    *,
    skip: str = "",
) -> str:
    """Run one retention pass for the next scopes with asset rows, strictly after `after`.

    Scopes other than the active vault's (an earlier vault, a chat-only launch, a moved vault)
    are collected here, a bounded page per call. `services` caches one service per scope across
    passes; `skip` names the scope the caller collects itself. Returns the cursor for the next
    call: the last scope read, or "" once the page came back short and the walk starts over.
    """
    scopes = await to_thread.run_sync(store.asset_scopes, after, MAX_SCOPES_PER_PASS)
    for scope in scopes:
        if scope == skip:
            continue
        service = services.get(scope)
        if service is None:
            try:
                service = AssetService(root, store, scope)
            except AssetError:
                continue  # not a scope this app writes; nothing here can collect it
            services[scope] = service
        try:
            await service.collect_due()
        except Exception:
            # No scope and no path: one failing scope must not stop the others' retention.
            logger.warning("asset retention pass unavailable")
    return scopes[-1] if len(scopes) == MAX_SCOPES_PER_PASS else ""
