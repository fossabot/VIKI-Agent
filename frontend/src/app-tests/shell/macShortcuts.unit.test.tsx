/**
 * macOS shortcuts: on macOS the Ctrl chords also answer to Cmd, and
 * Option+N is matched by its physical key code because macOS turns it into a dead key. The
 * Windows and Linux chords stay exactly as the frozen tests beside this one pin them
 * (src/accept/m3b/app.accept.test.tsx, src/accept/m4s/shortcut.accept.test.tsx).
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, localIso } from "../../accept/m3b/fakes";
import { shortcutLabel } from "../../commands";
import type { Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(cleanup);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(localIso(2026, 9, 13, 8, 0)));
});

afterEach(() => {
  vi.useRealTimers();
});

function rails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 1, sources: 1, inbox: 0 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "abc1234",
      committed_at: localIso(2026, 9, 13, 7, 0),
      dirty: 0,
    },
  };
}

function mount() {
  const rest = fakeRest({ sessions: [], rails: rails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  socket.sent.length = 0;
  return { ...utils, socket };
}

function opened(socket: { sent: Record<string, unknown>[] }): string[] {
  return socket.sent
    .filter((frame) => frame.type === "session.open")
    .map((frame) => String(frame.session_id));
}

/** `navigator.platform` is read once, when App's key-handler effect installs, so it must be set
 * before `mount()` runs. jsdom exposes it through a prototype getter, so this file's own
 * property is removed afterwards rather than redefined — there is no own descriptor to restore. */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

test("on macOS, Cmd+K opens the palette", () => {
  const restore = setPlatform("MacIntel");
  try {
    mount();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  } finally {
    restore();
  }
});

test("on macOS, Cmd+, opens settings", () => {
  const restore = setPlatform("MacIntel");
  try {
    const { container } = mount();
    fireEvent.keyDown(window, { key: ",", metaKey: true });
    expect(container.querySelector('[data-region="settings"]')).not.toBeNull();
  } finally {
    restore();
  }
});

test("on macOS, Option+N opens a session even though it reports a dead key", async () => {
  const restore = setPlatform("MacIntel");
  try {
    const { socket } = mount();
    fireEvent.keyDown(window, { key: "˜", altKey: true, code: "KeyN" });
    await waitFor(() => expect(opened(socket)).toHaveLength(1));
  } finally {
    restore();
  }
});

test("on macOS, Option+N in the composer types its dead key instead of opening a session", async () => {
  const restore = setPlatform("MacIntel");
  try {
    const { socket } = mount();
    const composer = screen.getByLabelText<HTMLTextAreaElement>("Message");
    const notPrevented = fireEvent.keyDown(composer, { key: "Dead", altKey: true, code: "KeyN" });
    expect(notPrevented).toBe(true);
    expect(opened(socket)).toEqual([]);
    fireEvent.keyDown(document.body, { key: "Dead", altKey: true, code: "KeyN" });
    await waitFor(() => expect(opened(socket)).toHaveLength(1));
  } finally {
    restore();
  }
});

test("on macOS, Option+N in a contenteditable element types its dead key instead of opening a session", async () => {
  const restore = setPlatform("MacIntel");
  const editable = document.createElement("div");
  Object.defineProperty(editable, "isContentEditable", { value: true });
  try {
    const { socket } = mount();
    document.body.appendChild(editable);
    const notPrevented = fireEvent.keyDown(editable, { key: "Dead", altKey: true, code: "KeyN" });
    expect(notPrevented).toBe(true);
    expect(opened(socket)).toEqual([]);
    fireEvent.keyDown(document.body, { key: "Dead", altKey: true, code: "KeyN" });
    await waitFor(() => expect(opened(socket)).toHaveLength(1));
  } finally {
    editable.remove();
    restore();
  }
});

test("on macOS, Cmd+Shift+O opens a session, and the mac labels show the mac hints", () => {
  const restore = setPlatform("MacIntel");
  try {
    const { socket } = mount();
    fireEvent.keyDown(window, { key: "O", metaKey: true, shiftKey: true });
    expect(opened(socket)).toHaveLength(1);
    expect(screen.getByText("Search sessions").closest("button")?.textContent).toContain("⌘K");
    expect(shortcutLabel("session.new", true)).toBe("⌥N");
  } finally {
    restore();
  }
});

test("on macOS, the app menu shows the Cmd labels for Settings and both searches", () => {
  const restore = setPlatform("MacIntel");
  try {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
    const settingsRow = screen.getByText("Settings").closest('[role="menuitem"]');
    expect(settingsRow?.textContent).toContain("⌘,");
    const conversationsRow = screen.getByText("Search conversations").closest('[role="menuitem"]');
    expect(conversationsRow?.textContent).toContain("⌘K");
    const vaultRow = screen.getByText("Search the vault").closest('[role="menuitem"]');
    expect(vaultRow?.textContent).toContain("⌘⇧K");
  } finally {
    restore();
  }
});

test("on the default jsdom platform, the Cmd chords do nothing", () => {
  const { container, socket } = mount();
  fireEvent.keyDown(window, { key: "k", metaKey: true });
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  fireEvent.keyDown(window, { key: ",", metaKey: true });
  expect(container.querySelector('[data-region="settings"]')).toBeNull();
  fireEvent.keyDown(window, { key: "˜", altKey: true, code: "KeyN" });
  fireEvent.keyDown(window, { key: "O", metaKey: true, shiftKey: true });
  expect(opened(socket)).toEqual([]);
});
