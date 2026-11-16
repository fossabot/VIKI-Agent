"""compiled_smoke.py's pure helpers: a dependency-free PNG and voice-model fixture, the dist
scan's bundled-CLI detector, the Windows PE subsystem reader, the isolation-probe env/marker
pair, and the winproc-backed `run()` wrapper.

The full check list (dist scan through the voice-worker comparison) needs a real compiled
binary, which no test here builds; that is `scripts/compiled_smoke.py`'s own `__main__` run,
proved separately against the real build (ADR-054).
"""

from __future__ import annotations

import io
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import compiled_smoke  # noqa: E402

from viki_agent.media import media_worker  # noqa: E402
from viki_agent.voice import voice_models  # noqa: E402


def test_png_opens_in_pillow_at_its_size() -> None:
    from PIL import Image

    data = compiled_smoke.png(37, 11)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.size == (37, 11)
        assert image.mode == "RGB"
        assert image.format == "PNG"


def test_fake_voice_models_sizes_equal_the_pins(tmp_path: Path) -> None:
    compiled_smoke.fake_voice_models(tmp_path)
    model = voice_models.MODELS["small-q8_0"]
    model_dir = tmp_path / "models" / "small-q8_0"
    assert (model_dir / model.weights.name).stat().st_size == model.weights.size
    assert (model_dir / voice_models.VAD.name).stat().st_size == voice_models.VAD.size


def test_bundled_cli_files_finds_both_forms_but_not_a_lookalike(tmp_path: Path) -> None:
    bundled = tmp_path / "claude_agent_sdk" / "_bundled"
    bundled.mkdir(parents=True)
    (bundled / "claude.exe").write_bytes(b"x")
    # A differently-named file under the same folder: only the directory-based match (not the
    # name list) can catch this, so it proves that branch independently of the name check below.
    (bundled / "LICENSE.txt").write_bytes(b"x")
    loose = tmp_path / "other"
    loose.mkdir()
    (loose / "claude").write_bytes(b"x")
    # The product's own resources ship a file like this; it must never be mistaken for the CLI.
    decoy = tmp_path / "viki_agent" / "resources" / "starter"
    decoy.mkdir(parents=True)
    (decoy / "CLAUDE.template.md").write_bytes(b"template")

    found = {
        path.relative_to(tmp_path).as_posix() for path in compiled_smoke.bundled_cli_files(tmp_path)
    }

    assert found == {
        "claude_agent_sdk/_bundled/claude.exe",
        "claude_agent_sdk/_bundled/LICENSE.txt",
        "other/claude",
    }


def test_bundled_cli_files_is_empty_for_a_clean_dist(tmp_path: Path) -> None:
    (tmp_path / "viki_agent" / "resources").mkdir(parents=True)
    (tmp_path / "viki_agent" / "resources" / "welcome.en.md").write_bytes(b"hello")
    assert compiled_smoke.bundled_cli_files(tmp_path) == []


def _synthetic_pe(subsystem: int) -> bytes:
    """The minimal bytes `pe_subsystem` needs: a 64-byte DOS stub pointing at an immediately
    following PE header, a COFF header naming a 112-byte optional header, and that optional
    header with `subsystem` at its fixed offset 68. Not a loadable executable, just enough of
    the format for the parser under test."""
    dos = b"MZ" + b"\x00" * 58 + struct.pack("<I", 64)
    assert len(dos) == 64
    coff = struct.pack("<HHIIIHH", 0x8664, 0, 0, 0, 0, 112, 0x22)
    optional = bytearray(112)
    struct.pack_into("<H", optional, 68, subsystem)
    return dos + b"PE\x00\x00" + coff + bytes(optional)


def test_pe_subsystem_reads_the_optional_headers_subsystem_field(tmp_path: Path) -> None:
    """Built from scratch rather than read off a real GUI binary: this venv's own `pythonw.exe`
    turns out to be a byte-for-byte copy of `python.exe` (both console, subsystem 3) under this
    uv-provisioned Python, so it cannot stand in for a real subsystem-2 file here or, presumably,
    on another machine provisioned the same way."""
    gui = tmp_path / "gui.exe"
    gui.write_bytes(_synthetic_pe(2))
    console = tmp_path / "console.exe"
    console.write_bytes(_synthetic_pe(3))
    assert compiled_smoke.pe_subsystem(gui) == 2
    assert compiled_smoke.pe_subsystem(console) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="PE subsystem is Windows-only")
def test_pe_subsystem_of_the_venv_interpreter_is_console() -> None:
    assert compiled_smoke.pe_subsystem(Path(sys.executable)) == 3


def test_isolation_probe_returns_the_three_keys_with_no_markers_yet(tmp_path: Path) -> None:
    env = compiled_smoke.isolation_probe(tmp_path)
    assert set(env) == {"PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP"}
    assert all(Path(value).is_absolute() for value in env.values())
    assert compiled_smoke.markers(tmp_path) == []


def test_markers_reports_an_empty_list_for_a_folder_with_no_markers_dir(tmp_path: Path) -> None:
    assert compiled_smoke.markers(tmp_path / "never-created") == []


async def test_run_executes_python_and_returns_its_code_and_streams(tmp_path: Path) -> None:
    code, out, err = await compiled_smoke.run(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.write('hi'); sys.exit(7)"],
        cwd=tmp_path,
        env=media_worker._worker_env(sys.platform, os.environ),
    )
    assert code == 7
    assert out == b"hi"
    assert isinstance(err, bytes)


async def test_run_kills_and_returns_on_timeout(tmp_path: Path) -> None:
    code, out, _err = await compiled_smoke.run(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=media_worker._worker_env(sys.platform, os.environ),
        timeout_s=0.2,
    )
    assert code != 0
    assert out == b""
