"""Rebuild VIKI's favicon and shared brand mark from the committed master.

    uv run python scripts/brand_icons.py                    # rebuild favicon.ico and mark.png
    uv run python scripts/brand_icons.py --crop original.png  # also rewrite the master first

`--crop` rewrites `scripts/brand/viki-mark.png` from a source PNG you pass: grayscale,
threshold at <200, keep the inked rows from the top down to the first wide blank gap (the mark;
a wordmark below it, if the source has one, sits past that gap and is dropped), crop the original
to that ink, and cap the long side at MASTER_MAX. The source path is read only; it is never
written into the repo, so pass it on the command line each time, never add a default for it.

Every size uses VIKI's full globe mark; there is no simplified small-size
variant. The welcome mark's 640px source image supports its 160 CSS px display size at 4x density.
The favicon carries a frame for every size Windows asks for; window_icon also uses it for the
taskbar. The script stamps the favicon
link in `frontend/index.html` and the mark's welcome URL with hashes of
their bytes so changed artwork has different URLs from previously cached images.

Every output is deterministic given the committed master, so running this script again with no
`--crop` reproduces the same bytes (within PNG re-encoding, pixel-identical) and leaves the
working tree unchanged.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import re
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
MASTER_PATH = REPO / "scripts" / "brand" / "viki-mark.png"
FAVICON_PATH = REPO / "frontend" / "public" / "favicon.ico"
INDEX_PATH = REPO / "frontend" / "index.html"
MARK_PATH = REPO / "frontend" / "public" / "brand" / "mark.png"
MARK_SOURCE_PATHS = (REPO / "frontend" / "src" / "components" / "Conversation" / "States.css",)

# The original black emblem on white, shared by both themes.
PAPER = (255, 255, 255)
INK_DARK = (0, 0, 0)

# Every size Windows asks for at 100-200% scaling (title bar, taskbar, Alt+Tab), plus 128 and 256.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
FAVICON_LINK = re.compile(r'href="/favicon\.ico(?:\?v=[0-9a-f]+)?"')
TOP_BAR_PX = 640  # The welcome mark displays up to 160 CSS px; 4x supports high-DPI screens.
MASTER_MAX = 4096  # long side of the committed master (the 4K upscale, kept at full resolution)
WORDMARK_GAP = 0.04  # blank run, as a fraction of the inked height, that separates a wordmark
CANVAS = 1024  # working resolution the mark is drawn at, then LANCZOS down to each output size
CANVAS_SCALE = 1.18  # canvas side == this many times the mark's long side
TILE_RADIUS_FRACTION = 0.22  # rounded-tile corner radius, as a fraction of the canvas side


def _lt(limit: int) -> Callable[[int], int]:
    """A point() LUT function: 255 where the pixel is below `limit`, else 0. A `def`, not a
    lambda: point()'s signature is also overloaded for "I"/"F" gamma transforms, and pyright
    needs the explicit `int` parameter to pick the plain per-pixel-value overload."""

    def _point(value: int) -> int:
        return 255 if value < limit else 0

    return _point


def crop_master(original: Path) -> Image.Image:
    """Rewrite the master from a source PNG: grayscale, threshold at <200, keep the
    inked rows down to the first blank gap wider than WORDMARK_GAP, crop the original to that
    ink, and cap the long side at MASTER_MAX."""
    source = Image.open(original).convert("RGBA")
    mask = source.convert("L").point(_lt(200))
    width, height = mask.size
    inked = [mask.crop((0, y, width, y + 1)).getbbox() is not None for y in range(height)]
    if True not in inked:
        raise ValueError(f"no ink found in {original}")
    top = inked.index(True)
    last = height - 1 - inked[::-1].index(True)
    # A wide blank run separates the emblem from the lettering below it.
    min_gap = max(1, round((last - top) * WORDMARK_GAP))
    bottom, run = last + 1, 0
    for y in range(top, last + 1):
        run = 0 if inked[y] else run + 1
        if run >= min_gap:
            bottom = y - run + 1
            break
    bbox = mask.crop((0, top, width, bottom)).getbbox()
    assert bbox is not None
    x0, y0, x1, y1 = bbox
    mark = source.crop((x0, top + y0, x1, top + y1))
    scale = MASTER_MAX / max(mark.size)
    if scale < 1:
        mark = mark.resize(
            (round(mark.width * scale), round(mark.height * scale)), Image.Resampling.LANCZOS
        )
    # Flatten to black and white: render() reads only the ink mask, and an
    # upscaler's grain in the ink and paper would otherwise bloat the committed PNG.
    flat = Image.new("RGBA", mark.size, (*PAPER, 255))
    flat.paste((*INK_DARK, 255), (0, 0), ink_mask(mark))
    return flat


def ink_mask(mark: Image.Image) -> Image.Image:
    """Same-size monochrome mask: 255 where the source's luminance is below 128."""
    return mark.convert("L").point(_lt(128))


@functools.lru_cache(maxsize=1)
def _master() -> Image.Image:
    return Image.open(MASTER_PATH).convert("RGBA")


def render(size: int) -> Image.Image:
    """Square RGBA canvas 1.18x the mark's long side, drawn at 1024px and LANCZOS'd down to
    `size`: black ink on a rounded white tile, with transparent outer corners."""
    mark = _master()
    ink = ink_mask(mark)

    scale = (CANVAS / CANVAS_SCALE) / max(mark.size)
    scaled_size = (round(mark.width * scale), round(mark.height * scale))
    ink = ink.resize(scaled_size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    radius = round(CANVAS * TILE_RADIUS_FRACTION)
    ImageDraw.Draw(canvas).rounded_rectangle(
        [0, 0, CANVAS - 1, CANVAS - 1], radius=radius, fill=(*PAPER, 255)
    )
    offset = ((CANVAS - scaled_size[0]) // 2, (CANVAS - scaled_size[1]) // 2)
    canvas.paste(Image.new("RGBA", scaled_size, (*INK_DARK, 255)), offset, ink)

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def build() -> dict[str, list[Image.Image]]:
    """Every owned output, rendered in memory: the nine ICO frames and the shared mark."""
    return {
        "favicon.ico": [render(s) for s in ICO_SIZES],
        "mark.png": [render(TOP_BAR_PX)],
    }


def asset_version(asset: bytes) -> str:
    """First 8 hex digits of the asset's SHA-256, used to change its URL with its bytes."""
    return hashlib.sha256(asset).hexdigest()[:8]


def versioned_index(index: str, favicon: bytes) -> str:
    """index.html with its favicon link pointing at /favicon.ico?v=<asset_version>."""
    link = f'href="/favicon.ico?v={asset_version(favicon)}"'
    updated, count = FAVICON_LINK.subn(link, index)
    if count != 1:
        raise ValueError(f"expected one favicon link in index.html, found {count}")
    return updated


def versioned_mark(source: str, mark: bytes) -> str:
    """Version the shared mark's single URL in its source file."""
    updated, count = re.subn(
        r"/brand/mark\.png(?:\?v=[0-9a-f]+)?", f"/brand/mark.png?v={asset_version(mark)}", source
    )
    if count != 1:
        raise ValueError(f"expected one /brand/mark.png reference, found {count}")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crop", metavar="ORIGINAL_PNG", help="rewrite the master from this file")
    args = parser.parse_args()

    if args.crop:
        cropped = crop_master(Path(args.crop))
        cropped.info.clear()
        MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(MASTER_PATH, format="PNG", optimize=True)
        print(MASTER_PATH)

    outputs = build()

    # Pillow's ICO writer skips any requested size larger than the image .save() is called on,
    # so that call must be the largest frame; the rest ride in as append_images.
    frames = outputs["favicon.ico"]
    FAVICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(
        FAVICON_PATH,
        format="ICO",
        sizes=[frame.size for frame in frames],
        append_images=frames[:-1],
        optimize=True,
    )
    print(FAVICON_PATH)

    index = INDEX_PATH.read_text(encoding="utf-8")
    updated = versioned_index(index, FAVICON_PATH.read_bytes())
    if updated != index:
        INDEX_PATH.write_text(updated, encoding="utf-8", newline="\n")
        print(INDEX_PATH)

    MARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    outputs["mark.png"][0].save(MARK_PATH, format="PNG", optimize=True)
    mark = MARK_PATH.read_bytes()
    print(MARK_PATH)

    for path in MARK_SOURCE_PATHS:
        source = path.read_text(encoding="utf-8")
        updated = versioned_mark(source, mark)
        if updated != source:
            path.write_text(updated, encoding="utf-8", newline="\n")
            print(path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
