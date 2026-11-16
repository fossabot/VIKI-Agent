"""Pins for the Voicebox 0.5.0 CUDA server and its Qwen3-TTS model, and the setup's pure helpers.

Stdlib only (plus ``viki_agent.voice_models`` for ``PinnedFile``/``present``): a setup child
process imports this module too.
"""

from __future__ import annotations

import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from viki_agent.voice.voice_models import PinnedFile, present

VOICEBOX_VERSION = "0.5.0"
SERVER_EXE = "voicebox-server-cuda.exe"

_RELEASE_BASE = f"https://github.com/jamiepine/voicebox/releases/download/v{VOICEBOX_VERSION}/"


def _archive(name: str, size: int, sha256: str) -> PinnedFile:
    return PinnedFile(name, size, sha256, _RELEASE_BASE + name)


ARCHIVES: tuple[PinnedFile, ...] = (
    _archive(
        "voicebox-server-cuda.tar.gz",
        1_034_697_956,
        "976b99e0e818ecd620b4a9a421cc6b8fced6153902d0e41f6dd254f6e5d94db4",
    ),
    _archive(
        "cuda-libs-cu128-v1.tar.gz",
        2_111_927_787,
        "df83ae5aa2e482af999152be5c84e5e305de15e9cea860af7dea75f18fafa663",
    ),
)
# The two archives' unpacked footprint on disk (server tree + extracted CUDA libs).
UNPACKED_BYTES = 2_275_133_440 + 3_105_300_480

MODEL_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_SIZE = "0.6B"

_HF_BASE = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/"


def _hf(name: str, size: int, sha256: str) -> PinnedFile:
    return PinnedFile(name, size, sha256, _HF_BASE + name)


MODEL_FILES: tuple[PinnedFile, ...] = (
    _hf(
        ".gitattributes",
        1519,
        "11ad7efa24975ee4b0c3c3a38ed18737f0658a5f75a0a96787b576a78a023361",
    ),
    _hf(
        "README.md",
        3263,
        "1e3adeecc7a72d6756fdb77c2847f8e994195e105b51206a7a5c049b0dfa48a8",
    ),
    _hf(
        "config.json",
        4908,
        "81aca2b6fac304944d8acf345272d8a9a727d5fc2e2e66b222ab4729340c7455",
    ),
    _hf(
        "generation_config.json",
        245,
        "f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa",
    ),
    _hf(
        "merges.txt",
        1671839,
        "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3",
    ),
    _hf(
        "model.safetensors",
        1811626576,
        "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb",
    ),
    _hf(
        "preprocessor_config.json",
        127,
        "efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119",
    ),
    _hf(
        "speech_tokenizer/config.json",
        2336,
        "ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167",
    ),
    _hf(
        "speech_tokenizer/configuration.json",
        76,
        "6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd",
    ),
    _hf(
        "speech_tokenizer/model.safetensors",
        682293092,
        "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
    ),
    _hf(
        "speech_tokenizer/preprocessor_config.json",
        234,
        "fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb",
    ),
    _hf(
        "tokenizer_config.json",
        7344,
        "dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670",
    ),
    _hf(
        "vocab.json",
        2776833,
        "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    ),
)

KOKORO_REPO = "hexgrad/Kokoro-82M"
KOKORO_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"

_KOKORO_BASE = f"https://huggingface.co/{KOKORO_REPO}/resolve/{KOKORO_REVISION}/"


def _kokoro(name: str, size: int, sha256: str) -> PinnedFile:
    return PinnedFile(name, size, sha256, _KOKORO_BASE + name)


KOKORO_FILES: tuple[PinnedFile, ...] = (
    _kokoro(
        "config.json",
        2351,
        "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f",
    ),
    _kokoro(
        "kokoro-v1_0.pth",
        327212226,
        "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4",
    ),
    _kokoro(
        "voices/af_alloy.pt",
        523425,
        "6d877149dd8b348fbad12e5845b7e43d975390e9f3b68a811d1d86168bef5aa3",
    ),  # Alloy, female, en
    _kokoro(
        "voices/af_aoede.pt",
        523425,
        "c03bd1a4c3716c2d8eaa3d50022f62d5c31cfbd6e15933a00b17fefe13841cc4",
    ),  # Aoede, female, en
    _kokoro(
        "voices/af_bella.pt",
        523425,
        "8cb64e02fcc8de0327a8e13817e49c76c945ecf0052ceac97d3081480e8e48d6",
    ),  # Bella, female, en
    _kokoro(
        "voices/af_heart.pt",
        523425,
        "0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff",
    ),  # Heart, female, en
    _kokoro(
        "voices/af_jessica.pt",
        523435,
        "cdfdccb8cc975aa34ee6b89642963b0064237675de0e41a30ae64cc958dd4e87",
    ),  # Jessica, female, en
    _kokoro(
        "voices/af_kore.pt",
        523420,
        "8bfbc512321c3db49dff984ac675fa5ac7eaed5a96cc31104d3a9080e179d69d",
    ),  # Kore, female, en
    _kokoro(
        "voices/af_nicole.pt",
        523430,
        "c5561808bcf5250fe8c5f5de32caf2d94f27e57e95befdb098c5c85991d4c5da",
    ),  # Nicole, female, en
    _kokoro(
        "voices/af_nova.pt",
        523420,
        "e0233676ddc21908c37a1f102f6b88a59e4e5c1bd764983616eb9eda629dbcd2",
    ),  # Nova, female, en
    _kokoro(
        "voices/af_river.pt",
        523425,
        "e149459bd9c084416b74756b9bd3418256a8b839088abb07d463730c369dab8f",
    ),  # River, female, en
    _kokoro(
        "voices/af_sarah.pt",
        523425,
        "49bd364ea3be9eb3e9685e8f9a15448c4883112a7c0ff7ab139fa4088b08cef9",
    ),  # Sarah, female, en
    _kokoro(
        "voices/af_sky.pt",
        523351,
        "c799548aed06e0cb0d655a85a01b48e7f10484d71663f9a3045a5b9362e8512c",
    ),  # Sky, female, en
    _kokoro(
        "voices/am_adam.pt",
        523420,
        "ced7e284aba12472891be1da3ab34db84cc05cc02b5889535796dbf2d8b0cb34",
    ),  # Adam, male, en
    _kokoro(
        "voices/am_echo.pt",
        523420,
        "8bcfdc852bc985fb45c396c561e571ffb9183930071f962f1b50df5c97b161e8",
    ),  # Echo, male, en
    _kokoro(
        "voices/am_eric.pt",
        523420,
        "ada66f0eefff34ec921b1d7474d7ac8bec00cd863c170f1c534916e9b8212aae",
    ),  # Eric, male, en
    _kokoro(
        "voices/am_fenrir.pt",
        523430,
        "98e507eca1db08230ae3b6232d59c10aec9630022d19accac4f5d12fcec3c37a",
    ),  # Fenrir, male, en
    _kokoro(
        "voices/am_liam.pt",
        523420,
        "c82550757ddb31308b97f30040dda8c2d609a9e2de6135848d0a948368138518",
    ),  # Liam, male, en
    _kokoro(
        "voices/am_michael.pt",
        523435,
        "9a443b79a4b22489a5b0ab7c651a0bcd1a30bef675c28333f06971abbd47bd37",
    ),  # Michael, male, en
    _kokoro(
        "voices/am_onyx.pt",
        523420,
        "e8452be16cd0f6da7b4579eaf7b1e4506e92524882053d86d72b96b9a7fed584",
    ),  # Onyx, male, en
    _kokoro(
        "voices/am_puck.pt",
        523420,
        "dd1d8973f4ce4b7d8ae407c77a435f485dabc052081b80ea75c4f30b84f36223",
    ),  # Puck, male, en
    _kokoro(
        "voices/am_santa.pt",
        523425,
        "7f2f7582fa2b1f160e90aafe6d0b442a685e773608b6667e545d743b073e97a7",
    ),  # Santa, male, en
    _kokoro(
        "voices/bf_alice.pt",
        523425,
        "d292651b6af6c0d81705c2580dcb4463fccc0ff7b8d618a471dbb4e45655b3f3",
    ),  # Alice, female, en
    _kokoro(
        "voices/bf_emma.pt",
        523420,
        "d0a423deabf4a52b4f49318c51742c54e21bb89bbbe9a12141e7758ddb5da701",
    ),  # Emma, female, en
    _kokoro(
        "voices/bf_isabella.pt",
        523440,
        "cdd4c37003805104d1d08fb1e05855c8fb2c68de24ca6e71f264a30aaa59eefd",
    ),  # Isabella, female, en
    _kokoro(
        "voices/bf_lily.pt",
        523420,
        "6e09c2e481e2d53004d7e5ae7d3a325369e130a6f45c35a6002de75084be9285",
    ),  # Lily, female, en
    _kokoro(
        "voices/bm_daniel.pt",
        523430,
        "fc3fce4e9c12ed4dbc8fa9680cfe51ee190a96444ce7c3ad647549a30823fc5d",
    ),  # Daniel, male, en
    _kokoro(
        "voices/bm_fable.pt",
        523425,
        "d44935f3135257a9064df99f007fc1342ff1aa767552b4a4fa4c3b2e6e59079c",
    ),  # Fable, male, en
    _kokoro(
        "voices/bm_george.pt",
        523430,
        "f1bc812213dc59774769e5c80004b13eeb79bd78130b11b2d7f934542dab811b",
    ),  # George, male, en
    _kokoro(
        "voices/bm_lewis.pt",
        523425,
        "b5204750dcba01029d2ac9cec17aec3b20a6d64073c579d694a23cb40effbd0e",
    ),  # Lewis, male, en
    _kokoro(
        "voices/ef_dora.pt",
        523420,
        "d9d69b0f8a2b87a345f269d89639f89dfbd1a6c9da0c498ae36dd34afcf35530",
    ),  # Dora, female, es
    _kokoro(
        "voices/em_alex.pt",
        523420,
        "5eac53f767c3f31a081918ba531969aea850bed18fe56419b804d642c6973431",
    ),  # Alex, male, es
    _kokoro(
        "voices/em_santa.pt",
        523430,
        "aa8620cb96cec705823efca0d956a63e158e09ad41aca934d354b7f0778f63cb",
    ),  # Santa, male, es
    _kokoro(
        "voices/ff_siwis.pt",
        523425,
        "8073bf2d2c4b9543a90f2f0fd2144de4ed157e2d4b79ddeb0d5123066171fbc9",
    ),  # Siwis, female, fr
    _kokoro(
        "voices/if_sara.pt",
        523425,
        "6c0b253b955fe32f1a1a86006aebe83d050ea95afd0e7be15182f087deedbf55",
    ),  # Sara, female, it
    _kokoro(
        "voices/im_nicola.pt",
        523341,
        "234ed06648649f9bd874b37508ea17560b9c993ef85b4ddb3e3a71e062bd2c12",
    ),  # Nicola, male, it
    _kokoro(
        "voices/pf_dora.pt",
        523425,
        "07e4ff987c5d5a8c3995efd15cc4f0db7c4c15e881b198d8ab7f67ecf51f5eb7",
    ),  # Dora, female, pt
    _kokoro(
        "voices/pm_alex.pt",
        523425,
        "cf0ba8c573c2480fc54123683a35cf1e2ae130428e441eb91f9149bdb188a526",
    ),  # Alex, male, pt
    _kokoro(
        "voices/pm_santa.pt",
        523430,
        "d42103169c5c872abbafb9129133af7e942bb9d272c3cc3b95c203e7d7198c29",
    ),  # Santa, male, pt
    _kokoro(
        "voices/zf_xiaobei.pt",
        523435,
        "9b76be63dab4f4f96962030acc0126a9aee9728608fbbe115e2b58a2bd504df6",
    ),  # Xiaobei, female, zh
    _kokoro(
        "voices/zf_xiaoni.pt",
        523430,
        "95b49f169bf1640f4f43c25e13daa39f7b98d15d00823e83ef5c14b3ced59e49",
    ),  # Xiaoni, female, zh
    _kokoro(
        "voices/zf_xiaoxiao.pt",
        523440,
        "cfaf6f2ded1ee56f1ff94fcd2b0e6cdf32e5b794bdc05b44e7439d44aef5887c",
    ),  # Xiaoxiao, female, zh
    _kokoro(
        "voices/zf_xiaoyi.pt",
        523430,
        "b5235dbaeef85a4c613bf78af9a88ff63c25bac5f26ba77e36186d8b7ebf05e2",
    ),  # Xiaoyi, female, zh
)

_archives_bytes = sum(pin.size for pin in ARCHIVES)
_model_bytes = sum(pin.size for pin in MODEL_FILES)
_kokoro_bytes = sum(pin.size for pin in KOKORO_FILES)
DOWNLOAD_BYTES = _archives_bytes + _model_bytes + _kokoro_bytes
DISK_BYTES = UNPACKED_BYTES + _model_bytes + _kokoro_bytes
NEED_BYTES = _archives_bytes + UNPACKED_BYTES + _model_bytes + _kokoro_bytes + 1024**3

LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it")
VOICES: dict[str, str] = {
    "zh": "Vivian",
    "en": "Ryan",
    "ja": "Ono_Anna",
    "ko": "Sohee",
    "de": "Ryan",
    "fr": "Ryan",
    "ru": "Ryan",
    "pt": "Ryan",
    "es": "Ryan",
    "it": "Ryan",
}

ENGINES = ("system", "pack", "voicebox")
MIN_DRIVER = (570, 65)
MIN_VRAM_MIB = 4096

SETUP_CODES = (
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
STEPS = ("server", "unpack", "model")


def pack_dir(data_dir: Path) -> Path:
    return data_dir / "voice" / "pack"


def server_dir(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "s"


def partial_dir(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "s.tmp"


def downloads_dir(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "dl"


def models_dir(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "models"


def repo_dir(data_dir: Path) -> Path:
    return models_dir(data_dir) / ("models--" + MODEL_REPO.replace("/", "--"))


def snapshot_dir(data_dir: Path) -> Path:
    return repo_dir(data_dir) / "snapshots" / MODEL_REVISION


def kokoro_repo_dir(data_dir: Path) -> Path:
    return models_dir(data_dir) / ("models--" + KOKORO_REPO.replace("/", "--"))


def kokoro_snapshot_dir(data_dir: Path) -> Path:
    return kokoro_repo_dir(data_dir) / "snapshots" / KOKORO_REVISION


def state_dir(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "data"


def hf_home(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "hf"


def _is_regular_file(path: Path) -> bool:
    """Whether `path` is a regular, non-reparse file, from lstat alone."""
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISREG(info.st_mode) and not reparse


def installed(data_dir: Path, files: tuple[PinnedFile, ...] = MODEL_FILES) -> bool:
    """Whether the server exe, the pinned model revision and its files are all in place."""
    if not _is_regular_file(server_dir(data_dir) / SERVER_EXE):
        return False
    try:
        ref = (repo_dir(data_dir) / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return ref == MODEL_REVISION and present(snapshot_dir(data_dir), files)


def kokoro_installed(data_dir: Path, files: tuple[PinnedFile, ...] = KOKORO_FILES) -> bool:
    """Whether the pinned Kokoro revision and its files are all in place."""
    try:
        ref = (kokoro_repo_dir(data_dir) / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return ref == KOKORO_REVISION and present(kokoro_snapshot_dir(data_dir), files)


def speech_engine(settings: Mapping[str, object]) -> str:
    """The effective speech engine for the flat effective settings, keyed like
    `voice.speech_engine` (table kept in sync with the frontend)."""
    engine = settings.get("voice.speech_engine")
    if isinstance(engine, str) and engine in ENGINES:
        return engine
    return "voicebox" if isinstance(settings.get("voice.voicebox_profile"), str) else "system"


@dataclass(frozen=True)
class Gpu:
    name: str
    memory_mib: int
    driver: tuple[int, int]


_MEMORY_RE = re.compile(r"\d+")
_DRIVER_RE = re.compile(r"\d{2,4}\.\d{1,3}")


def parse_gpu(stdout: str) -> Gpu | None:
    """Parse `nvidia-smi --query-gpu=name,memory.total,driver_version`'s first line."""
    lines = stdout.splitlines()
    if not lines:
        return None
    fields = lines[0].split(",")
    if len(fields) != 3:
        return None
    name, memory_s, driver_s = (field.strip() for field in fields)
    if not (1 <= len(name) <= 256 and name.isprintable()):
        return None
    if not _MEMORY_RE.fullmatch(memory_s):
        return None
    memory = int(memory_s)
    if memory <= 0:
        return None
    if not _DRIVER_RE.fullmatch(driver_s):
        return None
    major, minor = driver_s.split(".")
    return Gpu(name, memory, (int(major), int(minor)))


def unsupported(platform: str, machine: str, probe: Gpu | str | None) -> str | None:
    """Why the voice pack can't run here, or None if it can (or the GPU wasn't probed yet)."""
    if not (platform == "win32" and machine == "AMD64"):
        return "platform"
    if probe is None:
        return None
    if isinstance(probe, str):
        return probe
    if probe.driver < MIN_DRIVER:
        return "driver_old"
    if probe.memory_mib < MIN_VRAM_MIB:
        return "low_vram"
    return None


def free_bytes(data_dir: Path) -> int | None:
    """Free space on the nearest existing ancestor of the pack directory, or None if unreadable."""
    directory = pack_dir(data_dir)
    while not directory.exists():
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None
