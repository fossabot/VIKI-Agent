/**
 * Pins `tokens.css`'s own values against its source text (same technique as every other
 * `*.css.unit.test.ts` in this app, `../testing/css`): jsdom applies no CSS from stylesheets.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../testing/css.ts
import { readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { SESSION_COLORS } from "../clients/conversations/sessionMetadataApi";
import { BOT_HUES } from "../components/Bots/botIconDesign";
import { readCss } from "../testing/css";

const css = readCss(import.meta.url, "tokens.css");

/** The brace-balanced body of the first `{ ... }` block whose text contains `marker`, searched
 * from `from`, and the index just past its closing brace. One parser here rather than one per
 * test: `marker` alone cannot tell the top-level `[data-theme="dark"]` block from the same
 * selector text inside `@media (prefers-contrast: more)`, so callers resume from the previous
 * block's `end` to reach the second occurrence. */
function findBlock(source: string, marker: string, from = 0): { body: string; end: number } {
  const markerAt = source.indexOf(marker, from);
  if (markerAt < 0) throw new Error(`no "${marker}" block in tokens.css`);
  let depth = 0;
  for (let i = source.indexOf("{", markerAt); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) {
      return { body: source.slice(markerAt, i + 1), end: i + 1 };
    }
  }
  throw new Error(`unbalanced "${marker}" block in tokens.css`);
}

/** Every `--token: value;` declaration in a block's body, as a `Map<token, value>`. */
function tokensOf(body: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const m of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) {
    map.set(m[1] as string, (m[2] as string).trim());
  }
  return map;
}

const darkBlock = findBlock(css, '[data-theme="dark"] {');
const lightBlock = findBlock(css, '[data-theme="light"] {');
const darkContrast = findBlock(css, '[data-theme="dark"] {', darkBlock.end);
const lightContrast = findBlock(css, '[data-theme="light"] {', lightBlock.end);

const dark = tokensOf(darkBlock.body);
const light = tokensOf(lightBlock.body);
const darkMore = tokensOf(darkContrast.body);
const lightMore = tokensOf(lightContrast.body);

test("the dark role tokens use neutral surfaces and Ghost White accents", () => {
  const replaced: Record<string, string> = {
    "--on-or": "#000000",
    "--or-rgb": "248 248 255",
    "--fill-tool": "#0f0f0f",
    "--fill-error": "#121212",
    "--fill-pill": "#131313",
    "--fill-popover": "#141414",
    "--fill-toast": "#151515",
    "--fill-raised": "#1a1a1a",
    "--hair-header": "#1a1a1a",
    "--fill-hi": "#1d1d1d",
    "--b-pop": "#2e2e2e",
    "--b-strong": "#333333",
    "--b-alt": "#3a3a3a",
    "--t-head": "#a3a3a3",
    "--scrim": "rgb(0 0 0 / 50%)",
    "--scrim-image": "rgb(0 0 0 / 75%)",
    "--shadow-pill": "rgb(0 0 0 / 50%)",
    "--shadow": "rgb(0 0 0 / 55%)",
    "--shadow-toast": "rgb(0 0 0 / 60%)",
    "--face-dim": "#4a4a4a",
    "--accent-orange": "var(--or)",
    "--accent-red": "var(--err)",
    "--accent-slate": "#a3a3a3",
    "--accent-blue": "#b5b5b5",
    "--accent-green": "#c7c7c7",
    "--accent-purple": "#d9d9d9",
  };
  for (const [token, value] of Object.entries(replaced)) {
    expect(dark.get(token), token).toBe(value);
  }
});

test("the dark surfaces keep their layout palette and actions use Ghost White", () => {
  expect(dark.get("--g0")).toBe("#0b0b0b");
  expect(dark.get("--g1")).toBe("#0d0d0d");
  expect(dark.get("--or")).toBe("#f8f8ff");
});

test("dark secondary and error text use contrasting neutral tones", () => {
  expect(dark.get("--t3")).toBe("#a3a3a3");
  expect(dark.get("--err")).toBe("#ededed");
});

test("all seventeen base dark tokens hold their values", () => {
  const base: Record<string, string> = {
    "--g0": "#0b0b0b",
    "--g1": "#0d0d0d",
    "--g2": "#111111",
    "--g3": "#161616",
    "--line": "#262626",
    "--hair": "#1c1c1c",
    "--t0": "#ededed",
    "--t1": "#d4d4d4",
    "--t2": "#b3b3b3",
    "--t3": "#a3a3a3",
    "--or": "#f8f8ff",
    "--or-hi": "#ffffff",
    "--s0": "#111111",
    "--s1": "#161616",
    "--b1": "#222222",
    "--b2": "#2a2a2a",
    "--err": "#ededed",
  };
  expect(Object.keys(base)).toHaveLength(17);
  for (const [token, value] of Object.entries(base)) {
    expect(dark.get(token), token).toBe(value);
  }
});

test("the light block defines exactly the dark block's colour token set", () => {
  expect(new Set(light.keys())).toEqual(new Set(dark.keys()));
});

test("the more-contrast blocks define the same token set, in both themes", () => {
  expect(new Set(lightMore.keys())).toEqual(new Set(darkMore.keys()));
});

test("prefers-contrast: more overrides the named text and border tokens, in both themes", () => {
  for (const token of [
    "--t2",
    "--t3",
    "--face-dim",
    "--t-head",
    "--line",
    "--hair",
    "--b1",
    "--b2",
    "--hair-header",
    "--b-pop",
    "--b-strong",
    "--b-alt",
  ]) {
    expect(darkMore.has(token), `dark more-contrast ${token}`).toBe(true);
    expect(lightMore.has(token), `light more-contrast ${token}`).toBe(true);
  }
});

// --- WCAG 2 contrast, computed from the parsed blocks above: no library, so a check that fails
// names exactly which resolved value could not be read as a colour. ---

type Theme = "dark" | "light";

const THEMES: readonly Theme[] = ["dark", "light"];

const BASE: Record<Theme, Map<string, string>> = { dark, light };
const MORE: Record<Theme, Map<string, string>> = { dark: darkMore, light: lightMore };

/** A token's value in a theme: the theme block's own, else (under "more") the base theme's. A
 * `var(--x)` reference resolves recursively in the same theme (and the same "more"-ness), so
 * `--accent-orange: var(--or)` reaches `--or`'s own value. */
function resolveToken(theme: Theme, token: string, more: boolean): string {
  const base = BASE[theme];
  const override = more ? MORE[theme] : undefined;
  const raw = override?.get(token) ?? base.get(token);
  if (raw === undefined) throw new Error(`no ${token} in the ${theme} block`);
  const ref = /^var\((--[\w-]+)\)$/.exec(raw);
  return ref ? resolveToken(theme, ref[1] as string, more) : raw;
}

/** `#rrggbb` to channels, or a throw naming the theme, token and the unresolved form found. */
function toRgb(theme: Theme, token: string, more: boolean): [number, number, number] {
  const value = resolveToken(theme, token, more);
  const hex = /^#([0-9a-fA-F]{6})$/.exec(value);
  if (!hex) {
    throw new Error(
      `${theme}${more ? " (more contrast)" : ""} ${token} resolves to "${value}", not #rrggbb`,
    );
  }
  const n = Number.parseInt(hex[1] as string, 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** WCAG 2 relative luminance (the sRGB channel transform, then the 0.2126/0.7152/0.0722 mix). */
function relativeLuminance([r, g, b]: readonly [number, number, number]): number {
  const channel = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG 2 contrast ratio between two resolved tokens, and asserts it against `min`, with a
 * message naming the theme, the token, the surface and the ratio to two decimals. */
function assertContrast(
  theme: Theme,
  token: string,
  surface: string,
  min: number,
  more = false,
): void {
  const lToken = relativeLuminance(toRgb(theme, token, more));
  const lSurface = relativeLuminance(toRgb(theme, surface, more));
  const [hi, lo] = lToken >= lSurface ? [lToken, lSurface] : [lSurface, lToken];
  const ratio = (hi + 0.05) / (lo + 0.05);
  const label = `${theme}${more ? " more-contrast" : ""} ${token} on ${surface}`;
  expect(ratio, `${label}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(min);
}

const SURFACES = [
  "--g0",
  "--g1",
  "--g2",
  "--g3",
  "--s0",
  "--s1",
  "--fill-tool",
  "--fill-error",
  "--fill-pill",
  "--fill-popover",
  "--fill-toast",
  "--fill-raised",
  "--fill-hi",
];

const TEXT_TOKENS = ["--t0", "--t1", "--t2", "--t3", "--t-head", "--or", "--or-hi", "--err"];

const ACCENT_TOKENS = [
  "--accent-orange",
  "--accent-red",
  "--accent-slate",
  "--accent-blue",
  "--accent-green",
  "--accent-purple",
];

const BORDER_TOKENS = [
  "--line",
  "--hair",
  "--b1",
  "--b2",
  "--hair-header",
  "--b-pop",
  "--b-strong",
  "--b-alt",
];

/** Decorative pairs the design draws at 1.0-2.2:1 (the eight borders 1.0-1.9, --face-dim up to
 * 2.2): not checked against the WCAG minimums in the default themes, because the OS's own
 * "more contrast" preference already raises them (the `@media (prefers-contrast: more)` block,
 * pinned above). */
const EXEMPT_DEFAULT_THEME = [...BORDER_TOKENS, "--face-dim"];

test("the palette uses Ghost White accents and grayscale neutrals in both themes and contrast preferences", () => {
  for (const theme of THEMES) {
    for (const more of [false, true]) {
      for (const token of BASE[theme].keys()) {
        if (token.startsWith("--label-")) continue;
        const value = resolveToken(theme, token, more);
        if (!value.startsWith("#")) continue;
        const [r, g, b] = toRgb(theme, token, more);
        if (theme === "dark" && (token === "--or" || token === "--accent-orange")) {
          expect([r, g, b], `${theme} ${token}`).toEqual([248, 248, 255]);
          continue;
        }
        expect(r, `${theme} ${token}`).toBe(g);
        expect(r, `${theme} ${token}`).toBe(b);
      }
    }
  }
});

test("text tokens clear 4.5:1 on every text-bearing surface, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of TEXT_TOKENS) {
      for (const surface of SURFACES) {
        assertContrast(theme, token, surface, 4.5);
      }
    }
  }
});

test("--or clears the 3:1 indicator minimum on every surface, in both themes", () => {
  for (const theme of THEMES) {
    for (const surface of SURFACES) {
      assertContrast(theme, "--or", surface, 3);
    }
  }
});

test("each accent clears 4.5:1 on --s1, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of ACCENT_TOKENS) {
      assertContrast(theme, token, "--s1", 4.5);
    }
  }
});

test("--on-or clears 4.5:1 on --or, in both themes", () => {
  for (const theme of THEMES) {
    assertContrast(theme, "--on-or", "--or", 4.5);
  }
});

test("--or-rgb matches --or's own parsed channels, in both themes", () => {
  for (const theme of THEMES) {
    const [r, g, b] = toRgb(theme, "--or", false);
    expect(BASE[theme].get("--or-rgb"), theme).toBe(`${r} ${g} ${b}`);
  }
});

test("under prefers-contrast: more, --t0 to --t-head clear 7:1 on every surface, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of ["--t0", "--t1", "--t2", "--t3", "--t-head"]) {
      for (const surface of SURFACES) {
        assertContrast(theme, token, surface, 7, true);
      }
    }
  }
});

test("prefers-contrast: more raises the border tokens to 3:1 on every surface, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of BORDER_TOKENS) {
      for (const surface of SURFACES) {
        assertContrast(theme, token, surface, 3, true);
      }
    }
  }
});

test("--or stays at the 3:1 indicator minimum under prefers-contrast: more, in both themes", () => {
  for (const theme of THEMES) {
    for (const surface of SURFACES) {
      assertContrast(theme, "--or", surface, 3, true);
    }
  }
});

test("the exempt decorative tokens are not among the tokens checked in the default themes", () => {
  const checked = new Set([...TEXT_TOKENS, ...ACCENT_TOKENS, "--on-or"]);
  for (const token of EXEMPT_DEFAULT_THEME) {
    expect(checked.has(token), token).toBe(false);
  }
});

test("the context meter's fill and the reconnect spinner's arc (--or) clear 3:1 on --track, in both themes and under prefers-contrast: more", () => {
  for (const theme of THEMES) {
    assertContrast(theme, "--or", "--track", 3);
    assertContrast(theme, "--or", "--track", 3, true);
  }
});

// --- Session color labels (--label-*): one chromatic token per SESSION_COLORS key. ---

const LABEL_TOKENS = SESSION_COLORS.map((key) => `--label-${key}`);

/** sRGB (0-255 channels) to CIE Lab (D65 white), for CIE76 ΔE below. */
function toLab([r, g, b]: readonly [number, number, number]): [number, number, number] {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const [rl, gl, bl] = [linear(r), linear(g), linear(b)];
  const x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375;
  const y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.072175;
  const z = rl * 0.0193339 + gl * 0.119192 + bl * 0.9503041;
  const [xn, yn, zn] = [0.95047, 1, 1.08883];
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (841 / 108) * t + 4 / 29);
  const [fx, fy, fz] = [f(x / xn), f(y / yn), f(z / zn)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

/** CIE76 ΔE: Euclidean distance between two Lab colours. */
function deltaE76(
  a: readonly [number, number, number],
  b: readonly [number, number, number],
): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

test("the --label-* token names are exactly the SESSION_COLORS keys, in both themes", () => {
  const expected = [...SESSION_COLORS].sort();
  for (const theme of THEMES) {
    const actual = [...BASE[theme].keys()]
      .filter((token) => token.startsWith("--label-"))
      .map((token) => token.slice("--label-".length))
      .sort();
    expect(actual, theme).toEqual(expected);
  }
});

test("each --label-* is a #rrggbb hex with a chromatic spread of at least 64, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of LABEL_TOKENS) {
      const value = BASE[theme].get(token);
      expect(value, `${theme} ${token}`).toMatch(/^#[0-9a-f]{6}$/i);
      const [r, g, b] = toRgb(theme, token, false);
      const spread = Math.max(r, g, b) - Math.min(r, g, b);
      expect(spread, `${theme} ${token} spread`).toBeGreaterThanOrEqual(64);
    }
  }
});

test("each --label-* clears 3:1 against every SURFACES token, in both themes", () => {
  for (const theme of THEMES) {
    for (const token of LABEL_TOKENS) {
      for (const surface of SURFACES) {
        assertContrast(theme, token, surface, 3);
      }
    }
  }
});

test("Decision data palette maps its eight roles to label tokens and keeps each pair distinct", () => {
  const block = tokensOf(findBlock(css, ".dloop-data {").body);
  expect([...block.keys()].sort()).toEqual(
    [
      "--dloop-held",
      "--dloop-listed",
      "--dloop-attention",
      "--dloop-reports",
      "--dloop-useful",
      "--dloop-wrong",
      "--dloop-later",
      "--dloop-stopped",
    ].sort(),
  );
  expect(block).toEqual(
    new Map([
      ["--dloop-held", "var(--label-slate)"],
      ["--dloop-listed", "var(--label-blue)"],
      ["--dloop-attention", "var(--label-orange)"],
      ["--dloop-reports", "var(--label-purple)"],
      ["--dloop-useful", "var(--label-green)"],
      ["--dloop-wrong", "var(--label-red)"],
      ["--dloop-later", "var(--label-blue)"],
      ["--dloop-stopped", "var(--label-purple)"],
    ]),
  );

  for (const [name, token] of block) {
    expect(token, name).toMatch(/^var\(--label-[a-z]+\)$/);
    for (const theme of THEMES) {
      for (const surface of SURFACES) assertContrast(theme, token.slice(4, -1), surface, 3);
    }
  }
  expect(
    new Set([
      block.get("--dloop-held"),
      block.get("--dloop-listed"),
      block.get("--dloop-attention"),
      block.get("--dloop-reports"),
    ]).size,
  ).toBe(4);
  expect(
    new Set([
      block.get("--dloop-useful"),
      block.get("--dloop-wrong"),
      block.get("--dloop-later"),
      block.get("--dloop-stopped"),
    ]).size,
  ).toBe(4);
});

test("the --label-* colours are pairwise distinct: CIE76 ΔE of at least 25, in both themes", () => {
  for (const theme of THEMES) {
    const labs = LABEL_TOKENS.map((token) => toLab(toRgb(theme, token, false)));
    for (let i = 0; i < labs.length; i++) {
      for (let j = i + 1; j < labs.length; j++) {
        const dE = deltaE76(
          labs[i] as [number, number, number],
          labs[j] as [number, number, number],
        );
        expect(
          dE,
          `${theme} ${LABEL_TOKENS[i]} vs ${LABEL_TOKENS[j]}: ${dE.toFixed(1)}`,
        ).toBeGreaterThanOrEqual(25);
      }
    }
  }
});

// --- Bot icon hues (botIconDesign.ts BOT_HUES, botHues.ts): the two assigned-only hues' own
// colours, resolved through the bot-icon-scoped blocks below (tokens.css) and checked with the
// same properties as the --label-* tokens above. ---

const botHueDefault = tokensOf(findBlock(css, "[data-bot-hue] {").body);
const botHueLight = tokensOf(findBlock(css, '[data-theme="light"] [data-bot-hue] {').body);

/** `--bot-hue`'s resolved colour for `hue` in `theme`: a `--label-*` reference resolves through
 * the base theme map above; `--bot-amber`/`--bot-pink` resolve through the bot-icon-scoped pair
 * parsed above, which (like `.bot-icon` itself) default to the dark pair and take the light pair
 * only under a real `[data-theme="light"]` ancestor. */
function resolveBotHue(theme: Theme, hue: string): string {
  const raw = tokensOf(findBlock(css, `[data-bot-hue="${hue}"] {`).body).get("--bot-hue");
  if (raw === undefined) throw new Error(`no --bot-hue in the ${hue} block`);
  const ref = /^var\((--[\w-]+)\)$/.exec(raw);
  if (!ref) throw new Error(`[data-bot-hue="${hue}"] --bot-hue is "${raw}", not var(...)`);
  const name = ref[1] as string;
  if (name === "--bot-amber" || name === "--bot-pink") {
    const value = (theme === "light" ? botHueLight : botHueDefault).get(name);
    if (value === undefined) throw new Error(`no ${name} for ${theme}`);
    return value;
  }
  return resolveToken(theme, name, false);
}

/** `resolveBotHue`'s value as parsed channels, or a throw naming the theme, hue and the
 * unresolved form found (same contract as `toRgb` above). */
function botHueRgb(theme: Theme, hue: string): [number, number, number] {
  const value = resolveBotHue(theme, hue);
  const hex = /^#([0-9a-fA-F]{6})$/.exec(value);
  if (!hex) {
    throw new Error(`${theme} [data-bot-hue="${hue}"] resolves to "${value}", not #rrggbb`);
  }
  const n = Number.parseInt(hex[1] as string, 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

test("the [data-bot-hue] rules cover exactly BOT_HUES", () => {
  const found = [...css.matchAll(/\[data-bot-hue="([a-z]+)"\]/g)].map((m) => m[1] as string);
  expect(found.sort()).toEqual([...BOT_HUES].sort());
});

test("each resolved bot hue is a #rrggbb hex with a chromatic spread of at least 64, in both themes", () => {
  for (const theme of THEMES) {
    for (const hue of BOT_HUES) {
      const value = resolveBotHue(theme, hue);
      expect(value, `${theme} ${hue}`).toMatch(/^#[0-9a-f]{6}$/i);
      const [r, g, b] = botHueRgb(theme, hue);
      const spread = Math.max(r, g, b) - Math.min(r, g, b);
      expect(spread, `${theme} ${hue} spread`).toBeGreaterThanOrEqual(64);
    }
  }
});

test("each resolved bot hue clears 3:1 against every SURFACES token, in both themes", () => {
  for (const theme of THEMES) {
    for (const hue of BOT_HUES) {
      const lHue = relativeLuminance(botHueRgb(theme, hue));
      for (const surface of SURFACES) {
        const lSurface = relativeLuminance(toRgb(theme, surface, false));
        const [hi, lo] = lHue >= lSurface ? [lHue, lSurface] : [lSurface, lHue];
        const ratio = (hi + 0.05) / (lo + 0.05);
        expect(
          ratio,
          `${theme} [data-bot-hue="${hue}"] on ${surface}: ${ratio.toFixed(2)}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  }
});

test("the resolved bot hues are pairwise distinct: CIE76 ΔE of at least 25, in both themes", () => {
  for (const theme of THEMES) {
    const labs = BOT_HUES.map((hue) => toLab(botHueRgb(theme, hue)));
    for (let i = 0; i < labs.length; i++) {
      for (let j = i + 1; j < labs.length; j++) {
        const dE = deltaE76(
          labs[i] as [number, number, number],
          labs[j] as [number, number, number],
        );
        expect(
          dE,
          `${theme} ${BOT_HUES[i]} vs ${BOT_HUES[j]}: ${dE.toFixed(1)}`,
        ).toBeGreaterThanOrEqual(25);
      }
    }
  }
});

/** The text and the accent wash it sits on, as the stylesheets declare them: `.chip` (base.css,
 * --or-hi on --or at 10%), `.card` (Cards.css, --t0 to --t3 and --or on --or at 9%, its
 * gradient's stronger end), `.tile-hot` (RightRail.css, --or-hi on --or at 6%) and the vault
 * banner (VaultBanner.css, --t1 on --or at 7%). The wash is composited over each light surface a
 * panel can sit on. */
const WASHES: readonly { name: string; alpha: number; text: readonly string[] }[] = [
  { name: ".chip", alpha: 0.1, text: ["--or-hi"] },
  { name: ".card", alpha: 0.09, text: ["--t0", "--t1", "--t2", "--t3", "--or", "--or-hi"] },
  { name: ".tile-hot", alpha: 0.06, text: ["--or-hi"] },
  { name: "vault banner", alpha: 0.07, text: ["--t1"] },
];

test("light-theme text clears 4.5:1 on each accent wash over every light surface", () => {
  const or = toRgb("light", "--or", false);
  for (const surface of SURFACES) {
    const under = toRgb("light", surface, false);
    for (const { name, alpha, text } of WASHES) {
      const wash = or.map((c, i) => Math.round(c * alpha + (under[i] as number) * (1 - alpha)));
      const lWash = relativeLuminance(wash as [number, number, number]);
      for (const token of text) {
        const lText = relativeLuminance(toRgb("light", token, false));
        const [hi, lo] = lText >= lWash ? [lText, lWash] : [lWash, lText];
        const ratio = (hi + 0.05) / (lo + 0.05);
        expect(
          ratio,
          `light ${token} on the ${name} wash over ${surface}: ${ratio.toFixed(2)}`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  }
});

// --- Kanban status lanes (.kanban in tokens.css): each status is one session label hue. ---

const KANBAN_STATUSES = ["todo", "working", "blocked", "review", "done"];
const kanban = tokensOf(findBlock(css, ".kanban {").body);

/** The label token a status points at (the `.kanban` block is not a theme block). */
function kanbanToken(status: string): string {
  return /^var\((--label-[\w-]+)\)$/.exec(kanban.get(`--kanban-${status}`) ?? "")?.[1] as string;
}

/** `--kanban-<status>`'s colour at 7% over `surface`, as `.kanban-count` washes it. */
function kanbanWash(theme: Theme, status: string, surface: string): [number, number, number] {
  const c = toRgb(theme, kanbanToken(status), false);
  const s = toRgb(theme, surface, false);
  return c.map((v, i) => Math.round(v * 0.07 + (s[i] as number) * 0.93)) as [
    number,
    number,
    number,
  ];
}

function ratioOf(a: readonly [number, number, number], b: readonly [number, number, number]) {
  const [la, lb] = [relativeLuminance(a), relativeLuminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

test(".kanban holds exactly five distinct --label-* references, one per status", () => {
  expect([...kanban.keys()]).toEqual(KANBAN_STATUSES.map((status) => `--kanban-${status}`));
  const values = [...kanban.values()];
  for (const value of values) {
    const key = /^var\((--label-[\w-]+)\)$/.exec(value)?.[1];
    expect(LABEL_TOKENS, value).toContain(key);
  }
  expect(new Set(values).size).toBe(5);
});

test("each kanban colour clears 3:1 on every surface and on its own 7% wash, in both themes", () => {
  for (const theme of THEMES) {
    for (const status of KANBAN_STATUSES) {
      const colour = toRgb(theme, kanbanToken(status), false);
      for (const surface of SURFACES) {
        assertContrast(theme, kanbanToken(status), surface, 3);
        const ratio = ratioOf(colour, kanbanWash(theme, status, surface));
        expect(
          ratio,
          `${theme} ${status} on its wash over ${surface}: ${ratio.toFixed(2)}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  }
});

test("--t0 and --t1 clear 4.5:1 on each kanban 7% wash over every surface, in both themes", () => {
  for (const theme of THEMES) {
    for (const status of KANBAN_STATUSES) {
      for (const surface of SURFACES) {
        const wash = kanbanWash(theme, status, surface);
        for (const token of ["--t0", "--t1"]) {
          const ratio = ratioOf(toRgb(theme, token, false), wash);
          expect(
            ratio,
            `${theme} ${token} on the ${status} wash over ${surface}: ${ratio.toFixed(2)}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  }
});

const HEX_LITERAL = /#[0-9a-fA-F]{3,8}\b/g;
/** A colour function opening, in any case: a number, a sign or a dot (`hsl(-30 ...)`), or the
 * `from` of a relative colour; `color(` only when a colour space follows. */
const FUNC_LITERAL = new RegExp(
  "\\b(?:(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\\(\\s*(?:[-+.\\d]|from\\b)|" +
    "color\\(\\s*(?:from|srgb|display-p3|a98-rgb|prophoto-rgb|rec2020|xyz)\\b)",
  "gi",
);
const NAMED_COLOURS = [
  "aliceblue,antiquewhite,aqua,aquamarine,azure,beige,bisque,black,blanchedalmond,blue",
  "blueviolet,brown,burlywood,cadetblue,chartreuse,chocolate,coral,cornflowerblue,cornsilk",
  "crimson,cyan,darkblue,darkcyan,darkgoldenrod,darkgray,darkgreen,darkgrey,darkkhaki",
  "darkmagenta,darkolivegreen,darkorange,darkorchid,darkred,darksalmon,darkseagreen",
  "darkslateblue,darkslategray,darkslategrey,darkturquoise,darkviolet,deeppink,deepskyblue",
  "dimgray,dimgrey,dodgerblue,firebrick,floralwhite,forestgreen,fuchsia,gainsboro,ghostwhite",
  "gold,goldenrod,gray,green,greenyellow,grey,honeydew,hotpink,indianred,indigo,ivory,khaki",
  "lavender,lavenderblush,lawngreen,lemonchiffon,lightblue,lightcoral,lightcyan",
  "lightgoldenrodyellow,lightgray,lightgreen,lightgrey,lightpink,lightsalmon,lightseagreen",
  "lightskyblue,lightslategray,lightslategrey,lightsteelblue,lightyellow,lime,limegreen,linen",
  "magenta,maroon,mediumaquamarine,mediumblue,mediumorchid,mediumpurple,mediumseagreen",
  "mediumslateblue,mediumspringgreen,mediumturquoise,mediumvioletred,midnightblue,mintcream",
  "mistyrose,moccasin,navajowhite,navy,oldlace,olive,olivedrab,orange,orangered,orchid",
  "palegoldenrod,palegreen,paleturquoise,palevioletred,papayawhip,peachpuff,peru,pink,plum",
  "powderblue,purple,rebeccapurple,red,rosybrown,royalblue,saddlebrown,salmon,sandybrown",
  "seagreen,seashell,sienna,silver,skyblue,slateblue,slategray,slategrey,snow,springgreen",
  "steelblue,tan,teal,thistle,tomato,turquoise,violet,wheat,white,whitesmoke,yellow",
  "yellowgreen",
]
  .join(",")
  .replaceAll(",", "|");
/** A named colour as the value of a colour property: a CSS declaration, or a quoted attribute or
 * style value. Read only after a colour property, so a font or class name that happens to be a
 * colour word is not a hit. */
const COLOUR_PROPS =
  "color|background(?:-?color)?|border(?:-?\\w+)?|outline(?:-?color)?|fill|stroke|" +
  "caret-?color|accent-?color|(?:box|text)-?shadow";
const NAMED_LITERAL = new RegExp(
  `\\b(?:${COLOUR_PROPS})\\s*[:=]\\s*\\{?\\s*(?:["'\`]\\s*)?[^;"'\`}]*?\\b(?:${NAMED_COLOURS})\\b`,
  "gi",
);

/** Blanks out comment text while keeping every newline, so line numbers reported below still
 * match the source file. String literals are copied through unchanged: a `//` or `/*` inside one
 * is text, not a comment, and a colour inside one is still a colour. `//` is a comment only in
 * TypeScript (CSS has none, and a `url(...)` may hold one). An unterminated `'` or `"` ends at
 * its line, so an apostrophe in JSX text cannot swallow the rest of the file. */
function blankComments(source: string, stripLineComments: boolean): string {
  const blank = (text: string) => text.replace(/[^\n]/g, " ");
  let out = "";
  let i = 0;
  while (i < source.length) {
    const c = source[i] as string;
    const two = source.slice(i, i + 2);
    if (two === "/*" || (stripLineComments && two === "//")) {
      const close = two === "/*" ? source.indexOf("*/", i + 2) : source.indexOf("\n", i);
      const stop = close < 0 ? source.length : close + (two === "/*" ? 2 : 0);
      out += blank(source.slice(i, stop));
      i = stop;
    } else if (c === '"' || c === "'" || c === "`") {
      let j = i + 1;
      while (j < source.length && source[j] !== c && (c === "`" || source[j] !== "\n")) {
        j += source[j] === "\\" ? 2 : 1;
      }
      out += source.slice(i, j + 1);
      i = j + 1;
    } else {
      out += c;
      i++;
    }
  }
  return out;
}

/** The colour literals left in one source file's text, as `"line: match"` strings: `#hex`, a
 * colour function, a named colour as a value. `.ts` and `.tsx` carry `//` comments; `.css` and
 * `.svg` do not. */
function literalsIn(rel: string, raw: string): string[] {
  const text = blankComments(raw, /\.tsx?$/.test(rel));
  return findLiterals(text);
}

function findLiterals(text: string): string[] {
  const hits: string[] = [];
  text.split("\n").forEach((line, i) => {
    for (const re of [HEX_LITERAL, FUNC_LITERAL, NAMED_LITERAL]) {
      re.lastIndex = 0;
      let m: RegExpExecArray | null = re.exec(line);
      while (m !== null) {
        hits.push(`${i + 1}: ${m[0]}`);
        m = re.exec(line);
      }
    }
  });
  return hits;
}

// `here` must be a variable, not `import.meta.url` written inline in the `new URL(...)` call
// below: Vite statically rewrites that exact inline pattern into a dev-server URL for its own
// asset-import feature (../testing/css.ts's doc comment), which breaks `fileURLToPath` under
// Vitest.
const here = import.meta.url;
const srcDir = fileURLToPath(new URL("../", here)) as string;

/** Every `.css` (but `styles/tokens.css`), every `.svg`, and every `.ts` or `.tsx` whose name
 * holds no `.test.`, as paths relative to `frontend/src` with `/` separators. */
function scannedFiles(): string[] {
  const entries = readdirSync(srcDir, { recursive: true }) as string[];
  return entries
    .map((rel) => rel.replace(/\\/g, "/"))
    .filter((rel) => {
      if (rel.endsWith(".css")) return rel !== "styles/tokens.css";
      if (/\.(?:svg|tsx?)$/.test(rel)) return !(rel.split("/").pop() ?? "").includes(".test.");
      return false;
    });
}

test("no color literal (hex, function, named value) remains outside tokens.css", () => {
  const failures: string[] = [];
  for (const rel of scannedFiles()) {
    const raw = readFileSync(srcDir + rel, "utf-8") as string;
    for (const hit of literalsIn(rel, raw)) {
      failures.push(`${rel}:${hit}`);
    }
  }
  expect(failures).toEqual([]);
});

// Each rule above fails on a planted string, built here: no tracked file holds a literal.
test("the scan flags every colour form it names, in any case", () => {
  const planted = [
    ["a.css", "a { color: #fff; }"],
    ["a.css", "a { color: RGB(0 0 0); }"],
    ["a.css", "a { color: HSLA(0, 0%, 0%, 1); }"],
    ["a.css", "a { color: hsl(-30 50% 50%); }"],
    ["a.css", "a { color: rgb(from var(--x) r g b); }"],
    ["a.css", "a { color: hwb(90 0% 0%); }"],
    ["a.css", "a { color: lab(50% 0 0); }"],
    ["a.css", "a { color: lch(50% 0 0); }"],
    ["a.css", "a { color: oklab(0.5 0 0); }"],
    ["a.css", "a { color: OKLCH(0.5 0 0); }"],
    ["a.css", "a { color: color(display-p3 1 0 0); }"],
    ["a.css", "a { color: red; }"],
    ["a.css", "a { border: 1px solid Tomato; }"],
    ["a.css", "a { background-color: white }"],
    ["a.ts", 'const s = { color: "red" };'],
    ["a.ts", "const c = '#abc';"],
    ["a.svg", '<path fill="blue" />'],
    ["a.svg", '<path fill="#123456" />'],
  ] as const;
  for (const [rel, text] of planted) {
    expect(literalsIn(rel, text), `${rel}: ${text}`).not.toEqual([]);
  }
});

test("the scan covers stylesheets, .ts and .tsx sources, but not tokens.css or tests", () => {
  const files = scannedFiles();
  expect(files).toContain("App.tsx");
  expect(files).toContain("api.ts");
  expect(files).toContain("components/TopBar/TopBar.css");
  expect(files).not.toContain("styles/tokens.css");
  expect(files.filter((rel) => rel.includes(".test."))).toEqual([]);
});

test("the scan reads past a comment marker inside a string, and skips real comments", () => {
  expect(literalsIn("a.ts", 'const a = "//"; const b = "#fff";')).not.toEqual([]);
  expect(literalsIn("a.css", 'a::after { content: "/*"; color: #fff; } /* */')).not.toEqual([]);
  expect(literalsIn("a.css", "/* #fff */ a { color: Highlight; outline: 2px solid; }")).toEqual([]);
  expect(literalsIn("a.ts", "// #fff\nconst a = 1; // rgb(0 0 0)")).toEqual([]);
  expect(literalsIn("a.css", "a { color: var(--t0); background: transparent; }")).toEqual([]);
});
