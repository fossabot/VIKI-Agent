/**
 * SessionList's optional props, the listing note among them. The frozen m3b test
 * already pins the rows without them; this covers what they add, and the one thing that
 * matters most — that adding them changes nothing for a caller that does not pass them.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionRow } from "../../protocol";
import { listingNote, type SessionsView } from "../../state/catalog";
import type { SessionGroup } from "../../state/rows";
import type { AppSnapshot } from "../../state/store";
import type { SessionView } from "../../state/view";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { CHAT_DRAG_TYPE } from "../Panes/paneDrop";
import type { RowContext } from "./rowState";
import { SessionList, type SessionRowMenu } from "./SessionList";

afterEach(cleanup);

/* Local times, serialised: timeLabel reads local hours, so a UTC literal would pass here and
   fail in the user's timezone. */
const NOW = new Date(2026, 8, 12, 19, 30).getTime();
const AT = new Date(2026, 8, 12, 17, 20).toISOString();

const row = (over: Partial<SessionRow> = {}): SessionRow => ({
  session_id: "s1",
  title: "Dashboard v1",
  first_prompt: "",
  entrypoint: "viki-agent",
  modified: AT,
  cli_version: "2.1.259",
  other: false,
  ...over,
});

const view = (over: Partial<SessionView> = {}): SessionView => ({
  sessionId: "s1",
  generation: null,
  requestId: null,
  synced: false,
  truncated: false,
  historyRequestId: null,
  historyError: null,
  seq: 1,
  info: null,
  mode: "auto",
  turns: [],
  cards: [],
  blocked: null,
  closed: false,
  error: null,
  compacting: false,
  contextPercent: null,
  rateLimit: null,
  historyCursor: undefined,
  historyLoading: false,
  ...over,
});

function context(sessions: Record<string, SessionView>, over: Partial<RowContext> = {}) {
  const app: AppSnapshot = { sessions, others: [], status: "open" };
  return { app, elsewhere: new Map(), ...over } satisfies RowContext;
}

const groups: SessionGroup[] = [{ label: "Today", rows: [row()] }];

function mount(over: Partial<Parameters<typeof SessionList>[0]> = {}) {
  return render(
    <SessionList
      groups={groups}
      other={[]}
      selectedId={null}
      liveIds={new Set(["s1"])}
      nowMs={NOW}
      onOpen={vi.fn()}
      {...over}
    />,
  );
}

test("dragRows makes each row a drag source carrying its chat id; without it no row drags", () => {
  const { container, unmount } = mount();
  expect(container.querySelector(".sess")?.hasAttribute("draggable")).toBe(false);
  unmount();

  const dragging = mount({ dragRows: true }).container.querySelector(".sess") as HTMLElement;
  expect(dragging.getAttribute("draggable")).toBe("true");
  const carried = new Map<string, string>();
  fireEvent.dragStart(dragging, {
    dataTransfer: { setData: (type: string, value: string) => carried.set(type, value) },
  });
  expect([...carried]).toEqual([[CHAT_DRAG_TYPE, "s1"]]);
});

test("without rowContext the row keeps M3-b's wording to the character", () => {
  const { container } = mount();
  expect(container.querySelector(".m")?.textContent).toBe("17:20 · live");
  expect(container.querySelector(".sess-dot")?.className).toBe("sess-dot breathe");
  expect(container.querySelector(".sess-menu")).toBeNull();
  expect(container.querySelector(".sess-note")).toBeNull();
});

/** What the store would be holding for each freshness, so the rail is checked against the
 * copy the App will really pass it rather than against a literal typed here twice. */
function restView(over: Partial<SessionsView>): SessionsView {
  return {
    sessions: [row()],
    sessionsStatus: "ready",
    sessionsReason: "",
    sessionsOmitted: 0,
    ...over,
  };
}

test("the listing note says loading, incomplete, stale or unavailable and never no-sessions on failure", () => {
  const cases: [SessionsView, string][] = [
    [restView({ sessions: [], sessionsStatus: "loading" }), "loading sessions…"],
    [
      restView({ sessionsStatus: "partial", sessionsReason: "time_budget", sessionsOmitted: 12 }),
      "incomplete list · 12 not read yet",
    ],
    [
      restView({ sessionsStatus: "partial", sessionsReason: "read_failed", sessionsOmitted: 2 }),
      "incomplete list · 2 unreadable",
    ],
    [
      restView({ sessionsStatus: "error", sessionsReason: "transport" }),
      "sessions may be out of date · last read failed",
    ],
    [
      restView({ sessions: [], sessionsStatus: "error", sessionsReason: "transport" }),
      "sessions unavailable",
    ],
  ];
  for (const [rest, copy] of cases) {
    const { container, unmount } = mount({ listing: listingNote(rest) });
    const notes = [...container.querySelectorAll(".sess-note")];
    expect(notes.map((note) => note.textContent)).toEqual([copy]);
    // A failed read must never be told as an empty vault, in any of these states.
    expect(container.textContent).not.toContain("no sessions yet");
    // The note is neither a group label nor a row: "Other (N)" and every row count still
    // describe rows the server really listed.
    for (const note of notes) {
      expect(note.classList.contains("lbl")).toBe(false);
      expect(note.classList.contains("sess")).toBe(false);
    }
    expect(container.querySelectorAll(".lbl")).toHaveLength(1); // the "Today" group only
    expect(container.querySelectorAll(".sess")).toHaveLength(1);
    unmount();
  }
});

test("a ready listing and a refresh over retained rows say nothing at all", () => {
  for (const rest of [restView({}), restView({ sessionsStatus: "loading" })]) {
    const { container, unmount } = mount({ listing: listingNote(rest) });
    expect(container.querySelector(".sess-note")).toBeNull();
    unmount();
  }
});

test("the note renders above the groups, and its text is a text node, not markup", () => {
  const { container } = mount({ listing: "<b>incomplete list · 3 not read yet</b>" });
  const note = container.querySelector(".sess-note") as HTMLElement;
  expect(note.querySelector("b")).toBeNull();
  expect(note.textContent).toBe("<b>incomplete list · 3 not read yet</b>");
  const sessions = container.querySelector(".sessions") as HTMLElement;
  expect(sessions.firstElementChild).toBe(note);
});

test("with rowContext an idle held session reads as live · idle behind a ringed dot", () => {
  const { container } = mount({ rowContext: context({ s1: view() }) });
  expect(container.querySelector(".m")?.textContent).toBe("live · idle");
  expect(container.querySelector(".sess-dot")?.className).toBe("sess-dot sess-dot-live");
});

test("a session written to while the vault is dirty still reads live · idle: the row does not track git state", () => {
  const wrote = view({
    turns: [
      {
        id: "t1",
        user: null,
        status: "success",
        usage: null,
        durationMs: 0,
        history: false,
        blocks: [
          {
            kind: "tools",
            id: "b1",
            rows: [
              {
                id: "tu1",
                tool: "Write",
                row: { head: "Write", rest: "30-knowledge/x.md", tail: "+12" },
                done: true,
                isError: false,
                summary: "",
                preview: "",
              },
            ],
          },
        ],
      },
    ],
  });
  const { container } = mount({ rowContext: context({ s1: wrote }) });
  const meta = container.querySelector(".m") as HTMLElement;
  expect(meta.querySelector(".chip")).toBeNull();
  expect(meta.textContent).toBe("live · idle");
  expect(container.querySelector(".sess-dot")?.className).toBe("sess-dot sess-dot-live");
});

test("a session held by another process is dim-chipped and never claimed as ours", () => {
  const { container } = mount({
    liveIds: new Set(),
    rowContext: context({}, { elsewhere: new Map([["s1", "claude-vscode"]]) }),
  });
  const meta = container.querySelector(".m") as HTMLElement;
  expect(meta.querySelector(".chip")?.classList.contains("dim")).toBe(true);
  expect(meta.textContent).toBe("VS Codelive elsewhere");
  expect(container.querySelector(".sess")?.classList.contains("sess-live")).toBe(false);
});

test("a row rowContext calls plain falls back to M3-b's rule rather than going blank", () => {
  const { container } = mount({ liveIds: new Set(), rowContext: context({}) });
  expect(container.querySelector(".m")?.textContent).toBe("17:20");
});

function menuProps(): SessionRowMenu & { spies: Record<string, ReturnType<typeof vi.fn>> } {
  const spies = {
    fork: vi.fn(),
    transcript: vi.fn(),
    close: vi.fn(),
  };
  return {
    spies,
    onFork: spies.fork,
    onOpenTranscript: spies.transcript,
    onCloseSession: spies.close,
  };
}

test("the ⋯ opens the row menu without opening the session, and each item names its row", () => {
  const onOpen = vi.fn();
  const menu = menuProps();
  const { container } = mount({ onOpen, menu });
  const toggle = container.querySelector(".sess-menu") as HTMLElement;
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(onOpen).not.toHaveBeenCalled();
  expect(toggle.getAttribute("aria-expanded")).toBe("true");

  const items = [...container.querySelectorAll(".row-menu .mi")] as HTMLElement[];
  expect(items.map((item) => item.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Close session",
  ]);
  fireEvent.click(items[1] as HTMLElement);
  expect(menu.spies.transcript).toHaveBeenCalledWith("s1");
  // Picking an item closes the menu, and still does not open the session underneath it.
  expect(container.querySelector(".row-menu")).toBeNull();
  expect(onOpen).not.toHaveBeenCalled();
});

test("menu keys do not open the session", () => {
  for (const key of ["Enter", " "]) {
    const onOpen = vi.fn();
    const menu = menuProps();
    const { container, unmount } = mount({ onOpen, menu });
    const toggle = container.querySelector(".sess-menu") as HTMLElement;
    toggle.focus();
    const toggleDefaultAllowed = fireEvent.keyDown(toggle, { key });
    expect(onOpen).not.toHaveBeenCalled();
    expect(toggleDefaultAllowed).toBe(true);
    expect(document.activeElement).toBe(toggle);
    // fireEvent does not synthesize the native button click after an activation key.
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(toggle);

    const actions = [menu.spies.fork, menu.spies.transcript, menu.spies.close];
    for (const [index, action] of actions.entries()) {
      if (index > 0) fireEvent.click(toggle);
      const item = container.querySelectorAll<HTMLElement>(".row-menu .mi")[index];
      if (!item) throw new Error("menu item missing");
      item.focus();
      const itemDefaultAllowed = fireEvent.keyDown(item, { key });
      expect(onOpen).not.toHaveBeenCalled();
      expect(itemDefaultAllowed).toBe(true);
      expect(document.activeElement).toBe(item);
      fireEvent.click(item);
      expect(action).toHaveBeenCalledExactlyOnceWith("s1");
      expect(container.querySelector(".row-menu")).toBeNull();
      expect(onOpen).not.toHaveBeenCalled();
    }
    unmount();
  }
});

test("the row's own Enter and Space each open its session once", () => {
  const onOpen = vi.fn();
  const { container } = mount({ onOpen, menu: menuProps() });
  const session = container.querySelector(".sess") as HTMLElement;
  session.focus();
  for (const key of ["Enter", " "]) {
    onOpen.mockClear();
    expect(fireEvent.keyDown(session, { key })).toBe(false);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("s1");
    expect(document.activeElement).toBe(session);
  }
});

test("Escape closes the row menu, and a composing Escape is the IME's, not the user's", () => {
  const { container } = mount({ menu: menuProps() });
  fireEvent.click(container.querySelector(".sess-menu") as HTMLElement);
  fireEvent.keyDown(window, { key: "Escape", isComposing: true });
  expect(container.querySelector(".row-menu")).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(container.querySelector(".row-menu")).toBeNull();
});

test("a row the app is not holding gets no ⋯: there is nothing to close or fork", () => {
  const { container } = mount({ liveIds: new Set(), menu: menuProps() });
  expect(container.querySelector(".sess-menu")).toBeNull();
});

function archiveProps(
  over: Partial<{ isArchived: (id: string) => boolean; pending: ReadonlySet<string> }> = {},
) {
  return {
    isArchived: over.isArchived ?? (() => false),
    pending: over.pending ?? new Set<string>(),
    onToggle: vi.fn(),
  };
}

test("without archive no row carries sess-archivable or a sess-archive button", () => {
  const { container } = mount();
  expect(container.querySelector(".sess-archivable")).toBeNull();
  expect(container.querySelector(".sess-archive")).toBeNull();
});

test("with archive every row carries sess-archivable and a button labelled for its title", () => {
  const archive = archiveProps();
  const { container } = mount({ archive });
  const sess = container.querySelector(".sess") as HTMLElement;
  expect(sess.classList.contains("sess-archivable")).toBe(true);
  const button = sess.querySelector(".sess-archive") as HTMLElement;
  expect(button.getAttribute("aria-label")).toBe("Archive Dashboard v1");
});

test("an archived row's button reads Unarchive", () => {
  const archive = archiveProps({ isArchived: (id) => id === "s1" });
  const { container } = mount({ archive });
  const button = container.querySelector(".sess-archive") as HTMLElement;
  expect(button.getAttribute("aria-label")).toBe("Unarchive Dashboard v1");
});

test("the archive button is disabled while its row is pending", () => {
  const archive = archiveProps({ pending: new Set(["s1"]) });
  const { container } = mount({ archive });
  const button = container.querySelector(".sess-archive") as HTMLElement;
  expect(button.hasAttribute("disabled")).toBe(true);
});

test("clicking archive stops the row from opening and toggles only that id", () => {
  const onOpen = vi.fn();
  const archive = archiveProps();
  const { container } = mount({ onOpen, archive });
  fireEvent.click(container.querySelector(".sess-archive") as HTMLElement);
  expect(onOpen).not.toHaveBeenCalled();
  expect(archive.onToggle).toHaveBeenCalledExactlyOnceWith("s1");
});

const twoRows: SessionGroup[] = [
  { label: "Today", rows: [row({ session_id: "s1" }), row({ session_id: "s2" })] },
];

test("archiving a row's icon moves focus to the next row before it disappears", () => {
  const archive = archiveProps();
  const { container } = mount({ groups: twoRows, archive });
  const buttons = container.querySelectorAll<HTMLButtonElement>(".sess-archive");
  const first = buttons[0];
  if (!first) throw new Error("archive button missing");
  first.focus();
  fireEvent.click(first);
  const rows = [...container.querySelectorAll<HTMLElement>(".sess")];
  expect(document.activeElement).toBe(rows[1]);
  expect(archive.onToggle).toHaveBeenCalledExactlyOnceWith("s1");
});

test("archiving the last row's icon moves focus to the row before it", () => {
  const archive = archiveProps();
  const { container } = mount({ groups: twoRows, archive });
  const buttons = container.querySelectorAll<HTMLButtonElement>(".sess-archive");
  const last = buttons[1];
  if (!last) throw new Error("archive button missing");
  last.focus();
  fireEvent.click(last);
  const rows = [...container.querySelectorAll<HTMLElement>(".sess")];
  expect(document.activeElement).toBe(rows[0]);
});

test("archiving the only row's icon moves focus to the list itself", () => {
  const archive = archiveProps();
  const { container } = mount({ archive });
  const button = container.querySelector<HTMLButtonElement>(".sess-archive") as HTMLButtonElement;
  button.focus();
  fireEvent.click(button);
  expect(document.activeElement).toBe(container.querySelector(".sessions"));
});

test("a row the app is not holding still gets the archive button: archiving needs no live process", () => {
  const archive = archiveProps();
  const { container } = mount({ liveIds: new Set(), archive });
  expect(container.querySelector(".sess-archivable")).not.toBeNull();
  expect(container.querySelector(".sess-archive")).not.toBeNull();
});

test("RowMenu gets onArchive and archived when archive is passed, as a fourth item", () => {
  const archive = archiveProps({ isArchived: () => true });
  const { container } = mount({ menu: menuProps(), archive });
  fireEvent.click(container.querySelector(".sess-menu") as HTMLElement);
  const items = [...container.querySelectorAll(".row-menu .mi")] as HTMLElement[];
  expect(items.map((item) => item.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Unarchive",
    "Close session",
  ]);
  fireEvent.click(items[2] as HTMLElement);
  expect(archive.onToggle).toHaveBeenCalledExactlyOnceWith("s1");
  // Picking it closes the menu, like every other item.
  expect(container.querySelector(".row-menu")).toBeNull();
});

test("archiving through the row menu also moves focus to the next row", () => {
  const archive = archiveProps();
  const { container } = mount({
    groups: twoRows,
    liveIds: new Set(["s1", "s2"]),
    menu: menuProps(),
    archive,
  });
  fireEvent.click(container.querySelector(".sess-menu") as HTMLElement);
  const items = [...container.querySelectorAll<HTMLElement>(".row-menu .mi")];
  const archiveItem = items.find((item) => item.textContent === "Archive") as HTMLElement;
  archiveItem.focus();
  fireEvent.click(archiveItem);
  const rows = [...container.querySelectorAll<HTMLElement>(".sess")];
  expect(document.activeElement).toBe(rows[1]);
  expect(archive.onToggle).toHaveBeenCalledExactlyOnceWith("s1");
});

test("without archive RowMenu gets no onArchive, so it keeps its three-item form", () => {
  const { container } = mount({ menu: menuProps() });
  fireEvent.click(container.querySelector(".sess-menu") as HTMLElement);
  const items = [...container.querySelectorAll(".row-menu .mi")] as HTMLElement[];
  expect(items.map((item) => item.textContent)).toEqual([
    "Fork",
    "Open transcript in VS Code",
    "Close session",
  ]);
});

test("rowState.css floats the archive button over the row's corner, hidden until asked for", () => {
  const css = readCss(import.meta.url, "rowState.css");
  const base = ruleIn(css, ".sess-archivable .sess-archive");
  expect(base).toMatch(/position:\s*absolute/);
  expect(base).toMatch(/opacity:\s*0/);
  expect(ruleIn(css, ".sess-archivable .t")).toMatch(/padding-right/);
  expect(css).toContain(".sess-archivable:hover .sess-archive,");
  expect(css).toContain(".sess-archivable:focus-within .sess-archive,");
  expect(ruleIn(css, ".sess-archivable.sess-selected .sess-archive")).toMatch(/opacity:\s*1/);
});

test("rowState.css gives the archive button a forced-colors outline", () => {
  // Narrowed to the media block first: the plain (non-forced) state reuses the same selector
  // for its own `color: var(--t0)` rule, so searching the whole file could match that one instead.
  const forced = narrowBlock(
    readCss(import.meta.url, "rowState.css"),
    "@media (forced-colors: active) {",
  );
  expect(ruleIn(forced, ".sess-archivable .sess-archive:focus-visible")).toMatch(
    /outline:\s*2px solid Highlight/,
  );
});
