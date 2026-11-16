"""media_worker tests decode synthetic images through the real worker process.

A JPEG carrying a Multi-Picture Format (MPF) second image — a camera preview or a phone HDR
gain map — must decode as its primary frame instead of being refused, on both the inspect and
avatar paths; every format plugin but PNG/JPEG/WEBP must stay unreachable; and Pillow must
never load in the server process.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent import protection, winproc
from viki_agent.media import assets
from viki_agent.media.assets import AssetError, AssetService
from viki_agent.storage import AssetRow, SessionStore

SCOPE = "a" * 64


class FakeStore:
    """Just enough of SessionStore for AssetService.upload: a reservation count/byte tally
    (asset_usage) and the publish call (put_asset)."""

    def __init__(self) -> None:
        self.rows: dict[str, AssetRow] = {}

    def put_asset(self, row: AssetRow) -> None:
        self.rows[row["asset_id"]] = dict(row)

    def asset_usage(self, scope: str) -> tuple[int, int]:
        rows = [row for row in self.rows.values() if row["scope"] == scope]
        return len(rows), sum(row["byte_size"] for row in rows)


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssetService:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    return AssetService(tmp_path / "assets", cast(SessionStore, FakeStore()), SCOPE)


def _mpf_jpeg() -> bytes:
    """A synthetic 1600x1200 JPEG carrying a 640x480 MPF preview, written with Pillow's own
    MPO writer: Pillow opens this as format MPO with n_frames == 2, the shape a real camera JPEG
    or phone HDR gain map takes."""
    from PIL import Image

    primary = Image.new("RGB", (1600, 1200), (10, 20, 30))
    preview = Image.new("RGB", (640, 480), (40, 50, 60))
    buffer = io.BytesIO()
    primary.save(buffer, format="MPO", save_all=True, append_images=[preview])
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_real_worker_decodes_the_mpf_primary_frame(service: AssetService) -> None:
    data = _mpf_jpeg()

    uploaded = await service.upload(_chunks(data), name="camera.jpg", declared_bytes=len(data))

    assert (uploaded.media_type, uploaded.width, uploaded.height) == ("image/jpeg", 1600, 1200)


def test_real_worker_avatar_mode_accepts_the_mpf_primary_frame(tmp_path: Path) -> None:
    (tmp_path / "source").write_bytes(_mpf_jpeg())
    worker = Path(assets.__file__).with_name("media_worker.py")

    result = winproc.run(
        [sys.executable, "-I", "-B", str(worker), "source", "--avatar", "0", "0", "100", "0"],
        cwd=tmp_path,
    )

    answer = json.loads(result.stdout)
    assert answer["ok"] is True
    assert answer["master"]["size"] == 100
    assert len(answer["variants"]) == 5


def test_real_worker_exits_4_with_invalid_crop_for_a_crop_past_the_grid(tmp_path: Path) -> None:
    from PIL import Image

    with Image.new("RGB", (8, 8), (10, 20, 30)) as image:
        image.save(tmp_path / "source", format="PNG")
    worker = Path(assets.__file__).with_name("media_worker.py")

    result = winproc.run(
        [sys.executable, "-I", "-B", str(worker), "source", "--avatar", "0", "0", "9", "0"],
        cwd=tmp_path,
    )

    assert result.returncode == 4
    assert json.loads(result.stdout) == {"ok": False, "code": "invalid_crop"}


@pytest.mark.skipif(sys.platform != "linux", reason="the address-space ceiling is Linux only")
def test_real_worker_passes_a_smooth_20_mp_source_under_the_real_ceiling(
    tmp_path: Path,
) -> None:
    """A smooth 20 MP lossless RGBA WebP (5000x4000, no ICC chunk), cropped to 4000 square at
    rotation 0, passes under the real RLIMIT_AS the app applies. No peak is measured."""
    from PIL import Image

    from viki_agent.media import avatars, media_worker

    # Smooth synthetic pixels keep the file under the worker's 5 MiB source bound.
    ramp = Image.linear_gradient("L").resize((5000, 4000), Image.Resampling.BICUBIC)
    channels = (ramp, ramp.transpose(Image.Transpose.FLIP_LEFT_RIGHT), ramp, ramp)
    with Image.merge("RGBA", channels) as image:
        image.save(tmp_path / "source", format="WEBP", lossless=True)
    worker = Path(assets.__file__).with_name("media_worker.py")

    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(worker),
            *media_worker._worker_argv_prefix("linux", avatars.WORKER_ADDRESS_SPACE_BYTES),
            "source",
            "--avatar",
            "0",
            "0",
            "4000",
            "0",
        ],
        cwd=tmp_path,
    )

    answer = json.loads(result.stdout)
    assert answer["ok"] is True
    assert answer["master"]["size"] == 1024
    assert len(answer["variants"]) == 5


@pytest.mark.skipif(sys.platform != "linux", reason="the address-space ceiling is Linux only")
def test_real_worker_passes_the_heaviest_windows_source_under_the_real_ceiling(
    tmp_path: Path,
) -> None:
    """The heaviest admitted source measured on Windows: a 4472x4472 lossless RGBA
    WebP with a 4.9 MB ICC chunk, cropped whole at rotation 90, so a quarter turn holds a
    second 20 MP grid beside the source. It passes under the real RLIMIT_AS the app applies.
    No peak is measured."""
    from PIL import Image

    from viki_agent.media import avatars, media_worker

    # Smooth synthetic pixels and a zero-filled ICC chunk keep the file under the 5 MiB bound.
    ramp = Image.linear_gradient("L").resize((4472, 4472), Image.Resampling.BICUBIC)
    channels = (ramp, ramp.transpose(Image.Transpose.FLIP_LEFT_RIGHT), ramp, ramp)
    with Image.merge("RGBA", channels) as image:
        image.save(
            tmp_path / "source", format="WEBP", lossless=True, icc_profile=b"\x00" * 4_900_000
        )
    assert (tmp_path / "source").stat().st_size <= 5 << 20
    worker = Path(assets.__file__).with_name("media_worker.py")

    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(worker),
            *media_worker._worker_argv_prefix("linux", avatars.WORKER_ADDRESS_SPACE_BYTES),
            "source",
            "--avatar",
            "0",
            "0",
            "4472",
            "90",
        ],
        cwd=tmp_path,
    )

    answer = json.loads(result.stdout)
    assert answer["ok"] is True
    assert answer["master"]["size"] == 1024
    assert len(answer["variants"]) == 5


@pytest.mark.asyncio
async def test_animated_webp_stays_decode_failed(service: AssetService) -> None:
    from PIL import Image

    frames = [Image.new("RGB", (4, 4), (10, 0, 0)), Image.new("RGB", (4, 4), (200, 0, 0))]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    data = buffer.getvalue()

    with pytest.raises(AssetError, match="decode_failed"):
        await service.upload(_chunks(data), name="animated.webp", declared_bytes=len(data))


@pytest.mark.asyncio
async def test_gif_is_refused_before_any_gif_parsing(service: AssetService) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 0, 0)).save(buffer, format="GIF")
    data = buffer.getvalue()

    with pytest.raises(AssetError, match="decode_failed"):
        await service.upload(_chunks(data), name="picture.gif", declared_bytes=len(data))


@pytest.mark.parametrize("fmt", ["GIF", "TIFF"])
def test_inspect_never_reaches_the_gif_or_tiff_parser(
    fmt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In-process on purpose: the worker process answers decode_failed with or without
    # formats=, so only a spy on the plugin parsers pins that they are never reached. The
    # Pillow rule binds the server process, not the tests.
    from PIL import GifImagePlugin, Image, TiffImagePlugin, UnidentifiedImageError

    from viki_agent.media import media_worker

    parsed: list[str] = []
    for plugin in (GifImagePlugin.GifImageFile, TiffImagePlugin.TiffImageFile):
        real_open = plugin._open

        def spy(image: Image.Image, real_open: Any = real_open) -> None:
            parsed.append(type(image).__name__)
            real_open(image)

        monkeypatch.setattr(plugin, "_open", spy)
    path = tmp_path / "picture"
    Image.new("RGB", (4, 4), (200, 0, 0)).save(path, format=fmt)

    # _inspect sets Image.MAX_IMAGE_PIXELS as a process-wide side effect; restore it so this
    # in-process test does not change Pillow's decompression-bomb guard for tests that follow.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)

    with pytest.raises(UnidentifiedImageError):
        media_worker._inspect(path)

    assert parsed == []


def test_worker_exits_with_a_distinct_code_when_the_decoder_cannot_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from viki_agent.media import media_worker

    (tmp_path / "source").write_bytes(b"not-an-image")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["media_worker.py", "source"])
    # sys.modules["PIL"] = None makes every "import PIL" (and "from PIL import ...") raise
    # ImportError, without needing a real Pillow-less interpreter.
    monkeypatch.setitem(sys.modules, "PIL", None)

    exit_code = media_worker.main()

    assert exit_code == media_worker.DECODER_UNAVAILABLE_EXIT_CODE
    assert exit_code not in (0, 1, 2)
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is False
    assert answer["code"] != "decode_failed"


def test_main_with_explicit_argv_ignores_sys_argv_and_leaves_it_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from viki_agent.media import media_worker

    monkeypatch.chdir(tmp_path)
    # A name sys.argv could carry that would pass _safe_name and reach a real (missing) file:
    # if main() ever fell back to sys.argv despite the explicit argv, it would answer
    # decode_failed (exit 1) instead of the invalid_input this call's own argv demands.
    sentinel = ["media_worker.py", "ok-name"]
    monkeypatch.setattr(sys, "argv", sentinel)

    exit_code = media_worker.main(["x/y"])

    assert exit_code == 2
    assert sys.argv == sentinel
    answer = json.loads(capsys.readouterr().out)
    assert answer == {"ok": False, "code": "invalid_input"}


_NO_PIL_CHECK = """
import sys
sys.path.insert(0, sys.argv[1])
import viki_agent.__main__
import viki_agent.server
loaded = sorted(name for name in sys.modules if name == "PIL" or name.startswith("PIL."))
assert loaded == [], loaded
sys.stdout.write("ok")
"""


def test_server_process_never_loads_pillow(tmp_path: Path) -> None:
    src_dir = str(Path(winproc.__file__).parents[1])

    result = winproc.run(
        [sys.executable, "-I", "-c", _NO_PIL_CHECK, src_dir],
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def _png_header_only(width: int, height: int) -> bytes:
    """A PNG with an IHDR claiming the given size and no IDAT: Pillow can read its size from
    the header alone, but calling ``load`` on it raises, since there is no pixel data behind
    the claimed size."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.mark.parametrize("width, height", [(8001, 5000), (12000, 7000)])
def test_over_40mp_header_is_refused_before_any_pixel_load(
    width: int, height: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    from viki_agent.media import media_worker

    # _inspect sets Image.MAX_IMAGE_PIXELS as a process-wide side effect; restore it so this
    # in-process test does not change Pillow's decompression-bomb guard for tests that follow.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    loads: list[None] = []
    real_load = Image.Image.load

    def spy_load(self: Image.Image, *args: Any, **kwargs: Any) -> Any:
        loads.append(None)
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", spy_load)
    path = tmp_path / "picture"
    path.write_bytes(_png_header_only(width, height))

    with pytest.raises((Image.DecompressionBombWarning, Image.DecompressionBombError)):
        media_worker._inspect(path)

    assert loads == []


@pytest.mark.asyncio
async def test_real_worker_refuses_an_over_40mp_header_only_image(service: AssetService) -> None:
    data = _png_header_only(8001, 5000)

    with pytest.raises(AssetError, match="decode_failed"):
        await service.upload(_chunks(data), name="huge.png", declared_bytes=len(data))

    assert list(service._incoming.iterdir()) == []
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


def test_main_exits_1_for_an_over_40mp_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from PIL import Image

    from viki_agent.media import media_worker

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    (tmp_path / "source").write_bytes(_png_header_only(8001, 5000))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["media_worker.py", "source"])

    exit_code = media_worker.main()

    assert exit_code == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer == {"ok": False, "code": "decode_failed"}


@pytest.mark.skipif(
    sys.platform != "win32", reason="the reparse-point attribute is a Windows-only mechanism"
)
def test_main_refuses_a_stage_carrying_the_reparse_point_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the reparse-point check directly, independent of the symlink privilege the next
    test needs: a regular file whose lstat carries the reparse-point bit (what a real symlink
    or junction stage would report) is refused even though it is otherwise a valid, decodable
    file."""
    assert sys.platform == "win32"
    from PIL import Image

    from viki_agent.media import media_worker

    stage = tmp_path / "source"
    Image.new("RGB", (4, 4), (200, 0, 0)).save(stage, format="PNG")
    real_lstat = Path.lstat
    real_info = real_lstat(stage)
    fake_info = os.stat_result(
        (
            real_info.st_mode,
            real_info.st_ino,
            real_info.st_dev,
            real_info.st_nlink,
            real_info.st_uid,
            real_info.st_gid,
            real_info.st_size,
            real_info.st_atime,
            real_info.st_mtime,
            real_info.st_ctime,
        ),
        {"st_file_attributes": getattr(real_info, "st_file_attributes", 0) | 0x400},
    )

    def spy_lstat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        return fake_info if self.name == "source" else real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", spy_lstat)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["media_worker.py", "source"])

    exit_code = media_worker.main()

    assert exit_code == 1


def test_symlinked_stage_is_refused_even_when_the_target_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    from viki_agent.media import media_worker

    target = tmp_path / "target.png"
    Image.new("RGB", (4, 4), (200, 0, 0)).save(target, format="PNG")
    link = tmp_path / "source"
    try:
        os.symlink(target, link)
    except OSError as error:
        pytest.skip(f"cannot create a symlink here: {error}")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["media_worker.py", "source"])

    exit_code = media_worker.main()

    assert exit_code == 1


def test_directory_named_as_the_stage_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from viki_agent.media import media_worker

    (tmp_path / "source").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["media_worker.py", "source"])

    exit_code = media_worker.main()

    assert exit_code == 1


def _webp(width: int, height: int, **options: Any) -> bytes:
    """A real one-colour WebP written by Pillow. Lossless flat colour stays a few KB even at
    tens of megapixels, which is what makes a claimed size cheap to send."""
    from PIL import Image

    buffer = io.BytesIO()
    with Image.new("RGB", (width, height), (10, 20, 30)) as image:
        image.save(buffer, format="WEBP", **options)
    return buffer.getvalue()


def _vp8x_header(width: int, height: int) -> bytes:
    """The first 30 bytes of an extended WebP whose canvas is ``width`` x ``height``."""
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return (
        b"RIFF"
        + struct.pack("<I", 22)
        + b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + bytes(4)
        + canvas
    )


@pytest.fixture
def webp_decoders(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record and refuse every libwebp decoder Pillow builds: building one commits the whole
    canvas, so an oversized WebP must be refused before any is built."""
    from PIL import Image, _webp

    # _inspect and _avatar set Image.MAX_IMAGE_PIXELS process-wide; restore it for later tests.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    built: list[None] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        built.append(None)
        raise AssertionError("the WebP decoder was built")

    monkeypatch.setattr(_webp, "WebPAnimDecoder", refuse)
    return built


@pytest.mark.parametrize("width, height", [(8001, 5000), (12000, 7000)])
def test_over_40mp_webp_is_refused_before_its_decoder_is_built(
    width: int, height: int, tmp_path: Path, webp_decoders: list[None]
) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    # A header claiming the size, not a real 12,000 x 7,000-pixel encode.
    path.write_bytes(_vp8x_header(width, height))

    with pytest.raises(ValueError):
        media_worker._inspect(path)

    assert webp_decoders == []


def test_avatar_refuses_an_over_20mp_webp_before_its_decoder_is_built(
    tmp_path: Path, webp_decoders: list[None]
) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "source"
    # A header claiming the size, not a real 20-megapixel encode.
    path.write_bytes(_vp8x_header(5000, 4001))

    with pytest.raises(ValueError):
        media_worker._avatar(path, 0, 0, 100, 0)

    assert webp_decoders == []


# An embedded ICC profile makes Pillow write the extended (VP8X) layout.
_WEBP_CHUNKS = [("VP8 ", {}), ("VP8L", {"lossless": True}), ("VP8X", {"icc_profile": b"x" * 10})]


@pytest.mark.parametrize("chunk, options", _WEBP_CHUNKS)
def test_webp_size_limit_is_exact_for_the_vp8_vp8l_and_vp8x_layouts(
    chunk: str, options: dict[str, Any], tmp_path: Path
) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    # Both sides above 255, so a misplaced byte or bit changes the product.
    path.write_bytes(_webp(302, 298, **options))
    assert path.read_bytes()[12:16] == chunk.encode("ascii")

    media_worker._refuse_oversized_webp(path, 302 * 298)
    with pytest.raises(ValueError):
        media_worker._refuse_oversized_webp(path, 302 * 298 - 1)


def _webp_rgba_partial_alpha(width: int, height: int) -> bytes:
    """A real lossless WebP with a partially transparent pixel: libwebp only sets the VP8L
    header's alpha_is_used bit, the one sitting just past the 14-bit height field, when some
    pixel's alpha is neither uniformly opaque nor uniformly transparent."""
    from PIL import Image

    buffer = io.BytesIO()
    with Image.new("RGBA", (width, height), (10, 20, 30, 255)) as image:
        image.putpixel((0, 0), (10, 20, 30, 128))
        image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def test_webp_canvas_with_alpha_is_read_past_the_alpha_flag_bit(tmp_path: Path) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    path.write_bytes(_webp_rgba_partial_alpha(303, 299))
    assert path.read_bytes()[12:16] == b"VP8L"

    media_worker._refuse_oversized_webp(path, 303 * 299)
    with pytest.raises(ValueError):
        media_worker._refuse_oversized_webp(path, 303 * 299 - 1)


def _vp8_header(width: int, height: int, *, h_scale: int, v_scale: int) -> bytes:
    """The first 30 bytes of a lossy VP8 keyframe whose canvas is ``width`` x ``height``, with
    non-zero 2-bit upscaling hints packed above each 14-bit size (RFC 6386 section 9.1); the
    decoded size ignores them."""
    width_field = (width & 0x3FFF) | ((h_scale & 0x3) << 14)
    height_field = (height & 0x3FFF) | ((v_scale & 0x3) << 14)
    payload = bytes(3) + b"\x9d\x01\x2a" + struct.pack("<HH", width_field, height_field)
    return b"RIFF" + struct.pack("<I", 22) + b"WEBP" + b"VP8 " + struct.pack("<I", 10) + payload


def test_webp_canvas_with_scale_bits_ignores_them(tmp_path: Path) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    path.write_bytes(_vp8_header(302, 298, h_scale=1, v_scale=1))

    media_worker._refuse_oversized_webp(path, 302 * 298)
    with pytest.raises(ValueError):
        media_worker._refuse_oversized_webp(path, 302 * 298 - 1)


@pytest.mark.parametrize("chunk, options", _WEBP_CHUNKS)
def test_webp_within_the_limit_still_passes_inspect(
    chunk: str, options: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    from viki_agent.media import media_worker

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    path = tmp_path / "picture"
    path.write_bytes(_webp(301, 297, **options))

    assert media_worker._inspect(path) == {
        "ok": True,
        "media_type": "image/webp",
        "width": 301,
        "height": 297,
    }


def test_webp_header_at_exactly_40mp_is_accepted_and_one_pixel_more_refused(
    tmp_path: Path,
) -> None:
    from viki_agent.media import media_worker

    exact = tmp_path / "exact"
    exact.write_bytes(_vp8x_header(8000, 5000))
    over = tmp_path / "over"
    over.write_bytes(_vp8x_header(754_717, 53))  # 40,000,001 pixels

    media_worker._refuse_oversized_webp(exact, media_worker.MAX_PIXELS)
    with pytest.raises(ValueError):
        media_worker._refuse_oversized_webp(over, media_worker.MAX_PIXELS)


_RIFF_WEBP = b"RIFF" + struct.pack("<I", 22) + b"WEBP"


@pytest.mark.parametrize(
    "header",
    [
        _RIFF_WEBP,
        _vp8x_header(4, 4)[:29],
        _RIFF_WEBP + b"VP8L" + struct.pack("<I", 5) + b"\x00" + bytes(9),
        _RIFF_WEBP + b"VP8 " + struct.pack("<I", 10) + bytes(10),
        _RIFF_WEBP + b"VP8Q" + struct.pack("<I", 10) + bytes(10),
    ],
    ids=["no-chunk", "short-vp8x", "vp8l-bad-signature", "vp8-no-start-code", "unknown-chunk"],
)
def test_short_or_malformed_webp_header_is_refused(header: bytes, tmp_path: Path) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    path.write_bytes(header)

    with pytest.raises(ValueError):
        media_worker._refuse_oversized_webp(path, media_worker.MAX_PIXELS)


def test_non_webp_passes_the_webp_header_check_to_pillow(tmp_path: Path) -> None:
    from viki_agent.media import media_worker

    path = tmp_path / "picture"
    # Over the limit, but a PNG: Pillow's own size check refuses it without allocating.
    path.write_bytes(_png_header_only(8001, 5000))

    media_worker._refuse_oversized_webp(path, media_worker.MAX_PIXELS)


def test_worker_env_holds_exactly_the_named_keys_per_platform() -> None:
    from viki_agent.media import media_worker

    environ = {
        "PATH": "/bin",
        "HOME": "/home/x",
        "TMPDIR": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "SYSTEMROOT": "C:\\Windows",
        "WINDIR": "C:\\Windows",
        "TEMP": "C:\\Temp",
        "TMP": "C:\\Temp",
        "LD_PRELOAD": "/evil.so",
        "DYLD_INSERT_LIBRARIES": "/evil.dylib",
    }
    common = {"PYTHONUTF8", "PYTHONNOUSERSITE"}
    assert (
        set(media_worker._worker_env("win32", environ))
        == {
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
        }
        | common
    )
    assert (
        set(media_worker._worker_env("linux", environ))
        == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"} | common
    )
    assert (
        set(media_worker._worker_env("darwin", environ))
        == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"} | common
    )


def test_worker_argv_prefix_is_empty_off_linux() -> None:
    from viki_agent.media import media_worker

    assert media_worker._worker_argv_prefix("win32", 768 << 20) == []
    assert media_worker._worker_argv_prefix("darwin", 768 << 20) == []


def test_worker_argv_prefix_carries_the_literal_pair_on_linux() -> None:
    from viki_agent.media import media_worker

    assert media_worker._worker_argv_prefix("linux", 768 << 20) == [
        "--address-space",
        str(768 << 20),
    ]


@pytest.mark.asyncio
async def test_decode_in_worker_spawns_the_compiled_binarys_subcommand(
    service: AssetService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from viki_agent import compiled

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(compiled, "COMPILED", True)
    install_dir = tmp_path / "install"
    monkeypatch.setattr(compiled, "INSTALL_DIR", install_dir)
    captured: list[str] = []

    async def refuse(argv: list[str], **_options: object) -> None:
        captured.extend(argv)
        raise OSError

    monkeypatch.setattr(winproc, "spawn", refuse)
    stage = tmp_path / "stage-name"

    with pytest.raises(AssetError, match="worker_unavailable"):
        await service._decode_in_worker(stage)

    assert captured == [
        str(install_dir / compiled.APP_BINARY),
        "--viki-worker",
        "media",
        "--address-space",
        str(assets.WORKER_MEMORY_BYTES),
        stage.name,
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="the address-space ceiling is Linux only")
def test_address_space_clamps_to_a_tighter_inherited_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "linux"
    import resource

    from viki_agent.media import media_worker

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, 100 << 20))
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: calls.append(limits))
    (tmp_path / "source").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["media_worker.py", "--address-space", str(768 << 20), "source"]
    )
    monkeypatch.setattr(media_worker, "_inspect", lambda _path: {"ok": True})

    media_worker.main()

    assert calls == [(100 << 20, 100 << 20)]


@pytest.mark.skipif(sys.platform != "linux", reason="the address-space ceiling is Linux only")
def test_address_space_keeps_the_requested_ceiling_when_the_hard_limit_is_infinite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "linux"
    import resource

    from viki_agent.media import media_worker

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: calls.append(limits))
    (tmp_path / "source").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["media_worker.py", "--address-space", str(768 << 20), "source"]
    )
    monkeypatch.setattr(media_worker, "_inspect", lambda _path: {"ok": True})

    media_worker.main()

    assert calls == [(768 << 20, 768 << 20)]


# The avatar editor previews Chromium's own decode (createImageBitmap with imageOrientation
# "from-image") and sends a crop on that grid, so the worker must crop on the same one. Each
# expected grid is what Edge 153 showed for the same bytes: the upright size, then the colour at
# the centre of the top-left, top-right, bottom-left and bottom-right quadrants.
_COLOURS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0)}
_AS_STORED = ((96, 64), ("red", "green", "blue", "yellow"))
_TURNED_BY_6 = ((64, 96), ("blue", "red", "yellow", "green"))
_XMP_6 = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>'
)


def _encode(fmt: str, **options: Any) -> bytes:
    """A 96 x 64 image of four coloured quadrants, stored unrotated."""
    from PIL import Image

    buffer = io.BytesIO()
    with Image.new("RGB", (96, 64)) as image:
        for colour, box in zip(
            _COLOURS.values(),
            [(0, 0, 48, 32), (48, 0, 96, 32), (0, 32, 48, 64), (48, 32, 96, 64)],
            strict=True,
        ):
            image.paste(colour, box)
        image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _exif(orientation: int, *, kind: int = 3, count: int = 1, order: str = ">") -> bytes:
    """An Exif block whose IFD0 holds one Orientation entry of the given type and count."""
    if kind == 3:
        value = struct.pack(order + "HH", orientation, 0)  # a SHORT, left-justified
    else:
        value = struct.pack(order + "I", orientation)
    entry = struct.pack(order + "HHI", 0x0112, kind, count) + value
    tiff = struct.pack(order + "HIH", 42, 8, 1) + entry + struct.pack(order + "I", 0)
    return b"Exif\x00\x00" + (b"MM" if order == ">" else b"II") + tiff


def _jpeg(*app1: bytes) -> bytes:
    data = _encode("JPEG", quality=95, subsampling=0)
    segments = b"".join(b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body for body in app1)
    return data[:2] + segments + data[2:]


def _png(key: str, value: str) -> bytes:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    if key == "XML:com.adobe.xmp":
        info.add_itxt(key, value)
    else:
        info.add_text(key, value, zip=True)  # zTXt, the way ImageMagick writes a raw profile
    return _encode("PNG", pnginfo=info)


def _png_exif_after_idat(exif: bytes) -> bytes:
    data = _encode("PNG")
    body = exif[6:]
    chunk = struct.pack(">I", len(body)) + b"eXIf" + body
    chunk += struct.pack(">I", zlib.crc32(b"eXIf" + body))
    iend = len(data) - 12
    return data[:iend] + chunk + data[iend:]


def _raw_profile_6() -> str:
    hexed = _exif(6).hex()
    return f"\nexif\n{len(hexed) // 2:8d}\n{hexed}\n"


def _tiff(
    root: list[tuple[int, int, int]],
    *,
    order: str = ">",
    sub_ifd: list[tuple[int, int, int]] | None = None,
) -> bytes:
    """A TIFF block whose root IFD holds ``root``'s (tag, type, value) entries, each of count 1
    (a SHORT left-justified); with ``sub_ifd``, a last root entry 0x8769 (LONG) names an Exif
    sub-IFD holding those entries, right after the root."""

    def ifd(entries: list[tuple[int, int, int]]) -> bytes:
        packed = b"".join(
            struct.pack(order + "HHI", tag, kind, 1)
            + (
                struct.pack(order + "HH", value, 0)
                if kind == 3
                else struct.pack(order + "I", value)
            )
            for tag, kind, value in entries
        )
        return struct.pack(order + "H", len(entries)) + packed + struct.pack(order + "I", 0)

    if sub_ifd is not None:
        root = [*root, (0x8769, 4, 8 + 2 + 12 * (len(root) + 1) + 4)]
    head = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8)
    return head + ifd(root) + (b"" if sub_ifd is None else ifd(sub_ifd))


_ORIENTATION_6 = (0x0112, 3, 6)
_WIDTH_96 = (0x0100, 3, 96)  # ImageWidth: an entry that is not an orientation


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png_before_idat(*chunks: bytes) -> bytes:
    """The quadrant PNG with ``chunks`` right after IHDR (8-byte signature, 25-byte IHDR)."""
    data = _encode("PNG")
    return data[:33] + b"".join(chunks) + data[33:]


def _jpeg_junk_after_sof(app1: bytes) -> bytes:
    """The quadrant JPEG with two junk bytes, then an APP1 of ``app1``, right after its SOF0."""
    data = _encode("JPEG", quality=95, subsampling=0)
    sof = data.index(b"\xff\xc0")
    end = sof + 2 + int.from_bytes(data[sof + 2 : sof + 4])
    segment = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    return data[:end] + b"\x00\x00" + segment + data[end:]


_BROWSER_GRIDS = [
    pytest.param(lambda: _jpeg(_exif(6)), _TURNED_BY_6, id="jpeg-exif-short"),
    pytest.param(lambda: _encode("PNG", exif=_exif(6)), _TURNED_BY_6, id="png-exif"),
    pytest.param(lambda: _jpeg(_exif(6, kind=4)), _AS_STORED, id="jpeg-exif-long"),
    pytest.param(
        lambda: _jpeg(b"http://ns.adobe.com/xap/1.0/\x00" + _XMP_6.encode()),
        _AS_STORED,
        id="jpeg-xmp",
    ),
    pytest.param(lambda: _png("XML:com.adobe.xmp", _XMP_6), _AS_STORED, id="png-xmp"),
    pytest.param(
        lambda: _png("Raw profile type exif", _raw_profile_6()), _AS_STORED, id="png-raw-profile"
    ),
    pytest.param(lambda: _encode("WEBP", lossless=True, exif=_exif(6)), _AS_STORED, id="webp-exif"),
    pytest.param(
        lambda: _encode("WEBP", lossless=True, xmp=_XMP_6.encode()), _AS_STORED, id="webp-xmp"
    ),
    pytest.param(lambda: _png_exif_after_idat(_exif(6)), _AS_STORED, id="png-exif-after-idat"),
    pytest.param(lambda: _jpeg(_exif(1), _exif(6)), _AS_STORED, id="jpeg-second-exif-block"),
    pytest.param(lambda: _jpeg(_exif(6, count=2)), _AS_STORED, id="jpeg-exif-count-2"),
    pytest.param(lambda: _jpeg(_exif(6, order="<")), _TURNED_BY_6, id="jpeg-exif-little-endian"),
    pytest.param(lambda: _jpeg(_exif(1)), _AS_STORED, id="jpeg-exif-1"),
    pytest.param(
        lambda: _jpeg(_exif(2)), ((96, 64), ("green", "red", "yellow", "blue")), id="jpeg-exif-2"
    ),
    pytest.param(
        lambda: _jpeg(_exif(3)), ((96, 64), ("yellow", "blue", "green", "red")), id="jpeg-exif-3"
    ),
    pytest.param(
        lambda: _jpeg(_exif(4)), ((96, 64), ("blue", "yellow", "red", "green")), id="jpeg-exif-4"
    ),
    pytest.param(
        lambda: _jpeg(_exif(5)), ((64, 96), ("red", "blue", "green", "yellow")), id="jpeg-exif-5"
    ),
    pytest.param(
        lambda: _jpeg(_exif(7)), ((64, 96), ("yellow", "green", "blue", "red")), id="jpeg-exif-7"
    ),
    pytest.param(
        lambda: _jpeg(_exif(8)), ((64, 96), ("green", "yellow", "red", "blue")), id="jpeg-exif-8"
    ),
    pytest.param(lambda: _jpeg(_exif(9)), _AS_STORED, id="jpeg-exif-9"),
]

# These rows follow the source rules Chromium's decoders use: libjpeg's marker reader
# (next_marker) and Skia's EXIF reader (SkJpegMetadataDecoder, SkExif::Parse). Each row was
# measured in Edge 154.0.4258.37 on 2026-09-30 and showed the grid it expects.
_SKIA_RULE_GRIDS = [
    pytest.param(
        # Junk after the SOF reaches the decoder (the editor's own walk stops at the SOF).
        lambda: _jpeg_junk_after_sof(_exif(6)),
        _TURNED_BY_6,
        id="junk-after-sof-then-exif",
    ),
    pytest.param(
        lambda: _jpeg(b"Exif\x00\x00" + _tiff([(0x0112, 3, 9), _ORIENTATION_6])),
        _TURNED_BY_6,
        id="invalid-then-valid-orientation",
    ),
    pytest.param(
        lambda: _jpeg(b"Exif\x00\x00" + _tiff([_WIDTH_96], sub_ifd=[_ORIENTATION_6])),
        _TURNED_BY_6,
        id="orientation-only-in-exif-sub-ifd",
    ),
    pytest.param(
        lambda: _jpeg(b"Exif\x00\x01" + _tiff([_ORIENTATION_6])),
        _TURNED_BY_6,
        id="exif-app1-any-sixth-byte",
    ),
    pytest.param(
        lambda: _jpeg(
            b"Exif\x00\x00" + _tiff([_WIDTH_96]), b"Exif\x00\x00" + _tiff([_ORIENTATION_6])
        ),
        _AS_STORED,
        id="first-exif-app1-without-orientation",
    ),
    pytest.param(
        lambda: _png_before_idat(
            _png_chunk(b"eXIf", _tiff([_ORIENTATION_6])), _png_chunk(b"eXIf", _tiff([_WIDTH_96]))
        ),
        _TURNED_BY_6,
        id="png-first-exif-chunk-6",
    ),
    pytest.param(
        lambda: _png_before_idat(
            _png_chunk(b"eXIf", _tiff([_WIDTH_96])), _png_chunk(b"eXIf", _tiff([_ORIENTATION_6]))
        ),
        _AS_STORED,
        id="png-second-exif-chunk-6",
    ),
    pytest.param(
        lambda: _png_before_idat(
            _png_chunk(b"tEXt", b"exif\x00Exif\x00\x00" + _tiff([_ORIENTATION_6]))
        ),
        _AS_STORED,
        id="png-text-exif-keyword",
    ),
    pytest.param(
        lambda: _jpeg(b"Exif\x00\x00" + _tiff([_ORIENTATION_6])[: 8 + 2 + 6]),
        _AS_STORED,
        id="truncated-root-entry",
        # Pillow's own EXIF read (for the JPEG's resolution) warns about the same cut.
        marks=pytest.mark.filterwarnings("ignore:Corrupt EXIF data"),
    ),
    pytest.param(
        lambda: _png_before_idat(_png_chunk(b"eXIf", _tiff([_ORIENTATION_6], order="<"))),
        _TURNED_BY_6,
        id="png-little-endian-short-6",
    ),
    pytest.param(
        lambda: _jpeg(b"Exif\x00\x00" + _tiff([(0x010F, 3, 1), (0x0110, 3, 1), _ORIENTATION_6])),
        _TURNED_BY_6,
        id="orientation-after-make-and-model",
    ),
    pytest.param(
        # A little-endian LONG 6 begins with the bytes of a SHORT 6, so only the type refuses it.
        lambda: _jpeg(b"Exif\x00\x00" + _tiff([(0x0112, 4, 6)], order="<")),
        _AS_STORED,
        id="long-orientation-6",
    ),
    pytest.param(
        # The worker never reads a WebP's EXIF chunk (Chromium ignores it); the chunk holds a
        # bare TIFF, the form a reader of that chunk would parse.
        lambda: _encode("WEBP", lossless=True, exif=_tiff([_ORIENTATION_6])),
        _AS_STORED,
        id="webp-exif-chunk-6",
    ),
]


def _avatar_colour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, crop: tuple[int, int, int, int]
) -> str:
    """The colour name of the avatar master's top-left pixel for ``crop`` (x, y, edge, rotation)."""
    from PIL import Image

    from viki_agent.media import media_worker

    run = tmp_path / "-".join(map(str, crop))
    run.mkdir()
    (run / "source").write_bytes(data)
    monkeypatch.chdir(run)
    # _avatar sets this process-wide; restore it for the in-process tests that follow.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    media_worker._avatar(Path("source"), *crop)
    with Image.open(run / "master.png") as master:
        pixel = cast(tuple[int, int, int, int], master.getpixel((0, 0)))
    return min(
        _COLOURS,
        key=lambda name: sum(abs(a - b) for a, b in zip(_COLOURS[name], pixel[:3], strict=True)),
    )


@pytest.mark.parametrize("encode, grid", _BROWSER_GRIDS + _SKIA_RULE_GRIDS)
def test_avatar_crops_on_the_grid_the_browser_previews(
    encode: Any,
    grid: tuple[tuple[int, int], tuple[str, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = encode()
    (width, height), colours = grid
    centres = [(width // 4, height // 4), (3 * width // 4, height // 4)]
    centres += [(width // 4, 3 * height // 4), (3 * width // 4, 3 * height // 4)]

    # A quadrant centre outside the worker's grid refuses the crop, so the size is checked too.
    seen = tuple(_avatar_colour(tmp_path, monkeypatch, data, (x, y, 1, 0)) for x, y in centres)

    assert seen == colours


def test_avatar_applies_the_orientation_before_the_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mirror, then a clockwise quarter turn, of the stored quadrants (red, green / blue, yellow);
    # turning first and mirroring after gives red, blue / green, yellow.
    grid = ("yellow", "green", "blue", "red")

    seen = tuple(
        _avatar_colour(tmp_path, monkeypatch, _jpeg(_exif(2)), (x, y, 1, 90))
        for x, y in [(16, 24), (48, 24), (16, 72), (48, 72)]
    )

    assert seen == grid


@pytest.mark.parametrize("rotation, top_left", [(90, "red"), (180, "blue"), (270, "blue")])
def test_avatar_rotation_turns_clockwise(
    rotation: int, top_left: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    with Image.new("RGB", (8, 4), _COLOURS["red"]) as image:
        image.paste(_COLOURS["blue"], (4, 0, 8, 4))
        image.save(buffer, format="PNG")

    # Left half red, right half blue: a clockwise quarter turn brings the red half to the top.
    colour = _avatar_colour(tmp_path, monkeypatch, buffer.getvalue(), (0, 0, 1, rotation))

    assert colour == top_left


_EXIF_APP1 = b"\xff\xe1\x00\x12Exif\x00\x00" + b"MM\x00*" + b"\x00" * 6


@pytest.mark.parametrize(
    "data, found",
    [
        pytest.param(b"\xff\xd8" + _EXIF_APP1, True, id="app1-after-soi"),
        pytest.param(b"\xff\xd8\xff\xff\xff" + _EXIF_APP1[1:], True, id="fill-bytes-skipped"),
        pytest.param(b"\xff\xd8\xff\xd0\xff\x01" + _EXIF_APP1, True, id="standalone-markers"),
        pytest.param(b"\xff\xd8\xff\xe0\x00\x04ab" + _EXIF_APP1, True, id="after-another-segment"),
        pytest.param(b"\xff\xd8\xff\xda\x00\x02" + _EXIF_APP1, False, id="stops-at-sos"),
        pytest.param(b"\xff\xd8\xff\xd9" + _EXIF_APP1, False, id="stops-at-eoi"),
        pytest.param(b"\xff\xd8" + _EXIF_APP1[:-1], False, id="truncated-segment"),
        pytest.param(b"\xff\xd8\x00" + _EXIF_APP1, True, id="no-marker"),
        pytest.param(b"\xff\xd8\xff\xc0\x00\x04ab\x00\x00" + _EXIF_APP1, True, id="junk-after-sof"),
        pytest.param(
            b"\xff\xd8\xff\xe0\x00\x04ab\xff\x00" + _EXIF_APP1, True, id="stuffed-zero-after-app0"
        ),
        pytest.param(_EXIF_APP1, False, id="no-soi"),
        pytest.param(b"\xff\xd8\xff\xe1\x00\x08Exif\x00\x00", False, id="payload-of-six-bytes"),
    ],
)
def test_jpeg_exif_walks_the_markers_as_libjpeg_and_skia_do(data: bytes, found: bool) -> None:
    from viki_agent.media import media_worker

    assert (media_worker._jpeg_exif(data) == b"MM\x00*" + b"\x00" * 6) is found
