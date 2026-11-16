/**
 * The entry focus / scroll split (see useModalFocus.ts). jsdom's focus() never
 * scrolls anything, so the shell-jerk itself can only be measured in real Edge
 * (e2e/scenarios/shell/narrow-layout.test.ts); what a unit test can pin is the call jsdom does record: that entry
 * focus asks for `{ preventScroll: true }` while the Tab cycle does not. The Tab cycle must keep
 * the browser's default scroll-into-view — the drawer panel is its own scroller and a lower tile
 * can sit below the fold (RailDrawer.css) — so a fix that adds preventScroll everywhere would
 * pass this file's first test and silently break keyboard reachability, which the second test
 * guards against.
 */
import { fireEvent, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useModalFocus } from "./useModalFocus";

afterEach(() => {
  document.body.innerHTML = "";
});

function mountDialog(): { dialog: HTMLElement; close: HTMLButtonElement; row: HTMLButtonElement } {
  document.body.innerHTML = `
    <div id="dialog" tabindex="-1">
      <button id="row">Weekly review draft</button>
      <button id="close">Close</button>
    </div>`;
  return {
    dialog: document.getElementById("dialog") as HTMLElement,
    close: document.getElementById("close") as HTMLButtonElement,
    row: document.getElementById("row") as HTMLButtonElement,
  };
}

test("entry focus asks the browser not to scroll the shell into view", () => {
  const { dialog, close } = mountDialog();
  const closeFocus = vi.spyOn(close, "focus");

  renderHook(() =>
    useModalFocus({
      active: true,
      dialogRef: { current: dialog },
      initialFocusRef: { current: close },
      onDismiss: () => {},
    }),
  );

  expect(closeFocus).toHaveBeenCalledWith({ preventScroll: true });
});

test("the Tab cycle keeps the browser's own scroll-into-view", () => {
  const { dialog, close, row } = mountDialog();
  const rowFocus = vi.spyOn(row, "focus");

  renderHook(() =>
    useModalFocus({
      active: true,
      dialogRef: { current: dialog },
      initialFocusRef: { current: close },
      onDismiss: () => {},
    }),
  );

  close.focus();
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true });

  expect(rowFocus).toHaveBeenCalledTimes(1);
  expect(rowFocus).toHaveBeenCalledWith();
});

test("Tab reaches a closed disclosure's summary and footer without entering hidden contents", () => {
  document.body.innerHTML = `
    <div id="dialog" tabindex="-1">
      <button id="close">Close</button>
      <details id="outer">
        <summary id="summary">Browse Skills <button id="summary-action">Help</button></summary>
        <input id="search" aria-label="Search Skills" />
        <details id="inner">
          <summary id="inner-summary">More Skills</summary>
          <input id="inner-search" aria-label="Search more Skills" />
        </details>
      </details>
      <button id="footer">Context & loadout</button>
    </div>`;
  const get = (id: string) => document.getElementById(id) as HTMLElement;
  const outer = get("outer") as HTMLDetailsElement;
  const inner = get("inner") as HTMLDetailsElement;
  const next = (shiftKey = false) => fireEvent.keyDown(window, { key: "Tab", shiftKey });

  renderHook(() =>
    useModalFocus({
      active: true,
      dialogRef: { current: get("dialog") },
      initialFocusRef: { current: get("close") },
      onDismiss: () => {},
    }),
  );

  expect(document.activeElement).toBe(get("close"));
  next();
  expect(document.activeElement).toBe(get("summary"));
  next();
  expect(document.activeElement).toBe(get("summary-action"));
  next();
  expect(document.activeElement).toBe(get("footer"));
  next(true);
  expect(document.activeElement).toBe(get("summary-action"));
  next(true);
  expect(document.activeElement).toBe(get("summary"));
  next(true);
  expect(document.activeElement).toBe(get("close"));

  outer.open = true;
  get("summary-action").focus();
  next();
  expect(document.activeElement).toBe(get("search"));
  next();
  expect(document.activeElement).toBe(get("inner-summary"));
  next();
  expect(document.activeElement).toBe(get("footer"));

  inner.open = true;
  get("inner-summary").focus();
  next();
  expect(document.activeElement).toBe(get("inner-search"));
  outer.open = false;
  get("summary").focus();
  next();
  expect(document.activeElement).toBe(get("summary-action"));
  next();
  expect(document.activeElement).toBe(get("footer"));
});

test("closing restores the opener's focus with preventScroll too, not just entry focus", () => {
  // The opener has to be focused BEFORE the dialog mounts (the hook reads document.activeElement
  // as its own layout effect runs), so it is built alongside the dialog rather than through
  // mountDialog, which would replace it.
  document.body.innerHTML = `
    <button id="opener">Open</button>
    <div id="dialog" tabindex="-1">
      <button id="close">Close</button>
    </div>`;
  const opener = document.getElementById("opener") as HTMLButtonElement;
  const dialog = document.getElementById("dialog") as HTMLElement;
  const close = document.getElementById("close") as HTMLButtonElement;
  opener.focus();
  const openerFocus = vi.spyOn(opener, "focus");

  const { unmount } = renderHook(() =>
    useModalFocus({
      active: true,
      dialogRef: { current: dialog },
      initialFocusRef: { current: close },
      onDismiss: () => {},
    }),
  );

  unmount();

  expect(openerFocus).toHaveBeenCalledWith({ preventScroll: true });
});

// A resize handle's own window keydown listener (ResizeHandle.tsx, added only for a drag's
// duration) is registered strictly after this hook's own, so without this check it would never
// run — this hook's capture-phase listener would always dismiss (or step past onEscape) first,
// leaving the drag's pointer capture and preview live.
test("Escape steps aside while a resize handle inside the dialog is mid-drag, the same as an open menu", () => {
  document.body.innerHTML = `
    <div id="dialog" tabindex="-1">
      <div class="resize-handle" data-dragging></div>
      <button id="close">Close</button>
    </div>`;
  const dialog = document.getElementById("dialog") as HTMLElement;
  const close = document.getElementById("close") as HTMLButtonElement;
  const onDismiss = vi.fn();

  renderHook(() =>
    useModalFocus({
      active: true,
      dialogRef: { current: dialog },
      initialFocusRef: { current: close },
      onDismiss,
    }),
  );

  fireEvent.keyDown(window, { key: "Escape" });
  expect(onDismiss).not.toHaveBeenCalled();

  dialog.querySelector(".resize-handle")?.removeAttribute("data-dragging");
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onDismiss).toHaveBeenCalledTimes(1);
});
