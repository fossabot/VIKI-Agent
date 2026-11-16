"""Decode one staged image in an isolated helper process.

The server never imports Pillow.  It starts this file through ``winproc.spawn`` and accepts
only the small JSON document written to stdout.  Input names are opaque files in the worker's
current private directory, never caller paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_PIXELS = 40_000_000
AVATAR_MAX_PIXELS = 20_000_000
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "MPO": "image/jpeg", "WEBP": "image/webp"}

# Distinct from main()'s other exit codes: the worker could not even import its decoder (a
# broken install, an interrupted upgrade, an antivirus quarantine). The caller must tell this
# apart from a genuine decode failure, which is a verdict on the image, not on the worker.
DECODER_UNAVAILABLE_EXIT_CODE = 3
# The crop lies outside the image's grid: a verdict on the request, not on the image.
CROP_OUTSIDE_EXIT_CODE = 4


class _CropOutsideGrid(ValueError):
    pass


_WIN32_ENV_NAMES = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
_POSIX_ENV_NAMES = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")


def _answer(body: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(body, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()


def _safe_name(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 64
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and "\x00" not in value
    )


def _worker_env(platform: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The environment for an image worker process, chosen by OS; the caller passes
    nothing else to `winproc.spawn`."""
    names = _WIN32_ENV_NAMES if platform == "win32" else _POSIX_ENV_NAMES
    env = {name: environ[name] for name in names if name in environ}
    env.update({"PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"})
    return env


def _worker_argv_prefix(platform: str, memory_limit_bytes: int) -> list[str]:
    """The address-space ceiling argv pair, Linux only: winproc's Job validates the
    limit but applies nothing there, so the worker enforces it on itself."""
    return ["--address-space", str(memory_limit_bytes)] if platform == "linux" else []


def _refuse_oversized_webp(path: Path, limit: int) -> None:
    """Refuse a WebP whose header claims more than ``limit`` pixels, before Pillow opens it.

    Pillow's WebP plugin builds libwebp's decoder inside ``Image.open``, and that decoder
    commits the whole canvas before any pixel check can run. PNG and JPEG allocate nothing
    before the check, so every other file passes through unchanged. Byte offsets follow
    RFC 9649 (the WebP container and the lossless header) and RFC 6386 section 9.1 (the lossy
    key-frame header).
    """
    with path.open("rb") as file:
        header = file.read(30)
    if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        return
    chunk = header[12:16]
    if chunk == b"VP8X" and len(header) == 30:
        # After 1 flag byte and 3 reserved bytes: canvas width-1, then height-1, 24 bits each.
        width = int.from_bytes(header[24:27], "little") + 1
        height = int.from_bytes(header[27:30], "little") + 1
    elif chunk == b"VP8L" and len(header) >= 25 and header[20] == 0x2F:
        # After the 0x2F signature: width-1, then height-1, 14 bits each, least significant first.
        fields = int.from_bytes(header[21:25], "little")
        width = (fields & 0x3FFF) + 1
        height = (fields >> 14 & 0x3FFF) + 1
    elif chunk == b"VP8 " and len(header) == 30 and header[23:26] == b"\x9d\x01\x2a":
        # After the 3-byte frame tag and the start code: 14-bit width and height, each topped
        # by 2 upscaling bits that do not change the decoded size.
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
    else:
        raise ValueError
    if width * height > limit:
        raise ValueError


def _inspect(path: Path) -> dict[str, object]:
    # Pillow is deliberately imported only inside this worker process.
    from PIL import Image

    _refuse_oversized_webp(path, MAX_PIXELS)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as image:
            fmt = image.format or ""
            media_type = _FORMATS.get(fmt)
            if media_type is None:
                raise ValueError
            # A camera preview or phone HDR gain map makes a JPEG carry a second MPF image (format
            # MPO, n_frames == 2); frame 0, the primary image, is already active after open. Every
            # other format needs exactly one frame, but Pillow counts two for an APNG whose acTL
            # declares one frame and whose IDAT is a hidden default image; Chromium previews it.
            if fmt != "MPO" and getattr(image, "n_frames", 1) != 1:
                raise ValueError
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_PIXELS:
                raise ValueError
            image.verify()
        # verify() checks structure but does not decode pixels. Reopen and load the only frame
        # (frame 0 for MPO) so truncated streams and decoder failures are refused before
        # publication.
        with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as image:
            fmt = image.format or ""
            reopened_type = _FORMATS.get(fmt)
            if reopened_type != media_type:
                raise ValueError
            if fmt != "MPO" and getattr(image, "n_frames", 1) != 1:
                raise ValueError
            if image.size != (width, height):
                raise ValueError
            image.load()
    return {"ok": True, "media_type": media_type, "width": width, "height": height}


# The editor previews Chromium's decode, whose JPEG markers libjpeg reads (next_marker) and
# whose EXIF rules are Skia's (SkJpegMetadataDecoder, SkExif::Parse). These readers apply the
# same rules to the staged file's own bytes, so a crop lands on the grid the user saw; Pillow's
# info["exif"] follows other rules. The one known difference in the marker walk: a segment whose
# length field is 0 or 1 ends the walk here, where libjpeg steps over it and reads on.


def _jpeg_exif(data: bytes) -> bytes | None:
    """The TIFF block of the first APP1 whose payload starts ``Exif\\0`` (any sixth byte), met
    walking the markers from SOI to SOS or EOI; a truncated segment, or one whose length field is
    0 or 1, ends the walk (libjpeg steps over the latter). As in
    libjpeg's next_marker, bytes before a marker's 0xFF and a stuffed zero (FF 00) are junk."""
    if data[:2] != b"\xff\xd8":
        return None
    at = 2
    # Each pass moves past at least one 0xFF, so the walk ends within len(data) passes.
    while (at := data.find(b"\xff", at)) >= 0:
        while at < len(data) and data[at] == 0xFF:
            at += 1  # fill bytes before the marker code
        if at >= len(data) or data[at] in (0xD9, 0xDA):
            return None
        marker = data[at]
        at += 1
        if marker == 0x00 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue  # a stuffed zero or a standalone marker: no length follows
        if at + 2 > len(data):
            return None
        end = at + int.from_bytes(data[at : at + 2])
        if end < at + 2 or end > len(data):
            return None
        payload = data[at + 2 : end]
        if marker == 0xE1 and len(payload) > 6 and payload[:5] == b"Exif\x00":
            return payload[6:]
        at = end
    return None


def _png_exif(data: bytes) -> bytes | None:
    """The first eXIf chunk's data before IDAT; a tEXt ``exif`` keyword is not EXIF here."""
    at = 8
    while at + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, at)
        if kind == b"IDAT" or at + 12 + length > len(data):
            return None
        if kind == b"eXIf":
            return data[at + 8 : at + 8 + length]
        at += 12 + length
    return None


def _ifd_orientation(tiff: bytes, order: str, offset: int) -> tuple[int | None, int | None]:
    """One IFD's first valid Orientation (SHORT, count 1, 1 to 8) and first Exif sub-IFD offset
    (LONG, count 1), read over the entries that fit whole."""
    if offset + 2 > len(tiff):
        return None, None
    (count,) = struct.unpack_from(order + "H", tiff, offset)
    orientation: int | None = None
    sub_ifd: int | None = None
    for index in range(min(count, (len(tiff) - offset - 2) // 12)):
        tag, kind, number, value = struct.unpack_from(
            order + "HHI4s", tiff, offset + 2 + 12 * index
        )
        if tag == 0x0112 and orientation is None and kind == 3 and number == 1:
            (candidate,) = struct.unpack_from(order + "H", value)
            orientation = candidate if 1 <= candidate <= 8 else None
        elif tag == 0x8769 and sub_ifd is None and kind == 4 and number == 1:
            (sub_ifd,) = struct.unpack_from(order + "I", value)
    return orientation, sub_ifd


def _tiff_orientation(tiff: bytes) -> int:
    """The root IFD's Orientation, else the Exif sub-IFD's (followed once, never further);
    anything malformed is 1 (as stored)."""
    order = {b"II*\x00": "<", b"MM\x00*": ">"}.get(tiff[:4])
    if order is None or len(tiff) < 8:
        return 1
    (root,) = struct.unpack_from(order + "I", tiff, 4)
    orientation, sub_ifd = _ifd_orientation(tiff, order, root)
    if orientation is None and sub_ifd is not None:
        orientation, _ = _ifd_orientation(tiff, order, sub_ifd)
    return orientation or 1


def _orientation(path: Path, fmt: str) -> int:
    """The orientation Chromium applies to this file: JPEG (and MPO) and PNG by the readers
    above; a WebP's EXIF is ignored, as Chromium ignores it."""
    if fmt == "WEBP":
        return 1
    data = path.read_bytes()
    tiff = _png_exif(data) if fmt == "PNG" else _jpeg_exif(data)
    return 1 if tiff is None else _tiff_orientation(tiff)


def _avatar(path: Path, x: int, y: int, edge: int, rotation: int) -> dict[str, object]:
    """Sanitize pixels in this worker only; coordinates follow the editor's preview grid (the
    orientation Chromium applies, read from the file's own EXIF by ``_orientation``) and then
    the clockwise rotation."""
    from PIL import Image

    if any(type(value) is not int for value in (x, y, edge, rotation)) or (
        min(x, y) < 0 or edge < 1 or rotation not in {0, 90, 180, 270}
    ):
        raise ValueError
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= 5 << 20:
        raise ValueError
    _refuse_oversized_webp(path, AVATAR_MAX_PIXELS)
    Image.MAX_IMAGE_PIXELS = AVATAR_MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as source:
            fmt = source.format or ""
            # A camera preview or phone HDR gain map makes a JPEG carry a second MPF image,
            # which Pillow reports as format MPO. Frame 0 (the primary image) is already the
            # active frame after open, so an accepted upload can become an avatar.
            if fmt not in _FORMATS or (fmt != "MPO" and getattr(source, "n_frames", 1) != 1):
                raise ValueError
            if source.width * source.height > AVATAR_MAX_PIXELS:
                raise ValueError
            # The file's own bytes, read once (the size check above bounds it at 5 MiB): the
            # first Exif APP1 or eXIf chunk before IDAT, its root IFD then its Exif sub-IFD.
            # Pillow's info["exif"] is not used; it can join blocks or keep another chunk.
            orientation = _orientation(path, fmt)
            source.load()
            image = source
            for turn in (
                {
                    2: Image.Transpose.FLIP_LEFT_RIGHT,
                    3: Image.Transpose.ROTATE_180,
                    4: Image.Transpose.FLIP_TOP_BOTTOM,
                    5: Image.Transpose.TRANSPOSE,
                    6: Image.Transpose.ROTATE_270,
                    7: Image.Transpose.TRANSVERSE,
                    8: Image.Transpose.ROTATE_90,
                }.get(orientation),
                {
                    90: Image.Transpose.ROTATE_270,
                    180: Image.Transpose.ROTATE_180,
                    270: Image.Transpose.ROTATE_90,
                }.get(rotation),
            ):
                if turn is not None:
                    # Close each grid once the next exists, so at most two are held at a time.
                    turned = image.transpose(turn)
                    image.close()
                    image = turned
            try:
                if x + edge > image.width or y + edge > image.height:
                    raise _CropOutsideGrid
                cropped = image.crop((x, y, x + edge, y + edge))
            finally:
                image.close()
    try:
        master = cropped.convert("RGBA")
    finally:
        cropped.close()
    master.info.clear()
    size = min(edge, 1024)
    if master.width != size:
        resized = master.resize((size, size), Image.Resampling.LANCZOS)
        master.close()
        master = resized
    rows: list[dict[str, Any]] = []
    total = 0
    try:
        for name, output_size in [
            ("master.png", size),
            *((f"{n}.png", n) for n in (24, 32, 64, 128, 256)),
        ]:
            output = master.resize((output_size, output_size), Image.Resampling.LANCZOS)
            try:
                output.info.clear()
                # The worker runs isolated with -I and cannot import osfs, so it makes its own
                # 0600 output directly; O_CLOEXEC and O_BINARY are each absent on one OS.
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_BINARY", 0)
                )
                with os.fdopen(os.open(name, flags, 0o600), "wb") as destination:
                    output.save(destination, format="PNG", compress_level=6, optimize=False)
                    destination.flush()
                    os.fsync(destination.fileno())
            finally:
                output.close()
            data = Path(name).read_bytes()
            if len(data) > (5 << 20 if name == "master.png" else 1 << 20):
                raise ValueError
            total += len(data)
            if total > 8 << 20:
                raise ValueError
            with Image.open(name, formats=("PNG",)) as verified:
                verified.load()
                if (
                    verified.format != "PNG"
                    or verified.mode != "RGBA"
                    or verified.size != (output_size, output_size)
                    or verified.info
                    or getattr(verified, "n_frames", 1) != 1
                ):
                    raise ValueError
            rows.append(
                {
                    "name": name,
                    "size": output_size,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    finally:
        master.close()
    return {"ok": True, "master": rows[0], "variants": rows[1:]}


def main(argv: list[str] | None = None) -> int:
    """Decode or transform one staged file; `argv` is `sys.argv[1:]` when None.

    A source-mode launch reads the process's own argv; compiled, the dispatcher
    (`--viki-worker media`) calls this directly with its own remaining args via
    `compiled.worker_argv`, so `sys.argv` here is never read.
    """
    if argv is None:
        argv = sys.argv[1:]
    if (
        len(argv) >= 2
        and argv[0] == "--address-space"
        and argv[1].isascii()
        and argv[1].isdecimal()
        and len(argv[1]) <= 12
    ):
        limit = int(argv[1])
        if sys.platform == "linux":
            import resource

            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            n = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (n, n))
        argv = argv[2:]
    avatar = len(argv) == 6 and argv[1] == "--avatar"
    if (len(argv) != 1 and not avatar) or not _safe_name(argv[0]):
        _answer({"ok": False, "code": "invalid_input"})
        return 2
    if avatar and any(
        not value.isascii() or not value.isdecimal() or len(value) > 9 for value in argv[2:]
    ):
        _answer({"ok": False, "code": "invalid_input"})
        return 2
    path = Path(argv[0])
    try:
        info = path.lstat()
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & reparse_point
        ):
            raise ValueError
        _answer(_avatar(path, *map(int, argv[2:])) if avatar else _inspect(path))
        return 0
    except ImportError:
        # The decoder library itself is missing or broken. This is not a statement about the
        # image, so it must not carry the same exit code as a genuine decode failure.
        _answer({"ok": False, "code": "decoder_unavailable"})
        return DECODER_UNAVAILABLE_EXIT_CODE
    except _CropOutsideGrid:
        _answer({"ok": False, "code": "invalid_crop"})
        return CROP_OUTSIDE_EXIT_CODE
    except OSError, ValueError, SyntaxError, RuntimeError, Warning:
        _answer({"ok": False, "code": "decode_failed"})
        return 1
    except Exception:
        # Decoder/library failures are not exposed to the parent. The process has no reason to
        # print a path, exception or image metadata supplied by the request.
        _answer({"ok": False, "code": "decode_failed"})
        return 1


if __name__ == "__main__":
    # Keep the process protocol on stdout; Pillow must never inherit an interactive stderr UI.
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
