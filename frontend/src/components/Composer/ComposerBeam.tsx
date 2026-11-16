// Adapted from border-beam 1.3.0 by Jakub Antalik (MIT License; the notice ships in
// public/licenses/libraries-dev.txt). Only the mono "pulse-inner" breathe that the composer shows
// while a reply runs is kept: ComposerBeam.css paints the glow, and this loop eases its registered
// properties at about 30 fps with the library's timings, so every frame matches the original.
import { type ReactElement, useEffect, useRef } from "react";
import "./ComposerBeam.css";

type Theme = "dark" | "light";
type Wave = readonly [name: string, from: number, to: number, period: number, delay: number];

const FRAME_MS = 1000 / 30 - 2;
const TAU = Math.PI * 2;
const SPREAD = 0.28;
/** Offset drift (px), corner dip, glow-height swing, then the base, spread and glow periods (s). */
const BREATH = {
  dark: { drift: 33, dip: 0.48, swing: 0.34, base: 1.9, spread: 2.6, glow: 2.4 },
  light: { drift: 40, dip: 0.45, swing: 0.22, base: 2.6, spread: 4.6, glow: 5.5 },
} as const;

function waves(theme: Theme): Wave[] {
  const { drift, dip, swing, base, spread, glow } = BREATH[theme];
  const s = SPREAD;
  return [
    ["--beam-w1", 1 - s, 1 + s * 1.1, spread * 0.9, 0],
    ["--beam-h1", 1 + s * 0.9, 1 - s * 0.85, spread * 1.26, 0],
    ["--beam-x1", -drift, drift * 0.9, base * 1.6, 0],
    ["--beam-y1", drift * 0.55, -drift * 0.7, base * 1.6, 0],
    ["--beam-w2", 1 + s, 1 - s * 0.85, spread * 1.1, 0],
    ["--beam-h2", 1 - s * 0.8, 1 + s * 1.05, spread * 0.81, 0],
    ["--beam-x2", drift * 0.8, -drift * 0.9, base * 1.88, 0],
    ["--beam-y2", -drift, drift * 0.65, base * 1.88, 0],
    ["--beam-w3", 1 - s * 0.6, 1 + s * 1.15, spread * 0.98, 0],
    ["--beam-h3", 1 + s * 0.75, 1 - s, spread * 1.4, 0],
    ["--beam-x3", -drift * 0.6, drift, base * 1.45, 0],
    ["--beam-y3", -drift * 0.85, drift * 0.45, base * 1.45, 0],
    ["--beam-gh", 1 - swing, 1 + swing, glow, 0],
    ["--beam-tl", 1 - dip, 1, base, 0],
    ["--beam-tr", 1 - dip, 1, base * 1.32, base * 0.28],
    ["--beam-bl", 1 - dip, 1, base * 0.84, base * 0.55],
    ["--beam-br", 1 - dip, 1, base * 1.58, base * 0.83],
  ];
}

/** The composer's working glow. Mount it only while a reply runs and motion is allowed. */
export function ComposerBeam({ theme }: { theme: Theme }): ReactElement {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const breath = waves(theme);
    let frame = 0;
    let last = 0;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      if (now - last < FRAME_MS) return;
      last = now;
      const seconds = now / 1000;
      for (const [name, from, to, period, delay] of breath) {
        const value = from + (to - from) * ((1 - Math.cos(TAU * ((seconds - delay) / period))) / 2);
        const offset = name.startsWith("--beam-x") || name.startsWith("--beam-y");
        element.style.setProperty(name, offset ? `${value.toFixed(2)}px` : value.toFixed(4));
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [theme]);
  return (
    <div ref={ref} className="composer-beam" aria-hidden="true">
      <div className="composer-beam-bloom" />
    </div>
  );
}
