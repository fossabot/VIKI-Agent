"""voice_pack_setup: the setup child's orchestration, driven entirely through fakes; no network.
The two subprocess tests at the end are the only ones that start a real process, and only to
prove the module's imports resolve under `-I -B`."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection, winproc
from viki_agent.voice import voice_pack, voice_pack_fetch, voice_pack_setup
from viki_agent.voice.voice_pack import PinnedFile

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "src" / "viki_agent" / "voice" / "voice_pack_setup.py"


def _pin(name: str, data: bytes) -> PinnedFile:
    url = f"https://example.invalid/{name}"
    return PinnedFile(name, len(data), hashlib.sha256(data).hexdigest(), url)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _archive_pins() -> tuple[tuple[PinnedFile, PinnedFile], dict[str, bytes]]:
    """Two tiny archives, as the real pair is: one holds the exe, the other does not."""
    server_bytes = _tar_gz({voice_pack.SERVER_EXE: b"exe payload\n", "_internal/x": b"lib\n"})
    cuda_bytes = _tar_gz({"_internal/extra.dll": b"extra\n"})
    server_pin, cuda_pin = _pin("server.tar.gz", server_bytes), _pin("cuda.tar.gz", cuda_bytes)
    return (server_pin, cuda_pin), {server_pin.url: server_bytes, cuda_pin.url: cuda_bytes}


def _model_pins() -> tuple[tuple[PinnedFile, PinnedFile], dict[str, bytes]]:
    a, b = b"model part a\n" * 10, b"model part b\n" * 5
    pin_a, pin_b = _pin("weights.bin", a), _pin("sub/extra.bin", b)
    return (pin_a, pin_b), {pin_a.url: a, pin_b.url: b}


def _kokoro_pins() -> tuple[tuple[PinnedFile, PinnedFile], dict[str, bytes]]:
    a, b = b"kokoro voice a\n" * 7, b"kokoro voice b\n" * 3
    pin_a, pin_b = _pin("kokoro_a.bin", a), _pin("kokoro_b.bin", b)
    return (pin_a, pin_b), {pin_a.url: a, pin_b.url: b}


def _seed_server(data_dir: Path) -> None:
    """A server tree already in place, so install() skips straight to the model step."""
    exe = voice_pack.server_dir(data_dir) / voice_pack.SERVER_EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"already installed\n")


def _seed_model(data_dir: Path, files: tuple[PinnedFile, ...], blobs: dict[str, bytes]) -> None:
    """Qwen's snapshot and ref already in place, as a prior successful install would leave,
    so `voice_pack_fetch.fetch` sees every file already verified and skips its request."""
    snapshot = voice_pack.snapshot_dir(data_dir)
    for pin in files:
        target = snapshot / pin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[pin.url])
    ref = voice_pack.repo_dir(data_dir) / "refs"
    ref.mkdir(parents=True)
    (ref / "main").write_text(voice_pack.MODEL_REVISION, encoding="utf-8")


class _Answer(io.BytesIO):
    def __init__(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value


class _DropsMidBody(_Answer):
    """An answer whose body ends early, as a killed connection would."""

    def read(self, size: int | None = -1, /) -> bytes:
        chunk = super().read(size)
        if chunk:
            return chunk
        raise OSError("connection reset")


class FakeOpener:
    """Serves pinned bytes from memory by URL; honours Range with 206 and an exact Content-Range.

    `drop_next(url)` makes the next request for that URL return half its remaining body and then
    fail mid-read, as a killed connection would, leaving a resumable `.part` file behind.
    """

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs
        self._drop: set[str] = set()
        self.requests: list[tuple[str, int]] = []

    def drop_next(self, url: str) -> None:
        self._drop.add(url)

    def __call__(self, url: str, offset: int) -> _Answer:
        self.requests.append((url, offset))
        data = self._blobs[url]
        if url in self._drop:
            self._drop.discard(url)
            remaining = data[offset:]
            half = remaining[: max(1, len(remaining) // 2)]
            if offset == 0:
                return _DropsMidBody(200, half, {"Content-Length": str(len(data))})
            headers = {"Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}"}
            return _DropsMidBody(206, half, headers)
        if offset == 0:
            return _Answer(200, data, {"Content-Length": str(len(data))})
        headers = {"Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}"}
        return _Answer(206, data[offset:], headers)


def _lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _progress(lines: list[dict[str, Any]], step: str) -> list[tuple[int, int]]:
    return [(line["done"], line["total"]) for line in lines if line.get("step") == step]


# ---- a full, from-scratch install -------------------------------------------------------------


def test_full_install_produces_the_designed_layout_and_done(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    archives, archive_blobs = _archive_pins()
    files, file_blobs = _model_pins()
    opener = FakeOpener({**archive_blobs, **file_blobs})
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=archives,
        files=files,
        kokoro=(),
        unpacked=123,
        disk_free=lambda path: 10**12,
    )

    assert code == 0
    lines = _lines(out)
    assert lines[-1] == {"type": "done"}
    server_total = sum(pin.size for pin in archives)
    files_total = sum(pin.size for pin in files)
    for step, total in (("server", server_total), ("unpack", server_total), ("model", files_total)):
        points = _progress(lines, step)
        assert points, step
        assert all(total == t for _, t in points)
        assert [done for done, _ in points] == sorted(done for done, _ in points)
        assert points[-1][0] == total

    server_dir = voice_pack.server_dir(data)
    assert (server_dir / voice_pack.SERVER_EXE).read_bytes() == b"exe payload\n"
    assert (server_dir / "_internal" / "x").read_bytes() == b"lib\n"
    assert (server_dir / "_internal" / "extra.dll").read_bytes() == b"extra\n"
    assert not voice_pack.partial_dir(data).exists()
    assert list(voice_pack.downloads_dir(data).iterdir()) == []

    snapshot = voice_pack.snapshot_dir(data)
    assert (snapshot / "weights.bin").read_bytes() == file_blobs[files[0].url]
    assert (snapshot / "sub" / "extra.bin").read_bytes() == file_blobs[files[1].url]
    ref = voice_pack.repo_dir(data) / "refs" / "main"
    assert ref.read_bytes() == voice_pack.MODEL_REVISION.encode("ascii")
    assert voice_pack.installed(data, files)


def test_full_install_writes_both_model_snapshots_and_refs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    files, file_blobs = _model_pins()
    kokoro, kokoro_blobs = _kokoro_pins()
    opener = FakeOpener({**file_blobs, **kokoro_blobs})
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=files,
        kokoro=kokoro,
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 0, _lines(out)
    snapshot = voice_pack.snapshot_dir(data)
    assert (snapshot / "weights.bin").read_bytes() == file_blobs[files[0].url]
    kokoro_snapshot = voice_pack.kokoro_snapshot_dir(data)
    assert (kokoro_snapshot / "kokoro_a.bin").read_bytes() == kokoro_blobs[kokoro[0].url]
    assert (kokoro_snapshot / "kokoro_b.bin").read_bytes() == kokoro_blobs[kokoro[1].url]
    ref = voice_pack.repo_dir(data) / "refs" / "main"
    assert ref.read_bytes() == voice_pack.MODEL_REVISION.encode("ascii")
    kokoro_ref = voice_pack.kokoro_repo_dir(data) / "refs" / "main"
    assert kokoro_ref.read_bytes() == voice_pack.KOKORO_REVISION.encode("ascii")
    assert voice_pack.installed(data, files)
    assert voice_pack.kokoro_installed(data, kokoro)

    # Qwen and Kokoro share one "model" progress total.
    model_points = _progress(_lines(out), "model")
    total = sum(pin.size for pin in files) + sum(pin.size for pin in kokoro)
    assert model_points
    assert all(t == total for _, t in model_points)
    assert model_points[-1][0] == total


def test_install_fetches_kokoro_and_writes_its_ref(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    files, file_blobs = _model_pins()
    _seed_model(data, files, file_blobs)
    kokoro, kokoro_blobs = _kokoro_pins()
    opener = FakeOpener({**file_blobs, **kokoro_blobs})
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=files,
        kokoro=kokoro,
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 0, _lines(out)
    # Qwen's files already verify, so only Kokoro's pins are ever requested.
    assert {url for url, _ in opener.requests} == {pin.url for pin in kokoro}

    kokoro_snapshot = voice_pack.kokoro_snapshot_dir(data)
    assert (kokoro_snapshot / "kokoro_a.bin").read_bytes() == kokoro_blobs[kokoro[0].url]
    assert (kokoro_snapshot / "kokoro_b.bin").read_bytes() == kokoro_blobs[kokoro[1].url]
    kokoro_ref = voice_pack.kokoro_repo_dir(data) / "refs" / "main"
    assert kokoro_ref.read_bytes() == voice_pack.KOKORO_REVISION.encode("ascii")
    assert voice_pack.kokoro_installed(data, kokoro)
    # _install writes both refs even though only Kokoro needed fetching.
    ref = voice_pack.repo_dir(data) / "refs" / "main"
    assert ref.read_bytes() == voice_pack.MODEL_REVISION.encode("ascii")


def test_repair_rebuilds_the_server_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    stale_exe = voice_pack.server_dir(data) / voice_pack.SERVER_EXE
    stale_exe.parent.mkdir(parents=True)
    stale_exe.write_bytes(b"stale broken bytes")
    stray = voice_pack.partial_dir(data) / "leftover.bin"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"leftover")
    archives, archive_blobs = _archive_pins()
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data), "--repair"],
        opener=FakeOpener(archive_blobs),
        out=out,
        archives=archives,
        files=(),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 0
    assert _lines(out)[-1] == {"type": "done"}
    exe = voice_pack.server_dir(data) / voice_pack.SERVER_EXE
    assert exe.read_bytes() == b"exe payload\n"
    assert not voice_pack.partial_dir(data).exists()


def test_a_bad_archive_is_refused_without_promoting_a_broken_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    bad_bytes = _tar_gz({"_internal/x": b"no exe in here\n"})
    bad_pin = _pin("server.tar.gz", bad_bytes)
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=FakeOpener({bad_pin.url: bad_bytes}),
        out=out,
        archives=(bad_pin,),
        files=(),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 1
    assert _lines(out)[-1] == {"type": "error", "code": "bad_archive"}


# ---- disk space, checked before any request ----------------------------------------------------


def test_disk_full_sends_no_request(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    archive_pin = _pin("archives/one.tar.gz", b"x" * 1000)
    model_pin = _pin("model/a.bin", b"y" * 1000)
    opener = FakeOpener({archive_pin.url: b"x" * 1000, model_pin.url: b"y" * 1000})
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(archive_pin,),
        files=(model_pin,),
        kokoro=(),
        unpacked=10_000,
        disk_free=lambda path: 0,
    )

    assert code == 1
    assert opener.requests == []
    assert _lines(out) == [{"type": "error", "code": "disk_full"}]


def test_disk_check_counts_kokoro_bytes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    kokoro_pin = _pin("kokoro/voice.bin", b"k" * 1000)
    opener = FakeOpener({kokoro_pin.url: b"k" * 1000})
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=(),
        kokoro=(kokoro_pin,),
        unpacked=0,
        # Enough for the 1 GiB margin alone, not the 1000 Kokoro bytes on top of it.
        disk_free=lambda path: 2**30,
    )

    assert code == 1
    assert opener.requests == []
    assert _lines(out) == [{"type": "error", "code": "disk_full"}]


def test_an_installed_server_needs_no_disk_space_for_its_archives(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    archives, archive_blobs = _archive_pins()
    files, model_blobs = _model_pins()
    opener = FakeOpener({**archive_blobs, **model_blobs})
    out = io.StringIO()
    model_bytes = sum(pin.size for pin in files)

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=archives,
        files=files,
        kokoro=(),
        unpacked=10**12,
        # Room for the model and the margin only: the archives and their unpacked tree are done.
        disk_free=lambda path: model_bytes + 2**30,
    )

    assert code == 0, _lines(out)
    assert {url for url, _ in opener.requests} == {pin.url for pin in files}


def test_a_server_tree_without_its_exe_is_replaced(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    broken = voice_pack.server_dir(data)
    broken.mkdir(parents=True)
    (broken / "leftover.txt").write_bytes(b"from an earlier, broken unpack\n")
    archives, archive_blobs = _archive_pins()
    files, model_blobs = _model_pins()
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=FakeOpener({**archive_blobs, **model_blobs}),
        out=out,
        archives=archives,
        files=files,
        kokoro=(),
        unpacked=1000,
        disk_free=lambda path: None,
    )

    assert code == 0, _lines(out)
    assert (broken / voice_pack.SERVER_EXE).is_file()
    assert not (broken / "leftover.txt").exists()


# ---- resume across two launches, as a kill-and-relaunch would ----------------------------------


def test_install_resumes_after_a_kill(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    body = b"model weights payload\n" * 50
    pin = _pin("model.bin", body)
    opener = FakeOpener({pin.url: body})
    opener.drop_next(pin.url)

    first = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=io.StringIO(),
        archives=(),
        files=(pin,),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert first == 1
    part = voice_pack.snapshot_dir(data) / (pin.name + ".part")
    partial_size = part.stat().st_size
    assert 0 < partial_size < len(body)
    assert opener.requests == [(pin.url, 0)]

    out = io.StringIO()
    second = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=(pin,),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert second == 0
    assert opener.requests[-1] == (pin.url, partial_size)
    assert (voice_pack.snapshot_dir(data) / pin.name).read_bytes() == body
    assert _lines(out)[-1] == {"type": "done"}


# ---- progress throttling: at most 1/0.25s per step, first and last always land -----------------


def test_progress_is_throttled_but_first_and_last_always_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    monkeypatch.setattr(voice_pack_fetch, "CHUNK", 10)
    body = b"x" * 100
    pin = _pin("weights.bin", body)
    clock = iter(i * 0.09 for i in range(10))
    monkeypatch.setattr(voice_pack_setup.time, "monotonic", lambda: next(clock))
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=FakeOpener({pin.url: body}),
        out=out,
        archives=(),
        files=(pin,),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 0
    dones = [done for done, _ in _progress(_lines(out), "model")]
    assert dones == [10, 40, 70, 100]


# ---- errors: a structured code, and an unexpected failure --------------------------------------


def test_a_setup_error_surfaces_as_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()

    def refuse(path: Path) -> bool:
        raise protection.ProtectionError()

    monkeypatch.setattr(voice_pack_setup.protection, "create_private_directory", refuse)
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=FakeOpener({}),
        out=out,
        archives=(),
        files=(),
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 1
    assert _lines(out) == [{"type": "error", "code": "private_folder"}]


def test_an_existing_folder_that_is_not_private_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The folder already exists (create returns False), so it must pass the private check: a
    # junctioned or shared `voice` or `pack` would let another user swap the unsigned exe.
    data = tmp_path / "data"
    data.mkdir()

    def not_private(path: Path) -> None:
        raise protection.ProtectionError()

    monkeypatch.setattr(voice_pack_setup.protection, "create_private_directory", lambda p: False)
    monkeypatch.setattr(voice_pack_setup.protection, "require_private_directory", not_private)
    files, model_blobs = _model_pins()
    opener = FakeOpener(model_blobs)
    out = io.StringIO()

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=files,
        kokoro=(),
        unpacked=0,
        disk_free=lambda path: 10**12,
    )

    assert code == 1
    assert _lines(out) == [{"type": "error", "code": "private_folder"}]
    assert opener.requests == []


def test_bytes_already_on_disk_count_against_the_space_needed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed_server(data)
    files, model_blobs = _model_pins()
    first = files[0]
    part = voice_pack.snapshot_dir(data) / (first.name + ".part")
    part.parent.mkdir(parents=True)
    kept = model_blobs[first.url][:40]
    part.write_bytes(kept)
    opener = FakeOpener(model_blobs)
    out = io.StringIO()
    full_need = sum(pin.size for pin in files) + 2**30

    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=opener,
        out=out,
        archives=(),
        files=files,
        kokoro=(),
        unpacked=0,
        # Exactly what is still missing: the kept part's bytes must not be needed twice.
        disk_free=lambda path: full_need - len(kept),
    )

    assert code == 0, _lines(out)
    assert (first.url, len(kept)) in opener.requests


def test_an_unexpected_exception_is_write_failed_with_no_traceback_on_stdout(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()

    def boom(path: Path) -> int | None:
        raise RuntimeError("boom")

    out = io.StringIO()
    code = voice_pack_setup.main(
        ["install", "--data", str(data)],
        opener=FakeOpener({}),
        out=out,
        archives=(),
        files=(),
        kokoro=(),
        unpacked=0,
        disk_free=boom,
    )

    assert code == 1
    assert _lines(out) == [{"type": "error", "code": "write_failed"}]
    assert "Traceback" not in out.getvalue()


# ---- the import guard: only setup, fetch and unpack may import one another ---------------------


def test_only_setup_fetch_and_unpack_import_each_other() -> None:
    allowed = {"voice_pack_setup", "voice_pack_fetch", "voice_pack_unpack"}
    source_root = REPO / "src" / "viki_agent"
    allowed_paths = {source_root / "voice" / f"{name}.py" for name in allowed}
    forbidden = {
        f"{prefix}.{name}" for prefix in ("viki_agent", "viki_agent.voice") for name in allowed
    }
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if path in allowed_paths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = ".".join(("viki_agent", *path.relative_to(source_root).parts[:-1]))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                origin = node.module or ""
                if node.level:
                    origin = importlib.util.resolve_name("." * node.level + origin, package)
                names.add(origin)
                names.update(f"{origin}.{alias.name}" for alias in node.names)
        hit = {
            name
            for name in forbidden
            if any(imported == name or imported.startswith(name + ".") for imported in names)
        }
        if hit:
            offenders.append(f"{path.relative_to(REPO)}: {sorted(hit)}")
    assert offenders == []


# ---- the real process under -I -B: proves the imports resolve, no network ----------------------


def test_help_runs_under_isolated_mode(tmp_path: Path) -> None:
    result = winproc.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "--help"], cwd=tmp_path, timeout=10.0
    )

    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_bad_arguments_fail_cleanly_under_isolated_mode(tmp_path: Path) -> None:
    result = winproc.run([sys.executable, "-I", "-B", str(SCRIPT)], cwd=tmp_path, timeout=10.0)

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
