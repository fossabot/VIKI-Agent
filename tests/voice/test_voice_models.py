from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from viki_agent.media.media_worker import _safe_name
from viki_agent.voice import voice_models as vm

HEX64 = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[a-z0-9._-]{1,64}")
NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
MODEL_URL = re.compile(r"https://huggingface\.co/[\w.-]+/[\w.-]+/resolve/[0-9a-f]{40}/[\w.-]+")
ARCHIVE_URL = re.compile(
    r"https://github\.com/ggml-org/whisper\.cpp/releases/download/v1\.8\.5/[\w.-]+"
)


def _check_pin(pin: vm.PinnedFile) -> None:
    assert _safe_name(pin.name) and NAME.fullmatch(pin.name)
    assert pin.size > 0
    assert HEX64.fullmatch(pin.sha256)


def test_model_pins_are_well_formed() -> None:
    for model_id, model in vm.MODELS.items():
        assert ID.fullmatch(model_id) and model.model_id == model_id
        files = vm.model_files(model_id)
        assert [p.name for p in files] == [model.weights.name, vm.VAD.name]
        for pin in files:
            _check_pin(pin)
            assert MODEL_URL.fullmatch(pin.url) and pin.url.endswith("/" + pin.name)
        assert len({p.name for p in files}) == len(files)


def test_backend_pins_are_well_formed() -> None:
    cuda = vm.BACKENDS["cuda"]
    assert cuda.archive is not None
    _check_pin(cuda.archive)
    assert ARCHIVE_URL.fullmatch(cuda.archive.url)
    assert len(cuda.files) == 8
    names = [p.name for p in cuda.files]
    assert len(set(names)) == 8
    for pin in cuda.files:
        _check_pin(pin)
        assert pin.member.startswith("Release/") and _safe_name(pin.member[len("Release/") :])
    assert cuda.binding in names
    for backend_id, backend in vm.BACKENDS.items():
        assert ID.fullmatch(backend_id) and backend.backend_id == backend_id
    for backend in (vm.BACKENDS["cpu"], vm.BACKENDS["metal"]):
        assert backend.archive is None and backend.files == ()


def test_languages_are_well_formed() -> None:
    # Only the GPU rankings must carry ja and en: the CPU model small-q8_0 was measured without ja
    for model_id in {*vm.RANKING["cuda"], *vm.RANKING["metal"]}:
        assert {"ja", "en"} <= set(vm.MODELS[model_id].languages)
    for model in vm.MODELS.values():
        assert len(set(model.languages)) == len(model.languages)
        assert all(re.fullmatch(r"[a-z]{2,3}", c) for c in model.languages)
        assert "auto" not in model.languages
    assert vm.DICTATION_LANGUAGES[0] == "auto"
    assert len(set(vm.DICTATION_LANGUAGES)) == len(vm.DICTATION_LANGUAGES)
    union = {c for m in vm.MODELS.values() for c in m.languages}
    assert set(vm.DICTATION_LANGUAGES[1:]) == union


def test_ranking_names_pinned_models_only() -> None:
    assert set(vm.RANKING) == set(vm.BACKENDS)
    for ranking in vm.RANKING.values():
        assert set(ranking) <= set(vm.MODELS)
    assert set(vm.READY_TIMEOUT_S) == set(vm.WORKER_MEMORY_BYTES) == set(vm.BACKENDS)


def test_only_small_uses_proportional_partials() -> None:
    assert vm.MODELS["small-q8_0"].proportional_partials is True
    assert vm.MODELS["large-v3-turbo-q5_0"].proportional_partials is False
    assert vm.MODELS["large-v3-turbo-q8_0"].proportional_partials is False


def test_directories(tmp_path: Path) -> None:
    assert vm.model_dir(tmp_path, "m") == tmp_path / "voice" / "models" / "m"
    assert vm.backend_dir(tmp_path, "cuda") == tmp_path / "voice" / "backends" / "cuda"


@pytest.mark.parametrize(
    ("platform", "machine", "expected"),
    [
        ("win32", "AMD64", ("cuda", "cpu")),
        ("win32", "ARM64", ("cpu",)),
        ("darwin", "arm64", ("metal", "cpu")),
        ("darwin", "x86_64", ("cpu",)),
        ("linux", "x86_64", ("cpu",)),
        ("linux", "aarch64", ("cpu",)),
    ],
)
def test_offered(platform: str, machine: str, expected: tuple[str, ...]) -> None:
    assert vm.offered(platform, machine) == expected


TURBO = "large-v3-turbo-q8_0"
SMALL = "small-q8_0"


@pytest.mark.parametrize(
    ("platform", "machine", "started", "models", "want_model", "expected"),
    [
        # a started cuda wins whether its files are whole or broken (the engine passes a folder
        # that exists), so its start check can report it and the fallback is visible
        ("win32", "AMD64", {"cuda", "cpu"}, {TURBO, SMALL}, None, "cuda"),
        # the default install with the backend folders deleted: only the cpu is started
        ("win32", "AMD64", {"cpu"}, {TURBO}, None, "cpu"),
        ("win32", "AMD64", set(), set(), None, "cpu"),
        # a started cuda with no ranked model installed cannot run: the cpu with its model
        ("win32", "AMD64", {"cuda", "cpu"}, {SMALL}, None, "cpu"),
        # Apple silicon with metal started and only the small model installed
        ("darwin", "arm64", {"metal", "cpu"}, {SMALL}, None, "cpu"),
        ("darwin", "arm64", {"metal", "cpu"}, {TURBO, SMALL}, None, "metal"),
        # a backend this machine does not offer is never chosen
        ("linux", "x86_64", {"cuda", "metal", "cpu"}, {TURBO, SMALL}, None, "cpu"),
        # a pinned model needs no ranking: the first started backend wins
        ("win32", "AMD64", {"cuda", "cpu"}, set(), "large-v3-turbo-q5_0", "cuda"),
        ("darwin", "arm64", {"metal", "cpu"}, {SMALL}, SMALL, "metal"),
        ("win32", "AMD64", {"cpu"}, set(), SMALL, "cpu"),
    ],
)
def test_auto_backend(
    platform: str,
    machine: str,
    started: set[str],
    models: set[str],
    want_model: str | None,
    expected: str,
) -> None:
    assert vm.auto_backend(platform, machine, started, models, want_model) == expected


def test_q5_is_pinned_but_not_ranked() -> None:
    # its latency on the shipped path is unmeasured (partials run without the VAD)
    assert "large-v3-turbo-q5_0" in vm.MODELS
    assert all("large-v3-turbo-q5_0" not in ranking for ranking in vm.RANKING.values())


def test_auto_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(vm.RANKING, "cuda", ("large-v3-turbo-q8_0", "small-q8_0"))
    assert vm.auto_model("cuda", {"small-q8_0", "large-v3-turbo-q8_0"}) == "large-v3-turbo-q8_0"
    assert vm.auto_model("cuda", ["small-q8_0"]) == "small-q8_0"
    assert vm.auto_model("cuda", {"large-v3-turbo-q5_0"}) is None


def _pin(directory: Path, name: str, data: bytes) -> vm.PinnedFile:
    (directory / name).write_bytes(data)
    return vm.PinnedFile(name, len(data), hashlib.sha256(data).hexdigest())


def test_verify_accepts_matching_files(tmp_path: Path) -> None:
    files = (_pin(tmp_path, "a.bin", b"alpha"), _pin(tmp_path, "b.bin", b"beta"))
    assert vm.verify(tmp_path, files) is None
    assert vm.present(tmp_path, files)


def test_verify_reports_missing(tmp_path: Path) -> None:
    pin = _pin(tmp_path, "a.bin", b"alpha")
    (tmp_path / "a.bin").unlink()
    assert vm.verify(tmp_path, (pin,)) == "missing"
    assert not vm.present(tmp_path, (pin,))


def test_verify_reports_size_mismatch(tmp_path: Path) -> None:
    pin = _pin(tmp_path, "a.bin", b"alpha")
    (tmp_path / "a.bin").write_bytes(b"alph")
    assert vm.verify(tmp_path, (pin,)) == "size_mismatch"
    assert not vm.present(tmp_path, (pin,))


def test_verify_reports_hash_mismatch(tmp_path: Path) -> None:
    pin = _pin(tmp_path, "a.bin", b"alpha")
    (tmp_path / "a.bin").write_bytes(b"ALPHA")
    assert vm.verify(tmp_path, (pin,)) == "hash_mismatch"
    assert vm.present(tmp_path, (pin,))


def test_verify_reports_first_problem_in_file_order(tmp_path: Path) -> None:
    first = _pin(tmp_path, "a.bin", b"alpha")
    second = _pin(tmp_path, "b.bin", b"beta")
    (tmp_path / "a.bin").write_bytes(b"ALPHA")
    (tmp_path / "b.bin").unlink()
    assert vm.verify(tmp_path, (first, second)) == "hash_mismatch"


def test_verify_reports_folder_as_not_regular(tmp_path: Path) -> None:
    (tmp_path / "a.bin").mkdir()
    pin = vm.PinnedFile("a.bin", 5, "0" * 64)
    assert vm.verify(tmp_path, (pin,)) == "not_regular"
    assert not vm.present(tmp_path, (pin,))


def test_verify_reports_symlink_as_not_regular(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    pin = _pin(tmp_path, "target.bin", b"alpha")
    try:
        os.symlink(target, tmp_path / "a.bin")
    except OSError, NotImplementedError:
        pytest.skip("symlinks unavailable")
    link_pin = vm.PinnedFile("a.bin", pin.size, pin.sha256)
    assert vm.verify(tmp_path, (link_pin,)) == "not_regular"
    assert not vm.present(tmp_path, (link_pin,))
