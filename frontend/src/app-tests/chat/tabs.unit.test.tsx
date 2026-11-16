/**
 * Tab pins, order, the stored pane layout and the Alt+digit keys as App wires them. The tab
 * strip, the pure layout model and the pin button have their own suites; this one pins what App
 * passes them, which chats it opens for a stored layout, and the one window-level key listener.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { Rest } from "../../api";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { SessionHandoff } from "../../sessionHandoff";
import { parseLayout, serializeLayout } from "../../state/paneFormat";
import { groups, moveTab, openTab, type PaneLayout, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";

const IDS = [
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
] as const;
const [A, B, C] = IDS;
const TITLES: Record<string, string> = { [A]: "Alpha", [B]: "Beta", [C]: "Gamma" };
/** A canonical session id no listing, view or tab names until a test drops it. */
const D = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (window.navigator as { platform?: string }).platform;
  window.sessionStorage.clear();
  window.localStorage.clear();
});

function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

/** The sessions listing: `ids` as rows, complete or (`partial`) with one transcript left out. */
function listing(ids: readonly string[] = IDS, partial = false): Rest {
  const rows = ids.map((id) => sessionRow({ session_id: id, title: TITLES[id] ?? "" }));
  const rest = fakeRest({ sessions: rows });
  return partial
    ? {
        ...rest,
        sessions: async () => ({
          sessions: rows,
          state: "partial",
          reason: "time_budget",
          omitted: 1,
        }),
      }
    : rest;
}

interface MountOptions {
  rest?: Rest;
  /** false: every server frame is delivered by the test itself. */
  automatic?: boolean;
  sessionHandoff?: SessionHandoff;
}

function mount(options: MountOptions = {}) {
  const socket = new FakeSocket(options.automatic ?? true);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => socket,
  });
  viki.client.connect();
  render(
    <App
      viki={viki}
      rest={options.rest ?? listing()}
      durable={fakeDurable()}
      organization={organization()}
      {...(options.sessionHandoff ? { sessionHandoff: options.sessionHandoff } : {})}
    />,
  );
  act(() => socket.open());
  return { viki, socket };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

/** Opens each session from its rail row, in the given order, so each becomes a tab. */
async function openTabs(ids: readonly string[] = IDS): Promise<void> {
  await settled();
  for (const id of ids) {
    const row = document.querySelector(`.sess[data-session-id="${id}"]`);
    await act(async () => {
      fireEvent.click(row as Element);
    });
  }
}

/** A window at least `width` px wide, as far as every `(min-width: Npx)` query asks. */
function stubViewport(width: number): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: width >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

// usePanes' mirror of the layout: seeded into both browser storages and read from either, so
// these tests hold whichever one it keeps the layout in.
function storeLayout(layout: PaneLayout): void {
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  window.localStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
}
function storedLayout(): PaneLayout | null {
  return parseLayout(
    window.sessionStorage.getItem(PANES_STORAGE_KEY) ??
      window.localStorage.getItem(PANES_STORAGE_KEY),
  );
}

/** Alpha alone in the left pane, Beta in the right one, which has the focus. */
function twoPanes(): PaneLayout {
  return moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
}

const opens = (socket: FakeSocket) =>
  socket.sent
    .filter((frame) => frame.type === "session.open")
    .map((frame) => [frame.session_id, frame.existing_only ?? false]);
const tabIdsShown = () =>
  [...document.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );
const selectedTab = () =>
  document
    .querySelector('.session-tab-select[aria-selected="true"]')
    ?.id.replace("session-tab-", "");
const pinButton = (id: string) => screen.getByRole("button", { name: `Pin: ${TITLES[id]}` });
const closeButton = (id: string) => screen.queryByRole("button", { name: `Close: ${TITLES[id]}` });

/** Returns false when the key was cancelled (default prevented), as `fireEvent` does. */
const altDigit = (digit: number, target: Element | Window = document.body) =>
  fireEvent.keyDown(target, { key: String(digit), code: `Digit${digit}`, altKey: true });

/** Drops a tab drag carrying `id` on the strip itself, after its last tab. */
async function dropOnStrip(id: string): Promise<void> {
  const strip = document.querySelector('[role="tablist"].session-tabs') as Element;
  await act(async () => {
    fireEvent.drop(strip, {
      dataTransfer: { getData: (type: string) => (type === "text/x-viki-tab" ? id : "") },
    });
  });
}

test("pinning a tab moves it first and removes its close button", async () => {
  mount();
  await openTabs();
  expect(tabIdsShown()).toEqual([A, B, C]);
  expect(closeButton(C)).not.toBeNull();

  await act(async () => {
    fireEvent.click(pinButton(C));
  });

  expect(tabIdsShown()).toEqual([C, A, B]);
  expect(pinButton(C).getAttribute("aria-pressed")).toBe("true");
  expect(closeButton(C)).toBeNull();
  expect(closeButton(A)).not.toBeNull();
});

test("pins and order survive a remount through viki.panes", async () => {
  mount();
  await openTabs();
  await act(async () => {
    fireEvent.click(pinButton(B));
  });
  await act(async () => {
    fireEvent.keyDown(document.getElementById(`session-tab-${C}`) as HTMLElement, {
      key: "ArrowLeft",
      ctrlKey: true,
      shiftKey: true,
    });
  });
  expect(tabIdsShown()).toEqual([B, C, A]);
  expect(storedLayout()?.pinned).toEqual([B]);

  cleanup();
  mount();
  await settled();

  expect(tabIdsShown()).toEqual([B, C, A]);
  expect(pinButton(B).getAttribute("aria-pressed")).toBe("true");
  expect(selectedTab()).toBe(C);
});

test("Ctrl+Shift+Arrow on a tab reorders the strip and remembers the order", async () => {
  mount();
  await openTabs();
  const tab = document.getElementById(`session-tab-${C}`) as HTMLElement;

  await act(async () => {
    fireEvent.keyDown(tab, { key: "ArrowLeft", ctrlKey: true, shiftKey: true });
  });

  expect(tabIdsShown()).toEqual([A, C, B]);
  const stored = storedLayout();
  expect(stored && groups(stored).flatMap((group) => group.tabs)).toEqual([A, C, B]);
});

test("a closed chat's tab stays and reopens existing-only", async () => {
  const { socket } = mount();
  await openTabs();
  act(() =>
    socket.receive({
      v: 2,
      type: "session.closed",
      session_id: B,
      generation: GENERATION,
      request_id: "",
      seq: 1,
      data: { reason: "requested" },
    }),
  );
  expect(tabIdsShown()).toEqual([A, B, C]);
  socket.sent.length = 0;

  await act(async () => {
    fireEvent.click(document.getElementById(`session-tab-${B}`) as HTMLElement);
  });

  expect(opens(socket)).toEqual([[B, true]]);
  expect(selectedTab()).toBe(B);
});

test("a stored two-pane layout opens each pane's chat existing-only once and selects the focused one", async () => {
  storeLayout(twoPanes());
  const { socket } = mount();
  await settled();

  expect(opens(socket)).toEqual([
    [A, true],
    [B, true],
  ]);
  expect(tabIdsShown()).toEqual([A, B]);
  expect(selectedTab()).toBe(B);
});

test("a restored chat the server refuses drops its tab without an unavailable view", async () => {
  storeLayout(twoPanes());
  const { socket } = mount({ automatic: false });
  await settled();
  expect(selectedTab()).toBe(B);
  const request = socket.sent.find(
    (frame) => frame.type === "session.open" && frame.session_id === B,
  );

  await act(async () => {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: B,
      generation: "",
      request_id: request?.request_id,
      seq: 0,
      data: { reason: "unavailable" },
    });
  });

  expect(tabIdsShown()).toEqual([A]);
  expect(selectedTab()).toBe(A);
  expect(screen.queryByText("This conversation is unavailable.")).toBeNull();
});

test("a tab whose chat a complete listing lacks drops, and a partial listing drops none", async () => {
  // Gamma is a dormant tab: only the pane's active chat, Alpha, is opened.
  storeLayout(openTab(openTab(singlePane(), C), A));
  mount({ rest: listing([A, B], true) });
  await settled();
  expect(tabIdsShown()).toEqual([C, A]);

  cleanup();
  mount({ rest: listing([A, B]) });
  await settled();

  expect(tabIdsShown()).toEqual([A]);
});

test("a handoff window ignores a stored layout and adds its target tab only after sync", async () => {
  storeLayout(twoPanes());
  const { socket } = mount({
    automatic: false,
    sessionHandoff: { kind: "target", sessionId: C },
  });
  await settled();
  expect(tabIdsShown()).toEqual([]);
  expect(opens(socket)).toEqual([[C, true]]);

  await act(async () => {
    socket.synchronize(C);
  });

  expect(tabIdsShown()).toEqual([C]);
  expect(selectedTab()).toBe(C);
});

test("a dropped chat this window does not hold opens existing-only; a non-canonical id does nothing", async () => {
  const { socket } = mount();
  await openTabs([A]);
  socket.sent.length = 0;

  await dropOnStrip("DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD");
  expect(opens(socket)).toEqual([]);
  expect(tabIdsShown()).toEqual([A]);

  await dropOnStrip(D);
  expect(opens(socket)).toEqual([[D, true]]);
  expect(tabIdsShown()).toEqual([A, D]);
  expect(selectedTab()).toBe(D);
});

test("Alt+2 selects the second tab", async () => {
  mount();
  await openTabs();
  expect(selectedTab()).toBe(C);

  const notPrevented = altDigit(2);

  expect(notPrevented).toBe(false);
  expect(selectedTab()).toBe(B);
});

test("Alt+digit follows the displayed order, pinned tabs first", async () => {
  mount();
  await openTabs();
  await act(async () => {
    fireEvent.click(pinButton(C));
  });
  expect(tabIdsShown()).toEqual([C, A, B]);

  altDigit(2);

  expect(selectedTab()).toBe(A);
});

test("Alt+9 selects the last tab", async () => {
  mount();
  await openTabs();
  altDigit(1);
  expect(selectedTab()).toBe(A);

  altDigit(9);

  expect(selectedTab()).toBe(C);
});

test("with two panes in a wide window, Alt+digit counts the focused pane's tabs", async () => {
  stubViewport(1440);
  storeLayout(twoPanes());
  mount();
  await settled();
  expect(selectedTab()).toBe(B);

  // Every tab in order reads Alpha, Beta; the focused pane holds Beta alone.
  expect(altDigit(1)).toBe(false);
  expect(selectedTab()).toBe(B);
  expect(altDigit(2)).toBe(true);
  expect(selectedTab()).toBe(B);
});

test("with two panes in a narrow window, Alt+digit counts every tab", async () => {
  stubViewport(1000);
  storeLayout(twoPanes());
  mount();
  await settled();
  expect(selectedTab()).toBe(B);

  altDigit(1);

  expect(selectedTab()).toBe(A);
});

test("Alt+digit inside a textarea, an input or a select does nothing", async () => {
  mount();
  await openTabs();
  const input = document.createElement("input");
  const select = document.createElement("select");
  document.body.append(input, select);
  try {
    const targets: Element[] = [screen.getByLabelText("Message"), input, select];
    for (const target of targets) {
      const notPrevented = altDigit(1, target);
      expect(notPrevented).toBe(true);
      expect(selectedTab()).toBe(C);
    }
  } finally {
    input.remove();
    select.remove();
  }
});

test("Alt+digit with a modal open does nothing", async () => {
  mount();
  await openTabs();
  const modal = document.createElement("div");
  modal.setAttribute("aria-modal", "true");
  document.body.append(modal);
  try {
    const notPrevented = altDigit(1);
    expect(notPrevented).toBe(true);
    expect(selectedTab()).toBe(C);
  } finally {
    modal.remove();
  }
});

test("Alt+digit past the last tab does nothing and is not cancelled", async () => {
  mount();
  await openTabs([A, B]);
  expect(selectedTab()).toBe(B);

  const notPrevented = altDigit(5);

  expect(notPrevented).toBe(true);
  expect(selectedTab()).toBe(B);
});

test("Alt+2 works while a tab's pin button has focus", async () => {
  mount();
  await openTabs();
  const pin = pinButton(C);
  pin.focus();

  altDigit(2, pin);

  expect(selectedTab()).toBe(B);
});

test("Alt+2 works in a wide window while a tab's pin button has focus", async () => {
  stubViewport(1440);
  mount();
  await openTabs();
  const pin = pinButton(A);
  pin.focus();

  altDigit(2, pin);

  expect(selectedTab()).toBe(B);
});

test("Escape on a focused close button does not reach App's Escape action", async () => {
  mount();
  await openTabs();
  // The tab strip is not shown outside the chat view, so App's Escape action cannot be observed
  // through a view change; what App's window listener would see is.
  const seen: string[] = [];
  const spy = (event: KeyboardEvent) => seen.push(event.key);
  window.addEventListener("keydown", spy);
  try {
    const close = closeButton(A) as HTMLElement;
    close.focus();

    fireEvent.keyDown(close, { key: "Escape" });
    expect(seen).toEqual([]);

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(seen).toEqual(["Escape"]);
  } finally {
    window.removeEventListener("keydown", spy);
  }
});

test("Alt+N opens a new session while a tab's pin button has focus", async () => {
  const fake = mount();
  await openTabs();
  const pin = pinButton(C);
  pin.focus();
  fake.socket.sent.length = 0;

  const notPrevented = fireEvent.keyDown(pin, { key: "n", code: "KeyN", altKey: true });

  expect(notPrevented).toBe(false);
  expect(fake.socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(1);
});

test("Alt+2 in the Settings view does nothing and is not cancelled", async () => {
  mount();
  await openTabs();
  expect(window.sessionStorage.getItem("viki.selected")).toBe(C);
  await act(async () => {
    fireEvent.keyDown(document.body, { key: ",", ctrlKey: true });
  });
  await settled();
  expect(document.querySelector(".session-tabs")).toBeNull();

  const notPrevented = altDigit(2);

  expect(notPrevented).toBe(true);
  expect(window.sessionStorage.getItem("viki.selected")).toBe(C);
});

test("Alt+N still opens a new session", async () => {
  const fake = mount();
  await openTabs();
  fake.socket.sent.length = 0;

  const notPrevented = fireEvent.keyDown(document.body, { key: "n", code: "KeyN", altKey: true });

  expect(notPrevented).toBe(false);
  expect(fake.socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(1);
});
