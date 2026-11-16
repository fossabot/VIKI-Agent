import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { ActivityOrb } from "./ActivityOrb";
import { paintSolvingOrb } from "./solvingOrb";

vi.mock("./solvingOrb", async (importOriginal) => {
  const original = await importOriginal<typeof import("./solvingOrb")>();
  return { ...original, paintSolvingOrb: vi.fn(original.paintSolvingOrb) };
});

const frames = new Map<number, FrameRequestCallback>();
const media = new Map<string, { matches: boolean; listeners: Set<() => void> }>();
let nextId = 0;
let now = 0;
let ctx: CanvasRenderingContext2D;

beforeEach(() => {
  frames.clear();
  media.clear();
  nextId = 0;
  now = 0;
  vi.mocked(paintSolvingOrb).mockClear();
  ctx = {
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.spyOn(performance, "now").mockImplementation(() => now);
  vi.stubGlobal("IntersectionObserver", undefined);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++nextId, callback);
    return nextId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    frames.delete(id);
  });
  vi.stubGlobal("matchMedia", (query: string) => {
    const value = media.get(query) ?? { matches: false, listeners: new Set<() => void>() };
    media.set(query, value);
    return {
      get matches() {
        return value.matches;
      },
      addEventListener: (_event: string, callback: () => void) => value.listeners.add(callback),
      removeEventListener: (_event: string, callback: () => void) =>
        value.listeners.delete(callback),
    };
  });
});

afterEach(() => {
  cleanup();
  delete document.documentElement.dataset.motion;
  delete document.documentElement.dataset.theme;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function step(timestamp: number) {
  now = timestamp;
  const pending = [...frames.values()];
  frames.clear();
  act(() => {
    for (const callback of pending) callback(timestamp);
  });
}

test("the solving painter receives the orb clock and the dark theme on a 2x backing canvas", () => {
  vi.stubGlobal("devicePixelRatio", 3);
  const { container, queryByRole } = render(<ActivityOrb state="idle" />);
  expect(paintSolvingOrb).toHaveBeenCalledWith(ctx, 0.6, true);
  expect(ctx.arc).toHaveBeenCalled();
  const canvas = container.querySelector("canvas");
  expect(canvas?.width).toBe(128);
  expect(canvas?.height).toBe(128);
  expect(canvas?.dataset.renderer).toBe("thinking-orbs");
  expect(canvas?.dataset.preset).toBe("solving");
  expect(canvas?.parentElement?.getAttribute("aria-hidden")).toBe("true");
  expect(queryByRole("img")).toBeNull();
  expect(container.querySelector(".activity-orb-state")?.getAttribute("focusable")).toBe("false");
  expect(
    container.querySelector("button, a, [tabindex], .activity-orb-particle, .activity-orb-bands"),
  ).toBeNull();
  expect(container.textContent).toBe("");
});

test.each(["idle", "waiting", "offline", "error"] as const)(
  "%s paints one static frame without starting work",
  (state) => {
    const { container } = render(<ActivityOrb state={state} />);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    expect(frames.size).toBe(0);
    expect(container.querySelector(".activity-orb")?.getAttribute("data-paused")).toBe("true");
  },
);

test("fractional device pixels resize the backing canvas once rather than resetting every frame", () => {
  vi.stubGlobal("devicePixelRatio", 1.1);
  const width = vi.spyOn(HTMLCanvasElement.prototype, "width", "set");
  const height = vi.spyOn(HTMLCanvasElement.prototype, "height", "set");
  const { container } = render(<ActivityOrb state="active" />);
  expect(container.querySelector("canvas")?.width).toBe(70);
  expect(container.querySelector("canvas")?.height).toBe(70);
  expect(width).toHaveBeenCalledTimes(1);
  expect(height).toHaveBeenCalledTimes(1);
  step(17);
  step(34);
  expect(ctx.clearRect).toHaveBeenCalledTimes(3);
  expect(width).toHaveBeenCalledTimes(1);
  expect(height).toHaveBeenCalledTimes(1);
});

test("active paint is bounded near 60fps; waiting preserves the canvas and stops queued callbacks", () => {
  const { container, rerender, unmount } = render(<ActivityOrb state="active" />);
  const canvas = container.querySelector("canvas");
  step(7);
  step(14);
  expect(ctx.clearRect).toHaveBeenCalledTimes(1);
  step(17);
  expect(ctx.clearRect).toHaveBeenCalledTimes(2);
  const queued = [...frames.values()];
  rerender(<ActivityOrb state="waiting" />);
  expect(container.querySelector("canvas")).toBe(canvas);
  expect(frames.size).toBe(0);
  const calls = vi.mocked(ctx.clearRect).mock.calls.length;
  for (const callback of queued) callback(1000);
  expect(ctx.clearRect).toHaveBeenCalledTimes(calls);
  unmount();
  for (const callback of queued) callback(2000);
  expect(ctx.clearRect).toHaveBeenCalledTimes(calls);
  expect(frames.size).toBe(0);
});

test.each([
  "(prefers-reduced-motion: reduce)",
  "(forced-colors: active)",
  "(prefers-contrast: more)",
])("%s cancels painting and restores active motion", (query) => {
  const { container } = render(<ActivityOrb state="active" />);
  const preference = media.get(query);
  expect(preference).toBeDefined();
  if (!preference) return;
  act(() => {
    preference.matches = true;
    for (const callback of preference.listeners) callback();
  });
  expect(frames.size).toBe(0);
  expect(container.querySelector(".activity-orb")?.getAttribute("data-paused")).toBe("true");
  act(() => {
    preference.matches = false;
    for (const callback of preference.listeners) callback();
  });
  expect(frames.size).toBe(1);
});

test("app motion and document visibility gate work, while theme changes keep the same canvas", async () => {
  let hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  const { container } = render(<ActivityOrb state="active" />);
  const canvas = container.querySelector("canvas");
  act(() => {
    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(frames.size).toBe(0);
  expect(container.querySelector(".activity-orb")?.getAttribute("data-visible")).toBe("false");
  act(() => {
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(frames.size).toBe(1);
  await act(async () => {
    document.documentElement.dataset.motion = "reduce";
  });
  expect(frames.size).toBe(0);
  await act(async () => {
    document.documentElement.dataset.theme = "light";
  });
  expect(vi.mocked(paintSolvingOrb).mock.calls.at(-1)?.[2]).toBe(false);
  expect(container.querySelector("canvas")).toBe(canvas);
});

test("stale observer/media deliveries after pause or unmount cannot revive the painter", () => {
  let notify: IntersectionObserverCallback = () => {};
  const observer = { observe: vi.fn(), disconnect: vi.fn() };
  vi.stubGlobal(
    "IntersectionObserver",
    // biome-ignore lint/complexity/useArrowFunction: IntersectionObserver is constructed with new.
    vi.fn(function (callback: IntersectionObserverCallback) {
      notify = callback;
      return observer;
    }),
  );
  const { container, rerender, unmount } = render(<ActivityOrb state="active" />);
  act(() =>
    notify(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    ),
  );
  expect(frames.size).toBe(0);
  rerender(<ActivityOrb state="waiting" />);
  act(() =>
    notify(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    ),
  );
  expect(frames.size).toBe(0);
  expect(container.querySelector(".activity-orb")?.getAttribute("data-paused")).toBe("true");
  const stale = notify;
  const staleMedia = [...media.values()].flatMap((preference) => [...preference.listeners]);
  unmount();
  const calls = vi.mocked(ctx.clearRect).mock.calls.length;
  act(() => {
    stale(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    );
    for (const callback of staleMedia) callback();
  });
  step(1000);
  expect(ctx.clearRect).toHaveBeenCalledTimes(calls);
  expect(frames.size).toBe(0);
  expect(observer.disconnect).toHaveBeenCalled();
});

test("forced colors retain the explicit state badge while the decorative canvas stays out of the way", () => {
  const css = readCss(import.meta.url, "ActivityOrb.css");
  expect(ruleIn(css, ".activity-orb")).toContain("width: 64px");
  expect(ruleIn(css, ".activity-orb")).toContain("pointer-events: none");
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".activity-orb canvas")).toContain("visibility: hidden");
  expect(forced).toContain("color: CanvasText");
  expect(forced).toContain("fill: Canvas");
});
