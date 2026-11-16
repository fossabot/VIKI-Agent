/**
 * Panes as App renders them: a stored split shows one `section.pane` per group, each with
 * its own header, strip, cards, Stop and Message box. Shell's
 * pane DOM and the layout model have their own suites; this one pins which chat each pane's
 * controls act on, what follows the focused pane, and when the panes show. It carries the cases
 * of the deleted splitApp suite and jevApp's old split case.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow, snapshotData } from "../../accept/m3b/fakes";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import { createFakeDecisionLoopApi } from "../../clients/decisionLoop/fake";
import type { DecisionLoopApi } from "../../clients/decisionLoop/types";
import { serializeLayout } from "../../state/paneFormat";
import { clearActive, focusGroup, moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const TITLES: Record<string, string> = { [A]: "Alpha", [B]: "Beta" };
/** The left pane's own generation, apart from the right one's, so an answer names which. */
const LEFT_GENERATION = "77777777-7777-4777-8777-777777777777";

/** One matchMedia for the whole App, answering `(min-width: Npx)` for a viewport of `width`,
 * able to announce a resize. */
function stubViewport(width: number) {
  let current = width;
  const listeners = new Map<string, Set<(event: MediaQueryListEvent) => void>>();
  const matches = (query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query)?.[1];
    return min !== undefined && current >= Number(min);
  };
  vi.stubGlobal("matchMedia", (query: string) => {
    return {
      media: query,
      get matches(): boolean {
        return matches(query);
      },
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        const registered = listeners.get(query) ?? new Set();
        registered.add(listener);
        listeners.set(query, registered);
      },
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        listeners.get(query)?.delete(listener);
      },
    };
  });
  return {
    resizeTo(next: number): void {
      const previous = new Map([...listeners.keys()].map((query) => [query, matches(query)]));
      current = next;
      act(() => {
        for (const [query, registered] of listeners) {
          if (previous.get(query) === matches(query)) continue;
          const event = Object.assign(new Event("change"), {
            media: query,
            matches: matches(query),
          });
          for (const listener of [...registered]) listener(event);
        }
      });
    },
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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

/** Alpha alone in the left pane, Beta in the right one, which has the focus. */
function storeTwoPanes(): void {
  const layout = moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
}

function mount(options: { width?: number; decisionApi?: DecisionLoopApi } = {}) {
  const viewport = stubViewport(options.width ?? 1440);
  const fake = fakeViki();
  const durable = fakeDurable();
  const rest = fakeRest({
    sessions: [A, B].map((id) => sessionRow({ session_id: id, title: TITLES[id] ?? "" })),
  });
  render(
    <App
      viki={fake.viki}
      rest={rest}
      durable={durable}
      organization={organization()}
      {...(options.decisionApi ? { decisionApi: options.decisionApi } : {})}
    />,
  );
  act(() => fake.socket.open());
  return { ...fake, durable, viewport };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

async function pointerDown(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.pointerDown(element);
  });
}

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const pane = (index: number) => paneSections()[index] as HTMLElement;
const focusedBox = () =>
  within(
    document.querySelector<HTMLElement>("section.pane[data-focused]") ?? document.body,
  ).getByLabelText("Message") as HTMLTextAreaElement;
const tabIdsShown = () =>
  [...document.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );
const selectedRow = () =>
  document.querySelector(".sess.sess-selected")?.getAttribute("data-session-id");
const sessionOpens = (socket: { sent: Record<string, unknown>[] }) =>
  socket.sent.filter((frame) => frame.type === "session.open");

function permission(sessionId: string, generation: string, toolUseId: string) {
  return {
    v: 2,
    generation,
    request_id: "",
    type: "permission.requested",
    session_id: sessionId,
    seq: 1,
    data: {
      tool_use_id: toolUseId,
      kind: "tool",
      tool: "Write",
      summary: `${toolUseId}.md`,
      question: "",
      reason: "",
      agent: "",
      plan: "",
    },
  };
}

function started(sessionId: string) {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "turn.started",
    session_id: sessionId,
    seq: 1,
    data: { turn_id: `turn-${sessionId}` },
  };
}

function withContext(percent: number) {
  const data = snapshotData();
  return { ...data, state: { ...data.state, context_percent: percent } };
}

// jsdom's DragEvent does not carry clientX/clientY or dataTransfer through fireEvent, so a plain
// MouseEvent stands in, as in Shell.test.tsx.
function drag(type: string, clientX: number, chatId: string): Event {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY: 400 });
  const transfer = {
    types: ["text/x-viki-tab"],
    getData: (kind: string) => (kind === "text/x-viki-tab" ? chatId : ""),
  };
  Object.defineProperty(event, "dataTransfer", { value: transfer, configurable: true });
  return event;
}

test("a stored split shows a full Message box in every pane before focus", async () => {
  storeTwoPanes();
  mount();
  await settled();

  expect(paneSections().map((section) => section.getAttribute("aria-label"))).toEqual([
    "Pane 1",
    "Pane 2",
  ]);
  expect(within(pane(0)).getByRole("tablist", { name: "Pane 1 tabs" }).textContent).toContain(
    "Alpha",
  );
  expect(within(pane(1)).getByRole("tablist", { name: "Pane 2 tabs" }).textContent).toContain(
    "Beta",
  );
  expect(tabIdsShown()).toEqual([A, B]);
  expect(screen.getAllByLabelText("Message")).toHaveLength(2);
  for (const section of paneSections()) {
    expect(section.querySelector(".composer-wrap")).not.toBeNull();
    expect(within(section).getByRole("status", { name: "Chat activity" })).toBeTruthy();
  }
  expect(focusedBox().closest("section.pane")).toBe(pane(1));
  expect(pane(1).hasAttribute("data-focused")).toBe(true);
  // The focused pane's selected tab carries the title.
  expect(pane(1).querySelector(".conversation-title")?.textContent).toBe("Beta");
});

test("each pane's card answers for its own chat and generation", async () => {
  storeTwoPanes();
  const { socket, durable } = mount();
  await settled();
  act(() => socket.synchronize(A, LEFT_GENERATION));
  act(() => {
    socket.receive(permission(A, LEFT_GENERATION, "tu-left"));
    socket.receive(permission(B, GENERATION, "tu-right"));
  });

  expect(within(pane(0)).getByText("tu-left.md")).toBeTruthy();
  expect(within(pane(0)).queryByText("tu-right.md")).toBeNull();
  expect(within(pane(1)).getByText("tu-right.md")).toBeTruthy();
  expect(within(pane(1)).queryByText("tu-left.md")).toBeNull();

  await click(within(pane(0)).getByRole("button", { name: "Allow once" }));

  await waitFor(() => expect(durable.approvals.size).toBe(1));
  expect([...durable.approvals.values()][0]).toMatchObject({
    session_id: A,
    generation: LEFT_GENERATION,
    tool_use_id: "tu-left",
    choice: { kind: "permission", allow: true },
  });
});

test("a pane's original Stop survives activation and interrupts only its own chat", async () => {
  storeTwoPanes();
  const { socket } = mount();
  await settled();
  expect(within(pane(0)).queryByRole("button", { name: "Stop" })).toBeNull();
  act(() => {
    socket.receive(started(A));
    socket.receive(started(B));
  });
  socket.sent.length = 0;

  const stop = within(pane(0)).getByRole("button", { name: "Stop" });
  await pointerDown(stop);
  expect(within(pane(0)).getByRole("button", { name: "Stop" })).toBe(stop);
  await click(stop);

  const interrupts = socket.sent.filter((frame) => frame.type === "turn.interrupt");
  expect(interrupts.map((frame) => frame.session_id)).toEqual([A]);
  expect(pane(0).hasAttribute("data-focused")).toBe(true);
  const focusedStop = within(pane(1)).getAllByRole("button", { name: "Stop" });
  expect(focusedStop).toHaveLength(1);
  expect(focusedStop[0]?.classList.contains("composer-stop")).toBe(true);
});

test("pointerdown changes shared rails while both original composers stay mounted", async () => {
  storeTwoPanes();
  const { socket } = mount();
  await settled();
  act(() => {
    socket.synchronize(A, GENERATION, 0, withContext(12));
    socket.synchronize(B, GENERATION, 0, withContext(34));
  });
  const left = within(pane(0)).getByLabelText("Message");
  const right = within(pane(1)).getByLabelText("Message");
  expect(selectedRow()).toBe(B);
  expect(screen.getByText("34% used")).toBeTruthy();

  await pointerDown(pane(0));

  expect(within(pane(0)).getByLabelText("Message")).toBe(left);
  expect(within(pane(1)).getByLabelText("Message")).toBe(right);
  expect(pane(0).hasAttribute("data-focused")).toBe(true);
  expect(selectedRow()).toBe(A);
  expect(screen.getByText("12% used")).toBeTruthy();
  expect(screen.queryByText("34% used")).toBeNull();
});

test("pointerdown in the focused pane leaves its new chat's editor in place", async () => {
  // The right pane has the focus but no active chat: its Message box is a new chat's.
  const layout = clearActive(moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" }));
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  mount();
  await settled();
  expect(focusedBox().closest("section.pane")).toBe(pane(1));
  expect(selectedRow()).toBeUndefined();

  await pointerDown(focusedBox());

  expect(selectedRow()).toBeUndefined();
  expect(focusedBox().closest("section.pane")).toBe(pane(1));
  expect(within(pane(1)).queryByRole("tab", { selected: true })).toBeNull();
});

test("an unfocused pane with no active chat shows the empty state, not an Open note", async () => {
  const split = moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
  const layout = focusGroup(clearActive(focusGroup(split, "g1")), "g2");
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  mount();
  await settled();

  expect(within(pane(0)).getByText("New conversation")).toBeTruthy();
  expect(within(pane(0)).queryByText(/isn't open in this window/)).toBeNull();
  expect(within(pane(0)).queryByRole("tab", { selected: true })).toBeNull();
  expect(focusedBox().closest("section.pane")).toBe(pane(1));
});

test("a tab dropped on another pane's strip moves into that pane", async () => {
  storeTwoPanes();
  mount();
  await settled();
  const strip = within(pane(1)).getByRole("tablist", { name: "Pane 2 tabs" });

  await act(async () => {
    fireEvent.drop(strip, {
      dataTransfer: { getData: (type: string) => (type === "text/x-viki-tab" ? A : "") },
    });
  });

  // Alpha's pane emptied and closed: one pane holds Beta, then Alpha, which it shows.
  expect(paneSections()).toHaveLength(0);
  expect(tabIdsShown()).toEqual([B, A]);
  expect(selectedRow()).toBe(A);
});

test("below 1280 px one pane shows every tab; widening restores the panes", async () => {
  storeTwoPanes();
  const { viewport } = mount();
  await settled();
  expect(paneSections()).toHaveLength(2);

  viewport.resizeTo(1279);
  expect(paneSections()).toHaveLength(0);
  expect(document.querySelectorAll('[role="tablist"].session-tabs')).toHaveLength(1);
  expect(tabIdsShown()).toEqual([A, B]);
  expect(screen.getAllByLabelText("Message")).toHaveLength(1);

  viewport.resizeTo(1280);
  expect(paneSections()).toHaveLength(2);
  expect(within(pane(0)).getByRole("tablist", { name: "Pane 1 tabs" })).toBeTruthy();
});

test("both rails dock beside two panes; compact width retains a right drawer", async () => {
  mount();
  await settled();
  // One pane: the right rail docks at this width and the top bar has no toggle for it.
  expect(document.querySelector('[data-region="right-rail"]')).not.toBeNull();
  expect(screen.queryByRole("button", { name: "VIKI panel" })).toBeNull();
  cleanup();

  storeTwoPanes();
  const { viewport } = mount();
  await settled();
  expect(paneSections()).toHaveLength(2);
  expect(document.querySelectorAll('[data-region="left-rail"]')).toHaveLength(1);
  expect(document.querySelectorAll('[data-region="right-rail"]')).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "VIKI panel" })).toBeNull();

  viewport.resizeTo(1279);
  expect(paneSections()).toHaveLength(0);
  expect(document.querySelector('[data-region="right-rail"]')).toBeNull();
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  expect(screen.getByRole("dialog", { name: "VIKI panel" })).toBeTruthy();
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  viewport.resizeTo(1280);
  expect(paneSections()).toHaveLength(2);
  expect(document.querySelectorAll('[data-region="right-rail"]')).toHaveLength(1);
});

test("a stale window shows one alert and no panes", async () => {
  storeTwoPanes();
  const { socket } = mount();
  await settled();
  expect(paneSections()).toHaveLength(2);

  act(() => socket.onclose?.({ code: 4401 }));
  await settled();

  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(paneSections()).toHaveLength(0);
});

test("a fourth start with three chats live across the panes shows the session-limit toast", async () => {
  storeTwoPanes();
  const { socket } = mount();
  await settled();
  for (let i = 0; i < 2; i++) await click(screen.getByRole("button", { name: "New session" }));
  await pointerDown(pane(0));
  await click(screen.getByRole("button", { name: "New session" }));
  // Two new chats in the right pane beside Beta, one in the left pane beside Alpha.
  expect(within(pane(0)).getAllByRole("tab")).toHaveLength(2);
  expect(within(pane(1)).getAllByRole("tab")).toHaveLength(3);
  const opened = sessionOpens(socket).length;

  await click(screen.getByRole("button", { name: "New session" }));

  expect(screen.getByRole("alert").textContent).toContain("Up to 3 sessions can run at once");
  expect(sessionOpens(socket)).toHaveLength(opened);
});

test("each chat keeps its own draft as the focus moves between panes", async () => {
  storeTwoPanes();
  mount();
  await settled();
  const box = focusedBox;
  await act(async () => {
    fireEvent.change(box(), { target: { value: "draft for Beta" } });
  });

  await pointerDown(pane(0));
  await settled();
  expect(box().closest("section.pane")).toBe(pane(0));
  expect(box().value).toBe("");

  await pointerDown(pane(1));
  await settled();
  expect(box().closest("section.pane")).toBe(pane(1));
  expect(box().value).toBe("draft for Beta");
});

test("two empty chats in two panes show no brief in either pane", async () => {
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  for (let i = 0; i < 2; i++) {
    await click(screen.getByRole("button", { name: "New session" }));
    await settled();
  }
  const first = String(sessionOpens(socket)[0]?.session_id);

  // Drag the first chat's tab onto the right edge of the one conversation.
  const main = document.querySelector("main.conversation") as HTMLElement;
  main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  act(() => {
    main.dispatchEvent(drag("dragover", 990, first));
  });
  act(() => {
    main.dispatchEvent(drag("drop", 990, first));
  });
  await settled();

  expect(paneSections()).toHaveLength(2);
  const focused = document.querySelector("section.pane[data-focused]") as HTMLElement;
  expect(focused).toBe(pane(1));
  expect(within(focused).getByRole("tab", { selected: true }).id).toBe(`session-tab-${first}`);
  expect(within(focused).queryByRole("region", { name: "Brief" })).toBeNull();
  expect(within(pane(0)).queryByRole("region", { name: "Brief" })).toBeNull();
  expect(screen.queryAllByRole("region", { name: "Brief" })).toHaveLength(0);
  expect(sessionOpens(socket)).toHaveLength(2);
});
