"""`scripts/brand_icons.py`'s pure functions, and a round-trip check against the committed
favicon and shared mark: `build()`'s in-memory renders must match what is on disk, once
decoded, so a renderer change or a stale commit both fail here rather than at review."""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from PIL.IcoImagePlugin import IcoFile

TESTS = Path(__file__).parents[1]
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import brand_icons  # noqa: E402

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _favicon() -> IcoFile:
    # A fresh in-memory buffer per call: IcoFile.getimage() returns a lazily-decoded frame still
    # bound to the buffer it was built from, so the buffer must outlive the returned Image.
    return IcoFile(io.BytesIO(brand_icons.FAVICON_PATH.read_bytes()))


def _ico_frame(size: int) -> Image.Image:
    frame = _favicon().getimage((size, size))
    frame.load()
    return frame.convert("RGBA")


def _max_channel_diff(a: Image.Image, b: Image.Image) -> int:
    diff = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA"))
    return max(hi for _, hi in ImageStat.Stat(diff).extrema)


def _assert_monochrome(frame: Image.Image) -> None:
    r, g, b = frame.convert("RGB").split()
    assert ImageChops.difference(r, g).getbbox() is None
    assert ImageChops.difference(r, b).getbbox() is None


def test_crop_master_keeps_the_mark_and_drops_a_wordmark_below_it(tmp_path: Path) -> None:
    source = Image.new("RGB", (100, 100), brand_icons.PAPER)
    source.paste(brand_icons.INK_DARK, (20, 10, 80, 50))  # the mark
    source.paste(brand_icons.INK_DARK, (10, 70, 90, 85))  # a wordmark past a blank gap
    path = tmp_path / "logo.png"
    source.save(path)
    assert brand_icons.crop_master(path).size == (60, 40)


def test_shared_mark_is_640px_with_black_ink_on_white() -> None:
    mark = Image.open(brand_icons.MARK_PATH)
    assert mark.size == (brand_icons.TOP_BAR_PX, brand_icons.TOP_BAR_PX) == (640, 640)
    _assert_monochrome(mark)
    assert mark.getpixel((320, 0)) == (255, 255, 255, 255)
    low, high = ImageStat.Stat(mark.crop((160, 160, 480, 480)).convert("L")).extrema[0]
    assert low < 64
    assert high == 255
    alpha = mark.getchannel("A")
    assert alpha.getextrema() == (0, 255)
    assert alpha.getpixel((0, 0)) == 0


def test_favicon_has_exactly_the_nine_sizes() -> None:
    assert _favicon().sizes() == {(s, s) for s in ICO_SIZES}


def test_every_favicon_frame_holds_a_monochrome_emblem_on_white() -> None:
    for size in ICO_SIZES:
        frame = _ico_frame(size)
        _assert_monochrome(frame)
        low, high = ImageStat.Stat(frame.convert("L")).extrema[0]
        assert low < 64
        assert high == 255


def test_build_matches_the_committed_favicon_and_shared_mark() -> None:
    outputs = brand_icons.build()

    built_ico = outputs["favicon.ico"]
    assert [frame.size for frame in built_ico] == [(s, s) for s in ICO_SIZES]
    for built, size in zip(built_ico, ICO_SIZES, strict=True):
        assert _max_channel_diff(built, _ico_frame(size)) <= 2

    committed = Image.open(brand_icons.MARK_PATH)
    assert _max_channel_diff(outputs["mark.png"][0], committed) <= 2


def test_index_links_the_favicon_by_a_hash_of_its_bytes() -> None:
    index = brand_icons.INDEX_PATH.read_text(encoding="utf-8")
    version = brand_icons.asset_version(brand_icons.FAVICON_PATH.read_bytes())
    assert f'href="/favicon.ico?v={version}"' in index
    assert brand_icons.versioned_index(index, brand_icons.FAVICON_PATH.read_bytes()) == index


def test_welcome_links_the_mark_by_a_hash_of_its_bytes() -> None:
    mark = brand_icons.MARK_PATH.read_bytes()
    version = hashlib.sha256(mark).hexdigest()[:8]
    for path in brand_icons.MARK_SOURCE_PATHS:
        source = path.read_text(encoding="utf-8")
        assert f"/brand/mark.png?v={version}" in source
        assert brand_icons.versioned_mark(source, mark) == source


def test_changing_the_mark_changes_its_url_and_preserves_the_other_source_text() -> None:
    source = 'src="/brand/mark.png" other="unchanged"'
    previous = brand_icons.versioned_mark(source, b"first mark")
    current = brand_icons.versioned_mark(previous, b"new mark")
    version = hashlib.sha256(b"new mark").hexdigest()[:8]
    assert current != previous
    assert f"/brand/mark.png?v={version}" in current
    assert current.split(" ")[1] == previous.split(" ")[1]
    assert brand_icons.versioned_mark(current, b"new mark") == current
