/** VIKI AGENT in white tiles: the lettering the showcase's logo motion assembles from Syncopate
 * Bold. Large sizes use the film's own grid, 13 tile rows to the cap height plus one row for the
 * G's overshoot. Below about 40px those tiles merge, so small sizes use a hand-tuned 7-row cut of
 * the same letterforms. Given a title, the lettering is an image with that name; otherwise it is
 * decorative and the caller names it. */
import "./TileWordmark.css";

/** The 7-row glyphs, with two-tile stems, laid out at the film's proportions. */
const GLYPHS: Readonly<Record<string, readonly string[]>> = {
  V: ["##.....##", "##.....##", ".##...##.", ".##...##.", "..##.##..", "..##.##..", "...###..."],
  I: ["##", "##", "##", "##", "##", "##", "##"],
  K: ["##....##", "##...##.", "##..##..", "#####...", "##..##..", "##...##.", "##....##"],
  A: ["...###...", "..##.##..", "..##.##..", ".##...##.", ".#######.", "##.....##", "##.....##"],
  G: [".#######.", "##.....##", "##.......", "##...####", "##.....##", "##.....##", ".#######."],
  E: ["########", "##......", "##......", "#######.", "##......", "##......", "########"],
  N: ["##.....##", "###....##", "####...##", "##.##..##", "##..##.##", "##...####", "##....###"],
  T: ["########", "...##...", "...##...", "...##...", "...##...", "...##...", "...##..."],
};

/** Lays the glyphs out in one row of tile lines: `gap` empty columns between letters and a
 * `space`-column word space. */
function compose(text: string, gap: number, space: number): string[] {
  const glyphs = [...text].map((letter) => {
    if (letter === " ") return Array<string>(7).fill(".".repeat(space - 2 * gap));
    const glyph = GLYPHS[letter];
    if (!glyph) throw new Error(`No tile glyph for "${letter}"`);
    return glyph;
  });
  return Array.from({ length: 7 }, (_, row) =>
    glyphs.map((glyph) => glyph[row]).join(".".repeat(gap)),
  );
}

export const SMALL_TILES: readonly string[] = compose("VIKI AGENT", 2, 8);

/** The film's grid (docs/showcase), with both I stems three tiles wide. */
export const LARGE_TILES: readonly string[] = [
  "####.........####....###......###.......#####....###....................####.............###########......##############....#####........####...###############",
  ".####........####....###......###.....#####......###...................#####............#############.....##############....######.......####...###############",
  ".####.......####.....###......###....#####.......###..................#######..........######...#####.....#############.....#######......####...###############",
  "..####......####.....###......###...####.........###..................########........####..........#.....####..............########.....####.........###......",
  "..####.....####......###......#########..........###.................####.####.......####.................####..............#########....####.........###......",
  "...####....####......###......########...........###.................####..####......####.................#############.....####.#####...####.........###......",
  "...####...####.......###......#######............###................####...####......####.....#######.....#############.....####..#####..####.........###......",
  "....####..####.......###......########...........###................####....####.....####.....#######.....#############.....####...#####.####.........###......",
  "....####.####........###......###..#####.........###...............#############.....####.....#######.....####..............####....#########.........###......",
  ".....########........###......###...#####........###...............##############.....####........###.....####..............####.....########.........###......",
  ".....#######.........###......###....#####.......###..............###############.....#######...#####.....##############....####......#######.........###......",
  "......######.........###......###......#####.....###..............####........####.....##############.....##############....####.......######.........###......",
  "......#####..........###......###.......#####....###.............####.........####.......############.....##############....####........#####.........###......",
  "............................................................................................#####..............................................................",
];

/** One square per tile, anchored to its cell's top-left corner: at the small size's 2px cell,
 * each tile then keeps one whole lit pixel at 1x, so the grid stays visible instead of blurring
 * to grey. */
function tilePath(rows: readonly string[], tile: number): string {
  const side = String(tile).replace(/^0/, "");
  let path = "";
  rows.forEach((line, y) => {
    for (let x = 0; x < line.length; x++) {
      if (line[x] === "#") path += `M${x} ${y}h${side}v${side}h-${side}z`;
    }
  });
  return path;
}

function lettering(rows: readonly string[], tile: number) {
  return { viewBox: `0 0 ${rows[0]?.length ?? 0} ${rows.length}`, path: tilePath(rows, tile) };
}

const SIZES = {
  small: lettering(SMALL_TILES, 0.75),
  large: lettering(LARGE_TILES, 0.84),
} as const;

/** A `title` names the image through SVG's own `<title>`, which is never drawn: the name stays in
 * the page text without adding a text box beside the tiles. */
export function TileWordmark({ size, title }: { size: keyof typeof SIZES; title?: string }) {
  const { viewBox, path } = SIZES[size];
  const className = `tile-wordmark tile-wordmark-${size}`;
  return title ? (
    <svg className={className} viewBox={viewBox} role="img">
      <title>{title}</title>
      <path d={path} />
    </svg>
  ) : (
    <svg className={className} viewBox={viewBox} aria-hidden="true">
      <path d={path} />
    </svg>
  );
}
