"""voice_setup: pins are small test files, every byte source is injected, nothing leaves the
process and nothing is written outside the test's data folder."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import voice_fetch  # noqa: E402
import voice_machine  # noqa: E402
import voice_setup  # noqa: E402

from viki_agent import protection  # noqa: E402
from viki_agent.voice import voice_models  # noqa: E402

GIB = 1024**3
PRIVATE_MESSAGE = "cannot create a private folder, or an existing one is not private"
PINNED = voice_models.PinnedFile


def _pin(name: str, data: bytes, url: str = "", member: str = "") -> voice_models.PinnedFile:
    return PINNED(name, len(data), hashlib.sha256(data).hexdigest(), url, member)


WEIGHTS = b"test weights\n" * 50
VAD = b"test vad\n" * 20
MEMBERS = {
    "binding.dll": (b"the binding\n" * 10, "Release/whisper.dll"),
    "ggml.dll": (b"ggml\n" * 10, "Release/ggml.dll"),
    "cuda.dll": (b"cuda\n" * 10, "Release/cuda.dll"),
}
WEIGHTS_URL = "https://example.invalid/weights.bin"
VAD_URL = "https://example.invalid/vad.bin"
ARCHIVE_URL = "https://example.invalid/cuda.zip"


def _zip(members: dict[str, bytes]) -> bytes:
    # A fixed date_time and create_system, and an explicit compress_type on each member's own
    # ZipInfo, so the archive's bytes depend on `members` alone, never on `sys.platform` or the
    # clock (`writestr` with a bare name reads both into its auto-built ZipInfo).
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in members.items():
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buffer.getvalue()


def _archive_bytes(wrong: str = "") -> bytes:
    members = {member: data for data, member in MEMBERS.values()}
    if wrong:
        data, member = MEMBERS[wrong]
        members[member] = bytes(len(data))  # the pinned size, other bytes
    return _zip(members)


class Net:
    """The opener: serves the pinned test files by URL and records every request."""

    def __init__(self, archive: bytes) -> None:
        self.bodies = {WEIGHTS_URL: WEIGHTS, VAD_URL: VAD, ARCHIVE_URL: archive}
        self.requests: list[str] = []

    def __call__(self, url: str) -> BinaryIO:
        self.requests.append(url)
        return io.BytesIO(self.bodies[url])


def _refuse(url: str) -> BinaryIO:
    raise AssertionError("the opener must not be called")


def _machine(*, cpus: int = 4, gpu: voice_machine.Gpu | None = None, runtime: bool = False):
    return voice_machine.Machine("win32", "AMD64", cpus, 8 * GIB, gpu, runtime)


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Small pins for every model, the VAD and the cuda backend; returns the data folder."""
    data = tmp_path / "data"
    monkeypatch.setenv("VIKI_DATA_DIR", str(data))
    weights = _pin("weights.bin", WEIGHTS, WEIGHTS_URL)
    models = {
        model_id: dataclasses.replace(model, weights=weights)
        for model_id, model in voice_models.MODELS.items()
    }
    archive = _pin("cuda.zip", _archive_bytes(), ARCHIVE_URL)
    files = tuple(_pin(name, data, member=member) for name, (data, member) in MEMBERS.items())
    cuda = dataclasses.replace(voice_models.BACKENDS["cuda"], archive=archive, files=files)
    monkeypatch.setattr(voice_models, "MODELS", models)
    monkeypatch.setattr(voice_models, "VAD", _pin("vad.bin", VAD, VAD_URL))
    monkeypatch.setattr(voice_models, "BACKENDS", {**voice_models.BACKENDS, "cuda": cuda})
    return data


def _act_as(monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(voice_setup.platform, "machine", lambda: machine)


def _run(*argv: str, net=_refuse, probe=_machine) -> int:
    return voice_setup.main(list(argv), opener=net, probe=probe)


def _no_leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*") if p.name.endswith(".part"))


@pytest.mark.parametrize("flags", [[], ["--backend", "auto"]])
def test_default_install_takes_the_recommendation(
    pins: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    machine = _machine()
    backend, model = voice_machine.recommend(machine)
    net = Net(_archive_bytes())
    assert _run(*flags, net=net, probe=lambda: machine) == 0
    assert net.requests == [WEIGHTS_URL, VAD_URL]
    folder = voice_models.model_dir(pins, model)
    assert voice_models.verify(folder, voice_models.model_files(model)) is None
    assert not (pins / "voice" / "backends").exists()
    out = capsys.readouterr().out
    assert f"voice_bench.py --smoke --model {model} --backend {backend}" in out


def test_chosen_backend_gets_that_backends_model(
    pins: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        voice_models,
        "RANKING",
        {"cpu": ("small-q8_0",), "cuda": ("large-v3-turbo-q8_0",), "metal": ("small-q8_0",)},
    )
    monkeypatch.setattr(voice_models, "CPU_MIN_CPUS", 8)
    monkeypatch.setattr(voice_models, "CPU_MIN_MEMORY", 16 * GIB)
    gpu = voice_machine.Gpu("Fake RTX", 1000 * GIB, 12080)
    machine = voice_machine.Machine("win32", "AMD64", 16, 32 * GIB, gpu, True)
    assert voice_machine.recommend(machine) == ("cuda", "large-v3-turbo-q8_0")
    net = Net(_archive_bytes())
    assert _run("--backend", "cpu", net=net, probe=lambda: machine) == 0
    assert net.requests == [WEIGHTS_URL, VAD_URL]
    assert voice_models.model_dir(pins, "small-q8_0").exists()
    assert not voice_models.model_dir(pins, "large-v3-turbo-q8_0").exists()


def test_gpu_install_also_fetches_the_cpu_fallback_model(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    net = Net(_archive_bytes())
    assert _run("--model", "large-v3-turbo-q8_0", "--backend", "cuda", net=net) == 0
    assert net.requests == [WEIGHTS_URL, VAD_URL, WEIGHTS_URL, VAD_URL, ARCHIVE_URL]
    for model in ("large-v3-turbo-q8_0", "small-q8_0"):
        folder = voice_models.model_dir(pins, model)
        assert voice_models.verify(folder, voice_models.model_files(model)) is None
    out = capsys.readouterr().out
    assert out.count("fetching weights.bin") == 2
    assert "voice_bench.py --smoke --model large-v3-turbo-q8_0 --backend cuda" in out


def test_cpu_install_fetches_one_model(pins: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    net = Net(_archive_bytes())
    assert _run("--model", "large-v3-turbo-q8_0", "--backend", "cpu", net=net) == 0
    assert net.requests == [WEIGHTS_URL, VAD_URL]
    assert not voice_models.model_dir(pins, "small-q8_0").exists()


def test_list_makes_no_request_and_recommends(
    pins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(gpu=voice_machine.Gpu("Fake RTX", 8 * GIB, 12080), runtime=False)
    assert _run("--list", probe=lambda: machine) == 0
    out = capsys.readouterr().out
    backend, model = voice_machine.recommend(machine)
    assert f"recommended: --model {model} --backend {backend}" in out
    assert "nvidia gpu: Fake RTX, 8.0 GB, driver CUDA 12.8" in out
    assert "vc++ runtime: no" in out
    assert "Visual C++ 2015-2022 Redistributable (x64)" in out
    assert "not installed" in out
    assert not (pins / "voice").exists()


def test_list_marks_an_installed_model(pins: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    assert _run("--list") == 0
    lines = capsys.readouterr().out.splitlines()
    small = next(line for line in lines if line.strip().startswith("small-q8_0"))
    other = next(line for line in lines if line.strip().startswith("large-v3-turbo-q8_0"))
    assert small.endswith("  installed")
    assert other.endswith("not installed")


def test_list_names_the_cpu_fallback_model(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    assert _run("--list") == 0
    assert "cpu fallback: small-q8_0 (0.0 MB) not installed" in capsys.readouterr().out
    flags = ("--model", "large-v3-turbo-q8_0", "--backend", "cuda")
    assert _run(*flags, net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    assert _run("--list") == 0
    assert "cpu fallback: small-q8_0 (0.0 MB) installed" in capsys.readouterr().out


def test_backend_not_offered_returns_2(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "linux", "x86_64")
    assert _run("--backend", "cuda") == 2
    assert "choose one of: auto, cpu" in capsys.readouterr().err
    assert not (pins / "voice").exists()


def test_unknown_model_returns_2(pins: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("--model", "nope") == 2
    err = capsys.readouterr().err
    assert "unknown model 'nope'" in err
    assert "small-q8_0" in err
    assert not (pins / "voice").exists()


def test_cuda_install_extracts_pinned_members(pins: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    net = Net(_archive_bytes())
    assert _run("--model", "small-q8_0", "--backend", "cuda", net=net) == 0
    assert net.requests == [WEIGHTS_URL, VAD_URL, ARCHIVE_URL]
    folder = voice_models.backend_dir(pins, "cuda")
    for name, (data, _member) in MEMBERS.items():
        assert (folder / name).read_bytes() == data
    assert sorted(p.name for p in folder.iterdir()) == sorted(MEMBERS)
    assert _no_leftovers(pins) == []


def test_member_with_wrong_bytes_leaves_no_target_and_no_part(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    # The archive's own hash must match its pin, so re-pin it to the tampered bytes.
    tampered = _archive_bytes(wrong="ggml.dll")
    cuda = voice_models.BACKENDS["cuda"]
    repinned = dataclasses.replace(cuda, archive=_pin("cuda.zip", tampered, ARCHIVE_URL))
    monkeypatch.setitem(voice_models.BACKENDS, "cuda", repinned)
    assert _run("--model", "small-q8_0", "--backend", "cuda", net=Net(tampered)) == 1
    folder = voice_models.backend_dir(pins, "cuda")
    assert not (folder / "ggml.dll").exists()
    assert _no_leftovers(pins) == []
    assert "ggml.dll: hash_mismatch" in capsys.readouterr().err


def test_write_failure_in_extract_returns_1(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")

    def full_disk(*args: object) -> str | None:
        raise OSError("disk full")

    monkeypatch.setattr(voice_fetch, "extract", full_disk)
    assert _run("--model", "small-q8_0", "--backend", "cuda", net=Net(_archive_bytes())) == 1
    assert "binding.dll: write_failed" in capsys.readouterr().err


def test_verified_install_makes_no_request(pins: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    flags = ("--model", "small-q8_0", "--backend", "cuda")
    assert _run(*flags, net=Net(_archive_bytes())) == 0
    assert _run(*flags) == 0


def test_private_folder_failure_returns_1(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(path: Path) -> bool:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "create_private_directory", refuse)
    assert _run("--model", "small-q8_0", "--backend", "cpu") == 1
    assert PRIVATE_MESSAGE in capsys.readouterr().err


def test_an_existing_folder_that_is_not_private_returns_1(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    voice = pins / "voice"
    voice.mkdir(parents=True)

    def require(path: Path) -> None:
        if path == voice:
            raise protection.ProtectionError()

    monkeypatch.setattr(protection, "require_private_directory", require)
    assert _run("--model", "small-q8_0", "--backend", "cpu") == 1
    assert PRIVATE_MESSAGE in capsys.readouterr().err


def test_existing_private_folders_are_reused(pins: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flags = ("--model", "small-q8_0", "--backend", "cpu")
    assert _run(*flags, net=Net(_archive_bytes())) == 0
    checked: list[Path] = []
    monkeypatch.setattr(protection, "require_private_directory", checked.append)
    assert _run(*flags) == 0
    assert pins in checked
    assert pins / "voice" in checked
    assert voice_models.model_dir(pins, "small-q8_0") in checked


def test_check_states(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    assert _run("--check") == 1  # nothing installed
    assert "no model installed" in capsys.readouterr().out
    assert _run("--model", "small-q8_0", "--backend", "cuda", net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    assert _run("--check") == 0
    assert capsys.readouterr().out.splitlines() == ["ok small-q8_0", "ok cuda"]
    weights = voice_models.model_dir(pins, "small-q8_0") / "weights.bin"
    weights.write_bytes(bytes(len(WEIGHTS)))  # the pinned size, other bytes
    assert _run("--check") == 1
    assert capsys.readouterr().out.splitlines() == ["small-q8_0: hash_mismatch", "ok cuda"]


def test_config_error_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = Path.home() / ".claude" / "voice-setup-test"
    monkeypatch.setenv("VIKI_DATA_DIR", str(protected))
    assert _run("--list") == 2
    assert "VIKI_DATA_DIR" in capsys.readouterr().err
    assert not protected.exists()


@pytest.mark.parametrize("item", ["model", "backend"])
@pytest.mark.parametrize("damage", ["missing", "size_mismatch", "not_regular"])
def test_check_reports_incomplete_installed_choices(
    pins: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    item: str,
    damage: str,
) -> None:
    _act_as(monkeypatch, "win32", "AMD64")
    assert _run("--model", "small-q8_0", "--backend", "cuda", net=Net(_archive_bytes())) == 0
    if item == "model":
        item_id = "large-v3-turbo-q8_0"
        assert _run("--model", item_id, "--backend", "cpu", net=Net(_archive_bytes())) == 0
        path = voice_models.model_dir(pins, item_id) / "weights.bin"
    else:
        item_id = "cuda"
        path = voice_models.backend_dir(pins, item_id) / "binding.dll"
    capsys.readouterr()
    if damage == "size_mismatch":
        path.write_bytes(b"")
    else:
        path.unlink()
        if damage == "not_regular":
            path.mkdir()

    assert _run("--check") == 1
    lines = capsys.readouterr().out.splitlines()
    assert "ok small-q8_0" in lines
    assert f"{item_id}: {damage}" in lines


def test_check_leaves_absent_optional_choices_uninstalled(
    pins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("--model", "small-q8_0", "--backend", "cpu", net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    assert _run("--check") == 0
    assert capsys.readouterr().out.splitlines() == ["ok small-q8_0"]
    assert not voice_models.model_dir(pins, "large-v3-turbo-q8_0").exists()
    assert not (pins / "voice" / "backends").exists()


def test_check_reports_an_uninspectable_installation(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("--model", "small-q8_0", "--backend", "cpu", net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    folder = voice_models.backend_dir(pins, "cuda")
    folder.mkdir(parents=True)
    real_lstat = Path.lstat

    def denied(path: Path):
        if path == folder or folder in path.parents:
            raise PermissionError("synthetic denial")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied)
    assert _run("--check") == 1
    assert "cuda: not_regular" in capsys.readouterr().out.splitlines()


def test_check_reports_a_dangling_installation_link(
    pins: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dangling link has an lstat result while its expected children are missing."""
    assert _run("--model", "small-q8_0", "--backend", "cpu", net=Net(_archive_bytes())) == 0
    capsys.readouterr()
    folder = voice_models.backend_dir(pins, "cuda")
    real_lstat = Path.lstat

    def dangling(path: Path):
        if path == folder:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", dangling)
    assert _run("--check") == 1
    assert "cuda: missing" in capsys.readouterr().out.splitlines()
