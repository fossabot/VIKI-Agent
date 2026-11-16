import { act, cleanup, fireEvent, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import {
  closeHelp,
  openHelp,
  resetHelp,
  useHelpAvailable,
  useHelpHost,
  useHelpShortcut,
  useHelpTarget,
} from "./helpStore";

afterEach(cleanup);
beforeEach(resetHelp);

/** A help host: the only thing that makes `useHelpAvailable()` true elsewhere in the tree. */
function Host(): null {
  useHelpHost();
  return null;
}

function ShortcutProbe(): null {
  useHelpShortcut();
  return null;
}

describe("the help store", () => {
  test("openHelp sets the target; closeHelp clears it", () => {
    const { result } = renderHook(() => useHelpTarget());
    expect(result.current).toBeNull();
    act(() => openHelp({ kind: "tour" }));
    expect(result.current).toEqual({ kind: "tour" });
    act(() => openHelp({ kind: "search", query: "export" }));
    expect(result.current).toEqual({ kind: "search", query: "export" });
    act(() => closeHelp());
    expect(result.current).toBeNull();
  });

  test("useHelpAvailable counts mounted hosts rather than tracking a single flag", () => {
    const { result: available } = renderHook(() => useHelpAvailable());
    expect(available.current).toBe(false);
    const a = render(<Host />);
    expect(available.current).toBe(true);
    const b = render(<Host />);
    expect(available.current).toBe(true);
    a.unmount(); // one host remains
    expect(available.current).toBe(true);
    b.unmount(); // none left
    expect(available.current).toBe(false);
  });

  test("resetHelp clears the target and the host count", () => {
    const { result: target } = renderHook(() => useHelpTarget());
    const { result: available } = renderHook(() => useHelpAvailable());
    render(<Host />);
    act(() => openHelp({ kind: "whats-new" }));
    expect(target.current).toEqual({ kind: "whats-new" });
    expect(available.current).toBe(true);
    act(() => resetHelp());
    expect(target.current).toBeNull();
    expect(available.current).toBe(false);
  });
});

describe("useHelpShortcut", () => {
  test("a bare ? opens the keyboard-shortcuts topic and consumes the event", () => {
    render(<ShortcutProbe />);
    const { result: target } = renderHook(() => useHelpTarget());
    const notCancelled = fireEvent.keyDown(window, { key: "?" });
    expect(notCancelled).toBe(false); // false: a handler called preventDefault
    expect(target.current).toEqual({ kind: "topic", id: "keyboard-shortcuts" });
  });

  test.each([
    ["a different key", { key: "a" }],
    ["Ctrl held", { key: "?", ctrlKey: true }],
    ["Alt held", { key: "?", altKey: true }],
    ["Meta held", { key: "?", metaKey: true }],
    ["composing (isComposing)", { key: "?", isComposing: true }],
    ["composing (keyCode 229)", { key: "?", keyCode: 229 }],
  ])("does nothing: %s", (_name, init) => {
    render(<ShortcutProbe />);
    const { result: target } = renderHook(() => useHelpTarget());
    fireEvent.keyDown(window, init);
    expect(target.current).toBeNull();
  });

  test("does nothing once another handler already called preventDefault", () => {
    render(<ShortcutProbe />);
    const { result: target } = renderHook(() => useHelpTarget());
    const earlier = (event: KeyboardEvent) => event.preventDefault();
    window.addEventListener("keydown", earlier, true);
    try {
      fireEvent.keyDown(window, { key: "?" });
    } finally {
      window.removeEventListener("keydown", earlier, true);
    }
    expect(target.current).toBeNull();
  });

  test("does nothing while a modal is open", () => {
    render(<ShortcutProbe />);
    const { result: target } = renderHook(() => useHelpTarget());
    const modal = document.body.appendChild(document.createElement("div"));
    modal.setAttribute("aria-modal", "true");
    try {
      fireEvent.keyDown(window, { key: "?" });
    } finally {
      modal.remove();
    }
    expect(target.current).toBeNull();
  });

  test.each(["input", "textarea", "select", "contenteditable"])(
    "does nothing with a %s as the event target",
    (kind) => {
      render(<ShortcutProbe />);
      const { result: target } = renderHook(() => useHelpTarget());
      const field = document.createElement(kind === "contenteditable" ? "div" : kind);
      if (kind === "contenteditable") {
        field.setAttribute("contenteditable", "true");
        Object.defineProperty(field, "isContentEditable", { value: true });
      }
      document.body.appendChild(field);
      try {
        fireEvent.keyDown(field, { key: "?" });
      } finally {
        field.remove();
      }
      expect(target.current).toBeNull();
    },
  );

  test("stops listening once unmounted", () => {
    const { unmount } = render(<ShortcutProbe />);
    const { result: target } = renderHook(() => useHelpTarget());
    fireEvent.keyDown(window, { key: "?" });
    expect(target.current).toEqual({ kind: "topic", id: "keyboard-shortcuts" });
    act(() => closeHelp());
    unmount();
    fireEvent.keyDown(window, { key: "?" });
    expect(target.current).toBeNull();
  });
});
