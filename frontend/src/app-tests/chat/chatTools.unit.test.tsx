/**
 * The chat header's search button and Ctrl+F: the banner's old <details> "Find,
 * select and export" row is now a panel the header's search button toggles in place, and Ctrl+F
 * opens it focused on its search field — everywhere except where the browser's own find must
 * run instead: composing, the code views, an event an earlier listener already spoke for, no
 * chat selected, or outside the chat view.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App, type AppProps } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type { ChatArchivesApi } from "../../clients/conversations/chatArchivesApi";
import type { ConversationSearchApi } from "../../clients/conversations/conversationSearchApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import type { ReadOnlyConversationArchive } from "../../context/conversationTools";
import { serializeLayout } from "../../state/paneFormat";
import { moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

const SAVED_ARCHIVE: ReadOnlyConversationArchive = {
  formatVersion: 1,
  kind: "viki-conversation-archive",
  readOnly: true,
  session: { sessionId: "saved-session", title: "Saved copy" },
  turns: [],
};

function fakeArchives(): ChatArchivesApi {
  return {
    list: vi.fn(async () => [
      { archive_id: "a1", created_at: "2026-10-20T00:00:00.000Z", byte_size: 1024 },
    ]),
    get: vi.fn(async () => SAVED_ARCHIVE),
    save: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
  };
}
function fakeAssets(): AssetsApi {
  return { upload: vi.fn(), read: vi.fn() };
}
// Never settles: these tests check that the panel and focus land correctly, not what a real
// search would return.
function pendingSearch(): ConversationSearchApi {
  return {
    search: vi.fn<ConversationSearchApi["search"]>(() => new Promise(() => {})),
    anchor: vi.fn<ConversationSearchApi["anchor"]>(() => new Promise(() => {})),
  };
}
function toolsProps(): Partial<AppProps> {
  return { archives: fakeArchives(), assets: fakeAssets(), conversationSearch: pendingSearch() };
}

async function mount(extra: Partial<AppProps> = {}) {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const view = render(<App viki={viki} rest={rest} durable={fakeDurable()} {...extra} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  return { ...view };
}

/** Creates and selects a new chat, the same way the user's own click would, so the header
 * carries the search button and Ctrl+F has a chat view to act on. */
function selectChat(): void {
  fireEvent.click(screen.getByText("New session"));
}

function searchButton(): HTMLElement {
  return screen.getByRole("button", { name: "Find, select and export (Ctrl+F)" });
}
function panelOf(container: HTMLElement): HTMLElement {
  const found = container.querySelector<HTMLElement>(".conversation-tools");
  if (!found) throw new Error("No tools panel");
  return found;
}
/** A detached stand-in for CodePreview's/CodeEditor's own root element: findRef only cares about
 * the class `.closest` sees on the event's target, not a real editor. */
function codeLikeTarget(className: string): HTMLElement {
  const target = document.createElement("div");
  target.className = className;
  document.body.appendChild(target);
  return target;
}

test("the search button toggles the panel and reports it through aria-expanded, aria-controls and hidden", async () => {
  const { container } = await mount(toolsProps());
  const button = searchButton();
  const panel = panelOf(container);
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(button.getAttribute("aria-controls")).toBe(panel.id);
  expect(panel.hidden).toBe(true);

  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(panel.hidden).toBe(false);

  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(panel.hidden).toBe(true);
});

test("no search button appears without archives and assets", async () => {
  await mount();
  expect(screen.queryByRole("button", { name: "Find, select and export (Ctrl+F)" })).toBeNull();
});

test("opening a saved copy shows the search button in the archive header", async () => {
  const { container } = await mount(toolsProps());
  const importButton = container.querySelector<HTMLButtonElement>(".archive-tools li button");
  if (!importButton) throw new Error("No saved-copy row");
  fireEvent.click(importButton);
  await waitFor(() => expect(screen.getByRole("button", { name: "Back to chat" })).toBeTruthy());
  expect(searchButton()).toBeTruthy();
});

test("Ctrl+F on a selected chat opens the panel focused on its search field; Escape closes it and returns focus to the button", async () => {
  const { container } = await mount(toolsProps());
  selectChat();
  const panel = panelOf(container);

  const notDefaulted = fireEvent.keyDown(window, { key: "f", ctrlKey: true });
  expect(notDefaulted).toBe(false);
  expect(panel.hidden).toBe(false);
  expect(document.activeElement).toBe(panel.querySelector(".conversation-search input"));

  fireEvent.keyDown(panel, { key: "Escape" });
  expect(panel.hidden).toBe(true);
  expect(document.activeElement).toBe(searchButton());
});

test("no chat selected leaves Ctrl+F to the browser", async () => {
  const { container } = await mount(toolsProps());
  const panel = panelOf(container);
  const notPrevented = fireEvent.keyDown(window, { key: "f", ctrlKey: true });
  expect(notPrevented).toBe(true);
  expect(panel.hidden).toBe(true);
  expect(searchButton().getAttribute("aria-expanded")).toBe("false");
});

test("composing, the code views and the Settings view all leave Ctrl+F to the browser", async () => {
  const { container } = await mount(toolsProps());
  selectChat();
  const panel = panelOf(container);

  expect(fireEvent.keyDown(window, { key: "f", ctrlKey: true, keyCode: 229 })).toBe(true);
  expect(panel.hidden).toBe(true);

  const editor = codeLikeTarget("code-editor");
  try {
    expect(fireEvent.keyDown(editor, { key: "f", ctrlKey: true })).toBe(true);
    expect(panel.hidden).toBe(true);
  } finally {
    editor.remove();
  }

  const preview = codeLikeTarget("code-preview");
  try {
    expect(fireEvent.keyDown(preview, { key: "f", ctrlKey: true })).toBe(true);
    expect(panel.hidden).toBe(true);
  } finally {
    preview.remove();
  }

  fireEvent.keyDown(window, { key: ",", ctrlKey: true });
  expect(fireEvent.keyDown(window, { key: "f", ctrlKey: true })).toBe(true);
  expect(panel.hidden).toBe(true);
});

test("an already-prevented Ctrl+F is left alone", async () => {
  const earlier = (event: Event) => event.preventDefault();
  window.addEventListener("keydown", earlier);
  try {
    const { container } = await mount(toolsProps());
    selectChat();
    const panel = panelOf(container);
    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    expect(panel.hidden).toBe(true);
    expect(document.activeElement).not.toBe(panel.querySelector(".conversation-search input"));
  } finally {
    window.removeEventListener("keydown", earlier);
  }
});

const PANE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PANE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

/** Truthy only so App keeps its stored split rather than collapsing it to the single-tab
 * fallback (App.tsx's `multiPanes`); no method here is ever exercised. */
function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

/** PANE_A alone in the left pane, PANE_B in the right one, which has the focus — the split
 * app-tests/shell/panes.unit.test.tsx's own storeTwoPanes sets up. */
function storeTwoPanes(): void {
  const layout = moveTab(openTab(singlePane(), PANE_A), PANE_B, { group: "g1", zone: "right" });
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
}

function panes(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>("section.conversation.pane")];
}

test("two panes: the focused pane's header gets the search button before Session actions; the unfocused pane has none", async () => {
  storeTwoPanes();
  const rest = fakeRest({
    sessions: [PANE_A, PANE_B].map((id) => sessionRow({ session_id: id, title: id })),
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      organization={organization()}
      {...toolsProps()}
    />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const [paneLeft, paneRight] = panes(container);
  if (!paneLeft || !paneRight) throw new Error("expected two panes");

  expect(container.querySelectorAll(".header-tools-button")).toHaveLength(1);
  const button = searchButton();
  expect(paneRight.contains(button)).toBe(true);
  expect(
    within(paneLeft).queryByRole("button", { name: "Find, select and export (Ctrl+F)" }),
  ).toBeNull();

  const sessionActions = within(paneRight).getByRole("button", { name: "Session actions" });
  expect(
    button.compareDocumentPosition(sessionActions) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();

  const panel = panelOf(container);
  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(panel.hidden).toBe(false);
});
