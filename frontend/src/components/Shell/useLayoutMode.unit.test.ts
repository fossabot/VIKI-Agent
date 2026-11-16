/**
 * The L/M/S mode: L docks both rails (1280px and up), M only the left rail (800-1279px), S
 * neither (below 800px). matchMedia is stubbed per query rather than faked wholesale: the
 * hook asks the window twice, once per breakpoint, and a stub that answered one query for both
 * would pass while the app showed the wrong rail.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { LEFT_DOCKED_QUERY, RIGHT_DOCKED_QUERY, useLayoutMode } from "./useLayoutMode";

const rootStyle = document.documentElement.style;
const initialScale = rootStyle.getPropertyValue("--font-scale");
const initialPriority = rootStyle.getPropertyPriority("--font-scale");

afterEach(() => {
  cleanup();
  if (initialScale) rootStyle.setProperty("--font-scale", initialScale, initialPriority);
  else rootStyle.removeProperty("--font-scale");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** A matchMedia that answers `(min-width: Npx)` for a viewport of `width`, and can announce a
 * change the way Edge does when the window is resized or zoomed. */
function stubViewport(width: number) {
  let current = width;
  const listeners = new Set<{ query: string; listener: () => void }>();
  const queries: string[] = [];
  vi.stubGlobal("matchMedia", (query: string) => {
    queries.push(query);
    const min = Number(/min-width:\s*([\d.]+)px/.exec(query)?.[1] ?? "0");
    return {
      media: query,
      get matches(): boolean {
        return current >= min;
      },
      addEventListener: (_type: string, listener: () => void) => {
        listeners.add({ query, listener });
      },
      removeEventListener: (_type: string, listener: () => void) => {
        for (const binding of listeners) {
          if (binding.query === query && binding.listener === listener) listeners.delete(binding);
        }
      },
    };
  });
  return {
    queries,
    get listenerCount(): number {
      return listeners.size;
    },
    resizeTo(next: number): void {
      current = next;
      act(() => {
        for (const { listener } of [...listeners]) listener();
      });
    },
  };
}

test("with no matchMedia both rails dock, so every jsdom render keeps the L layout", () => {
  vi.stubGlobal("matchMedia", undefined);
  const { result } = renderHook(() => useLayoutMode());
  expect(result.current).toEqual({ leftDocked: true, rightDocked: true });
});

test("1440 docks both rails; 1024 docks only the left; 400 docks neither", () => {
  const wide = stubViewport(1440);
  expect(renderHook(() => useLayoutMode()).result.current).toEqual({
    leftDocked: true,
    rightDocked: true,
  });
  expect(wide.queries).toContain(LEFT_DOCKED_QUERY);
  expect(wide.queries).toContain(RIGHT_DOCKED_QUERY);

  vi.unstubAllGlobals();
  stubViewport(1024);
  expect(renderHook(() => useLayoutMode()).result.current).toEqual({
    leftDocked: true,
    rightDocked: false,
  });

  vi.unstubAllGlobals();
  stubViewport(400);
  expect(renderHook(() => useLayoutMode()).result.current).toEqual({
    leftDocked: false,
    rightDocked: false,
  });
});

test("the boundaries themselves: 800 docks the left rail, 1280 docks the right", () => {
  stubViewport(799);
  expect(renderHook(() => useLayoutMode()).result.current.leftDocked).toBe(false);
  vi.unstubAllGlobals();
  stubViewport(800);
  expect(renderHook(() => useLayoutMode()).result.current.leftDocked).toBe(true);
  vi.unstubAllGlobals();
  stubViewport(1279);
  expect(renderHook(() => useLayoutMode()).result.current.rightDocked).toBe(false);
  vi.unstubAllGlobals();
  stubViewport(1280);
  expect(renderHook(() => useLayoutMode()).result.current.rightDocked).toBe(true);
});

test("a change event re-renders with the new mode, and unmounting drops the listeners", () => {
  const viewport = stubViewport(400);
  const { result, unmount } = renderHook(() => useLayoutMode());
  expect(result.current).toEqual({ leftDocked: false, rightDocked: false });
  expect(viewport.listenerCount).toBe(2);

  viewport.resizeTo(1024);
  expect(result.current).toEqual({ leftDocked: true, rightDocked: false });
  viewport.resizeTo(1440);
  expect(result.current).toEqual({ leftDocked: true, rightDocked: true });
  viewport.resizeTo(400);
  expect(result.current).toEqual({ leftDocked: false, rightDocked: false });

  unmount();
  expect(viewport.listenerCount).toBe(0);
});

test("the value keeps its identity while the mode does not change", () => {
  const viewport = stubViewport(1440);
  const { result, rerender } = renderHook(() => useLayoutMode());
  const first = result.current;
  rerender();
  expect(result.current).toBe(first);
  viewport.resizeTo(400);
  expect(result.current).not.toBe(first);
});

// These are the supported src/viki_agent/app/settings.py font scales. The viewport still uses CSS pixels;
// root zoom reduces the local width available to dock a rail inside it.
test.each(
  [0.85, 1, 1.15, 1.3, 1.5].flatMap((scale) =>
    [800, 1280].map((threshold) => ({ scale, threshold })),
  ),
)(
  "root scale $scale applies the $threshold local-pixel docking threshold",
  ({ scale, threshold }) => {
    rootStyle.setProperty("--font-scale", String(scale));
    const viewport = stubViewport(threshold * scale - 1);
    const { result } = renderHook(() => useLayoutMode());
    expect(result.current).toEqual({ leftDocked: threshold === 1280, rightDocked: false });
    viewport.resizeTo(threshold * scale);
    expect(result.current).toEqual({ leftDocked: true, rightDocked: threshold === 1280 });
  },
);

async function changeRootScale(scale: string) {
  await act(async () => {
    rootStyle.setProperty("--font-scale", scale);
    // Deliver the root's native MutationObserver notification without a timer or resize event.
    await Promise.resolve();
  });
}

test("live root style changes update both docking decisions without a window resize", async () => {
  stubViewport(1152);
  const { result } = renderHook(() => useLayoutMode());
  expect(result.current).toEqual({ leftDocked: true, rightDocked: false });
  for (const [scale, leftDocked, rightDocked] of [
    ["1.5", false, false],
    ["0.85", true, true],
    ["1.15", true, false],
    ["1.3", true, false],
    ["1", true, false],
  ] as const) {
    await changeRootScale(scale);
    expect(result.current).toEqual({ leftDocked, rightDocked });
  }
  const unchanged = result.current;
  await act(async () => {
    rootStyle.setProperty("color", "red");
    await Promise.resolve();
    rootStyle.removeProperty("color");
  });
  expect(result.current).toBe(unchanged);
});

test.each(["", "0", "-2", "Infinity", "NaN", "invalid", "1.5junk"])(
  "invalid root scale %j uses the scale-1 docking boundaries",
  (scale) => {
    rootStyle.setProperty("--font-scale", scale);
    const viewport = stubViewport(799);
    const { result } = renderHook(() => useLayoutMode());
    for (const [width, leftDocked, rightDocked] of [
      [799, false, false],
      [800, true, false],
      [1279, true, false],
      [1280, true, true],
    ] as const) {
      viewport.resizeTo(width);
      expect(result.current).toEqual({ leftDocked, rightDocked });
    }
  },
);

test("scale updates and unmount release root observers and media listeners", async () => {
  const observe = vi.spyOn(MutationObserver.prototype, "observe");
  const disconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
  const viewport = stubViewport(1152);
  const { result, unmount } = renderHook(() => useLayoutMode());
  expect(viewport.listenerCount).toBe(2);
  await changeRootScale("1.5");
  expect(viewport.listenerCount).toBe(2);
  await changeRootScale("0.85");
  expect(viewport.listenerCount).toBe(2);
  const rootObservers = observe.mock.contexts.filter(
    (_observer, index) => observe.mock.calls[index]?.[0] === document.documentElement,
  );
  expect(rootObservers.length).toBeGreaterThan(0);
  const final = result.current;
  unmount();
  expect(viewport.listenerCount).toBe(0);
  for (const observer of rootObservers) expect(disconnect.mock.contexts).toContain(observer);
  await changeRootScale("1");
  viewport.resizeTo(400);
  expect(viewport.listenerCount).toBe(0);
  expect(result.current).toBe(final);
});
