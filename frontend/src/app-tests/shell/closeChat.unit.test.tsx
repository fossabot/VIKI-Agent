/**
 * Closing a chat closes its tab: once the server applies a close from any door — a row
 * menu, or Running work's Close in the Agents view — that chat's tab leaves this window's
 * panes, as its own × would; a close the server does not apply leaves the tab where it is.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { serializeLayout } from "../../state/paneFormat";
import { moveTab, openTab, type PaneLayout, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const TITLES: Record<string, string> = { [A]: "Alpha", [B]: "Beta" };

beforeEach(() => {
  window.sessionStorage.clear();
  // Wide enough to dock both rails, so a stored two-pane layout renders as two panes.
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

/** Empty (no profiles): enough for the Agents view and Running work to render. */
function emptyProfileApi(): ProfileApi {
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [],
    get: pending,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
  };
}

function mount(): FakeSocket {
  const socket = new FakeSocket(true);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => socket,
  });
  viki.client.connect();
  const rows = [A, B].map((id) => sessionRow({ session_id: id, title: TITLES[id] ?? "" }));
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: rows })}
      durable={fakeDurable()}
      organization={organization()}
      profileApi={emptyProfileApi()}
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

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const tabIds = (scope: ParentNode = document) =>
  [...scope.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );
const selectedTab = () =>
  document
    .querySelector('.session-tab-select[aria-selected="true"]')
    ?.id.replace("session-tab-", "");

/** Opens `id`'s row menu and chooses "Close session", as the user would from either the plain
 * or the organized row menu — both wire the same button and menuitem labels. */
async function closeFromRowMenu(id: string): Promise<void> {
  const row = document.querySelector(`.sess[data-session-id="${id}"]`) as HTMLElement;
  const trigger = within(row).getByRole("button", { name: "Session menu" });
  await act(async () => fireEvent.click(trigger));
  const panel = screen.getByRole("menu");
  await act(async () =>
    fireEvent.click(within(panel).getByRole("menuitem", { name: "Close session" })),
  );
}

const closeSends = (socket: FakeSocket): string[] =>
  socket.sent
    .filter((frame) => frame.type === "session.close")
    .map((frame) => String(frame.session_id));

test("closing the selected chat from its row menu removes its tab and selects the other", async () => {
  const socket = mount();
  await openTabs([A, B]);
  expect(selectedTab()).toBe(B);
  socket.sent.length = 0;

  await closeFromRowMenu(B);
  await settled();

  expect(closeSends(socket)).toEqual([B]);
  expect(tabIds()).toEqual([A]);
  expect(selectedTab()).toBe(A);
});

test("closing an unselected chat from its row menu removes only its tab and keeps the selection", async () => {
  const socket = mount();
  await openTabs([A, B]);
  expect(selectedTab()).toBe(B);
  socket.sent.length = 0;

  await closeFromRowMenu(A);
  await settled();

  expect(closeSends(socket)).toEqual([A]);
  expect(tabIds()).toEqual([B]);
  expect(selectedTab()).toBe(B);
});

test("closing a chat whose pane it alone occupies collapses the split and keeps the other pane's selection", async () => {
  storeLayout(twoPanes());
  const socket = mount();
  await settled();
  expect(selectedTab()).toBe(B);
  socket.sent.length = 0;

  await closeFromRowMenu(A);
  await settled();

  expect(closeSends(socket)).toEqual([A]);
  expect(paneSections()).toHaveLength(0);
  expect(tabIds()).toEqual([B]);
  expect(selectedTab()).toBe(B);
});

test("a close the server does not apply sends session.close but leaves every tab and the selection as they were", async () => {
  const socket = mount();
  await openTabs([A, B]);
  socket.controlStatus = "rejected";
  socket.sent.length = 0;

  await closeFromRowMenu(B);
  await settled();

  expect(closeSends(socket)).toEqual([B]);
  expect(tabIds()).toEqual([A, B]);
  expect(selectedTab()).toBe(B);
});

test("closing a live chat with Running work's Close in the Agents view also removes its tab", async () => {
  const socket = mount();
  await openTabs([A]);
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "vault.sessions",
      session_id: "",
      seq: 1,
      data: { others: [], live: [A] },
    }),
  );

  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  fireEvent.click(await screen.findByRole("tab", { name: "Running work" }));
  const panel = screen.getByRole("tabpanel", { name: "Running work" });
  const row = await within(panel).findByText("Alpha");
  const item = row.closest("li");
  if (!item) throw new Error("no row");

  await act(async () => fireEvent.click(within(item).getByText("Close")));
  await settled();

  // Read the tab back from the chat view, where the strip renders.
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(tabIds()).toEqual([]);
});
