import type { MagpieView } from "../../clients/app/magpieApi";

const COLORS = {
  bg: "--g0",
  panel: "--g2",
  raised: "--g3",
  text: "--t0",
  secondary: "--t1",
  muted: "--t2",
  faint: "--t3",
  line: "--line",
  hair: "--hair",
  accent: "--or",
  onAccent: "--on-or",
  popover: "--fill-popover",
} as const;

const OVERLAYS = '[role="dialog"], [role="menu"], [role="listbox"], .git-popover, .context-popover';

function color(value: string): string | null {
  const trimmed = value.trim().toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(trimmed)) return trimmed;
  if (/^#[0-9a-f]{3}$/.test(trimmed))
    return `#${[...trimmed.slice(1)].map((digit) => digit.repeat(2)).join("")}`;
  return null;
}

function prefers(query: string): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

/** Read only the shell's geometry and appearance; no window credential enters this projection. */
export function readMagpieView(id: string): MagpieView | null {
  const target = document.querySelector<HTMLElement>('main.app-view[data-region="view"]');
  if (!target) return null;
  const rect = target.getBoundingClientRect();
  if (![rect.left, rect.top, rect.right, rect.bottom].every(Number.isFinite)) return null;
  const x = Math.min(32768, Math.max(0, rect.left));
  const y = Math.min(32768, Math.max(0, rect.top));
  const width = Math.max(0, Math.min(32768, window.innerWidth, rect.right) - x);
  const height = Math.max(0, Math.min(32768, window.innerHeight, rect.bottom) - y);
  const root = document.documentElement;
  const styles = getComputedStyle(root);
  const palette = {} as MagpieView["appearance"]["palette"];
  for (const name of Object.keys(COLORS) as (keyof typeof COLORS)[]) {
    const value = color(styles.getPropertyValue(COLORS[name]));
    if (value === null) return null;
    palette[name] = value;
  }
  const scale = Number.parseFloat(styles.getPropertyValue("--font-scale"));
  const blocked = [...document.querySelectorAll<HTMLElement>(OVERLAYS)].some((overlay) => {
    if (overlay.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
    const style = getComputedStyle(overlay);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = overlay.getBoundingClientRect();
    return (
      box.width > 0 &&
      box.height > 0 &&
      box.left < x + width &&
      box.right > x &&
      box.top < y + height &&
      box.bottom > y
    );
  });
  return {
    id,
    bounds: { x, y, width, height },
    appearance: {
      theme: root.dataset.theme === "light" ? "light" : "dark",
      density: root.dataset.density === "compact" ? "compact" : "comfortable",
      scale: Number.isFinite(scale) ? Math.min(2, Math.max(0.75, scale)) : 1,
      reduce_motion:
        root.dataset.motion === "reduce" || prefers("(prefers-reduced-motion: reduce)"),
      locale: "en",
      palette,
    },
    visible: width > 0 && height > 0 && !target.closest("[hidden], [inert]") && !blocked,
  };
}

/** Coalesce changing shell layout and overlays to one measurement per animation frame. */
export function watchMagpieView(
  initial: MagpieView,
  onChange: (view: MagpieView) => void,
): () => void {
  let last = initial;
  let serialized = JSON.stringify(initial);
  let frame: number | null = null;
  let active = true;
  const schedule = () => {
    if (!active || frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!active) return;
      const next = readMagpieView(initial.id) ?? { ...last, visible: false };
      const value = JSON.stringify(next);
      if (value === serialized) return;
      serialized = value;
      last = next;
      onChange(next);
    });
  };
  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: [
      "style",
      "class",
      "data-theme",
      "data-density",
      "data-motion",
      "hidden",
      "inert",
      "aria-hidden",
      "open",
      "data-state",
    ],
  });
  const target = document.querySelector('main.app-view[data-region="view"]');
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
  if (target) resize?.observe(target);
  window.addEventListener("resize", schedule);
  document.addEventListener("scroll", schedule, true);
  const media =
    typeof window.matchMedia === "function"
      ? ["(prefers-contrast: more)", "(prefers-reduced-motion: reduce)"].map((query) =>
          window.matchMedia(query),
        )
      : [];
  for (const query of media) query.addEventListener("change", schedule);
  return () => {
    active = false;
    if (frame !== null) cancelAnimationFrame(frame);
    observer.disconnect();
    resize?.disconnect();
    window.removeEventListener("resize", schedule);
    document.removeEventListener("scroll", schedule, true);
    for (const query of media) query.removeEventListener("change", schedule);
  };
}
