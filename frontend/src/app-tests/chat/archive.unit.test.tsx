/**
 * App-level wiring for the archive controls. ArchiveBar.unit.test.tsx,
 * SessionList.unit.test.tsx, RowMenu.unit.test.tsx and SearchPalette.unit.test.tsx already pin
 * each component's own rendering; this file pins only what App itself is responsible for
 * supplying: archivedIds from the session metadata store, the chip's count over the listed
 * rows, the row's archive toggle wired to that store, the palette's Archived badge, and a send
 * into an archived session unarchiving it.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow, snapshotData } from "../../accept/m3b/fakes";
import { DurableApiError } from "../../clients/conversations/durableApi";
import {
  type SessionMetadata,
  type SessionMetadataApi,
  SessionMetadataError,
  type SessionMetadataPatch,
} from "../../clients/conversations/sessionMetadataApi";
import { createFakeDecisionLoopApi } from "../../clients/decisionLoop/fake";
import type { SearchHit } from "../../protocol";
import { serializeLayout } from "../../state/paneFormat";
import { moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi, settingsFixture } from "../../testShell";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  sessionStorage.clear();
});

function metadataRow(
  overrides: Partial<SessionMetadata> & { session_id: string },
): SessionMetadata {
  return { revision: 1, title: "", pinned: false, archived: false, ...overrides };
}

/** Resolves `list` at once; tracks every `patch` call so a test can read what App asked for. */
function instantMetadataApi(
  rows: SessionMetadata[],
): SessionMetadataApi & { patchCalls: { sessionId: string; patch: SessionMetadataPatch }[] } {
  let current = rows;
  const patchCalls: { sessionId: string; patch: SessionMetadataPatch }[] = [];
  return {
    patchCalls,
    async list() {
      return current;
    },
    async patch(sessionId, _expectedRevision, patch) {
      patchCalls.push({ sessionId, patch });
      const row = current.find((item) => item.session_id === sessionId);
      if (!row) throw new Error("missing row");
      const updated = { ...row, ...patch };
      current = current.map((item) => (item.session_id === sessionId ? updated : item));
      return updated;
    },
    async remove() {
      throw new Error("not used in this test");
    },
  };
}

async function openRow(container: HTMLElement, id: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row ${id}`);
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
}

async function sendFromComposer(text: string): Promise<void> {
  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  fireEvent.change(box, { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
}

function archiveChip(container: HTMLElement): HTMLElement {
  const found = container.querySelector<HTMLElement>(".archive-chip");
  if (!found) throw new Error("no archive chip yet");
  return found;
}

test("ArchiveBar replaces the old toggle, counts only listed sessions, and swaps the list on click", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s2", archived: true })]);
  const rest = fakeRest({
    sessions: [
      sessionRow({ session_id: "s1", title: "Active chat" }),
      sessionRow({ session_id: "s2", title: "Old chat" }),
    ],
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  const chip = await waitFor(() => archiveChip(container));
  expect(chip.textContent).toBe("Archived (1)");
  // The old plain-button toggle and its ternary copy are gone.
  expect(screen.queryByText("Archived sessions")).toBeNull();
  expect(screen.queryByText("Back to sessions")).toBeNull();
  expect(screen.getByText("Active chat")).not.toBeNull();
  expect(screen.queryByText("Old chat")).toBeNull();

  fireEvent.click(chip);
  expect(
    screen.getByText(/Archiving closes an open chat and hides it from this list/),
  ).not.toBeNull();
  expect(screen.getByText("Old chat")).not.toBeNull();
  expect(screen.queryByText("Active chat")).toBeNull();
});

test("the row's archive icon archives it through the session metadata store", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s1" })]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Keep chatting" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  const archiveButton = await waitFor(() => {
    const found = container.querySelector<HTMLButtonElement>(".sess-archive");
    if (!found) throw new Error("no archive button yet");
    return found;
  });
  expect(archiveButton.getAttribute("aria-label")).toBe("Archive Keep chatting");

  await act(async () => {
    fireEvent.click(archiveButton);
  });
  expect(metadata.patchCalls).toEqual([{ sessionId: "s1", patch: { archived: true } }]);
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toEqual([]);
  expect(socket.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
  expect(container.querySelector('.sess[data-session-id="s1"]')).toBeNull();
  // Archiving the only row drops it from the active list; the chip is how it is found again.
  fireEvent.click(archiveChip(container));
  await waitFor(() =>
    expect(
      container.querySelector<HTMLButtonElement>(".sess-archive")?.getAttribute("aria-label"),
    ).toBe("Unarchive Keep chatting"),
  );
});

test("Ctrl+K hits for an archived session carry the Archived badge", async () => {
  const hit: SearchHit = {
    session_id: "s2",
    title: "Old chat",
    snippet: "…",
    at: "",
    role: "title",
  };
  const metadata = instantMetadataApi([metadataRow({ session_id: "s2", archived: true })]);
  const rest = fakeRest({
    sessions: [sessionRow({ session_id: "s2", title: "Old chat" })],
    hits: [hit],
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  const searchButton = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(".rail-search");
    if (!found) throw new Error("no search button yet");
    return found;
  });
  fireEvent.click(searchButton);
  fireEvent.change(screen.getByPlaceholderText("Search sessions"), { target: { value: "old" } });

  const badge = await waitFor(() => {
    const found = container.querySelector('[data-session-id="s2"] .hit-badge');
    if (!found) throw new Error("no hit yet");
    return found;
  });
  expect(badge.textContent).toBe("Archived");
});

test("sending into an archived session unarchives it", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s1", archived: true })]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Resume me" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(
    <App viki={viki} rest={rest} durable={durable} organization={metadata} />,
  );
  act(() => socket.open());

  const chip = await waitFor(() => archiveChip(container));
  fireEvent.click(chip); // show archived rows, so the row is on screen to open

  await openRow(container, "s1");
  await sendFromComposer("hi");

  expect(durable.submissions).toHaveLength(1);
  expect(durable.submissions[0]).toMatchObject({ session_id: "s1" });
  await waitFor(() =>
    expect(metadata.patchCalls).toContainEqual({ sessionId: "s1", patch: { archived: false } }),
  );
});

test("the chip counts only archived rows the REST listing actually holds", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "s1", archived: true }),
    metadataRow({ session_id: "ghost", archived: true }), // no matching REST row
  ]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Old chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  const chip = await waitFor(() => archiveChip(container));
  expect(chip.textContent).toBe("Archived (1)");
});

test("a send into an unarchived session makes no archive patch", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s1", archived: false })]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Active chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  await openRow(container, "s1");
  await sendFromComposer("hi");

  expect(metadata.patchCalls).toEqual([]);
});

test("a send that gets no receipt into an archived session makes no patch", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s1", archived: true })]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Resume me" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  durable.submitCommand = async (input) => {
    durable.submissions.push(input);
    throw new DurableApiError(503, "unavailable");
  };
  const { container } = render(
    <App viki={viki} rest={rest} durable={durable} organization={metadata} />,
  );
  act(() => socket.open());

  const chip = await waitFor(() => archiveChip(container));
  fireEvent.click(chip); // show archived rows, so the row is on screen to open

  await openRow(container, "s1");
  await sendFromComposer("hi");

  expect(durable.submissions).toHaveLength(1);
  expect(metadata.patchCalls).toEqual([]);
});

test("the row's archive icon un-archives it when the row is already archived", async () => {
  const metadata = instantMetadataApi([metadataRow({ session_id: "s1", archived: true })]);
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Old chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  act(() => socket.open());

  const chip = await waitFor(() => archiveChip(container));
  fireEvent.click(chip); // show archived rows

  const archiveButton = await waitFor(() => {
    const found = container.querySelector<HTMLButtonElement>(".sess-archive");
    if (!found) throw new Error("no archive button yet");
    return found;
  });
  expect(archiveButton.getAttribute("aria-label")).toBe("Unarchive Old chat");
  await openRow(container, "s1");

  await act(async () => {
    fireEvent.click(archiveButton);
  });
  expect(metadata.patchCalls).toEqual([{ sessionId: "s1", patch: { archived: false } }]);
  expect(socket.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
});

const [ALPHA, BETA, GAMMA] = [
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
] as const;

async function openedArchiveApp(launcherChat = false) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: 1440 >= Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  const metadata = instantMetadataApi(
    [ALPHA, BETA, GAMMA].map((session_id) => metadataRow({ session_id })),
  );
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const settings = settingsFixture({ "onboarding.completed": true });
  const { container } = render(
    <App
      viki={viki}
      rest={fakeRest({
        sessions: [
          sessionRow({ session_id: ALPHA, title: "Alpha" }),
          sessionRow({ session_id: BETA, title: "Beta" }),
          sessionRow({ session_id: GAMMA, title: "Gamma" }),
        ],
        hits: [{ session_id: ALPHA, title: "Alpha", snippet: "", at: "", role: "title" }],
      })}
      durable={durable}
      organization={metadata}
      {...(launcherChat
        ? {
            decisionApi: createFakeDecisionLoopApi(),
            shell: fakeShellApi({
              settings: {
                ...settings,
                effective: { ...settings.effective, "decision.launcher_session": ALPHA },
              },
            }),
          }
        : {})}
    />,
  );
  act(() => socket.open());
  await openRow(container, ALPHA);
  return { container, viki, socket, metadata, durable };
}

function rowArchive(container: HTMLElement, id: string = ALPHA): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(
    `.sess[data-session-id="${id}"] .sess-archive`,
  );
  if (!button) throw new Error(`no archive button for ${id}`);
  return button;
}

test.each(["unavailable", "storage_unavailable"] as const)(
  "yesterday's chat can be archived after its existing-only open is refused as %s",
  async (reason) => {
    vi.spyOn(Date, "now").mockReturnValue(new Date(2026, 10, 6, 12).getTime());
    const yesterday = new Date(2026, 10, 5, 12);
    const metadata = instantMetadataApi([metadataRow({ session_id: ALPHA })]);
    const { viki, socket } = fakeViki();
    vi.spyOn(socket, "synchronize").mockImplementation(() => {});
    const { container } = render(
      <App
        viki={viki}
        rest={fakeRest({
          sessions: [
            sessionRow({
              session_id: ALPHA,
              title: "Yesterday's chat",
              modified: yesterday.toISOString(),
            }),
          ],
        })}
        durable={fakeDurable()}
        organization={metadata}
        sessionHandoff={{ kind: "target", sessionId: ALPHA }}
      />,
    );
    act(() => socket.open());
    await waitFor(() => rowArchive(container));
    expect(screen.getByText("Yesterday · 11-05")).toBeTruthy();
    act(() =>
      socket.receive({
        v: 2,
        type: "session.open.refused",
        session_id: ALPHA,
        generation: "",
        request_id: viki.client.subscriptionId(ALPHA),
        seq: 0,
        data: { reason },
      }),
    );
    expect(viki.store.getSnapshot().sessions[ALPHA]?.openRefused).toBe(reason);

    await click(rowArchive(container));

    expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
    expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
    expect(socket.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
    expect(tabsShown(container)).toEqual([]);
    expect(container.querySelector(`.sess[data-session-id="${ALPHA}"]`)).toBeNull();
    expect(archiveChip(container).textContent).toBe("Archived (1)");
    await click(archiveChip(container));
    expect(rowArchive(container).getAttribute("aria-label")).toBe("Unarchive Yesterday's chat");
  },
);

async function click(element: Element): Promise<void> {
  await act(async () => fireEvent.click(element));
}

const tabsShown = (container: ParentNode): string[] =>
  [...container.querySelectorAll(".session-tab-select")].map((tab) =>
    tab.id.replace("session-tab-", ""),
  );

function chatDrop(type: string, id: string): Event {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: 990,
    clientY: 400,
  });
  Object.defineProperty(event, "dataTransfer", {
    value: { types: ["text/x-viki-tab"], getData: () => id },
  });
  return event;
}

function splitChatRight(container: HTMLElement, id: string): void {
  const main = container.querySelector("main.conversation") as HTMLElement;
  main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  act(() => main.dispatchEvent(chatDrop("dragover", id)));
  act(() => main.dispatchEvent(chatDrop("drop", id)));
}

test.each([false, true])(
  "archiving a held idle chat closes it before saving (live=%s)",
  async (live) => {
    const { container, viki, socket, metadata } = await openedArchiveApp();
    const snapshot = snapshotData();
    snapshot.state.live = live;
    act(() => socket.synchronize(ALPHA, GENERATION, 0, snapshot));
    const patch = metadata.patch;
    vi.spyOn(metadata, "patch").mockImplementation(async (...args) => {
      expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
      return patch(...args);
    });

    await click(rowArchive(container));

    expect(socket.sent.filter((frame) => frame.type === "session.close")).toMatchObject([
      { session_id: ALPHA },
    ]);
    expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
    expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
    expect(tabsShown(container)).toEqual([]);
    expect(container.querySelector(`.sess[data-session-id="${ALPHA}"]`)).toBeNull();
    expect(sessionStorage.getItem("viki.selected")).toBeNull();
    expect(screen.queryByText("This conversation is closed.")).toBeNull();
  },
);

test.each(["menu", "organizer"])("the %s archive control closes an idle chat", async (control) => {
  const { container, viki, socket, metadata } = await openedArchiveApp();
  if (control === "menu") {
    await click(container.querySelector(`.sess[data-session-id="${ALPHA}"] .sess-menu`) as Element);
    await click(screen.getByRole("menuitem", { name: "Archive" }));
  } else {
    await click(
      within(container.querySelector(".session-organizer") as HTMLElement).getByRole("button", {
        name: "Archive",
      }),
    );
  }

  expect(socket.sent.filter((frame) => frame.type === "session.close")).toHaveLength(1);
  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
});

test.each(["rejected", "unknown"] as const)(
  "a %s close keeps the chat unarchived",
  async (status) => {
    const { container, socket, viki, metadata } = await openedArchiveApp();
    socket.controlStatus = status;

    await click(rowArchive(container));

    expect(metadata.patchCalls).toEqual([]);
    expect(viki.store.getSnapshot().sessions[ALPHA]).toBeDefined();
    expect(tabsShown(container)).toEqual([ALPHA]);
    expect(screen.getByText(/could not be closed/i)).toBeTruthy();
  },
);

test("an archive save failure reports the metadata error after closing", async () => {
  const { container, viki, metadata } = await openedArchiveApp();
  vi.spyOn(metadata, "patch").mockRejectedValue(new SessionMetadataError(409, "conflict"));

  await click(rowArchive(container));

  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(screen.getByText("Session settings could not be loaded or saved.")).toBeTruthy();
  expect(rowArchive(container).getAttribute("aria-label")).toBe("Archive Alpha");
});

test("a pending close blocks duplicate archive and keeps newer tabs, panes and selection", async () => {
  const { container, socket, viki, metadata } = await openedArchiveApp();
  const acknowledge = socket.acknowledge.bind(socket);
  let closeFrame: Record<string, unknown> | undefined;
  vi.spyOn(socket, "acknowledge").mockImplementation((frame, status) => {
    if (frame.type === "session.close") closeFrame = frame;
    else acknowledge(frame, status);
  });

  await click(rowArchive(container));
  expect(closeFrame).toBeDefined();
  expect(metadata.patchCalls).toEqual([]);
  expect(rowArchive(container).disabled).toBe(true);
  await click(rowArchive(container));
  await openRow(container, BETA);
  await openRow(container, GAMMA);
  splitChatRight(container, BETA);
  await openRow(container, GAMMA);
  expect(container.querySelectorAll("section.conversation.pane")).toHaveLength(2);
  await act(async () => acknowledge(closeFrame as Record<string, unknown>));

  expect(socket.sent.filter((frame) => frame.type === "session.close")).toHaveLength(1);
  expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(tabsShown(container)).toEqual([GAMMA, BETA]);
  expect(sessionStorage.getItem("viki.selected")).toBe(GAMMA);
  expect(container.querySelectorAll("section.conversation.pane")).toHaveLength(2);
});

test("archiving another pane's chat collapses only its empty pane", async () => {
  const layout = moveTab(openTab(singlePane(), ALPHA), BETA, { group: "g1", zone: "right" });
  sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  const { container } = await openedArchiveApp();
  await openRow(container, BETA);

  await click(rowArchive(container));

  expect(tabsShown(container)).toEqual([BETA]);
  expect(sessionStorage.getItem("viki.selected")).toBe(BETA);
  expect(container.querySelectorAll("section.conversation.pane")).toHaveLength(0);
});

test("archiving a chat this window no longer holds removes its placeholder tab without reopening", async () => {
  const { container, socket, viki, metadata } = await openedArchiveApp();
  await openRow(container, BETA);
  await act(async () => {
    await viki.actions.closeSession(ALPHA);
  });
  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(tabsShown(container)).toEqual([ALPHA, BETA]);
  const before = socket.sent.length;

  await click(rowArchive(container));

  expect(tabsShown(container)).toEqual([BETA]);
  expect(sessionStorage.getItem("viki.selected")).toBe(BETA);
  expect(
    socket.sent
      .slice(before)
      .filter((frame) => frame.type === "session.open" || frame.type === "session.close"),
  ).toEqual([]);
  expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
});

test.each(["row", "search", "pane"])(
  "a %s cannot reopen a chat during archive save",
  async (source) => {
    const { container, socket, viki, metadata } = await openedArchiveApp();
    let finishPatch: () => void = () => {};
    const waiting = new Promise<void>((resolve) => {
      finishPatch = resolve;
    });
    const patch = metadata.patch;
    vi.spyOn(metadata, "patch").mockImplementation(async (...args) => {
      await waiting;
      return patch(...args);
    });
    const opened = socket.sent.filter((frame) => frame.type === "session.open").length;

    await click(rowArchive(container));
    expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
    if (source === "row") await openRow(container, ALPHA);
    else if (source === "pane") splitChatRight(container, ALPHA);
    else {
      await click(container.querySelector(".rail-search") as Element);
      vi.useFakeTimers();
      fireEvent.change(screen.getByPlaceholderText("Search sessions"), {
        target: { value: "alpha" },
      });
      await act(async () => vi.advanceTimersByTimeAsync(150));
      await click(document.querySelector(".hit[data-session-id]") as HTMLElement);
      vi.useRealTimers();
    }
    await act(async () => finishPatch());

    expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
    expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(opened);
    expect(metadata.patchCalls).toEqual([{ sessionId: ALPHA, patch: { archived: true } }]);
    expect(tabsShown(container)).toEqual([]);
  },
);

test("a retained local draft cannot restart its chat during archive save", async () => {
  const { container, socket, viki, metadata, durable } = await openedArchiveApp();
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "unfinished draft" } });
  let finishPatch: () => void = () => {};
  const waiting = new Promise<void>((resolve) => {
    finishPatch = resolve;
  });
  const patch = metadata.patch;
  vi.spyOn(metadata, "patch").mockImplementation(async (...args) => {
    await waiting;
    return patch(...args);
  });
  const opened = socket.sent.filter((frame) => frame.type === "session.open").length;
  const start = vi.spyOn(viki.actions, "newSession");

  await click(rowArchive(container));
  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  await click(screen.getByText(/^Draft aaaaaaaa/));
  await sendFromComposer("unfinished draft");
  await act(async () => finishPatch());

  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(opened);
  expect(start).not.toHaveBeenCalled();
  expect(durable.submissions).toEqual([]);
  expect(screen.getByLabelText<HTMLTextAreaElement>("Message").value).toBe("unfinished draft");
  expect(screen.getByRole("alert").textContent).toContain("being archived");

  await click(archiveChip(container));
  await openRow(container, ALPHA);
  await sendFromComposer("unfinished draft");
  expect(durable.submissions).toHaveLength(1);
  expect(metadata.patchCalls).toEqual([
    { sessionId: ALPHA, patch: { archived: true } },
    { sessionId: ALPHA, patch: { archived: false } },
  ]);
});

test("the Decision launcher cannot reopen or send to a chat during archive save", async () => {
  const { container, socket, viki, metadata } = await openedArchiveApp(true);
  let finishPatch: () => void = () => {};
  const waiting = new Promise<void>((resolve) => {
    finishPatch = resolve;
  });
  const patch = metadata.patch;
  vi.spyOn(metadata, "patch").mockImplementation(async (...args) => {
    await waiting;
    return patch(...args);
  });
  const opened = socket.sent.filter((frame) => frame.type === "session.open").length;
  const send = vi.spyOn(viki.actions, "send");

  await click(rowArchive(container));
  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  await click(container.querySelector("button.dloop-launcher") as Element);
  const dialog = screen.getByRole("dialog", { name: "VIKI" });
  const box = within(dialog).getByRole<HTMLTextAreaElement>("textbox", { name: "Message VIKI" });
  fireEvent.change(box, { target: { value: "keep this draft" } });
  await act(async () => fireEvent.keyDown(box, { key: "Enter" }));
  await act(async () => finishPatch());

  expect(viki.store.getSnapshot().sessions[ALPHA]).toBeUndefined();
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(opened);
  expect(send).not.toHaveBeenCalled();
  expect(box.value).toBe("keep this draft");
  expect(within(dialog).getByRole("alert")).toBeTruthy();
});
