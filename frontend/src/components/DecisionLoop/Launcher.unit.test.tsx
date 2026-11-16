/**
 * Launcher.tsx. `position` is a plain fake object here, not the real `useLauncherPosition`
 * hook (that hook has its own test file): these tests only need to see that Launcher reads
 * `right`/`bottom`/`dragging` for its own look, spreads `bind` onto the button, and calls
 * `endedDrag()` from its click handler — not reproduce dragging itself.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { decisionLoopExamples } from "../../clients/decisionLoop/fake";
import type { LauncherView } from "../../clients/decisionLoop/types";
import { Launcher, type LauncherProps } from "./Launcher";
import type { UseLauncherPositionResult } from "./useLauncherPosition";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_700_000_000_000);
});

const EXAMPLE_VIEW = decisionLoopExamples().launcher;

function baseView(overrides: Partial<LauncherView> = {}): LauncherView {
  // `working` defaults to 0 here, overriding the example's own `working: 1`: now that working
  // can overlay any of four looks (not just idle), that example value would otherwise leak into
  // every test below that does not care about it. A test that wants the working arc sets
  // `working` itself.
  return { ...EXAMPLE_VIEW, working: 0, ...overrides };
}

function fakePosition(
  overrides: Partial<UseLauncherPositionResult> = {},
): UseLauncherPositionResult {
  return {
    right: 20,
    bottom: 72,
    dragging: false,
    bind: {
      onPointerDown: vi.fn(),
      onPointerMove: vi.fn(),
      onPointerUp: vi.fn(),
      onPointerCancel: vi.fn(),
      onKeyDown: vi.fn(),
    },
    endedDrag: vi.fn(() => false),
    ...overrides,
  };
}

function baseProps(overrides: Partial<LauncherProps> = {}): LauncherProps {
  return {
    view: baseView(),
    open: false,
    onToggle: vi.fn(),
    position: fakePosition(),
    typingUntilMs: 0,
    reducedMotion: false,
    ...overrides,
  };
}

function mount(overrides: Partial<LauncherProps> = {}) {
  const props = baseProps(overrides);
  const rendered = render(<Launcher {...props} />);
  return { ...rendered, props };
}

function advance(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

// --- each state's look class and accessible name ---

test("idle: the bare mark, named 'nothing needs you right now'", () => {
  mount({ view: baseView({ state: "on", level: "idle", working: 0 }) });
  const button = screen.getByRole("button", { name: "VIKI, nothing needs you right now" });
  expect(button.className).toContain("dloop-launcher-look-idle");
});

test("list: named 'new information available'", () => {
  mount({ view: baseView({ state: "on", level: "list" }) });
  const button = screen.getByRole("button", { name: "VIKI, new information available" });
  expect(button.className).toContain("dloop-launcher-look-list");
});

test("badge: shows the count and names it, plural", () => {
  mount({ view: baseView({ state: "on", level: "badge", count: 2 }) });
  const button = screen.getByRole("button", { name: "VIKI, 2 items need you" });
  expect(button.className).toContain("dloop-launcher-look-badge");
  expect(button.textContent).toContain("2");
});

test("badge: singular count reads '1 item needs you'", () => {
  mount({ view: baseView({ state: "on", level: "badge", count: 1 }) });
  const button = screen.getByRole("button", { name: "VIKI, 1 item needs you" });
  expect(button.textContent).toContain("1");
});

test("question: a '?' and a fixed name, regardless of count", () => {
  mount({ view: baseView({ state: "on", level: "question", count: 4 }) });
  const button = screen.getByRole("button", { name: "VIKI, a question for you" });
  expect(button.className).toContain("dloop-launcher-look-question");
  expect(button.textContent).toContain("?");
});

// Working is an overlay, not its own look: it layers a turning arc on idle/list/badge/question
// — whichever is already showing keeps its own count or "?" — and joins its own clause onto
// that look's accessible name.

test("working overlays idle (nothing else to show) and joins the name", () => {
  mount({ view: baseView({ state: "on", level: "idle", working: 1 }) });
  const button = screen.getByRole("button", {
    name: "VIKI, nothing needs you right now, working on 1 chat",
  });
  expect(button.className).toContain("dloop-launcher-look-idle");
  expect(button.className).toContain("dloop-launcher-working");
  expect(button.className).toContain("dloop-launcher-cue-spin");
});

test("working pluralizes its own clause: 'working on 2 chats'", () => {
  mount({ view: baseView({ state: "on", level: "idle", working: 2 }) });
  expect(
    screen.getByRole("button", { name: "VIKI, nothing needs you right now, working on 2 chats" }),
  ).toBeTruthy();
});

test("working overlays badge, keeps the count, and joins the name", () => {
  mount({ view: baseView({ state: "on", level: "badge", count: 2, working: 1 }) });
  const button = screen.getByRole("button", { name: "VIKI, 2 items need you, working on 1 chat" });
  expect(button.className).toContain("dloop-launcher-look-badge");
  expect(button.className).toContain("dloop-launcher-working");
  expect(button.textContent).toContain("2");
});

test("working overlays question, keeps the '?', and joins the name", () => {
  mount({ view: baseView({ state: "on", level: "question", count: 1, working: 3 }) });
  const button = screen.getByRole("button", {
    name: "VIKI, a question for you, working on 3 chats",
  });
  expect(button.className).toContain("dloop-launcher-look-question");
  expect(button.className).toContain("dloop-launcher-working");
  expect(button.textContent).toContain("?");
});

test("working overlays list: its brighten and the arc's spin can both play at once, on separate elements", () => {
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "list", glow: 1, working: 1 }),
  });
  rerender(
    <Launcher {...props} view={baseView({ state: "on", level: "list", glow: 2, working: 1 })} />,
  );
  const button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-cue-brighten");
  expect(button.className).toContain("dloop-launcher-cue-spin");
});

test("working is hidden while a nudge pulse is actually playing, even with a chat running", () => {
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "nudge", count: 1, nudge: 1, working: 1 }),
    typingUntilMs: 0,
  });
  rerender(
    <Launcher
      {...props}
      view={baseView({ state: "on", level: "nudge", count: 1, nudge: 2, working: 1 })}
    />,
  );
  const button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-look-nudge");
  expect(button.className).not.toContain("dloop-launcher-working");
  expect(button.getAttribute("aria-label")).toBe("VIKI, 1 item needs you");
});

test("working is hidden under alert, even with a chat running", () => {
  mount({ view: baseView({ state: "on", level: "alert", count: 3, working: 2 }) });
  const button = screen.getByRole("button", { name: "VIKI, 3 items need you" });
  expect(button.className).toContain("dloop-launcher-look-alert");
  expect(button.className).not.toContain("dloop-launcher-working");
});

test("working is hidden off, even with a chat running", () => {
  mount({ view: baseView({ state: "off", level: "idle", working: 1 }) });
  const button = screen.getByRole("button", { name: "VIKI, proactive VIKI is off" });
  expect(button.className).not.toContain("dloop-launcher-working");
});

test("working is hidden paused, even with a chat running", () => {
  mount({ view: baseView({ state: "paused", level: "idle", working: 1 }) });
  const button = screen.getByRole("button", { name: "VIKI, paused" });
  expect(button.className).not.toContain("dloop-launcher-working");
});

test("alert: a solid ring, named like badge", () => {
  mount({ view: baseView({ state: "on", level: "alert", count: 3 }) });
  const button = screen.getByRole("button", { name: "VIKI, 3 items need you" });
  expect(button.className).toContain("dloop-launcher-look-alert");
});

test("off: dimmed, named, but a click still opens the panel", () => {
  const { props } = mount({ view: baseView({ state: "off", level: "idle" }) });
  const button = screen.getByRole("button", { name: "VIKI, proactive VIKI is off" });
  expect(button.className).toContain("dloop-launcher-look-off");
  fireEvent.click(button);
  expect(props.onToggle).toHaveBeenCalledTimes(1);
});

test("paused: dimmed, named, and a click still opens the panel", () => {
  const { props } = mount({ view: baseView({ state: "paused", level: "idle" }) });
  const button = screen.getByRole("button", { name: "VIKI, paused" });
  expect(button.className).toContain("dloop-launcher-look-paused");
  fireEvent.click(button);
  expect(props.onToggle).toHaveBeenCalledTimes(1);
});

// --- aria wiring, drag/click interplay, position ---

test("aria-expanded follows open; aria-haspopup is dialog; a visible focus ring exists", () => {
  const { rerender, props } = mount({ open: false });
  expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByRole("button").getAttribute("aria-haspopup")).toBe("dialog");
  rerender(<Launcher {...props} open={true} />);
  expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe("true");
});

test("a plain click toggles the panel", () => {
  const { props } = mount();
  fireEvent.click(screen.getByRole("button"));
  expect(props.onToggle).toHaveBeenCalledTimes(1);
});

test("a click right after a drag does not toggle: endedDrag() is consulted and consumed", () => {
  const endedDrag = vi.fn(() => true);
  const { props } = mount({ position: fakePosition({ endedDrag }) });
  fireEvent.click(screen.getByRole("button"));
  expect(endedDrag).toHaveBeenCalledTimes(1);
  expect(props.onToggle).not.toHaveBeenCalled();
});

test("is positioned fixed at the hook's right/bottom", () => {
  mount({ position: fakePosition({ right: 37, bottom: 91 }) });
  const button = screen.getByRole("button");
  expect(button.style.right).toBe("37px");
  expect(button.style.bottom).toBe("91px");
});

test("every position.bind handler is wired straight onto the button", () => {
  const position = fakePosition();
  mount({ position });
  const button = screen.getByRole("button");
  fireEvent.pointerDown(button);
  expect(position.bind.onPointerDown).toHaveBeenCalledTimes(1);
  fireEvent.pointerMove(button);
  expect(position.bind.onPointerMove).toHaveBeenCalledTimes(1);
  fireEvent.pointerUp(button);
  expect(position.bind.onPointerUp).toHaveBeenCalledTimes(1);
  fireEvent.pointerCancel(button);
  expect(position.bind.onPointerCancel).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(button, { key: "ArrowLeft", altKey: true });
  expect(position.bind.onKeyDown).toHaveBeenCalledTimes(1);
});

// --- the nudge pulse: once per increase, held while typing, never on mount ---

test("a nudge increase plays one pulse immediately when not typing, then settles to badge", () => {
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "nudge", count: 2, nudge: 1 }),
    typingUntilMs: 0,
  });
  // Baseline: the starting nudge count is not itself a new one arriving.
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-badge");

  rerender(
    <Launcher {...props} view={baseView({ state: "on", level: "nudge", count: 2, nudge: 2 })} />,
  );
  let button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-look-nudge");
  expect(button.className).toContain("dloop-launcher-cue-pulse");

  advance(699);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-nudge");
  advance(1);
  button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-look-badge");
  expect(button.className).not.toContain("dloop-launcher-cue-pulse");
});

test("a nudge increase waits while typing, then plays once typing stops", () => {
  const now = Date.now();
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "nudge", count: 1, nudge: 5 }),
    typingUntilMs: now + 3000,
  });

  rerender(
    <Launcher
      {...props}
      view={baseView({ state: "on", level: "nudge", count: 1, nudge: 6 })}
      typingUntilMs={now + 3000}
    />,
  );
  // Still typing: held as the plain badge, not yet the pulse.
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-badge");

  advance(2999);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-badge");
  advance(1);
  // Typing has just stopped: the held pulse plays now.
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-nudge");

  advance(700);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-badge");
});

test("typing again before the pulse plays pushes it out instead of firing early", () => {
  const now = Date.now();
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "nudge", count: 1, nudge: 1 }),
    typingUntilMs: now + 1000,
  });
  rerender(
    <Launcher
      {...props}
      view={baseView({ state: "on", level: "nudge", count: 1, nudge: 2 })}
      typingUntilMs={now + 1000}
    />,
  );
  advance(900);
  // One more keystroke extends the hold well past the original 1000 ms mark.
  rerender(
    <Launcher
      {...props}
      view={baseView({ state: "on", level: "nudge", count: 1, nudge: 2 })}
      typingUntilMs={now + 900 + 3000}
    />,
  );
  advance(100);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-badge");
  advance(2900);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-look-nudge");
});

// --- the glow brighten: once per increase, never on mount ---

test("a glow increase brightens the list ring for 6 s, not on mount", () => {
  const { rerender, props } = mount({ view: baseView({ state: "on", level: "list", glow: 1 }) });
  expect(screen.getByRole("button").className).not.toContain("dloop-launcher-cue-brighten");

  rerender(<Launcher {...props} view={baseView({ state: "on", level: "list", glow: 2 })} />);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-cue-brighten");

  advance(5999);
  expect(screen.getByRole("button").className).toContain("dloop-launcher-cue-brighten");
  advance(1);
  expect(screen.getByRole("button").className).not.toContain("dloop-launcher-cue-brighten");
});

// --- reduced motion: the shared fade in place of a real animation; no spin at all ---

test("reduced motion turns a nudge pulse into the shared fade, never the real pulse", () => {
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "nudge", count: 1, nudge: 1 }),
    typingUntilMs: 0,
    reducedMotion: true,
  });
  rerender(
    <Launcher {...props} view={baseView({ state: "on", level: "nudge", count: 1, nudge: 2 })} />,
  );
  const button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-cue-fade");
  expect(button.className).not.toContain("dloop-launcher-cue-pulse");
});

test("reduced motion turns a glow increase into the shared fade, never the real brighten", () => {
  const { rerender, props } = mount({
    view: baseView({ state: "on", level: "list", glow: 1 }),
    reducedMotion: true,
  });
  rerender(<Launcher {...props} view={baseView({ state: "on", level: "list", glow: 2 })} />);
  const button = screen.getByRole("button");
  expect(button.className).toContain("dloop-launcher-cue-fade");
  expect(button.className).not.toContain("dloop-launcher-cue-brighten");
});

test("reduced motion shows the working arc as a still ring: no spin, no fade either", () => {
  mount({
    view: baseView({ state: "on", level: "badge", count: 1, working: 1 }),
    reducedMotion: true,
  });
  const button = screen.getByRole("button", { name: "VIKI, 1 item needs you, working on 1 chat" });
  expect(button.className).toContain("dloop-launcher-working");
  expect(button.className).not.toContain("dloop-launcher-cue-spin");
  expect(button.className).not.toContain("dloop-launcher-cue-fade");
});

// --- the optional shortcut prop: aria-keyshortcuts and the tooltip ---

test("a shortcut hint sets aria-keyshortcuts and the tooltip", () => {
  mount({ shortcut: { aria: "Alt+V", label: "Alt+V" } });
  const button = screen.getByRole("button");
  expect(button.getAttribute("aria-keyshortcuts")).toBe("Alt+V");
  expect(button.getAttribute("title")).toBe("VIKI's panel (Alt+V)");
});

test("the tooltip shows the platform label, not the aria value, when they differ", () => {
  mount({ shortcut: { aria: "Alt+V", label: "⌥V" } });
  expect(screen.getByRole("button").getAttribute("title")).toBe("VIKI's panel (⌥V)");
});

test("without a shortcut prop, neither aria-keyshortcuts nor a title is set", () => {
  mount();
  const button = screen.getByRole("button");
  expect(button.hasAttribute("aria-keyshortcuts")).toBe(false);
  expect(button.hasAttribute("title")).toBe(false);
});
