/**
 * The appearance mechanism: reading the four keys' effective values off a
 * settings document, and applying a set of values to the live document. Pure over
 * `document.documentElement` — one React hook, `usePrefersLight`, no fetch, nothing logged — so
 * `AppearancePage.tsx` and App's own effect can both call it with either the committed
 * values or a live preview.
 *
 * Nothing here exports a font-scale reader for other components: the Composer's height cap
 * reads `document.documentElement.clientHeight` and divides by the root's own
 * `--font-scale` itself (measured in Edge: root `zoom` scales element rects and `scrollHeight`
 * but neither `innerHeight` nor `clientHeight`), so no scale term needs to leave this module.
 */
import { useSyncExternalStore } from "react";
import type { SettingsView } from "../../clients/app/shellApi";
// The mechanism's own stylesheet (the root zoom, the density and motion overrides): imported
// here, beside `applyAppearance`, rather than by the page component, so the sheet reaches the
// bundle through whatever imports this module — the startup effect, not only the settings
// page — and a stored density/font_scale never depends on the Appearance page having been opened.
import "./appearance.css";

export interface AppearanceValues {
  theme: string;
  density: string;
  font_scale: number;
  reduce_motion: string;
}

/** `src/viki_agent/app/settings.py`'s defaults for the four keys (KEYS' `default`), used when no document has
 * loaded yet or a key is missing from `effective` (a corrupt or older document). */
export const DEFAULT_APPEARANCE: AppearanceValues = {
  theme: "system",
  density: "comfortable",
  font_scale: 1,
  reduce_motion: "system",
};

/** The four keys' effective values, typed and defaulted. `effective` is `Record<string,
 * unknown>`, so each field is read defensively: a value of the wrong shape (a corrupt document,
 * a future key type) falls back to the built-in default rather than reaching the DOM untyped.
 * `settings === null` (no document has loaded yet) gives theme `dark`: until a document loads
 * the app renders dark, so the first frame already matches and a no-backend page stays dark. A
 * loaded document with a missing or malformed theme key falls back to `DEFAULT_APPEARANCE.theme`
 * ("system") instead, since a document did load. */
export function appearanceFromSettings(settings: SettingsView | null): AppearanceValues {
  const effective = settings?.effective ?? {};
  const theme = effective["appearance.theme"];
  const density = effective["appearance.density"];
  const fontScale = effective["accessibility.font_scale"];
  const reduceMotion = effective["accessibility.reduce_motion"];
  return {
    theme:
      typeof theme === "string" ? theme : settings === null ? "dark" : DEFAULT_APPEARANCE.theme,
    density: typeof density === "string" ? density : DEFAULT_APPEARANCE.density,
    font_scale: typeof fontScale === "number" ? fontScale : DEFAULT_APPEARANCE.font_scale,
    reduce_motion:
      typeof reduceMotion === "string" ? reduceMotion : DEFAULT_APPEARANCE.reduce_motion,
  };
}

export type ResolvedTheme = "dark" | "light";

/** `theme` as stored (`appearance.theme`) resolved to what `data-theme` renders: `light` and
 * `dark` pass through; `system` follows `prefersLight` (the OS's `prefers-color-scheme`); any
 * other value is `dark`. */
export function resolveTheme(theme: string, prefersLight: boolean): ResolvedTheme {
  if (theme === "light" || theme === "dark") return theme;
  if (theme === "system") return prefersLight ? "light" : "dark";
  return "dark";
}

/** The media query `usePrefersLight` subscribes to. */
export const PREFERS_LIGHT_QUERY = "(prefers-color-scheme: light)";

function subscribePrefersLight(onStoreChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => {};
  const list = window.matchMedia(PREFERS_LIGHT_QUERY);
  list.addEventListener("change", onStoreChange);
  return () => list.removeEventListener("change", onStoreChange);
}

function snapshotPrefersLight(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(PREFERS_LIGHT_QUERY).matches;
}

/** Whether the OS currently prefers a light scheme, following `useLayoutMode.ts`'s
 * `useMediaQuery` pattern. `false` when `window.matchMedia` is not a function. */
export function usePrefersLight(): boolean {
  return useSyncExternalStore(subscribePrefersLight, snapshotPrefersLight);
}

/**
 * Applies `values` to `document.documentElement`. Idempotent: calling it twice with the same
 * values, or with different values in sequence, always leaves exactly this state — never an
 * attribute left over from an earlier call.
 *
 * - `data-theme` is `resolveTheme(values.theme, prefersLight)`: `light` or `dark` as stored, or
 *   `system` resolved against the OS's live preference. `prefersLight` defaults to `false`, so a
 *   caller that has not read `usePrefersLight()` still gets a deterministic (dark) result.
 * - `data-density` is always one of "comfortable"/"compact", matching `appearance.css`'s own
 *   two-value selector — no in-between state to leave stale.
 * - `data-motion` is set to "reduce" only when asked; "allow" and "system" remove it rather than
 *   write a value with no rule keyed on it, because neither can override
 *   `prefers-reduced-motion` at the OS level, and the record says so plainly ("allow sets
 *   nothing").
 * - `--font-scale` is always written, so `appearance.css`'s `html { zoom: var(--font-scale, 1) }`
 *   never depends on the property having been set by an earlier call.
 */
export function applyAppearance(values: AppearanceValues, prefersLight = false): void {
  const root = document.documentElement;
  root.dataset.theme = resolveTheme(values.theme, prefersLight);
  root.dataset.density = values.density === "compact" ? "compact" : "comfortable";
  if (values.reduce_motion === "reduce") root.dataset.motion = "reduce";
  else delete root.dataset.motion;
  root.style.setProperty("--font-scale", String(values.font_scale));
}
