/** A Bot's generated icon: the brand mark's pixel-tile sphere, with a dark visor, LED dot eyes
 * and a pixel emblem in one LED colour, dissolving into loose pixels on its right. The team's seat
 * names, and Builder, have fixed designs; any other Bot takes its shapes and colour from its profile id, so a
 * profile always gets the same icon and a new profile usually gets a new one. */

// Colours are tokens.css tokens only (tokens.unit.test.ts bans literals). BotIcon renders with
// data-theme="dark", so in either app theme the bot is a dark object and its LEDs take the dark
// theme's bright --label-* hues.
export const LED = {
  blue: "var(--label-blue)",
  green: "var(--label-green)",
  orange: "var(--label-orange)",
  purple: "var(--label-purple)",
  red: "var(--label-red)",
  // The slate label shows teal.
  teal: "var(--label-slate)",
  // Assigned, not drawn (botHues.ts): bot-only tokens, not session label colours.
  amber: "var(--bot-amber)",
  pink: "var(--bot-pink)",
} as const;
export type Hue = keyof typeof LED;

// 7x7 dot maps, "#" lit. A left eye: the right eye draws it mirrored.
const EYES = {
  round: ["..###..", ".#####.", "#######", "#######", "#######", ".#####.", "..###.."],
  ring: ["..###..", ".#...#.", "#.....#", "#.....#", "#.....#", ".#...#.", "..###.."],
  narrow: [".......", "##.....", "####...", "######.", "#######", ".#####.", "..###.."],
  arc: [".......", "..###..", ".#...#.", "#.....#", "#.....#", ".......", "......."],
  block: [".......", "##.....", "#####..", "#######", "#######", "#######", "......."],
  square: [".......", ".#####.", ".#####.", ".#####.", ".#####.", ".#####.", "......."],
  heart: [".......", ".##.##.", "#######", "#######", ".#####.", "..###..", "...#..."],
  plus: ["..###..", "..###..", "#######", "#######", "#######", "..###..", "..###.."],
  line: [".......", ".......", ".......", "#######", "#######", ".......", "......."],
  diamond: ["...#...", "..###..", ".#####.", "#######", ".#####.", "..###..", "...#..."],
  dot: [".......", ".......", "..###..", "..###..", "..###..", ".......", "......."],
  star: ["...#...", "...#...", "#######", ".#####.", "..###..", ".##.##.", ".#...#."],
  crescent: ["..###..", ".#####.", "#######", "##...##", "#.....#", ".......", "......."],
  bars: ["..###..", "..###..", "..###..", "..###..", "..###..", "..###..", "..###.."],
} as const satisfies Record<string, readonly string[]>;
export type EyeShape = keyof typeof EYES;

// 7x7 pixel maps, "#" lit. The first seven belong to the seats and are never drawn at random.
const EMBLEMS = {
  door: [".#####.", ".#...#.", ".#...#.", ".#..##.", ".#...#.", ".#...#.", ".#####."],
  loop: ["..###.#", ".#...##", "#...###", "#......", "#.....#", ".#...#.", "..###.."],
  check: [".......", "......#", ".....##", "#...##.", "##.##..", ".###...", "..#...."],
  lens: [".###...", "#...#..", "#...#..", "#...#..", ".####..", "....##.", ".....##"],
  nib: ["...#...", "..###..", ".##.##.", "##.#.##", ".##.##.", "..###..", "...#..."],
  wrench: ["....#.#", "....###", "...###.", "..###..", ".###...", "###....", ".#....."],
  tray: [".......", "#.....#", "#.....#", "##...##", "#.###.#", "#.....#", "#######"],
  bolt: ["....##.", "...##..", "..####.", ".####..", "...##..", "..##...", ".##...."],
  star: ["...#...", "..###..", "#######", ".#####.", "..###..", ".##.##.", "##...##"],
  heart: [".......", ".##.##.", "#######", "#######", ".#####.", "..###..", "...#..."],
  leaf: ["....###", "..#####", ".##..##", ".#..##.", ".#.##..", ".###...", "#......"],
  bell: ["...#...", "..###..", ".#####.", ".#####.", ".#####.", "#######", "...#..."],
  note: ["..#####", "..#...#", "..#...#", "..#...#", "###.###", "###.###", "......."],
  bubble: ["#######", "#.....#", "#.#.#.#", "#.....#", "#######", ".##....", ".#....."],
  key: [".###...", "#...#..", ".###...", "..#....", "..##...", "..#....", "..##..."],
  flag: [".#.....", ".#####.", ".######", ".#####.", ".#.....", ".#.....", ".#....."],
  flask: ["..###..", "...#...", "...#...", "..#.#..", ".#...#.", "#######", "#######"],
  shield: ["#######", "#######", "#######", ".#####.", ".#####.", "..###..", "...#..."],
  globe: ["..###..", ".#.#.#.", "#..#..#", "#######", "#..#..#", ".#.#.#.", "..###.."],
  pin: ["..###..", ".#####.", ".##.##.", ".#####.", "..###..", "...#...", "...#..."],
  cloud: [".......", "..##...", ".####..", ".#####.", "#######", "#######", "......."],
  book: [".......", "###.###", "#.#.#.#", "#.#.#.#", "#.#.#.#", "###.###", "...#..."],
  chart: [".....##", ".....##", "...####", "...####", ".######", ".######", "#######"],
  spark: ["...#...", "...#...", "..###..", "#######", "..###..", "...#...", "...#..."],
  moon: ["..###..", ".##....", "##.....", "##.....", "##.....", ".##....", "..###.."],
  gear: [".#.#.#.", "#######", ".##.##.", "###.###", ".##.##.", "#######", ".#.#.#."],
  target: ["..###..", ".#...#.", "#..#..#", "#.###.#", "#..#..#", ".#...#.", "..###.."],
} as const satisfies Record<string, readonly string[]>;
export type Emblem = keyof typeof EMBLEMS;

export type Head = "orb" | "block" | "capsule";
export type Visor = "pill" | "band" | "round" | "goggles";
export type Gear = "none" | "antenna" | "twin" | "ear";

export interface BotDesign {
  seat: Seat | null;
  head: Head;
  visor: Visor;
  gear: Gear;
  eyes: EyeShape;
  emblem: Emblem;
  hue: Hue;
  /** Seeds the tile mosaic and the loose pixels. */
  grain: number;
}

const SEATS = {
  chief: { eyes: "ring", emblem: "door", hue: "orange" },
  ops: { eyes: "round", emblem: "loop", hue: "green" },
  checker: { eyes: "narrow", emblem: "check", hue: "blue" },
  research: { eyes: "round", emblem: "lens", hue: "purple" },
  writer: { eyes: "arc", emblem: "nib", hue: "red" },
  engineer: { eyes: "block", emblem: "wrench", hue: "amber" },
  builder: { eyes: "block", emblem: "wrench", hue: "amber" },
  desk: { eyes: "square", emblem: "tray", hue: "teal" },
} as const satisfies Record<string, { eyes: EyeShape; emblem: Emblem; hue: Hue }>;
export type Seat = keyof typeof SEATS;
export const SEAT_NAMES = Object.keys(SEATS) as Seat[];
const SEAT_EMBLEMS: readonly Emblem[] = SEAT_NAMES.map((seat) => SEATS[seat].emblem);

export const EYE_SHAPES = Object.keys(EYES) as EyeShape[];
export const FREE_EMBLEMS = (Object.keys(EMBLEMS) as Emblem[]).filter(
  (emblem) => !SEAT_EMBLEMS.includes(emblem),
);
export const BOT_ICON_IDS = [
  "door",
  "loop",
  "lens",
  "nib",
  "wrench",
  "tray",
  "check",
  ...FREE_EMBLEMS,
] as const satisfies readonly Emblem[];
export function isBotIconId(value: unknown): value is Emblem {
  return typeof value === "string" && BOT_ICON_IDS.includes(value as Emblem);
}
// The pool a profile's seed draws from (designFor): fixed, so an existing Bot's drawn hue never
// moves when a new hue joins the palette below.
export const HUES = ["blue", "green", "orange", "purple", "red", "teal"] as Hue[];
/** Every hue a Bot can show: the drawn pool above, plus the two assigned-only extras
 * (botHues.ts), in tie-break order. */
export const BOT_HUES: readonly Hue[] = [...HUES, "amber", "pink"];
export const dotMap = (shape: EyeShape): readonly string[] => EYES[shape];
export const pixelMap = (emblem: Emblem): readonly string[] => EMBLEMS[emblem];

/** FNV-1a: a stable 32-bit number for a string. */
function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** mulberry32: a small seeded generator, so the same seed always draws the same icon. */
function seeded(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick<T>(random: () => number, items: readonly T[]): T {
  return items[Math.floor(random() * items.length)] as T;
}

/** The orb is the brand's own figure, so it comes up most often. */
const HEADS: readonly Head[] = ["orb", "orb", "orb", "block", "capsule"];
const VISORS: readonly Visor[] = ["pill", "pill", "band", "round", "goggles"];
const GEARS: readonly Gear[] = ["none", "none", "antenna", "twin", "ear"];

/** A seat's design when the profile is named after one, otherwise one drawn from `seed`. */
export function designFor(seed: string, name: string, icon?: string): BotDesign {
  const key = name.trim().toLowerCase();
  if (Object.hasOwn(SEATS, key)) {
    const seat = key as Seat;
    const design: BotDesign = {
      seat,
      head: "orb",
      visor: "pill",
      gear: "none",
      ...SEATS[seat],
      grain: hash(seat),
    };
    return isBotIconId(icon) ? { ...design, emblem: icon } : design;
  }
  const random = seeded(hash(seed));
  const design: BotDesign = {
    seat: null,
    head: pick(random, HEADS),
    visor: pick(random, VISORS),
    gear: pick(random, GEARS),
    eyes: pick(random, EYE_SHAPES),
    emblem: pick(random, FREE_EMBLEMS),
    hue: pick(random, HUES),
    grain: hash(`${seed}:grain`),
  };
  return isBotIconId(icon) ? { ...design, emblem: icon } : design;
}

// Geometry, in a 100x100 view box.
const CX = 45;
const CY = 53;
const TAU = Math.PI * 2;
type Vec = [number, number, number];
type Point = [number, number];
const BOXES: Record<Head, { hw: number; hh: number; r: number }> = {
  orb: { hw: 33, hh: 33, r: 33 },
  block: { hw: 31, hh: 30, r: 15 },
  capsule: { hw: 27, hh: 35, r: 27 },
};

const normalize = ([x, y, z]: Vec): Vec => {
  const length = Math.hypot(x, y, z) || 1;
  return [x / length, y / length, z / length];
};
const dot = (a: Vec, b: Vec) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
// Light from the upper left, in front; y points up, z toward the viewer.
const LIGHT = normalize([-0.5, 0.62, 0.6]);
const HALFWAY = normalize([LIGHT[0], LIGHT[1], LIGHT[2] + 1]);
const round = (v: number) => Math.round(v * 100) / 100;

/** Tiles are one light token over the dark head; their opacity sets each tile's grey. */
export const TILE = "var(--t0)";
export const HEAD = "var(--g0)";
function tileOpacity(level: number): string {
  // --g0 is 11/255 and --t0 237/255 in the dark theme: solve for the grey `level` asks for.
  const opacity = (Math.max(0.03, Math.min(0.95, level)) * 255 - 11) / 226;
  return String(Math.max(0.02, Math.round(opacity * 25) / 25));
}

/** A tile's lit top edge, as an opacity step of the highlight colour; too faint to draw is null. */
function glintOpacity(amount: number): string | null {
  const step = Math.round(Math.min(0.8, amount) * 10) / 10;
  return step >= 0.1 ? String(step) : null;
}

// Loose pixels sit on the avatar's own fill, light or dark, so they take solid dark-theme greys.
const LOOSE_GREYS: [token: string, level: number][] = [
  ["var(--b1)", 0x22],
  ["var(--b-strong)", 0x33],
  ["var(--face-dim)", 0x4a],
  ["var(--t3)", 0xa3],
];
function looseGrey(level: number): string {
  const target = level * 255;
  let best = LOOSE_GREYS[0] as [string, number];
  for (const grey of LOOSE_GREYS) {
    if (Math.abs(grey[1] - target) < Math.abs(best[1] - target)) best = grey;
  }
  return best[0];
}

function roundedRect(x: number, y: number, w: number, h: number, r: number): string {
  const q = round(Math.min(r, w / 2, h / 2));
  return [
    `M${round(x + q)} ${round(y)}H${round(x + w - q)}A${q} ${q} 0 0 1 ${round(x + w)} ${round(y + q)}`,
    `V${round(y + h - q)}A${q} ${q} 0 0 1 ${round(x + w - q)} ${round(y + h)}`,
    `H${round(x + q)}A${q} ${q} 0 0 1 ${round(x)} ${round(y + h - q)}`,
    `V${round(y + q)}A${q} ${q} 0 0 1 ${round(x + q)} ${round(y)}Z`,
  ].join("");
}

const circle = (cx: number, cy: number, r: number) =>
  `M${round(cx - r)} ${round(cy)}a${round(r)} ${round(r)} 0 1 0 ${round(r * 2)} 0a${round(r)} ${round(r)} 0 1 0 ${round(-r * 2)} 0Z`;
const rect = (x: number, y: number, w: number, h: number) =>
  `M${round(x)} ${round(y)}h${round(w)}v${round(h)}h${round(-w)}Z`;
const square = (x: number, y: number, s: number) => rect(x, y, s, s);
const line = (a: Point, b: Point) => `M${round(a[0])} ${round(a[1])}L${round(b[0])} ${round(b[1])}`;

/** A small cube seen from the front: light on its top and left edges, shade on the others. */
function voxel(x: number, y: number, s: number) {
  const e = s * 0.24;
  return {
    face: square(x, y, s),
    light: rect(x, y, s, e) + rect(x, y + e, e, s - e),
    shade: rect(x, y + s - e, s, e) + rect(x + s - e, y + e, e, s - 2 * e),
  };
}

function insideHead(head: Head, x: number, y: number): boolean {
  const { hw, hh, r } = BOXES[head];
  const dx = Math.max(Math.abs(x - CX) - (hw - r), 0);
  const dy = Math.max(Math.abs(y - CY) - (hh - r), 0);
  return Math.hypot(dx, dy) <= r;
}

interface Tile {
  outline: Point[];
  normal: Vec;
  x: number;
  y: number;
  size: number;
}

/** The edge of a tile that faces up, which catches the light from above. */
function topEdge(outline: Point[]): string {
  let best = 0;
  let bestY = Number.POSITIVE_INFINITY;
  outline.forEach((point, i) => {
    const next = outline[(i + 1) % outline.length] as Point;
    if (point[1] + next[1] < bestY) {
      bestY = point[1] + next[1];
      best = i;
    }
  });
  return line(outline[best] as Point, outline[(best + 1) % outline.length] as Point);
}

// The orb's tiles: a latitude-longitude grid on a sphere turned a little to the viewer's left.
const YAW = 0.16;
const PITCH = 0.1;
function view([x, y, z]: Vec): Vec {
  const x1 = x * Math.cos(YAW) - z * Math.sin(YAW);
  const z1 = x * Math.sin(YAW) + z * Math.cos(YAW);
  return [
    x1,
    y * Math.cos(PITCH) - z1 * Math.sin(PITCH),
    y * Math.sin(PITCH) + z1 * Math.cos(PITCH),
  ];
}
const onSphere = (lat: number, lon: number): Vec => [
  Math.cos(lat) * Math.sin(lon),
  Math.sin(lat),
  Math.cos(lat) * Math.cos(lon),
];
const toScreen = ([x, y]: Vec, r: number): Point => [CX + r * x, CY - r * y];

function orbTiles(): Tile[] {
  const r = BOXES.orb.r;
  const tiles: Tile[] = [];
  const bands = 13;
  const columns = 28;
  for (let i = 0; i < bands; i++) {
    const lat0 = -Math.PI / 2 + ((i + 0.06) * Math.PI) / bands;
    const lat1 = -Math.PI / 2 + ((i + 0.94) * Math.PI) / bands;
    for (let j = 0; j < columns; j++) {
      const lon0 = ((j + 0.06) * TAU) / columns;
      const lon1 = ((j + 0.94) * TAU) / columns;
      const middle = view(onSphere((lat0 + lat1) / 2, (lon0 + lon1) / 2));
      if (middle[2] < 0.06) continue;
      const corners = [
        onSphere(lat0, lon0),
        onSphere(lat0, lon1),
        onSphere(lat1, lon1),
        onSphere(lat1, lon0),
      ];
      const [x, y] = toScreen(middle, r);
      tiles.push({
        outline: corners.map((corner) => toScreen(view(corner), r)),
        normal: middle,
        x,
        y,
        size: (r * Math.PI) / bands,
      });
    }
  }
  return tiles;
}

// The block and the capsule tile flat; a domed or cylindrical normal still shades them round.
function gridTiles(head: Head): Tile[] {
  const { hw, hh } = BOXES[head];
  const step = 5.4;
  const gap = 0.8;
  const tiles: Tile[] = [];
  for (let y = CY - hh; y < CY + hh; y += step) {
    for (let x = CX - hw; x < CX + hw; x += step) {
      const mx = x + step / 2;
      const my = y + step / 2;
      if (!insideHead(head, mx, my)) continue;
      const nx = (mx - CX) / hw;
      const ny = (CY - my) / hh;
      const normal: Vec =
        head === "capsule"
          ? normalize([nx, ny * 0.3, Math.sqrt(Math.max(0.05, 1 - nx * nx))])
          : normalize([nx * 0.7, ny * 0.7, 1]);
      const a = gap / 2;
      const b = step - gap / 2;
      tiles.push({
        outline: [
          [x + a, y + a],
          [x + b, y + a],
          [x + b, y + b],
          [x + a, y + b],
        ],
        normal,
        x: mx,
        y: my,
        size: step - gap,
      });
    }
  }
  return tiles;
}

/** One SVG path per fill keeps an icon to a few dozen nodes instead of a few hundred. */
export type Paths = Record<string, string>;
function add(paths: Paths, key: string, d: string) {
  paths[key] = (paths[key] ?? "") + d;
}

export interface Ellipse {
  cx: number;
  cy: number;
  rx: number;
  ry: number;
}

export interface BotGeometry {
  design: BotDesign;
  led: string;
  head: string;
  /** Keyed by the opacity of the TILE token over the HEAD fill. */
  tiles: Paths;
  /** Each tile's lit top edge, keyed by the highlight's opacity. */
  glints: Paths;
  /** Keyed by the loose pixels' grey token. */
  loose: Paths;
  looseLight: string;
  visor: string[];
  /** The bevelled rim around each visor glass. */
  frame: string[];
  /** Bands of reflected light, drawn clipped to the glass. */
  streak: string;
  gloss: string;
  eyesLit: string;
  /** The hot white centre of each lit eye dot. */
  eyesHot: string;
  eyesDim: string;
  emblem: string;
  emblemLight: string;
  emblemShade: string;
  sparks: { d: string; opacity: number }[];
  gear: { stems: string; balls: string; ballsHot: string; ear: string; earLight: string };
  /** A broad soft highlight, a small sharp one and the light bounced back from below. */
  shine: Ellipse;
  glint: Ellipse;
  bounce: Ellipse;
}

/** Two slanted bands of reflected light across a glass box. */
function streaks(x: number, y: number, w: number, h: number): string {
  const band = (from: number, to: number) =>
    `M${round(x + w * from)} ${round(y - 1)}L${round(x + w * to)} ${round(y - 1)}` +
    `L${round(x + w * (to - 0.2))} ${round(y + h + 1)}L${round(x + w * (from - 0.2))} ${round(y + h + 1)}Z`;
  return band(0.08, 0.27) + band(0.31, 0.36);
}

const BEVEL = 1.7;

function visorShape(design: BotDesign) {
  const { hw, hh } = BOXES[design.head];
  const front =
    design.head === "orb" ? toScreen(view([0, 0, 1]), hw) : [CX - 0.1 * hw, CY + 0.05 * hh];
  const [vx, vy] = front as Point;
  if (design.visor === "goggles") {
    const r = 0.3 * hw;
    const gap = 0.36 * hw;
    const lenses: Point[] = [
      [vx - gap, vy],
      [vx + gap, vy],
    ];
    return {
      paths: lenses.map(([x, y]) => circle(x, y, r)),
      frame: lenses.map(([x, y]) => circle(x, y, r + BEVEL)),
      streak: lenses.map(([x, y]) => streaks(x - r, y - r, r * 2, r * 2)).join(""),
      eyes: lenses,
      pitch: r * 0.26,
      gloss: `M${round(vx - gap - r * 0.55)} ${round(vy - r * 0.55)}A${round(r)} ${round(r)} 0 0 1 ${round(vx - gap + r * 0.2)} ${round(vy - r * 0.85)}`,
    };
  }
  const size = {
    pill: { w: 1.3 * hw, h: 0.62 * hw, r: 0.31 * hw },
    band: { w: 1.7 * hw, h: 0.5 * hw, r: 0.16 * hw },
    round: { w: 1.08 * hw, h: 0.86 * hw, r: 0.38 * hw },
  }[design.visor];
  const x = vx - size.w / 2;
  const y = vy - size.h / 2;
  return {
    paths: [roundedRect(x, y, size.w, size.h, size.r)],
    frame: [
      roundedRect(x - BEVEL, y - BEVEL, size.w + BEVEL * 2, size.h + BEVEL * 2, size.r + BEVEL),
    ],
    streak: streaks(x, y, size.w, size.h),
    eyes: [
      [vx - size.w * 0.23, vy],
      [vx + size.w * 0.23, vy],
    ] as Point[],
    pitch: Math.min(size.h * 0.108, size.w * 0.07),
    gloss: `M${round(x + size.r)} ${round(y + 1.6)}H${round(x + size.w - size.r)}`,
  };
}

function eyeDots(design: BotDesign, centers: Point[], pitch: number) {
  let lit = "";
  let hot = "";
  let dim = "";
  const r = pitch * 0.38;
  const rows = EYES[design.eyes];
  centers.forEach(([ex, ey], side) => {
    rows.forEach((row, i) => {
      for (let j = 0; j < 7; j++) {
        const col = side === 0 ? j : 6 - j;
        const x = ex + (j - 3) * pitch;
        const y = ey + (i - 3) * pitch;
        if (row[col] === "#") {
          lit += circle(x, y, r);
          hot += circle(x - r * 0.15, y - r * 0.15, r * 0.42);
        } else if (Math.hypot(j - 3, i - 3) <= 3.2) dim += circle(x, y, r);
      }
    });
  });
  return { lit, hot, dim };
}

function gearShapes(design: BotDesign) {
  const { hw, hh } = BOXES[design.head];
  const top = (x: number) =>
    design.head === "orb" ? CY - Math.sqrt(Math.max(0, hw * hw - (x - CX) ** 2)) : CY - hh;
  const none = { stems: "", balls: "", ballsHot: "", ear: "", earLight: "" };
  const stem = (x0: number, x1: number, lift: number) => {
    const y0 = top(x0) + 2;
    const y1 = y0 - lift;
    return {
      d: line([x0, y0], [x1, y1]),
      ball: circle(x1, y1, 2.4),
      hot: circle(x1 - 0.7, y1 - 0.7, 0.9),
    };
  };
  if (design.gear === "antenna") {
    const one = stem(CX - 0.2 * hw, CX - 0.3 * hw, 10);
    return { ...none, stems: one.d, balls: one.ball, ballsHot: one.hot };
  }
  if (design.gear === "twin") {
    const left = stem(CX - 0.42 * hw, CX - 0.62 * hw, 9);
    const right = stem(CX + 0.18 * hw, CX + 0.32 * hw, 10);
    return {
      ...none,
      stems: left.d + right.d,
      balls: left.ball + right.ball,
      ballsHot: left.hot + right.hot,
    };
  }
  if (design.gear === "ear") {
    const x = CX - hw + 0.4;
    return {
      ...none,
      ear: `M${round(x)} ${round(CY - 6.5)}a3.4 6.5 0 1 0 0.01 0Z`,
      earLight: `M${round(x - 2.2)} ${round(CY - 4.2)}A3.4 6.5 0 0 0 ${round(x - 2.2)} ${round(CY + 4.2)}`,
    };
  }
  return none;
}

export function buildGeometry(design: BotDesign): BotGeometry {
  const random = seeded(design.grain);
  const { hw, hh } = BOXES[design.head];
  const tiles: Paths = {};
  const glints: Paths = {};
  const loose: Paths = {};
  let looseLight = "";
  let looseCount = 0;
  // Flat tiles are bigger and catch the light as one face, so they sparkle less than the orb's.
  const flat = design.head !== "orb";
  for (const tile of flat ? gridTiles(design.head) : orbTiles()) {
    const diffuse = Math.max(0, dot(tile.normal, LIGHT));
    const shine = Math.max(0, dot(tile.normal, HALFWAY)) ** 28;
    let level = 0.035 + 0.24 * diffuse ** 1.4 + (random() - 0.5) * 0.07;
    // Bright tiles here and there, like the mark's mosaic catching the light.
    if (random() < (flat ? 0.05 : 0.09)) level += (0.22 + 0.42 * diffuse) * (flat ? 0.7 : 1);
    level += (flat ? 0.45 : 0.7) * shine;
    // The right side dissolves: a tile there may leave the head and drift off as a loose pixel.
    const side = (tile.x - CX) / hw;
    if (side > 0.5 && random() < ((side - 0.5) / 0.5) * 0.7) {
      const size = tile.size * (0.45 + 0.4 * random());
      const x = tile.x + 2 + random() * 12;
      const y = tile.y + (random() - 0.5) * 7 - size / 2;
      if (looseCount < 24 && x + size < 99) {
        add(loose, looseGrey(level * 1.1 + 0.05), square(x, y, size));
        looseLight += rect(x, y, size, size * 0.26);
        looseCount++;
      }
      continue;
    }
    const [first, ...rest] = tile.outline;
    if (!first) continue;
    add(
      tiles,
      tileOpacity(level),
      `M${round(first[0])} ${round(first[1])}${rest.map(([x, y]) => `L${round(x)} ${round(y)}`).join("")}Z`,
    );
    const glint = glintOpacity(0.06 + 0.5 * diffuse + 0.9 * shine);
    if (glint) add(glints, glint, topEdge(tile.outline));
  }

  const visor = visorShape(design);
  const eyes = eyeDots(design, visor.eyes, visor.pitch);

  // The emblem sits on the dissolving edge, built from small lit cubes.
  const ex = CX + hw + 3;
  const ey = CY - 1;
  const pixel = 2.6;
  const cube = pixel * 0.86;
  let emblem = "";
  let emblemLight = "";
  let emblemShade = "";
  EMBLEMS[design.emblem].forEach((row, i) => {
    for (let j = 0; j < 7; j++) {
      if (row[j] !== "#") continue;
      const parts = voxel(ex + (j - 3) * pixel - cube / 2, ey + (i - 3) * pixel - cube / 2, cube);
      emblem += parts.face;
      emblemLight += parts.light;
      emblemShade += parts.shade;
    }
  });
  const sparks: { d: string; opacity: number }[] = [];
  for (let k = 0; k < 6; k++) {
    const s = 1.1 + random() * 0.9;
    const x = ex + (random() - 0.5) * 26;
    const y = ey + (random() - 0.5) * 30;
    if (Math.abs(x - ex) < 9.5 && Math.abs(y - ey) < 9.5) continue;
    sparks.push({ d: square(Math.min(x, 98 - s), y, s), opacity: round(0.5 + random() * 0.5) });
  }

  return {
    design,
    led: LED[design.hue],
    head:
      design.head === "orb"
        ? circle(CX, CY, hw)
        : roundedRect(CX - hw, CY - hh, hw * 2, hh * 2, BOXES[design.head].r),
    tiles,
    glints,
    loose,
    looseLight,
    visor: visor.paths,
    frame: visor.frame,
    streak: visor.streak,
    gloss: visor.gloss,
    eyesLit: eyes.lit,
    eyesHot: eyes.hot,
    eyesDim: eyes.dim,
    emblem,
    emblemLight,
    emblemShade,
    sparks,
    gear: gearShapes(design),
    shine: { cx: CX - 0.4 * hw, cy: CY - 0.48 * hh, rx: 0.42 * hw, ry: 0.24 * hh },
    glint: { cx: CX - 0.5 * hw, cy: CY - 0.6 * hh, rx: 0.13 * hw, ry: 0.07 * hh },
    bounce: { cx: CX + 0.4 * hw, cy: CY + 0.66 * hh, rx: 0.4 * hw, ry: 0.13 * hh },
  };
}
