import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { type ReactElement, useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { decisionLoopExamples } from "../../clients/decisionLoop/fake";
import type { ItemView, LauncherView } from "../../clients/decisionLoop/types";
import { LauncherPanel, type LauncherPanelAnchor, type LauncherPanelProps } from "./LauncherPanel";

// jsdom's own default (1024x768), pinned explicitly so the geometry tests below have a known,
// stable window to compute against regardless of test order or jsdom version (the same reason
// useLauncherPosition.unit.test.ts's beforeEach pins it for the launcher's own clamping).
beforeEach(() => {
  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 768);
});

afterEach(() => {
  document.documentElement.style.removeProperty("--font-scale");
  cleanup();
  vi.unstubAllGlobals();
});

const NOW_MS = Date.parse("2026-10-21T14:04:00.000Z");
const DEFAULT_ANCHOR: LauncherPanelAnchor = { right: 20, bottom: 72, size: 60 };

function launcherWith(overrides: Partial<LauncherView> = {}): LauncherView {
  return { ...decisionLoopExamples().launcher, ...overrides };
}

function baseProps(overrides: Partial<LauncherPanelProps> = {}): LauncherPanelProps {
  return {
    view: launcherWith(),
    checking: false,
    anchor: DEFAULT_ANCHOR,
    returnFocus: () => null,
    onClose: vi.fn(),
    onAction: vi.fn(),
    onFeedback: vi.fn(),
    onMenu: vi.fn(),
    onReview: vi.fn(),
    chat: <div>chat</div>,
    nowMs: NOW_MS,
    ...overrides,
  };
}

function renderPanel(overrides: Partial<LauncherPanelProps> = {}) {
  const props = baseProps(overrides);
  const view = render(<LauncherPanel {...props} />);
  return { ...view, props };
}

// --- dialog shell ---

test("is a dialog named VIKI", () => {
  renderPanel();
  expect(screen.getByRole("dialog", { name: "VIKI" }).getAttribute("aria-modal")).toBe("true");
});

test("shows the status line from statusLine(view, nowMs)", () => {
  const view = launcherWith({
    state: "on",
    open: 4,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
  });
  renderPanel({ view });
  expect(screen.getByText("Watching · 4 open")).toBeTruthy();
});

// --- Escape and the backdrop ---

test("Escape calls onClose and, once the caller unmounts the panel, returns focus to returnFocus's element", () => {
  function Harness(): ReactElement {
    const [open, setOpen] = useState(true);
    const launcherRef = useRef<HTMLButtonElement>(null);
    return (
      <div>
        <button ref={launcherRef} type="button">
          Launcher
        </button>
        {open ? (
          <LauncherPanel
            {...baseProps({
              returnFocus: () => launcherRef.current,
              onClose: () => setOpen(false),
            })}
          />
        ) : null}
      </div>
    );
  }
  render(<Harness />);
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Launcher" }));
});

test("a mousedown on the backdrop closes the panel; a mousedown inside it does not", () => {
  const { container, props } = renderPanel();
  fireEvent.mouseDown(screen.getByRole("dialog"));
  expect(props.onClose).not.toHaveBeenCalled();
  const backdrop = container.querySelector(".dloop-panel-backdrop") as Element;
  fireEvent.mouseDown(backdrop);
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

// --- checking / items / empty ---

test("shows the loading kit while checking, not the items or the empty text", () => {
  renderPanel({ checking: true });
  expect(screen.getByRole("status").textContent).toContain("Checking…");
  expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
});

test("renders every item, in view order, when not checking", () => {
  const items = decisionLoopExamples().launcher.items;
  renderPanel({ checking: false, view: launcherWith({ items }) });
  for (const item of items) {
    expect(screen.getByText(item.what)).toBeTruthy();
  }
});

test("renders the empty text when there are no items and not checking", () => {
  renderPanel({ checking: false, view: launcherWith({ items: [] }) });
  expect(screen.getByText("Nothing needs you right now.")).toBeTruthy();
});

test("renders a planted <b> in an item's what as literal text, never as HTML", () => {
  const item: ItemView = {
    ...decisionLoopExamples().launcher.items[0],
    what: "<b>evil</b>",
  } as ItemView;
  renderPanel({ checking: false, view: launcherWith({ items: [item] }) });
  expect(screen.getByText("<b>evil</b>")).toBeTruthy();
  expect(document.querySelector("b")).toBeNull();
});

// --- the weekly check card ---

test("the weekly check card is absent while review.due is false", () => {
  renderPanel({ view: launcherWith({ review: { due: false, count: 3 } }) });
  expect(screen.queryByText("Check")).toBeNull();
  expect(screen.queryByText("Skip this week")).toBeNull();
});

test("the weekly check card names the count VIKI held back last week, while due", () => {
  renderPanel({ view: launcherWith({ review: { due: true, count: 3 } }) });
  expect(
    screen.getByText("Weekly check: 3 things VIKI held back last week. Was it right?"),
  ).toBeTruthy();
});

test("the weekly check card uses the singular for one thing", () => {
  renderPanel({ view: launcherWith({ review: { due: true, count: 1 } }) });
  expect(
    screen.getByText("Weekly check: 1 thing VIKI held back last week. Was it right?"),
  ).toBeTruthy();
});

test("Check calls onReview with check", () => {
  const { props } = renderPanel({ view: launcherWith({ review: { due: true, count: 2 } }) });
  fireEvent.click(screen.getByRole("button", { name: "Check" }));
  expect(props.onReview).toHaveBeenCalledWith("check");
});

test("Skip this week calls onReview with skip", () => {
  const { props } = renderPanel({ view: launcherWith({ review: { due: true, count: 2 } }) });
  fireEvent.click(screen.getByRole("button", { name: "Skip this week" }));
  expect(props.onReview).toHaveBeenCalledWith("skip");
});

// --- threading onAction/onFeedback through to the right item ---

test("Not now calls onFeedback with the item, later and no reason", () => {
  // Scoped to one item: every item's own "Not now" button carries the same generic text, so a
  // multi-item view (the fixture's default) would make this query ambiguous.
  const item = decisionLoopExamples().launcher.items[0] as ItemView;
  const view = launcherWith({ items: [item] });
  const { props } = renderPanel({ view });
  fireEvent.click(screen.getByRole("button", { name: "Not now" }));
  expect(props.onFeedback).toHaveBeenCalledWith(item, "later", undefined);
});

test("a primary action calls onAction with the action and its own item", () => {
  const item = decisionLoopExamples().launcher.items[0] as ItemView;
  const primary = item.actions.find((action) => action.primary);
  if (!primary) throw new Error("fixture item has no primary action");
  // Scoped to one item, same reason as the "Not now" test above: a sibling item could in
  // principle carry an action with the same label.
  const { props } = renderPanel({ view: launcherWith({ items: [item] }) });
  fireEvent.click(screen.getByRole("button", { name: primary.label }));
  expect(props.onAction).toHaveBeenCalledWith(primary, item);
});

// --- the header "⋯" menu, and "Turn on in Decision" while off ---

describe("the header menu", () => {
  function openMenu(viewOverrides: Partial<LauncherView> = {}) {
    const result = renderPanel({ view: launcherWith({ items: [], ...viewOverrides }) });
    const toggle = screen.getByRole("button", { name: "More" });
    expect(toggle.getAttribute("aria-haspopup")).toBe("menu");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const menu = screen.getByRole("menu", { name: "More" });
    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((el) => el.textContent);
    return { ...result, toggle, labels };
  }

  test("on, not paused, no focus: both pause rows, then Focus for 1 hour, then Open Decision", () => {
    const { labels } = openMenu({ state: "on", paused_until: null, focus_until: null });
    expect(labels).toEqual([
      "Pause for 1 hour",
      "Pause until tomorrow",
      "Focus for 1 hour",
      "Open Decision",
    ]);
  });

  test("paused: Resume instead of the two pause rows", () => {
    const { labels } = openMenu({
      state: "paused",
      paused_until: "2026-10-21T15:00:00.000Z",
      focus_until: null,
    });
    expect(labels).toEqual(["Resume", "Focus for 1 hour", "Open Decision"]);
  });

  test("a focus spell: End focus instead of Focus for 1 hour", () => {
    const { labels } = openMenu({
      state: "on",
      paused_until: null,
      focus_until: "2026-10-21T16:00:00.000Z",
    });
    expect(labels).toEqual([
      "Pause for 1 hour",
      "Pause until tomorrow",
      "End focus",
      "Open Decision",
    ]);
  });

  test("a row calls onMenu with its own choice, closes the menu and returns focus to the toggle", () => {
    const { props, toggle } = openMenu({ state: "on", paused_until: null, focus_until: null });
    fireEvent.click(screen.getByRole("menuitem", { name: "Open Decision" }));
    expect(props.onMenu).toHaveBeenCalledWith("open_decision");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });

  test("Escape closes the menu before it reaches the dialog", () => {
    openMenu({ state: "on", paused_until: null, focus_until: null });
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

test("off: Turn on in Decision appears under the status line and calls onMenu with open_decision", () => {
  const view = launcherWith({ items: [], state: "off", paused_until: null, focus_until: null });
  const { props } = renderPanel({ view });
  expect(screen.queryByRole("button", { name: "More" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Turn on in Decision" }));
  expect(props.onMenu).toHaveBeenCalledWith("open_decision");
});

// --- full-window sheet below 560x480, recomputed on resize ---

test("fills the window below 560 width or 480 height, but not at the threshold itself, and reacts to resize", () => {
  renderPanel();
  const dialog = () => screen.getByRole("dialog");
  expect(dialog().className).not.toContain("dloop-panel--full");

  vi.stubGlobal("innerWidth", 560);
  vi.stubGlobal("innerHeight", 480);
  act(() => {
    fireEvent(window, new Event("resize"));
  });
  expect(dialog().className).not.toContain("dloop-panel--full");

  vi.stubGlobal("innerWidth", 559);
  act(() => {
    fireEvent(window, new Event("resize"));
  });
  expect(dialog().className).toContain("dloop-panel--full");

  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 479);
  act(() => {
    fireEvent(window, new Event("resize"));
  });
  expect(dialog().className).toContain("dloop-panel--full");
});

// --- font scale: the root zoom scales the anchor's pixels but not the window's ---

describe.each([1, 1.15, 1.5])("at font scale %s in a 1440x900 window", (scale) => {
  beforeEach(() => {
    vi.stubGlobal("innerWidth", 1440);
    vi.stubGlobal("innerHeight", 900);
    document.documentElement.style.setProperty("--font-scale", String(scale));
  });

  test.each([
    ["the default spot", { right: 20, bottom: 72, size: 60 }],
    ["the bottom-right corner", { right: 8, bottom: 8, size: 60 }],
    ["the top-left corner", { right: 1000, bottom: 500, size: 60 }],
  ])("keeps the panel inside the window with the launcher at %s", (_name, anchor) => {
    renderPanel({ anchor });
    const dialog = screen.getByRole("dialog");
    const left = Number.parseFloat(dialog.style.left);
    const top = Number.parseFloat(dialog.style.top);
    expect(left).toBeGreaterThanOrEqual(8);
    expect(top).toBeGreaterThanOrEqual(8);
    expect(left + Number.parseFloat(dialog.style.width)).toBeLessThanOrEqual(
      Math.floor(1440 / scale) - 8,
    );
    expect(top + Number.parseFloat(dialog.style.height)).toBeLessThanOrEqual(
      Math.floor(900 / scale) - 8,
    );
  });
});

test("judges the full-window threshold in scaled pixels", () => {
  vi.stubGlobal("innerWidth", 800);
  vi.stubGlobal("innerHeight", 700);
  renderPanel();
  expect(screen.getByRole("dialog").className).not.toContain("dloop-panel--full");
  cleanup();
  // 800 / 1.5 is 533 scaled pixels wide, under 560.
  document.documentElement.style.setProperty("--font-scale", "1.5");
  renderPanel();
  expect(screen.getByRole("dialog").className).toContain("dloop-panel--full");
});

// --- placement: the side with more room, flipping near an edge ---

test("opens above and to the left when that side has more room (1024x768, the default anchor)", () => {
  renderPanel({ anchor: { right: 20, bottom: 72, size: 60 } });
  const dialog = screen.getByRole("dialog");
  expect(dialog.style.left).toBe("624px");
  expect(dialog.style.top).toBe("148px");
  expect(dialog.style.width).toBe("380px");
  expect(dialog.style.height).toBe("480px");
});

test("keeps MIN_PANEL_HEIGHT when neither side of the launcher has that much room, covering part of the launcher", () => {
  // A 700px window and the launcher's own default spot (right 20, bottom 176, the 100px circle)
  // leave 416px above it; the panel holds 440 from the top inset instead of clipping its Composer.
  vi.stubGlobal("innerHeight", 700);
  renderPanel({ anchor: { right: 20, bottom: 176, size: 100 } });
  const dialog = screen.getByRole("dialog");
  expect(dialog.style.top).toBe("8px");
  expect(dialog.style.height).toBe("440px");
  expect(dialog.style.width).toBe("380px");
});

test("flips to open below and to the right when the launcher sits near the top-left corner", () => {
  renderPanel({ anchor: { right: 900, bottom: 650, size: 60 } });
  const dialog = screen.getByRole("dialog");
  expect(dialog.style.left).toBe("64px");
  expect(dialog.style.top).toBe("126px");
  expect(dialog.style.width).toBe("380px");
  expect(dialog.style.height).toBe("480px");
});

// --- resizing: savedSize, the two outer-edge handles, onSaveSize ---

// jsdom has no pointer capture; the same tiny polyfill ResizeHandle.unit.test.tsx's own `pointer`
// and `capture` helpers use.
function pointer(handle: Element, type: string, init: PointerEventInit = {}) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX: 100,
    clientY: 100,
    ...init,
  });
  Object.defineProperties(event, {
    pointerId: { value: init.pointerId ?? 1 },
    isPrimary: { value: init.isPrimary ?? true },
  });
  return fireEvent(handle, event);
}

function capturePointer(handle: Element) {
  let held: number | null = null;
  Object.assign(handle, {
    setPointerCapture: (id: number) => {
      held = id;
    },
    releasePointerCapture: (_id: number) => {
      held = null;
    },
    hasPointerCapture: (id: number) => held === id,
  });
}

// A missing savedSize (the key left out entirely, not set to undefined: exactOptionalPropertyTypes
// forbids the latter) is already covered by the placement tests above, which pin 380x480 without
// passing savedSize at all.
test.each([null, "", "abc", "100", "100,100,100", "-5,10"])(
  "a malformed saved size (%j) falls back to 380x480",
  (savedSize) => {
    renderPanel({ savedSize });
    const dialog = screen.getByRole("dialog");
    expect(dialog.style.width).toBe("380px");
    expect(dialog.style.height).toBe("480px");
  },
);

test("a saved size that fits the room renders at that exact size", () => {
  renderPanel({ savedSize: "520,600" });
  const dialog = screen.getByRole("dialog");
  expect(dialog.style.width).toBe("520px");
  expect(dialog.style.height).toBe("600px");
});

test("a saved size below the minimum clamps up to 320x440", () => {
  renderPanel({ savedSize: "10,10" });
  const dialog = screen.getByRole("dialog");
  expect(dialog.style.width).toBe("320px");
  expect(dialog.style.height).toBe("440px");
});

test("no onSaveSize means no resize handles, even though there is room beside the launcher", () => {
  renderPanel();
  expect(screen.queryByRole("separator")).toBeNull();
});

test("fills the window below the threshold and renders no resize handles even with onSaveSize", () => {
  vi.stubGlobal("innerWidth", 500);
  vi.stubGlobal("innerHeight", 400);
  renderPanel({ onSaveSize: vi.fn() });
  expect(screen.getByRole("dialog").className).toContain("dloop-panel--full");
  expect(screen.queryByRole("separator")).toBeNull();
});

test("the width handle's ArrowLeft grows the default (left-growing) panel and saves; Enter resets it", () => {
  const onSaveSize = vi.fn();
  renderPanel({ onSaveSize });
  const handle = screen.getByRole("separator", { name: "Resize panel width" });
  fireEvent.keyDown(handle, { key: "ArrowLeft" });
  expect(onSaveSize).toHaveBeenLastCalledWith("390,480");
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(onSaveSize).toHaveBeenLastCalledWith("380,480");
});

test("the height handle's ArrowUp grows the default (up-growing) panel and saves", () => {
  const onSaveSize = vi.fn();
  renderPanel({ onSaveSize });
  const handle = screen.getByRole("separator", { name: "Resize panel height" });
  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(onSaveSize).toHaveBeenLastCalledWith("380,490");
});

test("a flipped anchor (growing right and down) grows with ArrowRight/ArrowDown instead", () => {
  const onSaveSize = vi.fn();
  renderPanel({ anchor: { right: 900, bottom: 650, size: 60 }, onSaveSize });
  fireEvent.keyDown(screen.getByRole("separator", { name: "Resize panel width" }), {
    key: "ArrowRight",
  });
  expect(onSaveSize).toHaveBeenLastCalledWith("390,480");
  fireEvent.keyDown(screen.getByRole("separator", { name: "Resize panel height" }), {
    key: "ArrowDown",
  });
  expect(onSaveSize).toHaveBeenLastCalledWith("390,490");
});

test("a pointer drag previews the live size and saves once, on release", () => {
  const onSaveSize = vi.fn();
  renderPanel({ onSaveSize });
  const handle = screen.getByRole("separator", { name: "Resize panel width" });
  capturePointer(handle);
  pointer(handle, "pointerdown", { clientX: 100 });
  pointer(handle, "pointermove", { clientX: 80 });
  expect(screen.getByRole("dialog").style.width).toBe("400px");
  expect(onSaveSize).not.toHaveBeenCalled();
  pointer(handle, "pointerup", { clientX: 80 });
  expect(onSaveSize).toHaveBeenCalledTimes(1);
  expect(onSaveSize).toHaveBeenCalledWith("400,480");
});

// Escape mid-drag must not reach useModalFocus's onDismiss first (dismissing the panel, or just
// ending voice once onEscape intercepts), which would leave the drag's pointer capture and
// preview live. ResizeHandle's own data-dragging makes useModalFocus step aside, the same as an
// open menu, so this Escape only cancels the drag; the next one reaches the dialog.
test("Escape mid-drag cancels the drag, not the panel: width reverts, no onEscape/onClose/onSaveSize; the next Escape closes", () => {
  const onClose = vi.fn();
  const onEscape = vi.fn(() => false);
  const onSaveSize = vi.fn();
  renderPanel({ onClose, onEscape, onSaveSize });
  const handle = screen.getByRole("separator", { name: "Resize panel width" });
  capturePointer(handle);
  pointer(handle, "pointerdown", { clientX: 100 });
  pointer(handle, "pointermove", { clientX: 80 });
  expect(screen.getByRole("dialog").style.width).toBe("400px");

  fireEvent.keyDown(window, { key: "Escape" });
  expect(onEscape).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(onSaveSize).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog").style.width).toBe("380px");

  fireEvent.keyDown(window, { key: "Escape" });
  expect(onEscape).toHaveBeenCalledTimes(1);
  expect(onClose).toHaveBeenCalledTimes(1);
});

// Escape closing with no onEscape at all is already covered above ("Escape calls onClose...");
// exactOptionalPropertyTypes forbids spreading an explicit onEscape: undefined into the props.
test("onEscape false lets Escape close the panel as usual", () => {
  const onClose = vi.fn();
  renderPanel({ onClose, onEscape: () => false });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("onEscape true intercepts Escape and keeps the panel open", () => {
  const onClose = vi.fn();
  const onEscape = vi.fn(() => true);
  renderPanel({ onClose, onEscape });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onEscape).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeTruthy();
});

test('initialFocus="message" focuses a planted Message VIKI textarea inside the dialog', () => {
  renderPanel({
    initialFocus: "message",
    chat: (
      <div>
        <textarea aria-label="Message VIKI" />
      </div>
    ),
  });
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Message VIKI" }));
});

test("without initialFocus, mount focuses today's first control even with a planted textarea", () => {
  renderPanel({
    chat: (
      <div>
        <textarea aria-label="Message VIKI" />
      </div>
    ),
  });
  expect(document.activeElement).not.toBe(screen.getByRole("textbox", { name: "Message VIKI" }));
});

test("a window resize that leaves less room than the saved size clamps the render only; it never calls onSaveSize", () => {
  const onSaveSize = vi.fn();
  renderPanel({ savedSize: "700,700", onSaveSize });
  expect(screen.getByRole("dialog").style.width).toBe("700px");
  act(() => {
    vi.stubGlobal("innerWidth", 600);
    fireEvent(window, new Event("resize"));
  });
  // 600 window width, the default anchor's 20px right inset and 60px size leave only 520px of
  // room on the wider (left) side: the saved 700 is clamped down for this render, but onSaveSize
  // (and so the next open's savedSize) never hears about it.
  expect(screen.getByRole("dialog").style.width).toBe("520px");
  expect(onSaveSize).not.toHaveBeenCalled();
});
