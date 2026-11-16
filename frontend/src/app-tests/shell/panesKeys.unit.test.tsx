/**
 * Panes as the user drives them: a rail row dragged onto a pane, the tab menu's Split,
 * Move and Close pane, the window chords Ctrl+\, Ctrl+1–4 and Alt+W, the dimmed active tab of a
 * pane without the focus, and the hidden window title across pane and view changes. The drop zones, the
 * layout model and the strip have their own suites; this one pins what App does with them.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, sessionRow } from "../../accept/m3b/fakes";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import { CHAT_DRAG_TYPE } from "../../components/Panes/paneDrop";
import { serializeLayout } from "../../state/paneFormat";
import { focusGroup, moveTab, openTab, type PaneLayout, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const D = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const TITLES: Record<string, string> = { [A]: "Alpha", [B]: "Beta", [C]: "Gamma", [D]: "Delta" };
/** A canonical id no listing, view or tab names until a test drops it. */
const FOREIGN = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

beforeEach(() => {
  window.sessionStorage.clear();
  // Wide enough to dock both rails, so a new pane may open.
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: 1440 >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
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

/** `automatic: false` leaves every server frame to the test itself. */
function mount(options: { automatic?: boolean } = {}): FakeSocket {
  const socket = new FakeSocket(options.automatic ?? true);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => socket,
  });
  viki.client.connect();
  const rows = [A, B, C, D].map((id) => sessionRow({ session_id: id, title: TITLES[id] ?? "" }));
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: rows })}
      durable={fakeDurable()}
      organization={organization()}
    />,
  );
  act(() => socket.open());
  return socket;
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

/** Opens each chat from its rail row, in order, so each becomes a tab of the focused pane. */
async function openTabs(ids: readonly string[]): Promise<void> {
  await settled();
  for (const id of ids) {
    await act(async () => {
      fireEvent.click(document.querySelector(`.sess[data-session-id="${id}"]`) as Element);
    });
  }
}

function storeLayout(layout: PaneLayout): void {
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
}

/** Alpha alone in the left pane, Beta in the right one, which has the focus. */
function twoPanes(): PaneLayout {
  return moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
}

/** Alpha, Beta, Gamma and Delta in a row of four panes, the last one focused. */
function fourPanes(): PaneLayout {
  let layout = openTab(singlePane(), A);
  for (const [tab, group] of [
    [B, "g1"],
    [C, "g2"],
    [D, "g3"],
  ] as const)
    layout = moveTab(layout, tab, { group, zone: "right" });
  return layout;
}

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const pane = (index: number) => paneSections()[index] as HTMLElement;
const focusedBox = () =>
  within(
    document.querySelector<HTMLElement>("section.pane[data-focused]") ?? document.body,
  ).getByLabelText("Message");
const main = () => document.querySelector("main.conversation") as HTMLElement;
const tabIds = (scope: ParentNode = document) =>
  [...scope.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );
const selectedTab = () =>
  document
    .querySelector('.session-tab-select[aria-selected="true"]')
    ?.id.replace("session-tab-", "");
const opens = (socket: FakeSocket) =>
  socket.sent
    .filter((frame) => frame.type === "session.open")
    .map((frame) => [frame.session_id, frame.existing_only ?? false]);

/** jsdom lays nothing out: give `element` a box for the drop zones and the Split checks. */
function stubRect(element: HTMLElement, width: number, height: number): void {
  element.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: height, width, height, x: 0, y: 0 }) as DOMRect;
}

/** jsdom never measures the shell frame: stub it so the lone pane's row room — the frame's
 * width minus the default 272px left rail, minus 1 — comes out to exactly `room`. Split right
 * and Ctrl+\ read this, not the pane's own (still fully right-rail-docked) box. The height is
 * a tall, unconstraining 800: once a stubbed width makes the frame "measured", multi mode's own
 * height check would otherwise read an unmeasured (0) height and refuse to render any split. */
function stubRoom(room: number): void {
  const frame = document.querySelector(".shell-body") as HTMLElement;
  Object.defineProperty(frame, "clientWidth", { configurable: true, value: room + 272 + 1 });
  Object.defineProperty(frame, "clientHeight", { configurable: true, value: 800 });
  act(() => {
    window.dispatchEvent(new Event("resize"));
  });
}

/** Starts a drag on chat `id`'s rail row and returns what it carries as a chat tab. */
function dragRow(id: string): string {
  const carried = new Map<string, string>();
  fireEvent.dragStart(document.querySelector(`.sess[data-session-id="${id}"]`) as Element, {
    dataTransfer: { setData: (type: string, value: string) => carried.set(type, value) },
  });
  return carried.get(CHAT_DRAG_TYPE) ?? "";
}

// jsdom's DragEvent carries neither clientX/clientY nor dataTransfer through fireEvent, so a
// plain MouseEvent stands in, as in src/app-tests/shell/panes.unit.test.tsx.
function chatDrag(type: string, x: number, y: number, payload: string): Event {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
  const transfer = {
    types: [CHAT_DRAG_TYPE],
    getData: (kind: string) => (kind === CHAT_DRAG_TYPE ? payload : ""),
  };
  Object.defineProperty(event, "dataTransfer", { value: transfer, configurable: true });
  return event;
}

/** Drags `payload` over `target` at (x, y), then drops it there. */
async function dropAt(target: Element, x: number, y: number, payload: string): Promise<void> {
  act(() => {
    target.dispatchEvent(chatDrag("dragover", x, y, payload));
  });
  await act(async () => {
    target.dispatchEvent(chatDrag("drop", x, y, payload));
  });
}

/** Opens `title`'s tab menu and returns its items as label and disabled. */
async function openTabMenu(title: string): Promise<{ label: string; disabled: boolean }[]> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: `Tab actions: ${title}` }));
  });
  return screen.getAllByRole("menuitem").map((item) => ({
    label: item.textContent ?? "",
    disabled: item.getAttribute("aria-disabled") === "true",
  }));
}

async function choose(label: string): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: label }));
  });
}

/** Returns false when the key was cancelled (default prevented), as `fireEvent` does. */
function press(target: Element, init: KeyboardEventInit): boolean {
  let notPrevented = true;
  act(() => {
    notPrevented = fireEvent.keyDown(target, init);
  });
  return notPrevented;
}

const altW = { key: "w", code: "KeyW", altKey: true };

test("a rail row dropped on the right edge of the one pane opens a second pane with it, focused", async () => {
  mount();
  await openTabs([A]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);

  const payload = dragRow(B);
  expect(payload).toBe(B);
  await dropAt(main(), 990, 400, payload);
  await settled();

  expect(paneSections()).toHaveLength(2);
  expect(tabIds(pane(0))).toEqual([A]);
  expect(tabIds(pane(1))).toEqual([B]);
  expect(pane(1).hasAttribute("data-focused")).toBe(true);
  expect(selectedTab()).toBe(B);
});

test("a rail row dropped in a pane's centre moves its tab into that pane", async () => {
  storeLayout(twoPanes());
  mount();
  await settled();
  stubRect(pane(1), 600, 800);

  await dropAt(pane(1), 300, 400, dragRow(A));

  // Alpha's pane emptied and closed: one pane holds Beta, then Alpha, which it shows.
  expect(paneSections()).toHaveLength(0);
  expect(tabIds()).toEqual([B, A]);
  expect(selectedTab()).toBe(A);
});

test("a dropped chat this window does not know opens existing-only and drops silently when refused", async () => {
  const socket = mount({ automatic: false });
  await openTabs([A]);
  act(() => socket.synchronize(A));
  stubRect(main(), 1000, 800);

  await dropAt(main(), 500, 400, FOREIGN);

  expect(opens(socket)).toEqual([
    [A, false],
    [FOREIGN, true],
  ]);
  expect(tabIds()).toEqual([A, FOREIGN]);
  const request = socket.sent.find(
    (frame) => frame.type === "session.open" && frame.session_id === FOREIGN,
  );
  await act(async () => {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: FOREIGN,
      generation: "",
      request_id: request?.request_id,
      seq: 0,
      data: { reason: "unavailable" },
    });
  });

  expect(tabIds()).toEqual([A]);
  expect(selectedTab()).toBe(A);
  expect(screen.queryByText("This conversation is unavailable.")).toBeNull();
});

test("a drop whose payload is not a canonical chat id does nothing", async () => {
  const socket = mount();
  await openTabs([A]);
  stubRect(main(), 1000, 800);
  socket.sent.length = 0;

  for (const payload of ["not-a-chat", FOREIGN.toUpperCase(), ""])
    await dropAt(main(), 990, 400, payload);

  expect(opens(socket)).toEqual([]);
  expect(tabIds()).toEqual([A]);
  expect(paneSections()).toHaveLength(0);
});

test("at four panes a pane's edge offers no zone: a drop there lands in its centre", async () => {
  storeLayout(fourPanes());
  mount();
  await settled();
  expect(paneSections()).toHaveLength(4);
  stubRect(pane(0), 1000, 800);

  act(() => {
    pane(0).dispatchEvent(chatDrag("dragover", 990, 400, D));
  });
  expect(pane(0).querySelector(".pane-drop")?.getAttribute("data-zone")).toBe("center");
  await act(async () => {
    pane(0).dispatchEvent(chatDrag("drop", 990, 400, D));
  });

  expect(paneSections()).toHaveLength(3);
  expect(tabIds(pane(0))).toEqual([A, D]);
  expect(selectedTab()).toBe(D);
});

test.each([
  ["Split right", "row"],
  ["Split down", "column"],
] as const)("the tab menu's %s moves the tab into a new pane, focused", async (label, dir) => {
  mount();
  await openTabs([A, B]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);

  // One pane: nothing to move to and no pane to close.
  expect(await openTabMenu("Beta")).toEqual([
    { label: "Split right", disabled: false },
    { label: "Split down", disabled: false },
  ]);
  await choose(label);

  expect(document.querySelector(`.pane-split[data-dir="${dir}"]`)).not.toBeNull();
  expect(tabIds(pane(0))).toEqual([A]);
  expect(tabIds(pane(1))).toEqual([B]);
  expect(pane(1).hasAttribute("data-focused")).toBe(true);
  expect(selectedTab()).toBe(B);
});

test("Split is disabled for a pane's only tab and for a pane too small to halve", async () => {
  mount();
  await openTabs([A]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);
  expect((await openTabMenu("Alpha")).map((item) => item.disabled)).toEqual([true, true]);
  cleanup();
  window.sessionStorage.clear();

  mount();
  await openTabs([A, B]);
  // Under two pane floors on either axis: one short of the 840-wide row room, and the pane's
  // own box (unchanged for the column axis) one short of 480 tall.
  stubRect(main(), 839, 479);
  stubRoom(839);
  expect((await openTabMenu("Beta")).map((item) => item.disabled)).toEqual([true, true]);
});

test("Split right and Ctrl+\\ work from a lone pane whose own box is still narrow, once the frame leaves the row room a split would actually give it", async () => {
  mount();
  await openTabs([A, B]);
  // Both rails still docked over the one pane, same as a sub-1432px window: its own box reads
  // narrower than the 840 floor, but the frame is wide enough that undocking the right rail for
  // a split would leave each half that much room.
  stubRect(main(), 700, 800);
  stubRoom(1000);

  expect((await openTabMenu("Beta")).map((item) => item.disabled)).toEqual([false, false]);
  expect(press(document.body, { key: "\\", code: "Backslash", ctrlKey: true })).toBe(false);

  expect(document.querySelector('.pane-split[data-dir="row"]')).not.toBeNull();
  expect(selectedTab()).toBe(B);
});

test("at two panes Split right still reads the pane's own box, not the frame's room", async () => {
  // Alpha and Gamma share the left pane; Beta alone on the right, focused.
  storeLayout(moveTab(openTab(openTab(singlePane(), A), C), B, { group: "g1", zone: "right" }));
  mount();
  await settled();
  // Plenty of frame room, but the per-pane rect rule still gates a 2+-group layout.
  stubRoom(2000);
  stubRect(pane(0), 600, 800);

  expect(await openTabMenu("Alpha")).toEqual([
    { label: "Split right", disabled: true },
    { label: "Split down", disabled: false },
    { label: "Move to pane 2", disabled: false },
    { label: "Close pane", disabled: false },
  ]);
});

test("at four panes Split is disabled, and every other pane is a Move target", async () => {
  storeLayout(fourPanes());
  mount();
  await settled();
  stubRect(pane(0), 1000, 800);

  expect(await openTabMenu("Alpha")).toEqual([
    { label: "Split right", disabled: true },
    { label: "Split down", disabled: true },
    { label: "Move to pane 2", disabled: false },
    { label: "Move to pane 3", disabled: false },
    { label: "Move to pane 4", disabled: false },
    { label: "Close pane", disabled: false },
  ]);
});

test("Move to pane N moves the tab into that pane and shows it there", async () => {
  storeLayout(moveTab(twoPanes(), C, { group: "g2", zone: "right" }));
  mount();
  await settled();
  expect(paneSections()).toHaveLength(3);

  expect((await openTabMenu("Alpha")).map((item) => item.label)).toEqual([
    "Split right",
    "Split down",
    "Move to pane 2",
    "Move to pane 3",
    "Close pane",
  ]);
  await choose("Move to pane 3");

  // Alpha's pane emptied and closed; the last pane holds Gamma, then Alpha, which it shows.
  expect(paneSections()).toHaveLength(2);
  expect(tabIds(pane(1))).toEqual([C, A]);
  expect(pane(1).hasAttribute("data-focused")).toBe(true);
  expect(selectedTab()).toBe(A);
});

test("Close pane closes every tab in it, shows the next pane's chat and keeps the chats running", async () => {
  // Alpha and Gamma (active) in the left pane, Beta alone in the right one, which is focused.
  storeLayout(moveTab(openTab(openTab(singlePane(), A), C), B, { group: "g1", zone: "right" }));
  const socket = mount();
  await settled();
  expect(selectedTab()).toBe(B);
  socket.sent.length = 0;

  await openTabMenu("Beta");
  await choose("Close pane");

  expect(paneSections()).toHaveLength(0);
  expect(tabIds()).toEqual([A, C]);
  expect(selectedTab()).toBe(C);
  expect(socket.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
  expect(document.querySelector(`.sess[data-session-id="${B}"]`)?.classList).toContain("sess-live");
});

test.each(["Backslash", "IntlYen", "IntlRo"])(
  "Ctrl+\\ splits the selected tab right, read by key so %s works",
  async (code) => {
    mount();
    await openTabs([A, B]);
    stubRect(main(), 1000, 800);
    stubRoom(1000);

    expect(press(document.body, { key: "\\", code, ctrlKey: true })).toBe(false);

    expect(document.querySelector('.pane-split[data-dir="row"]')).not.toBeNull();
    expect(tabIds(pane(1))).toEqual([B]);
    expect(pane(1).hasAttribute("data-focused")).toBe(true);
    expect(selectedTab()).toBe(B);
  },
);

test("Ctrl+\\ with nothing to split is not cancelled", async () => {
  mount();
  await openTabs([A]);
  stubRect(main(), 1000, 800);
  stubRoom(1000);

  expect(press(document.body, { key: "\\", code: "Backslash", ctrlKey: true })).toBe(true);

  expect(paneSections()).toHaveLength(0);
});

test("a backslash typed with AltGr, which Windows reports as Ctrl+Alt, stays typing", async () => {
  mount();
  await openTabs([A, B]);
  stubRect(main(), 1000, 800);

  // A German layout types "\" as AltGr+ß.
  const typed = { key: "\\", code: "Minus", ctrlKey: true, altKey: true };
  expect(press(focusedBox(), typed)).toBe(true);

  expect(paneSections()).toHaveLength(0);
});

test("Ctrl+2 focuses the second pane and puts the keyboard on its tab", async () => {
  storeLayout(focusGroup(twoPanes(), "g1"));
  mount();
  await settled();
  expect(selectedTab()).toBe(A);

  expect(press(document.body, { key: "2", code: "Digit2", ctrlKey: true })).toBe(false);

  expect(pane(1).hasAttribute("data-focused")).toBe(true);
  expect(selectedTab()).toBe(B);
  expect(document.activeElement).toBe(document.getElementById(`session-tab-${B}`));
  // No third pane, and Shift or Alt make it another chord: none is cancelled.
  expect(press(document.body, { key: "3", code: "Digit3", ctrlKey: true })).toBe(true);
  expect(press(document.body, { key: "1", code: "Digit1", ctrlKey: true, shiftKey: true })).toBe(
    true,
  );
  expect(press(document.body, { key: "1", code: "Digit1", ctrlKey: true, altKey: true })).toBe(
    true,
  );
  expect(pane(1).hasAttribute("data-focused")).toBe(true);
});

test("Alt+W closes the focused pane's selected tab, also from the Message box on Windows", async () => {
  mount();
  await openTabs([A, B]);

  expect(press(focusedBox(), altW)).toBe(false);

  expect(tabIds()).toEqual([A]);
  expect(selectedTab()).toBe(A);
});

test("Alt+W leaves a pinned tab, ignores a held key and does nothing under a modal", async () => {
  mount();
  await openTabs([A, B]);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Pin: Beta" }));
  });
  expect(press(document.body, altW)).toBe(true);
  expect(tabIds()).toEqual([B, A]);

  await act(async () => {
    fireEvent.click(document.getElementById(`session-tab-${A}`) as HTMLElement);
  });
  expect(press(document.body, { ...altW, repeat: true })).toBe(true);
  const modal = document.createElement("div");
  modal.setAttribute("aria-modal", "true");
  document.body.append(modal);
  try {
    expect(press(document.body, altW)).toBe(true);
  } finally {
    modal.remove();
  }
  expect(tabIds()).toEqual([B, A]);

  expect(press(document.body, altW)).toBe(false);
  expect(tabIds()).toEqual([B]);
});

test("on macOS Option+W in the Message box types instead; elsewhere it closes the tab", async () => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  mount();
  await openTabs([A, B]);

  expect(press(focusedBox(), { ...altW, key: "∑" })).toBe(true);
  expect(tabIds()).toEqual([A, B]);

  expect(press(document.body, { ...altW, key: "∑" })).toBe(false);
  expect(tabIds()).toEqual([A]);
});

test("Alt+2 works while a tab's menu button has focus", async () => {
  mount();
  await openTabs([A, B, C]);
  const menu = screen.getByRole("button", { name: "Tab actions: Gamma" });
  menu.focus();

  press(menu, { key: "2", code: "Digit2", altKey: true });

  expect(selectedTab()).toBe(B);
});

test("a pane without the focus marks its own active tab, which is not the selected one", async () => {
  storeLayout(twoPanes());
  mount();
  await settled();

  const left = within(pane(0)).getByRole("tab", { name: /Alpha/ });
  expect(left.closest(".session-tab")?.getAttribute("data-active")).toBe("true");
  expect(left.getAttribute("aria-selected")).toBe("false");
  const right = within(pane(1)).getByRole("tab", { name: /Beta/ });
  expect(right.closest(".session-tab")?.hasAttribute("data-active")).toBe(false);
  expect(right.getAttribute("aria-selected")).toBe("true");
});

test("the window title stays visually blank when focus or view changes", async () => {
  const base = "\u2060";
  document.title = base;
  storeLayout(twoPanes());
  mount();
  await settled();
  expect(document.title).toBe(base);

  await act(async () => {
    fireEvent.pointerDown(pane(0));
  });
  expect(document.title).toBe(base);

  await act(async () => {
    fireEvent.keyDown(document.body, { key: ",", ctrlKey: true });
  });
  await settled();
  expect(document.title).toBe(base);
});
