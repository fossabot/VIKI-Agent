/**
 * `launcherShortcutOf` is tested directly as a pure function, with a minimal fake event object —
 * the same "build exactly the fields the function reads" pattern `useLauncherPosition.unit.test.ts`
 * (`fakeKey`) uses next door, rather than a real dispatched DOM event. The hook itself genuinely
 * listens on `window`, so it is exercised the way `helpStore.unit.test.tsx` exercises its own
 * bare-`?` shortcut: mount it with `renderHook`, then dispatch a real `window` keydown via
 * `fireEvent` and read what it did.
 */
import { cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  launcherShortcutHint,
  launcherShortcutOf,
  useLauncherShortcuts,
} from "./useLauncherShortcuts";

afterEach(cleanup);

function fakeKeyboardEvent(init: {
  code?: string;
  key?: string;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
  keyCode?: number;
}): KeyboardEvent {
  return {
    code: init.code ?? "",
    key: init.key ?? "",
    altKey: init.altKey ?? false,
    ctrlKey: init.ctrlKey ?? false,
    metaKey: init.metaKey ?? false,
    shiftKey: init.shiftKey ?? false,
    repeat: init.repeat ?? false,
    isComposing: init.isComposing ?? false,
    keyCode: init.keyCode ?? 0,
  } as unknown as KeyboardEvent;
}

// --- launcherShortcutHint ---

test("the panel hint is Alt+V, labelled ⌥V on macOS", () => {
  expect(launcherShortcutHint("panel", false)).toEqual({ aria: "Alt+V", label: "Alt+V" });
  expect(launcherShortcutHint("panel", true)).toEqual({ aria: "Alt+V", label: "⌥V" });
});

test("the voice hint is Alt+T, labelled ⌥T on macOS", () => {
  expect(launcherShortcutHint("voice", false)).toEqual({ aria: "Alt+T", label: "Alt+T" });
  expect(launcherShortcutHint("voice", true)).toEqual({ aria: "Alt+T", label: "⌥T" });
});

// --- launcherShortcutOf: the matcher table ---

describe("launcherShortcutOf", () => {
  test.each([
    ["Alt+KeyV matches panel", { altKey: true, code: "KeyV" }, "panel"],
    ["Alt+KeyT matches voice", { altKey: true, code: "KeyT" }, "voice"],
    [
      "macOS's √ (Option+V): the physical key matches, not the typed character",
      { altKey: true, code: "KeyV", key: "√" },
      "panel",
    ],
    ["Ctrl held refuses the match", { altKey: true, code: "KeyV", ctrlKey: true }, null],
    ["Meta held refuses the match", { altKey: true, code: "KeyV", metaKey: true }, null],
    ["Shift held refuses the match", { altKey: true, code: "KeyV", shiftKey: true }, null],
    ["without Alt held, nothing matches", { code: "KeyV" }, null],
    ["a held-key repeat refuses the match", { altKey: true, code: "KeyV", repeat: true }, null],
    [
      "mid-IME-composition refuses the match",
      { altKey: true, code: "KeyV", isComposing: true },
      null,
    ],
    [
      "keyCode 229 (an IME's other signal) refuses the match",
      { altKey: true, code: "KeyV", keyCode: 229 },
      null,
    ],
    ["an unrelated physical key refuses the match", { altKey: true, code: "KeyA" }, null],
  ])("%s", (_name, init, expected) => {
    expect(launcherShortcutOf(fakeKeyboardEvent(init))).toBe(expected);
  });
});

// --- useLauncherShortcuts: the hook itself ---

test("Alt+V calls the panel handler and prevents default", () => {
  const onPanel = vi.fn();
  const onVoice = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { panel: onPanel, voice: onVoice }));
  const notPrevented = fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  expect(notPrevented).toBe(false);
  expect(onPanel).toHaveBeenCalledTimes(1);
  expect(onVoice).not.toHaveBeenCalled();
});

test("Alt+T calls the voice handler", () => {
  const onVoice = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { voice: onVoice }));
  fireEvent.keyDown(window, { altKey: true, code: "KeyT" });
  expect(onVoice).toHaveBeenCalledTimes(1);
});

test("fires and prevents default even when the event's target is a textarea (no text-entry guard)", () => {
  const onPanel = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { panel: onPanel }));
  const textarea = document.body.appendChild(document.createElement("textarea"));
  try {
    const notPrevented = fireEvent.keyDown(textarea, { altKey: true, code: "KeyV" });
    expect(notPrevented).toBe(false);
  } finally {
    textarea.remove();
  }
  expect(onPanel).toHaveBeenCalledTimes(1);
});

test("does nothing while another, non-panel dialog is the open aria-modal element", () => {
  const onPanel = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { panel: onPanel }));
  const modal = document.body.appendChild(document.createElement("div"));
  modal.setAttribute("aria-modal", "true");
  try {
    fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  } finally {
    modal.remove();
  }
  expect(onPanel).not.toHaveBeenCalled();
});

test("still fires while VIKI's own panel is the open aria-modal element", () => {
  const onPanel = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { panel: onPanel }));
  const panel = document.body.appendChild(document.createElement("div"));
  panel.className = "dloop-panel";
  panel.setAttribute("aria-modal", "true");
  try {
    fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  } finally {
    panel.remove();
  }
  expect(onPanel).toHaveBeenCalledTimes(1);
});

test("adds no listener while disabled: nothing fires", () => {
  const onPanel = vi.fn();
  renderHook(() => useLauncherShortcuts(false, { panel: onPanel }));
  fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  expect(onPanel).not.toHaveBeenCalled();
});

test("does nothing once another handler already called preventDefault", () => {
  const onPanel = vi.fn();
  renderHook(() => useLauncherShortcuts(true, { panel: onPanel }));
  const earlier = (event: KeyboardEvent) => event.preventDefault();
  window.addEventListener("keydown", earlier, true);
  try {
    fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  } finally {
    window.removeEventListener("keydown", earlier, true);
  }
  expect(onPanel).not.toHaveBeenCalled();
});

test("a match with no handler for it does nothing", () => {
  renderHook(() => useLauncherShortcuts(true, {}));
  const notPrevented = fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  expect(notPrevented).toBe(true);
});
