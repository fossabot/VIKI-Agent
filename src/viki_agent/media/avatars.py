"""Private, immutable avatar transforms and authenticated variant reads."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import sqlite3
import stat
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anyio import to_thread

from viki_agent import compiled, osfs, protection, storage, winproc
from viki_agent.app import settings
from viki_agent.conversations.drafts import require_private_store
from viki_agent.media import media_worker
from viki_agent.media.assets import AssetError, AssetService, read_worker_output
from viki_agent.storage import SessionStore, StateError

logger = logging.getLogger(__name__)

AVATAR_SOURCE_BYTES = 5 << 20
MASTER_BYTES = 5 << 20
VARIANT_BYTES = 1 << 20
TOTAL_BYTES = 8 << 20
MAX_AVATARS = 256
MAX_STORED_BYTES = 512 << 20
WORKER_OUTPUT_BYTES = 4096
WORKER_TIMEOUT_SECONDS = 12.0
WORKER_MEMORY_BYTES = 384 << 20
# On Linux the worker's ceiling is an address-space limit (RLIMIT_AS), which counts mapped
# libraries and reservations, not the commit charge the Windows Job limits, so it is the
# intake worker's ceiling.
WORKER_ADDRESS_SPACE_BYTES = 768 << 20
COLLECT_LIMIT = 100
MAX_CROP_VALUE = 999_999_999  # media_worker refuses an argv number longer than nine digits.
ORPHAN_TTL = timedelta(hours=24)
VARIANT_SIZES = (24, 32, 64, 128, 256)
_OUTPUT_NAMES = ("master.png", "24.png", "32.png", "64.png", "128.png", "256.png")
_PNG = b"\x89PNG\r\n\x1a\n"
# profile_avatar_refs.avatar_id is NOT NULL, so NOT IN over it is exact (no NULL ever hides a
# row), and a plain subquery here is a one-shot list scan rather than a per-row correlated one.
_RETENTION_CANDIDATES = (
    "SELECT avatar_id FROM avatars WHERE created_at<? AND avatar_id NOT IN "
    "(SELECT avatar_id FROM profile_avatar_refs) ORDER BY created_at,avatar_id LIMIT ?"
)


class AvatarError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class AvatarFile:
    name: str
    size: int
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class Avatar:
    avatar_id: str
    master: AvatarFile
    variants: tuple[AvatarFile, ...]
    total_bytes: int
    created_at: str


@dataclass(frozen=True)
class AvatarDownload:
    byte_size: int
    sha256: str
    body: bytes


@dataclass(frozen=True)
class AvatarCollection:
    rows_removed: int
    directories_removed: int


Worker = Callable[[Path, Crop, int], Awaitable[Mapping[str, object]]]


def _stamp(instant: datetime | None = None) -> str:
    value = datetime.now(UTC) if instant is None else instant.astimezone(UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(value: str, code: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise AvatarError(code)
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise AvatarError(code) from None
    if parsed != value:
        raise AvatarError(code)
    return value


def _crop(value: Mapping[str, object]) -> Crop:
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise AvatarError("invalid_crop")
    if any(type(value[name]) is not int for name in ("x", "y", "width", "height")):
        raise AvatarError("invalid_crop")
    crop = Crop(value["x"], value["y"], value["width"], value["height"])  # type: ignore[arg-type]
    if (
        crop.x < 0
        or crop.y < 0
        or crop.width < 1
        or crop.width != crop.height
        or max(crop.x, crop.y, crop.width) > MAX_CROP_VALUE
    ):
        raise AvatarError("invalid_crop")
    return crop


def _request_hash(source_asset_id: str, crop: Crop, rotation: int) -> str:
    encoded = json.dumps(
        [source_asset_id, crop.x, crop.y, crop.width, crop.height, rotation],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


async def _run_to_end[T, *Ts](step: Callable[[*Ts], T], *args: *Ts) -> T:
    # A thread cannot be stopped: on a cancel, wait for the step to end before the cancel goes
    # on, so no cleanup runs under a step still using the files it would remove.
    running = asyncio.ensure_future(to_thread.run_sync(step, *args))
    try:
        return await asyncio.shield(running)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(running)
        raise


def _sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AvatarError("corrupt")
    return value


class AvatarService:
    def __init__(
        self,
        root: Path,
        store: SessionStore,
        source_assets: AssetService,
        *,
        worker: Worker | None = None,
    ) -> None:
        self.root = Path(root)
        self._store = store
        self._source_assets = source_assets
        self._worker = worker or self._transform_in_worker
        self._incoming = self.root / "incoming"
        self._blobs = self.root / "blobs"
        self._prepare_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._prepared = False

    def _prepare(self) -> None:
        if not self.root.parent.is_dir():
            raise AvatarError("unavailable")
        try:
            protection.require_private_directory(self.root.parent)
            for directory in (self.root, self._incoming, self._blobs):
                if not directory.exists():
                    protection.create_private_directory(directory)
                protection.require_private_directory(directory)
                storage.check_file(directory / ".viki-avatar-boundary")
        except OSError, StateError, protection.ProtectionError:
            raise AvatarError("protection") from None

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
            raise AvatarError("protection") from None

    def _checked_call[T](self, call: Callable[[], T]) -> T:
        self._private_store()
        try:
            return call()
        except StateError as exc:
            raise AvatarError(exc.code) from None

    async def _store_call[T](self, call: Callable[[], T]) -> T:
        return await to_thread.run_sync(self._checked_call, call)

    @staticmethod
    def _manifest_file(raw: object, *, expected_name: str, expected_size: int | None) -> AvatarFile:
        if not isinstance(raw, dict) or set(raw) != {"name", "size", "byte_size", "sha256"}:
            raise AvatarError("transform_failed")
        name, size, byte_size = raw["name"], raw["size"], raw["byte_size"]
        if (
            name != expected_name
            or type(size) is not int
            or type(byte_size) is not int
            or not 1 <= size <= 1024
            or (expected_size is not None and size != expected_size)
            or not 1 <= byte_size <= (MASTER_BYTES if expected_size is None else VARIANT_BYTES)
        ):
            raise AvatarError("transform_failed")
        try:
            digest = _sha(raw["sha256"])
        except AvatarError:
            raise AvatarError("transform_failed") from None
        return AvatarFile(name, size, byte_size, digest)

    def _manifest(self, raw: Mapping[str, object]) -> tuple[AvatarFile, tuple[AvatarFile, ...]]:
        if not isinstance(raw, Mapping) or set(raw) != {"ok", "master", "variants"}:
            raise AvatarError("transform_failed")
        if raw["ok"] is not True or not isinstance(raw["variants"], list):
            raise AvatarError("transform_failed")
        master = self._manifest_file(raw["master"], expected_name="master.png", expected_size=None)
        if master.size < 1 or len(raw["variants"]) != len(VARIANT_SIZES):
            raise AvatarError("transform_failed")
        variants = tuple(
            self._manifest_file(item, expected_name=f"{size}.png", expected_size=size)
            for item, size in zip(raw["variants"], VARIANT_SIZES, strict=True)
        )
        return master, variants

    @staticmethod
    def _verify_complete_directory(path: Path) -> None:
        try:
            if {entry.name for entry in path.iterdir()} != set(_OUTPUT_NAMES):
                raise AvatarError("transform_failed")
        except OSError:
            raise AvatarError("transform_failed") from None

    @staticmethod
    def _verify_output(path: Path, expected: AvatarFile) -> bytes:
        try:
            if not storage.check_file(path):
                raise AvatarError("transform_failed")
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != expected.byte_size
            ):
                raise AvatarError("transform_failed")
            protection.require_private_file(path)
            with path.open("rb") as source:
                data = source.read(expected.byte_size + 1)
        except AvatarError:
            raise
        except OSError, StateError, protection.ProtectionError:
            raise AvatarError("transform_failed") from None
        if (
            len(data) != expected.byte_size
            or not data.startswith(_PNG)
            or hashlib.sha256(data).hexdigest() != expected.sha256
        ):
            raise AvatarError("transform_failed")
        return data

    def _load(self, avatar_id: str) -> Avatar | None:
        with self._store._connection() as connection:
            row = connection.execute(
                "SELECT master_size,master_bytes,master_sha256,total_bytes,created_at "
                "FROM avatars WHERE avatar_id=?",
                (avatar_id,),
            ).fetchone()
            if row is None:
                return None
            variants = connection.execute(
                "SELECT size,byte_size,sha256 FROM avatar_variants WHERE avatar_id=? ORDER BY size",
                (avatar_id,),
            ).fetchall()
        if len(variants) != len(VARIANT_SIZES):
            raise AvatarError("corrupt")
        master_size, master_bytes, master_sha256, total_bytes, created_at = row
        if (
            type(master_size) is not int
            or not 1 <= master_size <= 1024
            or type(master_bytes) is not int
            or not 1 <= master_bytes <= MASTER_BYTES
            or type(total_bytes) is not int
            or not 6 <= total_bytes <= TOTAL_BYTES
            or not isinstance(created_at, str)
            or len(created_at) != 24
        ):
            raise AvatarError("corrupt")
        master = AvatarFile("master.png", master_size, master_bytes, _sha(master_sha256))
        parsed: list[AvatarFile] = []
        for raw, expected_size in zip(variants, VARIANT_SIZES, strict=True):
            size, byte_size, digest = raw
            if (
                size != expected_size
                or type(byte_size) is not int
                or not 1 <= byte_size <= VARIANT_BYTES
            ):
                raise AvatarError("corrupt")
            parsed.append(AvatarFile(f"{size}.png", size, byte_size, _sha(digest)))
        if total_bytes != master.byte_size + sum(item.byte_size for item in parsed):
            raise AvatarError("corrupt")
        return Avatar(avatar_id, master, tuple(parsed), total_bytes, created_at)

    def _existing(self, avatar_id: str, request_hash: str) -> Avatar | None:
        with self._store._connection() as connection:
            row = connection.execute(
                "SELECT request_hash FROM avatars WHERE avatar_id=?", (avatar_id,)
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise AvatarError("command_conflict")
        avatar = self._load(avatar_id)
        if avatar is None:
            raise AvatarError("corrupt")
        return avatar

    @staticmethod
    def _require_room(connection: sqlite3.Connection, total_bytes: int) -> None:
        count, used = connection.execute(
            "SELECT count(*),coalesce(sum(total_bytes),0) FROM avatars"
        ).fetchone()
        if count >= MAX_AVATARS or used + total_bytes > MAX_STORED_BYTES:
            raise AvatarError("capacity")

    def _check_room(self, total_bytes: int) -> None:
        with self._store._connection() as connection:
            self._require_room(connection, total_bytes)

    def _insert(self, avatar: Avatar, request_hash: str) -> None:
        with self._store._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT request_hash FROM avatars WHERE avatar_id=?", (avatar.avatar_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != request_hash:
                    raise AvatarError("command_conflict")
                return
            self._require_room(connection, avatar.total_bytes)
            connection.execute(
                "INSERT INTO avatars "
                "(avatar_id,request_hash,master_size,master_bytes,master_sha256,"
                "total_bytes,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    avatar.avatar_id,
                    request_hash,
                    avatar.master.size,
                    avatar.master.byte_size,
                    avatar.master.sha256,
                    avatar.total_bytes,
                    avatar.created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO avatar_variants (avatar_id,size,byte_size,sha256) VALUES (?,?,?,?)",
                [
                    (avatar.avatar_id, item.size, item.byte_size, item.sha256)
                    for item in avatar.variants
                ],
            )

    @staticmethod
    def _publish_directory(stage: Path, final: Path) -> None:
        try:
            final.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise AvatarError("unavailable") from None
        else:
            raise AvatarError("command_conflict")
        try:
            # POSIX rename would replace an empty directory raced in after the check above.
            osfs.rename_no_replace(stage, final)
        except OSError:
            try:
                final.lstat()
            except FileNotFoundError:
                raise AvatarError("unavailable") from None
            except OSError:
                raise AvatarError("unavailable") from None
            raise AvatarError("command_conflict") from None
        osfs.fsync_dir(final.parent)

    async def transform(
        self,
        avatar_id: str,
        source_asset_id: str,
        crop: Mapping[str, object],
        rotation: int,
    ) -> Avatar:
        avatar_id = _uuid(avatar_id, "invalid_avatar")
        source_asset_id = _uuid(source_asset_id, "invalid_asset")
        parsed_crop = _crop(crop)
        if type(rotation) is not int or rotation not in {0, 90, 180, 270}:
            raise AvatarError("invalid_rotation")
        request_hash = _request_hash(source_asset_id, parsed_crop, rotation)
        async with self._mutation_lock:
            existing = await self._store_call(lambda: self._existing(avatar_id, request_hash))
            if existing is not None:
                await self._ready()
                try:
                    await to_thread.run_sync(self._verify_avatar, existing)
                except AvatarError:
                    raise AvatarError("unavailable") from None
                return existing
            await self._ready()
            # The count cap alone (no size known yet) is checked here, before any stage or
            # download, so a full store never pays for either; the byte total is checked below
            # once it is known.
            await self._store_call(lambda: self._check_room(0))
            stage = self._incoming / str(uuid.uuid4())
            final = self._blobs / avatar_id
            published = False
            committed = False
            stage_owned = False
            try:
                creating = asyncio.ensure_future(
                    to_thread.run_sync(protection.create_private_directory, stage)
                )
                try:
                    stage_owned = await asyncio.shield(creating)
                except asyncio.CancelledError:
                    # A cancel here must not skip the cleanup below: wait for the thread,
                    # so a stage it goes on to create is still this call's to remove.
                    with contextlib.suppress(Exception):
                        stage_owned = await asyncio.shield(creating)
                    raise
                if not stage_owned:
                    raise AvatarError("unavailable")
                source = self._source_assets.download(source_asset_id)
                downloaded = await source
                if downloaded.byte_size > AVATAR_SOURCE_BYTES:
                    raise AvatarError("too_large")
                source_path = stage / "source"

                handle = await _run_to_end(osfs.open_new_private, source_path)
                written = 0
                try:
                    async for chunk in downloaded.body:
                        if len(chunk) > AVATAR_SOURCE_BYTES - written:
                            raise AvatarError("too_large")
                        await _run_to_end(handle.write, chunk)
                        written += len(chunk)
                    if written != downloaded.byte_size or written == 0:
                        raise AvatarError("source_changed")
                    await _run_to_end(handle.flush)
                    await _run_to_end(os.fsync, handle.fileno())
                finally:
                    await _run_to_end(handle.close)
                await _run_to_end(protection.require_private_file, source_path)
                master, variants = self._manifest(await self._worker(stage, parsed_crop, rotation))
                for item in (master, *variants):
                    await _run_to_end(self._verify_output, stage / item.name, item)
                total = master.byte_size + sum(item.byte_size for item in variants)
                if total > TOTAL_BYTES:
                    raise AvatarError("transform_failed")
                # At the cap, publish nothing. The insert below still decides a race.
                await self._store_call(lambda: self._check_room(total))
                # The source asset stays until the 24-hour asset collector expires it, so a
                # failed transform can be retried. Only this stage's private copy goes.
                await _run_to_end(source_path.unlink)
                await _run_to_end(self._verify_complete_directory, stage)
                avatar = Avatar(avatar_id, master, variants, total, _stamp())

                # Each flag is set inside its thread step, so a cancel that lands while the
                # step runs still sees what the step did.
                def publish() -> None:
                    nonlocal published
                    self._publish_directory(stage, final)
                    published = True

                def insert() -> None:
                    nonlocal committed
                    self._insert(avatar, request_hash)
                    committed = True

                await _run_to_end(publish)
                # The row commit is the publication point: recheck the renamed directory first.
                await _run_to_end(protection.require_private_directory, final)
                await _run_to_end(self._verify_avatar, avatar)
                await _run_to_end(self._checked_call, insert)
                return avatar
            except AvatarError:
                raise
            except AssetError as exc:
                raise AvatarError(exc.code) from None
            except OSError, StateError, protection.ProtectionError:
                raise AvatarError("unavailable") from None
            finally:
                if published and not committed:
                    # No row names the directory: this call found none for its id under the
                    # mutation lock, and the insert never ran or rolled back. Remove it so an
                    # exact retry can publish again; the cleanup refuses a directory holding
                    # anything but this call's own files.
                    await _run_to_end(self._cleanup_directory, final)
                elif stage_owned and not published:
                    await _run_to_end(self._cleanup_directory, stage)

    def _read(self, avatar_id: str, item: AvatarFile) -> bytes:
        # A committed avatar whose bytes went missing or changed keeps its row; the read fails.
        try:
            return self._verify_output(self._blobs / avatar_id / item.name, item)
        except AvatarError:
            raise AvatarError("unavailable") from None

    def _verify_avatar(self, avatar: Avatar) -> None:
        directory = self._blobs / avatar.avatar_id
        self._verify_complete_directory(directory)
        for item in (avatar.master, *avatar.variants):
            self._verify_output(directory / item.name, item)

    async def download(self, avatar_id: str, size: int) -> AvatarDownload:
        avatar_id = _uuid(avatar_id, "invalid_avatar")
        if type(size) is not int or size not in VARIANT_SIZES:
            raise AvatarError("invalid_size")
        avatar = await self._store_call(lambda: self._load(avatar_id))
        if avatar is None:
            raise AvatarError("not_found")
        await self._ready()
        item = next(variant for variant in avatar.variants if variant.size == size)
        body = await to_thread.run_sync(self._read, avatar_id, item)
        return AvatarDownload(item.byte_size, item.sha256, body)

    @staticmethod
    def _cleanup_directory(path: Path) -> bool:
        try:
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or path.name != str(uuid.UUID(path.name))
            ):
                return False
            protection.require_private_directory(path)
            # Seven names are allowed, so reading one more entry is enough to find a foreign one.
            with os.scandir(path) as iterator:
                entries = [Path(entry.path) for entry in itertools.islice(iterator, 8)]
            if any(entry.name not in {*_OUTPUT_NAMES, "source"} for entry in entries):
                return False
            for entry in entries:
                item = entry.lstat()
                if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                    return False
                protection.require_private_file(entry)
            for entry in entries:
                entry.unlink()
            path.rmdir()
            return True
        except FileNotFoundError, OSError, ValueError, protection.ProtectionError:
            return False

    def _collect_rows(self, before: str, limit: int) -> list[str]:
        with self._store._connection(write=True) as connection:
            row = connection.execute("SELECT document FROM settings WHERE scope='app'").fetchone()
            keep = frozenset[str]() if row is None else settings.identity_avatar_ids(row[0])
            if keep is None:
                # An unreadable document proves nothing about which avatars it names.
                return []
            rows = connection.execute(_RETENTION_CANDIDATES, (before, limit + len(keep))).fetchall()
            removed = [avatar_id for (avatar_id,) in rows if avatar_id not in keep][:limit]
            # One write transaction holds this connection for the whole call, so no other
            # writer can add a reference between the select above and the deletes below.
            connection.executemany(
                "DELETE FROM avatar_variants WHERE avatar_id=?",
                [(avatar_id,) for avatar_id in removed],
            )
            connection.executemany(
                "DELETE FROM avatars WHERE avatar_id=?", [(avatar_id,) for avatar_id in removed]
            )
        return removed

    def _known_ids(self) -> set[str]:
        with self._store._connection() as connection:
            return {row[0] for row in connection.execute("SELECT avatar_id FROM avatars")}

    async def collect_due(
        self, *, now: datetime | None = None, limit: int = COLLECT_LIMIT
    ) -> AvatarCollection:
        if type(limit) is not int or not 1 <= limit <= COLLECT_LIMIT:
            raise AvatarError("invalid_retention")
        instant = datetime.now(UTC) if now is None else now
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise AvatarError("invalid_retention")
        instant = instant.astimezone(UTC)
        cutoff = instant - ORPHAN_TTL
        before = _stamp(cutoff)
        await self._ready()
        removed = await self._store_call(lambda: self._collect_rows(before, limit))
        known = await self._store_call(self._known_ids)
        directories = await to_thread.run_sync(
            self._collect_directories, removed, known, cutoff, limit
        )
        return AvatarCollection(len(removed), directories)

    def _collect_directories(
        self, removed: list[str], known: set[str], cutoff: datetime, limit: int
    ) -> int:
        directories = sum(self._cleanup_directory(self._blobs / avatar_id) for avatar_id in removed)
        # The budget covers every tracked directory plus `limit` more entries, and bounds a
        # directory flooded with foreign names.
        scanned = 0
        for parent in (self._blobs, self._incoming):
            try:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        if directories >= limit or scanned >= MAX_AVATARS + limit:
                            return directories
                        scanned += 1
                        try:
                            canonical = entry.name == str(uuid.UUID(entry.name))
                            modified = entry.stat(follow_symlinks=False).st_mtime
                        except OSError, ValueError:
                            continue
                        if (
                            canonical
                            and entry.name not in known
                            and modified < cutoff.timestamp()
                            and self._cleanup_directory(Path(entry.path))
                        ):
                            directories += 1
            except OSError:
                continue
        return directories

    async def _transform_in_worker(
        self, stage: Path, crop: Crop, rotation: int
    ) -> Mapping[str, object]:
        worker = Path(__file__).with_name("media_worker.py")
        env = media_worker._worker_env(sys.platform, os.environ)
        # media_worker.main's avatar shape: source, --avatar, x, y, edge, rotation.
        argv = compiled.worker_argv(
            "media",
            [
                *media_worker._worker_argv_prefix(sys.platform, WORKER_ADDRESS_SPACE_BYTES),
                "source",
                "--avatar",
                str(crop.x),
                str(crop.y),
                str(crop.width),
                str(rotation),
            ],
            python=sys.executable,
            script=worker,
        )
        try:
            child = await winproc.spawn(
                argv,
                cwd=stage,
                env=env,
                memory_limit_bytes=WORKER_MEMORY_BYTES,
            )
        except OSError:
            raise AvatarError("worker_unavailable") from None
        communication = asyncio.create_task(read_worker_output(child, WORKER_OUTPUT_BYTES))
        try:
            try:
                async with asyncio.timeout(WORKER_TIMEOUT_SECONDS):
                    stdout = await asyncio.shield(communication)
            except TimeoutError:
                child.kill_tree()
                await communication
                raise AvatarError("worker_timeout") from None
            except asyncio.CancelledError:
                child.kill_tree()
                await asyncio.shield(communication)
                raise
            code = child.process.returncode
            if code != 0:
                # The integer only: never a path, stage name, crop value or worker output.
                logger.warning("avatar worker exited: %s", code)
                if code == media_worker.DECODER_UNAVAILABLE_EXIT_CODE:
                    raise AvatarError("decoder_unavailable")
                if code == media_worker.CROP_OUTSIDE_EXIT_CODE:
                    raise AvatarError("invalid_crop")
                raise AvatarError("transform_failed")
            if len(stdout) > WORKER_OUTPUT_BYTES:
                raise AvatarError("transform_failed")
            try:
                answer = json.loads(stdout.decode("ascii"))
            except UnicodeError, ValueError, RecursionError:
                raise AvatarError("transform_failed") from None
            if not isinstance(answer, dict):
                raise AvatarError("transform_failed")
            return answer
        finally:
            child.release()
