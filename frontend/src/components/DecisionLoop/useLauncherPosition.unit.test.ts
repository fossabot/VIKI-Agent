/**
 * useLauncherPosition exposes plain callback functions (`bind.onPointerDown` and friends), not a
 * DOM node of its own, so these tests call them directly with minimal fake Pointer/Keyboard
 * event objects (`fakePointer`/`fakeKey`) rather than rendering a button and using `fireEvent` —
 * the handlers only ever read the handful of fields built below.
 */
import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import type { KeyboardEvent, PointerEvent } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  type UseLauncherPositionOptions,
  type UseLauncherPositionResult,
  useLauncherPosition,
} from "./useLauncherPosition";

function fakePointer(init: {
  clientX: number;
  clientY: number;
  pointerId?: number;
  button?: number;
  isPrimary?: boolean;
}): PointerEvent<HTMLButtonElement> {
  const currentTarget = {
    setPointerCapture: vi.fn(),
    releasePointerCapture: vi.fn(),
  };
  return {
    pointerId: init.pointerId ?? 1,
    clientX: init.clientX,
    clientY: init.clientY,
    button: init.button ?? 0,
    isPrimary: init.isPrimary ?? true,
    currentTarget,
    preventDefault: vi.fn(),
  } as unknown as PointerEvent<HTMLButtonElement>;
}

function fakeKey(init: {
  key: string;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  isComposing?: boolean;
  keyCode?: number;
}): KeyboardEvent<HTMLButtonElement> {
  return {
    key: init.key,
    altKey: init.altKey ?? false,
    ctrlKey: init.ctrlKey ?? false,
    metaKey: init.metaKey ?? false,
    keyCode: init.keyCode ?? 0,
    nativeEvent: { isComposing: init.isComposing ?? false },
    preventDefault: vi.fn(),
  } as unknown as KeyboardEvent<HTMLButtonElement>;
}

function send<E>(handler: (event: E) => void, event: E): void {
  act(() => handler(event));
}

function advance(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 768);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--font-scale");
});

/** The appearance scale as `applyAppearance` writes it; jsdom lays nothing out, so these tests
 * check the hook's arithmetic against the window size, not a rendered box. */
function setScale(scale: number): void {
  document.documentElement.style.setProperty("--font-scale", String(scale));
}

function stubWindow(width: number, height: number): void {
  vi.stubGlobal("innerWidth", width);
  vi.stubGlobal("innerHeight", height);
}

/** The launcher's box in window pixels, as root zoom draws it from the hook's scaled pixels. */
function drawn(position: UseLauncherPositionResult, size: number, scale: number) {
  return {
    left: window.innerWidth - (position.right + size) * scale,
    top: window.innerHeight - (position.bottom + size) * scale,
    right: window.innerWidth - position.right * scale,
    bottom: window.innerHeight - position.bottom * scale,
  };
}

function expectInsideWindow(position: UseLauncherPositionResult, size: number, scale: number) {
  const box = drawn(position, size, scale);
  expect(box.left).toBeGreaterThanOrEqual(0);
  expect(box.top).toBeGreaterThanOrEqual(0);
  expect(box.right).toBeLessThanOrEqual(window.innerWidth);
  expect(box.bottom).toBeLessThanOrEqual(window.innerHeight);
}

test("defaults to (20, 176) when nothing is saved", () => {
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: true, onSave: vi.fn() }),
  );
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  expect(result.current.dragging).toBe(false);
});

test.each(["", "20", "20,", ",72", "20,72,1", "-20,72", "20.5,72", "abc,def", "99999,72"])(
  "falls back to (20, 176) for the malformed saved value %j",
  (saved) => {
    const { result } = renderHook(() =>
      useLauncherPosition({ saved, writable: true, onSave: vi.fn() }),
    );
    expect(result.current.right).toBe(20);
    expect(result.current.bottom).toBe(176);
  },
);

test("parses a valid saved R,B that already fits the window", () => {
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "100,150", writable: true, onSave: vi.fn() }),
  );
  expect(result.current.right).toBe(100);
  expect(result.current.bottom).toBe(150);
});

test("clamps a saved value that does not fit the window on load", () => {
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "9000,9000", writable: true, onSave: vi.fn() }),
  );
  expect(result.current.right).toBe(956);
  expect(result.current.bottom).toBe(700);
});

test("a larger size clamps further from the edges on load", () => {
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "1000,700", writable: true, onSave: vi.fn(), size: 100 }),
  );
  expect(result.current.right).toBe(916);
  expect(result.current.bottom).toBe(660);
});

test("re-clamps on resize but never saves merely from clamping", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "900,700", writable: true, onSave }),
  );
  expect(result.current.right).toBe(900);
  expect(result.current.bottom).toBe(700);
  vi.stubGlobal("innerWidth", 200);
  vi.stubGlobal("innerHeight", 200);
  act(() => {
    fireEvent(window, new Event("resize"));
  });
  expect(result.current.right).toBe(132);
  expect(result.current.bottom).toBe(132);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("a press-release under 4 px is a click: it never drags, moves or saves", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 102, clientY: 101 }));
  expect(result.current.dragging).toBe(false);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 102, clientY: 101 }));
  expect(result.current.dragging).toBe(false);
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  expect(result.current.endedDrag()).toBe(false);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("a drag past the 4 px threshold moves the launcher, flags the drag once, and saves debounced", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.dragging).toBe(true);
  expect(result.current.right).toBe(30);
  expect(result.current.bottom).toBe(176);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.dragging).toBe(false);
  expect(result.current.endedDrag()).toBe(true);
  expect(result.current.endedDrag()).toBe(false);
  advance(299);
  expect(onSave).not.toHaveBeenCalled();
  advance(1);
  expect(onSave).toHaveBeenCalledTimes(1);
  expect(onSave).toHaveBeenCalledWith("30,176");
});

test("onPointerCancel restores the drag's start position and saves nothing", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.right).toBe(30);
  send(result.current.bind.onPointerCancel, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.dragging).toBe(false);
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("a non-left button or a non-primary pointer never starts a drag", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  send(
    result.current.bind.onPointerDown,
    fakePointer({ clientX: 100, clientY: 100, button: 2, pointerId: 5 }),
  );
  send(
    result.current.bind.onPointerDown,
    fakePointer({ clientX: 100, clientY: 100, isPrimary: false, pointerId: 6 }),
  );
  send(
    result.current.bind.onPointerMove,
    fakePointer({ clientX: 500, clientY: 500, pointerId: 5 }),
  );
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 500, clientY: 500, pointerId: 5 }));
  expect(result.current.dragging).toBe(false);
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  expect(result.current.endedDrag()).toBe(false);
});

test("pointer events for a different pointerId are ignored during an active drag", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  send(
    result.current.bind.onPointerDown,
    fakePointer({ clientX: 100, clientY: 100, pointerId: 1 }),
  );
  send(
    result.current.bind.onPointerMove,
    fakePointer({ clientX: 999, clientY: 999, pointerId: 2 }),
  );
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 999, clientY: 999, pointerId: 2 }));
  expect(result.current.dragging).toBe(false);
  expect(result.current.right).toBe(20);
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 90, clientY: 100, pointerId: 1 }));
  expect(result.current.dragging).toBe(true);
  expect(result.current.right).toBe(30);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 90, clientY: 100, pointerId: 1 }));
  expect(result.current.endedDrag()).toBe(true);
});

test.each([
  ["ArrowLeft", 216, 200],
  ["ArrowRight", 184, 200],
  ["ArrowUp", 200, 216],
  ["ArrowDown", 200, 184],
] as const)("Alt+%s moves 16 px and saves debounced", (key, right, bottom) => {
  const onSave = vi.fn();
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "200,200", writable: true, onSave }),
  );
  const event = fakeKey({ key, altKey: true });
  send(result.current.bind.onKeyDown, event);
  expect(event.preventDefault).toHaveBeenCalledTimes(1);
  expect(result.current.right).toBe(right);
  expect(result.current.bottom).toBe(bottom);
  advance(300);
  expect(onSave).toHaveBeenCalledWith(`${right},${bottom}`);
});

test("Home resets to the clamped default and saves debounced", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "500,500", writable: true, onSave }),
  );
  send(result.current.bind.onKeyDown, fakeKey({ key: "Home" }));
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  advance(300);
  expect(onSave).toHaveBeenCalledWith("20,176");
});

test.each([
  { key: "ArrowRight" },
  { key: "ArrowRight", altKey: true, ctrlKey: true },
  { key: "ArrowRight", altKey: true, metaKey: true },
  { key: "ArrowRight", altKey: true, isComposing: true },
  { key: "ArrowRight", altKey: true, keyCode: 229 },
  { key: "a" },
])("key input %j leaves the launcher untouched", (init) => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  const event = fakeKey(init);
  send(result.current.bind.onKeyDown, event);
  expect(event.preventDefault).not.toHaveBeenCalled();
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("rapid keyboard moves within the debounce window save only once, with the latest value", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() => useLauncherPosition({ saved: null, writable: true, onSave }));
  const press = () =>
    send(result.current.bind.onKeyDown, fakeKey({ key: "ArrowUp", altKey: true }));
  press();
  press();
  press();
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(224);
  advance(300);
  expect(onSave).toHaveBeenCalledTimes(1);
  expect(onSave).toHaveBeenCalledWith("20,224");
});

test("never saves while writable is false, even though drags and keyboard moves still work", () => {
  const onSave = vi.fn();
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: false, onSave }),
  );
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.right).toBe(30);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 90, clientY: 100 }));
  send(result.current.bind.onKeyDown, fakeKey({ key: "ArrowUp", altKey: true }));
  expect(result.current.bottom).toBe(192);
  advance(1000);
  expect(onSave).not.toHaveBeenCalled();
});

// Adoption after mount: settings load asynchronously, so `saved` routinely arrives a moment
// after this hook (and the launcher) already mounted with none.

test("adopts a saved value that arrives after mount, without saving it back", () => {
  const onSave = vi.fn();
  const { result, rerender } = renderHook<UseLauncherPositionResult, UseLauncherPositionOptions>(
    (props) => useLauncherPosition(props),
    { initialProps: { saved: "", writable: true, onSave } },
  );
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(176);
  rerender({ saved: "300,200", writable: true, onSave });
  expect(result.current.right).toBe(300);
  expect(result.current.bottom).toBe(200);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("a rerender with the hook's own last saved string causes no position change", () => {
  const onSave = vi.fn();
  const { result, rerender } = renderHook<UseLauncherPositionResult, UseLauncherPositionOptions>(
    (props) => useLauncherPosition(props),
    { initialProps: { saved: null, writable: true, onSave } },
  );
  send(result.current.bind.onKeyDown, fakeKey({ key: "ArrowUp", altKey: true }));
  advance(300);
  expect(onSave).toHaveBeenCalledWith("20,192");
  // A second move is already pending (not yet fired) when the first save's own echo arrives.
  send(result.current.bind.onKeyDown, fakeKey({ key: "ArrowUp", altKey: true }));
  expect(result.current.bottom).toBe(208);
  rerender({ saved: "20,192", writable: true, onSave });
  expect(result.current.right).toBe(20);
  expect(result.current.bottom).toBe(208);
  expect(onSave).toHaveBeenCalledTimes(1);
  advance(300);
  expect(onSave).toHaveBeenCalledTimes(2);
  expect(onSave).toHaveBeenLastCalledWith("20,208");
});

test("a saved change during a drag does not move the launcher mid-drag", () => {
  const onSave = vi.fn();
  const { result, rerender } = renderHook<UseLauncherPositionResult, UseLauncherPositionOptions>(
    (props) => useLauncherPosition(props),
    { initialProps: { saved: null, writable: true, onSave } },
  );
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.right).toBe(30);
  rerender({ saved: "500,500", writable: true, onSave });
  expect(result.current.dragging).toBe(true);
  expect(result.current.right).toBe(30);
  expect(result.current.bottom).toBe(176);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 90, clientY: 100 }));
  expect(result.current.dragging).toBe(false);
});

test("a malformed later value keeps the current position", () => {
  const onSave = vi.fn();
  const { result, rerender } = renderHook<UseLauncherPositionResult, UseLauncherPositionOptions>(
    (props) => useLauncherPosition(props),
    { initialProps: { saved: "300,200", writable: true, onSave } },
  );
  expect(result.current.right).toBe(300);
  rerender({ saved: "not-a-position", writable: true, onSave });
  expect(result.current.right).toBe(300);
  expect(result.current.bottom).toBe(200);
  expect(onSave).not.toHaveBeenCalled();
});

// The appearance scale: root zoom scales the launcher's own pixels, not the window's size or a
// pointer's travel.

test.each([
  { scale: 1.15, saved: "800,20" },
  { scale: 1.3, saved: "800,20" },
  { scale: 1.5, saved: "800,20" },
  { scale: 1.5, saved: "20,800" },
  { scale: 1.5, saved: "9000,9000" },
])(
  "at scale $scale a saved $saved is clamped inside a 400x700 window on load",
  ({ scale, saved }) => {
    setScale(scale);
    stubWindow(400, 700);
    const { result } = renderHook(() =>
      useLauncherPosition({ saved, writable: true, onSave: vi.fn(), size: 100 }),
    );
    expectInsideWindow(result.current, 100, scale);
  },
);

test("at scale 1.5 the clamp bound is the window's size in scaled pixels, floored", () => {
  setScale(1.5);
  stubWindow(400, 700);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "9000,9000", writable: true, onSave: vi.fn(), size: 100 }),
  );
  // 400 / 1.5 = 266.67 and 700 / 1.5 = 466.67, each floored, less the circle and the 8 px inset.
  expect(result.current.right).toBe(158);
  expect(result.current.bottom).toBe(358);
});

test("at scale 1.5 a resize re-clamps against the scaled window", () => {
  setScale(1.5);
  stubWindow(1440, 900);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "800,20", writable: true, onSave: vi.fn(), size: 100 }),
  );
  expect(result.current.right).toBe(800);
  stubWindow(400, 700);
  act(() => {
    fireEvent(window, new Event("resize"));
  });
  expect(result.current.right).toBe(158);
  expectInsideWindow(result.current, 100, 1.5);
});

test("a scale change alone re-clamps the launcher, with no resize event and no save", async () => {
  const onSave = vi.fn();
  stubWindow(1440, 900);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: "1000,400", writable: true, onSave, size: 100 }),
  );
  expect(result.current.right).toBe(1000);
  await act(async () => {
    setScale(1.5);
    // Deliver the root's MutationObserver notification without a timer or resize event.
    await Promise.resolve();
  });
  expect(result.current.right).toBe(852);
  expect(result.current.bottom).toBe(400);
  expectInsideWindow(result.current, 100, 1.5);
  advance(500);
  expect(onSave).not.toHaveBeenCalled();
});

test("at scale 1.5 a drag follows the pointer: 300 window px move the launcher 300 window px", () => {
  const onSave = vi.fn();
  setScale(1.5);
  stubWindow(1440, 900);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: true, onSave, size: 100 }),
  );
  const before = drawn(result.current, 100, 1.5);
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 1300, clientY: 500 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 1000, clientY: 350 }));
  const after = drawn(result.current, 100, 1.5);
  expect(before.left - after.left).toBe(300);
  expect(before.top - after.top).toBe(150);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: 1000, clientY: 350 }));
  advance(300);
  expect(onSave).toHaveBeenCalledWith("220,276");
});

test("at scale 1.5 a drag to the window's left edge stops inside it and saves whole numbers", () => {
  const onSave = vi.fn();
  setScale(1.5);
  stubWindow(400, 700);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: true, onSave, size: 100 }),
  );
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 300, clientY: 300 }));
  // 7 window px is 4.67 scaled px: an uneven travel must not reach the save as a fraction either.
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 293, clientY: 300 }));
  expect(result.current.right).toBe(25);
  send(result.current.bind.onPointerMove, fakePointer({ clientX: -2000, clientY: -2000 }));
  expectInsideWindow(result.current, 100, 1.5);
  send(result.current.bind.onPointerUp, fakePointer({ clientX: -2000, clientY: -2000 }));
  advance(300);
  expect(onSave).toHaveBeenCalledTimes(1);
  expect(onSave).toHaveBeenCalledWith("158,358");
});

test("at scale 1.5 Alt+ArrowLeft cannot walk the launcher out of a 400 px window", () => {
  setScale(1.5);
  stubWindow(400, 700);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: true, onSave: vi.fn(), size: 100 }),
  );
  for (let press = 0; press < 20; press += 1)
    send(result.current.bind.onKeyDown, fakeKey({ key: "ArrowLeft", altKey: true }));
  expect(result.current.right).toBe(158);
  expectInsideWindow(result.current, 100, 1.5);
});

test("the 4 px drag threshold counts the pointer's own travel at any scale", () => {
  setScale(1.5);
  const { result } = renderHook(() =>
    useLauncherPosition({ saved: null, writable: true, onSave: vi.fn() }),
  );
  send(result.current.bind.onPointerDown, fakePointer({ clientX: 100, clientY: 100 }));
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 103, clientY: 100 }));
  expect(result.current.dragging).toBe(false);
  send(result.current.bind.onPointerMove, fakePointer({ clientX: 95, clientY: 100 }));
  expect(result.current.dragging).toBe(true);
});
