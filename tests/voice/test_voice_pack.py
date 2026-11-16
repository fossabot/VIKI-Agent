from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from viki_agent.voice import voice_pack as vp
from viki_agent.voice.voice_models import PinnedFile

HEX64_RE = re.compile(r"[0-9a-f]{64}")

_ARCHIVE_SPECS = {
    "voicebox-server-cuda.tar.gz": (
        1_034_697_956,
        "976b99e0e818ecd620b4a9a421cc6b8fced6153902d0e41f6dd254f6e5d94db4",
    ),
    "cuda-libs-cu128-v1.tar.gz": (
        2_111_927_787,
        "df83ae5aa2e482af999152be5c84e5e305de15e9cea860af7dea75f18fafa663",
    ),
}

_MODEL_FILE_SPECS = {
    ".gitattributes": (1519, "11ad7efa24975ee4b0c3c3a38ed18737f0658a5f75a0a96787b576a78a023361"),
    "README.md": (3263, "1e3adeecc7a72d6756fdb77c2847f8e994195e105b51206a7a5c049b0dfa48a8"),
    "config.json": (4908, "81aca2b6fac304944d8acf345272d8a9a727d5fc2e2e66b222ab4729340c7455"),
    "generation_config.json": (
        245,
        "f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa",
    ),
    "merges.txt": (1671839, "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"),
    "model.safetensors": (
        1811626576,
        "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb",
    ),
    "preprocessor_config.json": (
        127,
        "efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119",
    ),
    "speech_tokenizer/config.json": (
        2336,
        "ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167",
    ),
    "speech_tokenizer/configuration.json": (
        76,
        "6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd",
    ),
    "speech_tokenizer/model.safetensors": (
        682293092,
        "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
    ),
    "speech_tokenizer/preprocessor_config.json": (
        234,
        "fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb",
    ),
    "tokenizer_config.json": (
        7344,
        "dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670",
    ),
    "vocab.json": (2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
}

_KOKORO_FILE_SPECS = {
    "config.json": (2351, "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f"),
    "kokoro-v1_0.pth": (
        327212226,
        "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4",
    ),
    "voices/af_alloy.pt": (
        523425,
        "6d877149dd8b348fbad12e5845b7e43d975390e9f3b68a811d1d86168bef5aa3",
    ),
    "voices/af_aoede.pt": (
        523425,
        "c03bd1a4c3716c2d8eaa3d50022f62d5c31cfbd6e15933a00b17fefe13841cc4",
    ),
    "voices/af_bella.pt": (
        523425,
        "8cb64e02fcc8de0327a8e13817e49c76c945ecf0052ceac97d3081480e8e48d6",
    ),
    "voices/af_heart.pt": (
        523425,
        "0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff",
    ),
    "voices/af_jessica.pt": (
        523435,
        "cdfdccb8cc975aa34ee6b89642963b0064237675de0e41a30ae64cc958dd4e87",
    ),
    "voices/af_kore.pt": (
        523420,
        "8bfbc512321c3db49dff984ac675fa5ac7eaed5a96cc31104d3a9080e179d69d",
    ),
    "voices/af_nicole.pt": (
        523430,
        "c5561808bcf5250fe8c5f5de32caf2d94f27e57e95befdb098c5c85991d4c5da",
    ),
    "voices/af_nova.pt": (
        523420,
        "e0233676ddc21908c37a1f102f6b88a59e4e5c1bd764983616eb9eda629dbcd2",
    ),
    "voices/af_river.pt": (
        523425,
        "e149459bd9c084416b74756b9bd3418256a8b839088abb07d463730c369dab8f",
    ),
    "voices/af_sarah.pt": (
        523425,
        "49bd364ea3be9eb3e9685e8f9a15448c4883112a7c0ff7ab139fa4088b08cef9",
    ),
    "voices/af_sky.pt": (
        523351,
        "c799548aed06e0cb0d655a85a01b48e7f10484d71663f9a3045a5b9362e8512c",
    ),
    "voices/am_adam.pt": (
        523420,
        "ced7e284aba12472891be1da3ab34db84cc05cc02b5889535796dbf2d8b0cb34",
    ),
    "voices/am_echo.pt": (
        523420,
        "8bcfdc852bc985fb45c396c561e571ffb9183930071f962f1b50df5c97b161e8",
    ),
    "voices/am_eric.pt": (
        523420,
        "ada66f0eefff34ec921b1d7474d7ac8bec00cd863c170f1c534916e9b8212aae",
    ),
    "voices/am_fenrir.pt": (
        523430,
        "98e507eca1db08230ae3b6232d59c10aec9630022d19accac4f5d12fcec3c37a",
    ),
    "voices/am_liam.pt": (
        523420,
        "c82550757ddb31308b97f30040dda8c2d609a9e2de6135848d0a948368138518",
    ),
    "voices/am_michael.pt": (
        523435,
        "9a443b79a4b22489a5b0ab7c651a0bcd1a30bef675c28333f06971abbd47bd37",
    ),
    "voices/am_onyx.pt": (
        523420,
        "e8452be16cd0f6da7b4579eaf7b1e4506e92524882053d86d72b96b9a7fed584",
    ),
    "voices/am_puck.pt": (
        523420,
        "dd1d8973f4ce4b7d8ae407c77a435f485dabc052081b80ea75c4f30b84f36223",
    ),
    "voices/am_santa.pt": (
        523425,
        "7f2f7582fa2b1f160e90aafe6d0b442a685e773608b6667e545d743b073e97a7",
    ),
    "voices/bf_alice.pt": (
        523425,
        "d292651b6af6c0d81705c2580dcb4463fccc0ff7b8d618a471dbb4e45655b3f3",
    ),
    "voices/bf_emma.pt": (
        523420,
        "d0a423deabf4a52b4f49318c51742c54e21bb89bbbe9a12141e7758ddb5da701",
    ),
    "voices/bf_isabella.pt": (
        523440,
        "cdd4c37003805104d1d08fb1e05855c8fb2c68de24ca6e71f264a30aaa59eefd",
    ),
    "voices/bf_lily.pt": (
        523420,
        "6e09c2e481e2d53004d7e5ae7d3a325369e130a6f45c35a6002de75084be9285",
    ),
    "voices/bm_daniel.pt": (
        523430,
        "fc3fce4e9c12ed4dbc8fa9680cfe51ee190a96444ce7c3ad647549a30823fc5d",
    ),
    "voices/bm_fable.pt": (
        523425,
        "d44935f3135257a9064df99f007fc1342ff1aa767552b4a4fa4c3b2e6e59079c",
    ),
    "voices/bm_george.pt": (
        523430,
        "f1bc812213dc59774769e5c80004b13eeb79bd78130b11b2d7f934542dab811b",
    ),
    "voices/bm_lewis.pt": (
        523425,
        "b5204750dcba01029d2ac9cec17aec3b20a6d64073c579d694a23cb40effbd0e",
    ),
    "voices/ef_dora.pt": (
        523420,
        "d9d69b0f8a2b87a345f269d89639f89dfbd1a6c9da0c498ae36dd34afcf35530",
    ),
    "voices/em_alex.pt": (
        523420,
        "5eac53f767c3f31a081918ba531969aea850bed18fe56419b804d642c6973431",
    ),
    "voices/em_santa.pt": (
        523430,
        "aa8620cb96cec705823efca0d956a63e158e09ad41aca934d354b7f0778f63cb",
    ),
    "voices/ff_siwis.pt": (
        523425,
        "8073bf2d2c4b9543a90f2f0fd2144de4ed157e2d4b79ddeb0d5123066171fbc9",
    ),
    "voices/if_sara.pt": (
        523425,
        "6c0b253b955fe32f1a1a86006aebe83d050ea95afd0e7be15182f087deedbf55",
    ),
    "voices/im_nicola.pt": (
        523341,
        "234ed06648649f9bd874b37508ea17560b9c993ef85b4ddb3e3a71e062bd2c12",
    ),
    "voices/pf_dora.pt": (
        523425,
        "07e4ff987c5d5a8c3995efd15cc4f0db7c4c15e881b198d8ab7f67ecf51f5eb7",
    ),
    "voices/pm_alex.pt": (
        523425,
        "cf0ba8c573c2480fc54123683a35cf1e2ae130428e441eb91f9149bdb188a526",
    ),
    "voices/pm_santa.pt": (
        523430,
        "d42103169c5c872abbafb9129133af7e942bb9d272c3cc3b95c203e7d7198c29",
    ),
    "voices/zf_xiaobei.pt": (
        523435,
        "9b76be63dab4f4f96962030acc0126a9aee9728608fbbe115e2b58a2bd504df6",
    ),
    "voices/zf_xiaoni.pt": (
        523430,
        "95b49f169bf1640f4f43c25e13daa39f7b98d15d00823e83ef5c14b3ced59e49",
    ),
    "voices/zf_xiaoxiao.pt": (
        523440,
        "cfaf6f2ded1ee56f1ff94fcd2b0e6cdf32e5b794bdc05b44e7439d44aef5887c",
    ),
    "voices/zf_xiaoyi.pt": (
        523430,
        "b5235dbaeef85a4c613bf78af9a88ff63c25bac5f26ba77e36186d8b7ebf05e2",
    ),
}


def _check_pin(pin: PinnedFile) -> None:
    assert pin.size > 0
    assert HEX64_RE.fullmatch(pin.sha256)
    assert pin.url.endswith("/" + pin.name)


def test_pins_match_the_primary_sources() -> None:
    assert vp.VOICEBOX_VERSION == "0.5.0"
    assert vp.SERVER_EXE == "voicebox-server-cuda.exe"
    assert [a.name for a in vp.ARCHIVES] == list(_ARCHIVE_SPECS)
    assert len({a.name for a in vp.ARCHIVES}) == len(vp.ARCHIVES)
    for archive in vp.ARCHIVES:
        size, sha256 = _ARCHIVE_SPECS[archive.name]
        assert (archive.size, archive.sha256) == (size, sha256)
        _check_pin(archive)
        assert archive.url == (
            f"https://github.com/jamiepine/voicebox/releases/download/v0.5.0/{archive.name}"
        )
    assert vp.UNPACKED_BYTES == 2_275_133_440 + 3_105_300_480

    assert vp.MODEL_REPO == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    assert vp.MODEL_REVISION == "85e237c12c027371202489a0ec509ded67b5e4b5"
    assert vp.MODEL_SIZE == "0.6B"
    assert [f.name for f in vp.MODEL_FILES] == list(_MODEL_FILE_SPECS)
    assert len({f.name for f in vp.MODEL_FILES}) == len(vp.MODEL_FILES)
    for pin in vp.MODEL_FILES:
        size, sha256 = _MODEL_FILE_SPECS[pin.name]
        assert (pin.size, pin.sha256) == (size, sha256)
        _check_pin(pin)
        assert pin.url == (
            "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/resolve/"
            f"85e237c12c027371202489a0ec509ded67b5e4b5/{pin.name}"
        )

    assert vp.KOKORO_REPO == "hexgrad/Kokoro-82M"
    assert vp.KOKORO_REVISION == "f3ff3571791e39611d31c381e3a41a3af07b4987"
    assert [f.name for f in vp.KOKORO_FILES] == list(_KOKORO_FILE_SPECS)
    assert len({f.name for f in vp.KOKORO_FILES}) == len(vp.KOKORO_FILES) == 43
    for pin in vp.KOKORO_FILES:
        size, sha256 = _KOKORO_FILE_SPECS[pin.name]
        assert (pin.size, pin.sha256) == (size, sha256)
        _check_pin(pin)
        assert pin.url == (
            "https://huggingface.co/hexgrad/Kokoro-82M/resolve/"
            f"f3ff3571791e39611d31c381e3a41a3af07b4987/{pin.name}"
        )

    archives_bytes = sum(size for size, _ in _ARCHIVE_SPECS.values())
    model_bytes = sum(size for size, _ in _MODEL_FILE_SPECS.values())
    kokoro_bytes = sum(size for size, _ in _KOKORO_FILE_SPECS.values())
    assert kokoro_bytes == 348_674_884
    assert vp.DOWNLOAD_BYTES == archives_bytes + model_bytes + kokoro_bytes == 5_993_689_019
    assert vp.DISK_BYTES == vp.UNPACKED_BYTES + model_bytes + kokoro_bytes == 8_227_497_196
    assert (
        archives_bytes + vp.UNPACKED_BYTES + model_bytes + kokoro_bytes + 1024**3 == vp.NEED_BYTES
    )
    assert vp.NEED_BYTES == 12_447_864_763


def test_small_constant_tables() -> None:
    assert vp.LANGUAGES == ("zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it")
    assert set(vp.VOICES) == set(vp.LANGUAGES)
    assert vp.VOICES["zh"] == "Vivian"
    assert vp.VOICES["en"] == "Ryan"
    assert vp.VOICES["ja"] == "Ono_Anna"
    assert vp.VOICES["ko"] == "Sohee"
    for lang in ("de", "fr", "ru", "pt", "es", "it"):
        assert vp.VOICES[lang] == "Ryan"
    assert vp.ENGINES == ("system", "pack", "voicebox")
    assert vp.MIN_DRIVER == (570, 65)
    assert vp.MIN_VRAM_MIB == 4096
    assert vp.SETUP_CODES == (
        "network",
        "write_failed",
        "not_regular",
        "size_mismatch",
        "hash_mismatch",
        "bad_range",
        "bad_archive",
        "disk_full",
        "private_folder",
    )
    assert vp.STEPS == ("server", "unpack", "model")


def test_path_helpers(tmp_path: Path) -> None:
    pack = vp.pack_dir(tmp_path)
    assert pack == tmp_path / "voice" / "pack"
    assert vp.server_dir(tmp_path) == pack / "s"
    assert vp.partial_dir(tmp_path) == pack / "s.tmp"
    assert vp.downloads_dir(tmp_path) == pack / "dl"
    assert vp.models_dir(tmp_path) == pack / "models"
    assert vp.repo_dir(tmp_path) == (
        vp.models_dir(tmp_path) / "models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice"
    )
    assert vp.snapshot_dir(tmp_path) == vp.repo_dir(tmp_path) / "snapshots" / vp.MODEL_REVISION
    assert vp.kokoro_repo_dir(tmp_path) == vp.models_dir(tmp_path) / "models--hexgrad--Kokoro-82M"
    assert (
        vp.kokoro_snapshot_dir(tmp_path)
        == vp.kokoro_repo_dir(tmp_path) / "snapshots" / vp.KOKORO_REVISION
    )
    assert vp.state_dir(tmp_path) == pack / "data"
    assert vp.hf_home(tmp_path) == pack / "hf"


_FAKE_FILES = (PinnedFile("a.bin", 3, "0" * 64), PinnedFile("sub/b.bin", 2, "1" * 64))


def _make_installed(data_dir: Path, files: tuple[PinnedFile, ...]) -> None:
    exe = vp.server_dir(data_dir) / vp.SERVER_EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    refs = vp.repo_dir(data_dir) / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(vp.MODEL_REVISION, encoding="utf-8")
    snapshot = vp.snapshot_dir(data_dir)
    snapshot.mkdir(parents=True)
    for pin in files:
        target = snapshot / pin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\0" * pin.size)


def test_installed_true_when_all_present(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    assert vp.installed(tmp_path, _FAKE_FILES) is True


def test_installed_false_missing_exe(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    (vp.server_dir(tmp_path) / vp.SERVER_EXE).unlink()
    assert vp.installed(tmp_path, _FAKE_FILES) is False


def test_installed_false_exe_is_directory(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    exe = vp.server_dir(tmp_path) / vp.SERVER_EXE
    exe.unlink()
    exe.mkdir()
    assert vp.installed(tmp_path, _FAKE_FILES) is False


def test_installed_false_wrong_revision(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    (vp.repo_dir(tmp_path) / "refs" / "main").write_text("deadbeef", encoding="utf-8")
    assert vp.installed(tmp_path, _FAKE_FILES) is False


def test_installed_false_missing_ref_file(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    (vp.repo_dir(tmp_path) / "refs" / "main").unlink()
    assert vp.installed(tmp_path, _FAKE_FILES) is False


def test_installed_false_missing_model_file(tmp_path: Path) -> None:
    _make_installed(tmp_path, _FAKE_FILES)
    (vp.snapshot_dir(tmp_path) / "a.bin").unlink()
    assert vp.installed(tmp_path, _FAKE_FILES) is False


def _make_kokoro_installed(data_dir: Path, files: tuple[PinnedFile, ...]) -> None:
    refs = vp.kokoro_repo_dir(data_dir) / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(vp.KOKORO_REVISION, encoding="utf-8")
    snapshot = vp.kokoro_snapshot_dir(data_dir)
    snapshot.mkdir(parents=True)
    for pin in files:
        target = snapshot / pin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\0" * pin.size)


def test_kokoro_installed_true_when_all_present(tmp_path: Path) -> None:
    _make_kokoro_installed(tmp_path, _FAKE_FILES)
    assert vp.kokoro_installed(tmp_path, _FAKE_FILES) is True


def test_kokoro_installed_false_wrong_revision(tmp_path: Path) -> None:
    _make_kokoro_installed(tmp_path, _FAKE_FILES)
    (vp.kokoro_repo_dir(tmp_path) / "refs" / "main").write_text("deadbeef", encoding="utf-8")
    assert vp.kokoro_installed(tmp_path, _FAKE_FILES) is False


def test_kokoro_installed_false_missing_ref_file(tmp_path: Path) -> None:
    _make_kokoro_installed(tmp_path, _FAKE_FILES)
    (vp.kokoro_repo_dir(tmp_path) / "refs" / "main").unlink()
    assert vp.kokoro_installed(tmp_path, _FAKE_FILES) is False


def test_kokoro_installed_false_missing_voice_file(tmp_path: Path) -> None:
    _make_kokoro_installed(tmp_path, _FAKE_FILES)
    (vp.kokoro_snapshot_dir(tmp_path) / "a.bin").unlink()
    assert vp.kokoro_installed(tmp_path, _FAKE_FILES) is False


_MISSING = object()


@pytest.mark.parametrize(
    ("engine", "profile", "expected"),
    [
        ("system", "p", "system"),
        ("pack", None, "pack"),
        ("voicebox", None, "voicebox"),
        (None, "p", "voicebox"),
        (None, None, "system"),
        ("bogus", "p", "voicebox"),
        (_MISSING, 7, "system"),
    ],
)
def test_speech_engine_table(engine: object, profile: object, expected: str) -> None:
    # The flat effective settings, as `settings.read().effective` hands them over.
    settings: dict[str, object] = {"voice.voicebox_profile": profile}
    if engine is not _MISSING:
        settings["voice.speech_engine"] = engine
    assert vp.speech_engine(settings) == expected


def test_speech_engine_defaults_to_system_without_voice_keys() -> None:
    assert vp.speech_engine({}) == "system"


def test_speech_engine_ignores_a_nested_voice_section() -> None:
    assert vp.speech_engine({"voice": {"speech_engine": "pack"}}) == "system"


def test_parse_gpu_reads_first_line() -> None:
    gpu = vp.parse_gpu("NVIDIA GeForce RTX 4070 Laptop GPU, 8188, 572.47")
    assert gpu == vp.Gpu("NVIDIA GeForce RTX 4070 Laptop GPU", 8188, (572, 47))


def test_parse_gpu_ignores_lines_after_the_first() -> None:
    gpu = vp.parse_gpu("NVIDIA GeForce RTX 4070 Laptop GPU, 8188, 572.47\nextra garbage\n")
    assert gpu is not None
    assert gpu.memory_mib == 8188


@pytest.mark.parametrize(
    ("driver_s", "expected"),
    [("99.9", (99, 9)), ("9999.999", (9999, 999))],
)
def test_parse_gpu_accepts_driver_boundaries(driver_s: str, expected: tuple[int, int]) -> None:
    gpu = vp.parse_gpu(f"GPU, 1024, {driver_s}")
    assert gpu is not None
    assert gpu.driver == expected


def test_parse_gpu_name_length_and_printable_bounds() -> None:
    assert vp.parse_gpu(f"{'A' * 256}, 1024, 572.47") is not None
    assert vp.parse_gpu(f"{'A' * 257}, 1024, 572.47") is None
    assert vp.parse_gpu("bad\x00name, 1024, 572.47") is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "only, two",
        "a, b, c, d",
        ", 8188, 572.47",
        "GPU, -8, 572.47",
        "GPU, 0, 572.47",
        "GPU, 8188, 5.47",
        "GPU, 8188, 572.4700",
        "GPU, 8188, abc",
        "GPU, abc, 572.47",
    ],
)
def test_parse_gpu_rejects_malformed_lines(stdout: str) -> None:
    assert vp.parse_gpu(stdout) is None


def test_unsupported_platform_check_wins_over_a_good_probe() -> None:
    gpu = vp.Gpu("x", 8192, (999, 0))
    assert vp.unsupported("darwin", "arm64", None) == "platform"
    assert vp.unsupported("win32", "ARM64", gpu) == "platform"
    assert vp.unsupported("linux", "AMD64", gpu) == "platform"


def test_unsupported_not_probed_yet_is_none() -> None:
    assert vp.unsupported("win32", "AMD64", None) is None


@pytest.mark.parametrize("code", ["no_nvidia", "probe_failed"])
def test_unsupported_passes_probe_codes_through(code: str) -> None:
    assert vp.unsupported("win32", "AMD64", code) == code


def test_unsupported_driver_old() -> None:
    gpu = vp.Gpu("x", 8192, (570, 64))
    assert vp.unsupported("win32", "AMD64", gpu) == "driver_old"


def test_unsupported_low_vram() -> None:
    gpu = vp.Gpu("x", 4095, (999, 0))
    assert vp.unsupported("win32", "AMD64", gpu) == "low_vram"


def test_unsupported_ok_at_the_exact_minimums() -> None:
    gpu = vp.Gpu("x", 4096, (570, 65))
    assert vp.unsupported("win32", "AMD64", gpu) is None


def test_free_bytes_uses_the_nearest_existing_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    real_disk_usage = shutil.disk_usage

    def fake_disk_usage(path: Path):
        seen.append(Path(path))
        return real_disk_usage(path)._replace(free=4096)

    monkeypatch.setattr(vp.shutil, "disk_usage", fake_disk_usage)
    result = vp.free_bytes(tmp_path)
    # A fixed free count, since the real one moves while other processes write.
    assert result == 4096
    assert seen == [tmp_path]


def test_free_bytes_none_on_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: Path):
        raise OSError("no disk")

    monkeypatch.setattr(vp.shutil, "disk_usage", boom)
    assert vp.free_bytes(tmp_path) is None
