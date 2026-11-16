/** VIKI's presentation for the pinned Magpie dashboard, without changing its executable. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

export const MAGPIE_FONTS = [
  ["ibm-plex-sans", "IBM Plex Sans", 400],
  ["ibm-plex-sans", "IBM Plex Sans", 500],
  ["ibm-plex-sans", "IBM Plex Sans", 600],
  ["ibm-plex-mono", "IBM Plex Mono", 400],
  ["ibm-plex-mono", "IBM Plex Mono", 500],
];

/** The fallback for older servers which do not send a VIKI view. */
export const DEFAULT_MAGPIE_APPEARANCE = {
  theme: "dark",
  density: "comfortable",
  scale: 1,
  reduce_motion: false,
  locale: "en",
  palette: {
    bg: "#0b0b0b",
    panel: "#111111",
    raised: "#161616",
    text: "#ededed",
    secondary: "#d4d4d4",
    muted: "#b3b3b3",
    faint: "#a3a3a3",
    line: "#262626",
    hair: "#1c1c1c",
    accent: "#f8f8ff",
    onAccent: "#000000",
    popover: "#141414",
  },
};

let fontStyles;

/** Embed only existing local font bytes; the dashboard never requests VIKI's origin. */
export function magpieFonts() {
  if (fontStyles !== undefined) return fontStyles;
  fontStyles = MAGPIE_FONTS.map(([pkg, family, weight]) => {
    const name = `${pkg}-latin-${weight}-normal.woff2`;
    for (const path of [
      join(HERE, "fonts", name),
      join(HERE, "..", "frontend", "node_modules", "@fontsource", pkg, "files", name),
    ]) {
      try {
        const data = readFileSync(path);
        if (data.length > 128 * 1024 || data.subarray(0, 4).toString("ascii") !== "wOF2") break;
        return `@font-face{font-family:"${family}";font-style:normal;font-weight:${weight};font-display:swap;src:url(data:font/woff2;base64,${data.toString("base64")}) format("woff2")}`;
      } catch {
        // The packaged files take precedence; a source checkout uses its existing dependency.
      }
    }
    return "";
  }).join("\n");
  return fontStyles;
}

/** @param {import("./policy.mjs").MagpieAppearance} appearance */
export function magpieStyle(appearance) {
  const p = appearance.palette;
  const tokens = {
    bg: p.bg,
    card: p.panel,
    "card-2": p.raised,
    pill: p.raised,
    "pill-hover": p.line,
    "seg-track": p.bg,
    "seg-thumb": p.raised,
    "seg-edge": `inset 0 0 0 1px ${p.line}`,
    fg: p.text,
    "fg-2": p.secondary,
    muted: p.muted,
    faint: p.faint,
    "ctl-fg": p.muted,
    line: p.line,
    "line-2": p.hair,
    accent: p.accent,
    "accent-soft": p.raised,
    "accent-fg": p.onAccent,
    sel: p.raised,
    "pop-bg": p.popover,
    shadow: `0 12px 36px ${appearance.theme === "dark" ? "#00000088" : "#00000022"}`,
    font: '"IBM Plex Sans", "IBM Plex Sans JP", "Yu Gothic UI", Meiryo, system-ui, sans-serif',
    "ui-font": '"IBM Plex Sans", "IBM Plex Sans JP", "Yu Gothic UI", Meiryo, system-ui, sans-serif',
    code: '"IBM Plex Mono", Consolas, monospace',
    "code-font": '"IBM Plex Mono", Consolas, monospace',
    "logo-invert": appearance.theme === "dark" ? "1" : "0",
  };
  return `${magpieFonts()}
:root, :root body { ${Object.entries(tokens)
    .map(([name, value]) => `--${name}:${value} !important;`)
    .join(" ")} color-scheme:${appearance.theme} !important; }
body { font-size:${appearance.density === "compact" ? 13 : 14}px !important;
  font-family:var(--ui-font) !important; background:${p.bg} !important; color:${p.text} !important; }
.brand { display:none !important; }
.top { border-bottom:1px solid ${p.hair} !important; }
.sidebar { border-right:1px solid ${p.line} !important; }
button:focus-visible, input:focus-visible, a:focus-visible, [tabindex]:focus-visible {
  outline:2px solid ${p.accent} !important; outline-offset:2px !important;
}
${appearance.reduce_motion ? "*, *::before, *::after { animation:none !important; transition:none !important; scroll-behavior:auto !important; }" : ""}`;
}

/** @param {import("./policy.mjs").MagpieAppearance} appearance @param {boolean} failed */
export function magpieHeader(appearance, failed = false) {
  const p = appearance.palette;
  const status = failed
    ? "Magpie could not load. Return to chat models and try again."
    : "Powered by Magpie";
  return `data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:">
<style>${magpieFonts()}
*{box-sizing:border-box}body{margin:0;height:48px;display:flex;align-items:center;gap:16px;padding:0 16px;
font:13px "IBM Plex Sans",system-ui,sans-serif;background:${p.bg};color:${p.text};border-bottom:1px solid ${p.line};color-scheme:${appearance.theme}}
.brand{font:500 12px "IBM Plex Mono",monospace;letter-spacing:.18em;flex:none}
.title{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.credit{margin-left:auto;color:${p.muted};font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
a{flex:none;color:${p.secondary};text-decoration:none;padding:6px 9px;border:1px solid ${p.line};border-radius:6px}
a:hover{background:${p.raised};color:${p.text}}a:focus-visible{outline:2px solid ${p.accent};outline-offset:2px}
@media(max-width:650px){.credit{display:none}.title{flex:1}.brand{display:none}body{gap:10px;padding:0 10px}}
</style><span class="brand">VIKI</span><span class="title">Providers and routing</span>
<span class="credit">${status}</span><a href="https://viki-magpie.invalid/back">← Back to chat models</a></html>`)}`;
}

/**
 * CSS viewport coordinates become native DIPs once; the display's pixel ratio is not involved.
 * @param {import("./policy.mjs").MagpieView | null} view
 * @param {[number, number]} size
 * @param {number} zoom
 */
export function magpieBounds(view, size, zoom) {
  const rect = view?.bounds ?? { x: 0, y: 0, width: size[0] / zoom, height: size[1] / zoom };
  const x = Math.min(size[0], Math.max(0, Math.round(rect.x * zoom)));
  const y = Math.min(size[1], Math.max(0, Math.round(rect.y * zoom)));
  const right = Math.min(size[0], Math.max(x, Math.round((rect.x + rect.width) * zoom)));
  const bottom = Math.min(size[1], Math.max(y, Math.round((rect.y + rect.height) * zoom)));
  const header = Math.min(bottom - y, Math.ceil(48 * zoom * (view?.appearance.scale ?? 1)));
  return {
    header: { x, y, width: right - x, height: header },
    dashboard: { x, y: y + header, width: right - x, height: bottom - y - header },
  };
}
