/**
 * App-level wiring for the right rail's Agents section: the run list and the roster render in the
 * right rail and nowhere in the left rail; a run's Jump really does switch the app to the run's
 * chat when it is another one and lands on its routing row, outlined and focused — the part only
 * a full App render can prove (the list's own groups, Peek and Jump calls are pinned at the
 * component level, components/RightRail/AgentRuns.unit.test.tsx, and the row's own target in
 * components/Conversation/Conversation.unit.test.tsx); the roster sits in a closed Library fold
 * under the runs; and a reviewed mark is App's, so it outlives the right rail unmounting while
 * another view is on screen.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  agent,
  emptyRails,
  type FakeSocket,
  fakeRest,
  fakeViki,
  GENERATION,
  sessionRow,
} from "../../accept/m3b/fakes";
import type { CommandReceipt } from "../../clients/conversations/durableApi";
import type { RunTranscript, SubagentsApi } from "../../clients/conversations/subagentsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { Envelope, EventMap } from "../../protocol";
import type { VaultApi } from "../../state/vaultApi";
import { type FakeDurable, fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

function frame<K extends keyof EventMap>(
  sessionId: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq,
    data,
  } as Envelope;
}

function started(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "session.started", {
    model: "sonnet",
    mode: "auto",
    effort: "low",
    commands: [],
    agents: [],
    models: [],
    cli_version: "2.1.259",
    resumed: true,
  });
}

function userMessage(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "user.message", {
    text: "hi",
    command: "",
    at: "2026-10-08T09:00:00.000Z",
  });
}

/** A "researcher" run, block a1, starting in the session's current turn. */
function researcherStarted(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "subagent.started", {
    block_id: "a1",
    agent: "researcher",
    model: "",
    description: "Survey sources",
    reason: "",
  });
}

function runSnapshot(sessionId: string, events: Envelope[], truncated = false) {
  return {
    v: 2,
    type: "session.snapshot",
    session_id: sessionId,
    generation: GENERATION,
    request_id: "",
    seq: 200,
    data: {
      events: events.map(({ type, data, seq }) => ({
        type,
        data,
        ...(type === "subagent.done" ? { source_seq: seq } : {}),
      })),
      state: {
        live: true,
        info: null,
        mode: "default",
        blocked: null,
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
      },
      truncated,
    },
  };
}

/** The vault names a session live and this window discovers it, exactly as makeLive does for
 * the Running-work suite (src/app-tests/agents/runningWork.unit.test.tsx) — the only way to get a routing block running
 * in a session this test never selects. */
function vaultSessions(seq: number, live: string[]): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq,
    data: { others: [], live },
  } as Envelope;
}

/** Clicks New session and returns the new chat's id: the chat on screen from then on. */
function newSession(socket: FakeSocket): string {
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  return opened.session_id;
}

/** No profiles, every other call left pending: the Agents tab needs a profile client to exist. */
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

/** Every call left pending: the top bar's Chat tab is a live control only while a vault client
 * is wired, as main.tsx always wires one. */
function pendingVaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: pending,
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: pending,
  };
}

function conversationTitle(container: HTMLElement): string | null | undefined {
  return container.querySelector(".conversation-title")?.textContent;
}

function rightRailOf(container: HTMLElement): HTMLElement {
  return container.querySelector('[data-region="right-rail"]') as HTMLElement;
}

test("the roster and its here-now count render in the right rail; the left rail has none of it", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const rightRail = container.querySelector('[data-region="right-rail"]');
  const leftRail = container.querySelector('[data-region="left-rail"]');
  expect(rightRail).not.toBeNull();
  expect(leftRail).not.toBeNull();

  expect(rightRail?.querySelector(".rail-agents-head .lbl")?.textContent).toBe(
    "VIKI and subagents",
  );
  expect(rightRail?.querySelector(".rail-agents")?.getAttribute("aria-label")).toBe(
    "VIKI and subagents",
  );
  expect(
    [...(rightRail?.querySelectorAll(".agent") ?? [])].map((el) => el.getAttribute("data-agent")),
  ).toEqual(["VIKI", "researcher"]);

  expect(leftRail?.querySelector(".rail-agents")).toBeNull();
  expect(leftRail?.querySelector(".agent")).toBeNull();
});

test("Jump to a run in another chat switches to it and outlines its row", async () => {
  const other = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const rest = fakeRest({
    rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] },
    sessions: [sessionRow({ session_id: other, title: "Research session" })],
  });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  // The session on screen until the Jump: a fresh one this test never hands a running agent.
  newSession(socket);
  expect(conversationTitle(container)).toBe("Untitled session");

  // `other` is discovered live in the background and given a running "researcher" block; it is
  // never selected directly, so the run list is the only way there.
  act(() => socket.receive(vaultSessions(1, [other])));
  act(() => socket.receive(vaultSessions(2, [other])));
  act(() => socket.receive(started(other, 1)));
  act(() => socket.receive(userMessage(other, 2)));
  act(() => socket.receive(researcherStarted(other, 3)));
  expect(conversationTitle(container)).toBe("Untitled session");
  expect(container.querySelector('.routing[data-routing-id="a1"]')).toBeNull();

  fireEvent.click(
    within(rightRailOf(container)).getByRole("button", {
      name: "Jump to researcher in Research session",
    }),
  );
  expect(conversationTitle(container)).toBe("Research session");
  const row = container.querySelector('.routing[data-routing-id="a1"]');
  expect(row?.classList.contains("routing-target")).toBe(true);
  expect(document.activeElement).toBe(row);
  // The row carries the highlight, not its turn.
  expect(row?.closest(".turn")?.classList.contains("turn-search-target")).toBe(false);
});

test("Jump in this chat outlines and focuses the row", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(researcherStarted(sessionId, 2)));
  expect(
    container.querySelector('.routing[data-routing-id="a1"]')?.classList.contains("routing-target"),
  ).toBe(false);

  fireEvent.click(
    within(rightRailOf(container)).getByRole("button", { name: "Jump to researcher in this chat" }),
  );
  const row = container.querySelector<HTMLElement>('.routing[data-routing-id="a1"]');
  expect(row?.classList.contains("routing-target")).toBe(true);
  expect(document.activeElement).toBe(row);
  // The row carries the highlight, not its turn.
  expect(row?.closest(".turn")?.classList.contains("turn-search-target")).toBe(false);
  expect(conversationTitle(container)).toBe("Untitled session");
});

test("the roster sits in a closed Library fold under the run list", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const rightRail = rightRailOf(container);
  const library = rightRail.querySelector<HTMLDetailsElement>("details.agents-library");
  expect(library).not.toBeNull();
  expect(library?.open).toBe(false);
  expect(library?.querySelector(":scope > summary")?.textContent).toBe("Library · 1 subagent");
  expect(
    [...(library?.querySelectorAll(".agent") ?? [])].map((el) => el.getAttribute("data-agent")),
  ).toEqual(["VIKI", "researcher"]);
  // Every roster row is inside the fold, and the fold comes after the run list.
  expect(rightRail.querySelectorAll(".agent")).toHaveLength(2);
  const runs = rightRail.querySelector(".agent-runs");
  expect(runs).not.toBeNull();
  expect(
    library && runs ? runs.compareDocumentPosition(library) & Node.DOCUMENT_POSITION_FOLLOWING : 0,
  ).toBeTruthy();
});

test("a finished run marked reviewed stays Done after leaving the chat view and coming back", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      durable={fakeDurable()}
      viki={viki}
      rest={rest}
      profileApi={emptyProfileApi()}
      vaultApi={pendingVaultApi()}
    />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(researcherStarted(sessionId, 2)));
  act(() =>
    socket.receive(frame(sessionId, 3, "subagent.done", { block_id: "a1", status: "completed" })),
  );

  const runs = () => container.querySelector(".agent-runs");
  const doneSummary = () => container.querySelector(".run-done > summary")?.textContent;
  expect(runs()?.textContent).toContain("Ready for review (1)");
  expect(doneSummary()).toBeUndefined();

  fireEvent.click(within(rightRailOf(container)).getByRole("button", { name: "Mark reviewed" }));
  expect(runs()?.textContent).not.toContain("Ready for review");
  expect(doneSummary()).toBe("Done (1)");

  // Another view unmounts the whole right rail, the run list with it.
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  expect(runs()).toBeNull();

  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(runs()?.textContent).not.toContain("Ready for review");
  expect(doneSummary()).toBe("Done (1)");
});

test.each([
  ["stopped", "normal"],
  ["stopped", "batched"],
  ["completed", "normal"],
  ["completed", "batched"],
  ["stopped", "snapshot"],
  ["completed", "snapshot"],
  ["stopped", "trimmed"],
  ["completed", "trimmed"],
] as const)(
  "a reviewed %s run needs a new review after %s resume delivery",
  async (status, delivery) => {
    const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
    const { viki, socket } = fakeViki();
    const { container } = render(
      <App
        durable={fakeDurable()}
        viki={viki}
        rest={rest}
        profileApi={emptyProfileApi()}
        vaultApi={pendingVaultApi()}
      />,
    );
    act(() => socket.open());
    await waitFor(() => expect(rest.calls).toContain("rails"));
    const sessionId = newSession(socket);
    const initial = [
      userMessage(sessionId, 1),
      researcherStarted(sessionId, 10),
      frame(sessionId, 20, "subagent.done", { block_id: "a1", status }),
      frame(sessionId, 30, "subagent.started", {
        block_id: "sibling",
        agent: "curator",
        model: "",
        description: "Organize sources",
        reason: "",
      }),
      frame(sessionId, 40, "subagent.done", { block_id: "sibling", status: "completed" }),
    ];
    act(() => {
      for (const item of initial) socket.receive(item);
    });
    fireEvent.click(within(rightRailOf(container)).getByRole("button", { name: "Mark reviewed" }));
    fireEvent.click(within(rightRailOf(container)).getByText("Done (2)"));
    fireEvent.click(
      within(rightRailOf(container)).getByRole("button", {
        name: "Jump to researcher in this chat",
      }),
    );
    expect(container.querySelector('.routing[data-routing-id="a1"]')).toBe(document.activeElement);

    const navigation = () => screen.getByRole("navigation", { name: "Navigation and sessions" });
    const resumed = researcherStarted(sessionId, 100);
    const completion = frame(sessionId, 200, "subagent.done", {
      block_id: "a1",
      status: "completed",
    });
    const events = [...initial, resumed, completion];
    if (delivery !== "normal") {
      fireEvent.click(within(navigation()).getByRole("button", { name: "Agents" }));
      expect(container.querySelector(".agent-runs")).toBeNull();
      if (delivery === "batched") {
        act(() => {
          socket.receive(resumed);
          socket.receive(completion);
        });
      } else {
        act(() =>
          socket.receive(
            runSnapshot(
              sessionId,
              delivery === "trimmed"
                ? [...initial.slice(0, 1), ...initial.slice(3), resumed, completion]
                : events,
              delivery === "trimmed",
            ),
          ),
        );
      }
      fireEvent.click(within(navigation()).getByRole("button", { name: "Chat" }));
    } else {
      act(() => socket.receive(resumed));
      expect(rightRailOf(container).textContent).toContain("Working (1)");
      act(() => socket.receive(completion));
    }
    expect(rightRailOf(container).textContent).toContain("Ready for review (1)");
    expect(container.querySelector(".run-done > summary")?.textContent).toBe("Done (1)");
    expect(container.querySelectorAll(`[data-run-key="${sessionId}:a1"]`)).toHaveLength(1);
    expect(container.querySelectorAll(`[data-run-key="${sessionId}:sibling"]`)).toHaveLength(1);
    fireEvent.click(within(rightRailOf(container)).getByRole("button", { name: "Mark reviewed" }));
    const snapshot = runSnapshot(sessionId, events);
    act(() => socket.receive(snapshot));
    act(() => socket.receive(snapshot));
    expect(rightRailOf(container).textContent).not.toContain("Ready for review");
    expect(container.querySelector(".run-done > summary")?.textContent).toBe("Done (2)");
    if (delivery === "snapshot") {
      const unknown = runSnapshot(sessionId, events);
      for (const item of unknown.data.events) {
        if (item.type === "subagent.done" && "block_id" in item.data && item.data.block_id === "a1")
          delete item.source_seq;
      }
      act(() => socket.receive(unknown));
      expect(rightRailOf(container).textContent).toContain("Ready for review (1)");
      expect(container.querySelector(".run-done > summary")?.textContent).toBe("Done (1)");
      fireEvent.click(
        within(rightRailOf(container)).getByRole("button", { name: "Mark reviewed" }),
      );
      expect(rightRailOf(container).textContent).not.toContain("Ready for review");
      expect(container.querySelector(".run-done > summary")?.textContent).toBe("Done (2)");
    }
  },
);

const CAP_COMMAND = "77777777-7777-4777-8777-777777777777";
const EDITOR_FULL =
  "This window has reached its limit of open drafts. Check the save status, then reopen the window.";

/** One send record whose body restores as an empty text. Restoring opens a NEW editor and keeps
 * the current one, so repeating it fills this window's editor table cheaply — the same device
 * src/app-tests/chat/backgroundPermissions.unit.test.tsx's own fillEditors uses to reach the real cap. */
function durableWithSubmission(): FakeDurable {
  const durable = fakeDurable();
  const receipt: CommandReceipt = {
    command_id: CAP_COMMAND,
    session_id: "s-cap",
    generation: GENERATION,
    draft_revision: 1,
    state: "accepted",
    reason: "",
    created_at: "2026-10-08T09:00:00.000Z",
    updated_at: "2026-10-08T09:00:00.000Z",
    bytes: 0,
  };
  durable.listCommands = async () => ({ commands: [receipt], next_after: null });
  durable.getSubmission = async () => ({ ...receipt, text: "" });
  return durable;
}

/** Restores that one body over and over until the editor store refuses the next editor (state/
 * editors.ts MAX_EDITORS = 64) — the only honest way to reach the cap is through the real UI. */
async function fillEditors(limit: number): Promise<void> {
  fireEvent.click(screen.getByText("Drafts and send history"));
  const restore = await screen.findByLabelText(`Restore text from send ${CAP_COMMAND}`);
  for (let index = 0; index < limit; index++) {
    if (screen.queryByText(EDITOR_FULL)) return;
    await act(async () => {
      fireEvent.click(restore);
    });
  }
  throw new Error("the editor cap was never reached");
}

// Like src/app-tests/chat/backgroundPermissions.unit.test.tsx's own cap tests, this one's real App renders (fillEditors)
// can run long enough beside the rest of the suite to brush vitest's default 5 s per-test
// timeout; 30_000 below is that hang guard, not a bound on the work.
test("a Jump the app refuses changes nothing and marks nothing", async () => {
  const other = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const mine = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const rest = fakeRest({
    rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] },
    sessions: [
      sessionRow({ session_id: other, title: "Research session" }),
      sessionRow({ session_id: mine, title: "My chat" }),
    ],
  });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={durableWithSubmission()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  // `other` finishes a "researcher" run in the background, unreviewed, exactly as the cross-chat
  // Jump test above sets one up — except this run is already done, so it sits in Ready for
  // review and Jump would otherwise mark it reviewed. `mine` gets a user turn of its own at the
  // same seq, so its turn id ("local-2") collides with the turn the run lives under in `other`.
  act(() => socket.receive(vaultSessions(1, [other, mine])));
  act(() => socket.receive(vaultSessions(2, [other, mine])));
  act(() => socket.receive(started(mine, 1)));
  act(() => socket.receive(userMessage(mine, 2)));
  act(() => socket.receive(started(other, 1)));
  act(() => socket.receive(userMessage(other, 2)));
  act(() => socket.receive(researcherStarted(other, 3)));
  act(() =>
    socket.receive(frame(other, 4, "subagent.done", { block_id: "a1", status: "completed" })),
  );
  const runs = () => rightRailOf(container).querySelector(".agent-runs");
  expect(runs()?.textContent).toContain("Ready for review (1)");

  // Select `mine` once, while there is still room, so it already holds an editor record; once
  // the cap below leaves the blank new-chat editor on screen (see fillEditors), reselecting
  // `mine` switches back to it for free. That puts a real chat, with a real turn, on screen for
  // the refused Jump below, so its "nothing is outlined or focused" assertions have something to
  // fail against.
  const mineRow = () => container.querySelector(`.sess[data-session-id="${mine}"]`) as Element;
  await act(async () => {
    fireEvent.click(mineRow());
  });

  // Exhaust this window's editor capacity, so the openSession the next Jump attempts is a real
  // refusal (allowEditor), not a contrived one.
  await fillEditors(80);

  await act(async () => {
    fireEvent.click(mineRow());
  });
  expect(conversationTitle(container)).toBe("My chat");

  fireEvent.click(
    within(rightRailOf(container)).getByRole("button", {
      name: "Jump to researcher in Research session",
    }),
  );

  // Nothing moved: the chat on screen is the same, nothing is outlined or focused as a jump
  // target, and the run is still waiting to be reviewed rather than marked by a jump that never
  // landed.
  expect(conversationTitle(container)).toBe("My chat");
  expect(container.querySelector(".routing-target")).toBeNull();
  expect(container.querySelector(".turn-search-target")).toBeNull();
  expect(runs()?.textContent).toContain("Ready for review (1)");
}, 30_000);

test("the run list reads a running run's transcript through the app's subagentsApi", async () => {
  const rest = fakeRest({ rails: { ...emptyRails(), agents: [agent({ name: "researcher" })] } });
  const { viki, socket } = fakeViki();
  const read = vi.fn(
    async (): Promise<RunTranscript> => ({
      prompt: "",
      lines: [],
      omitted: 0,
      partial: false,
      startedAt: null,
      tokens: null,
    }),
  );
  const subagentsApi: SubagentsApi = { read };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} subagentsApi={subagentsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(researcherStarted(sessionId, 2)));
  // The effect that calls read() runs synchronously on commit; this only lets its promise settle
  // so a stray "unhandled rejection" warning can't mask an assertion failure below.
  await act(async () => {
    await Promise.resolve();
  });

  const openArea = container.querySelector(`[data-run-key="${sessionId}:a1"] .run-open`);
  expect(openArea?.tagName).toBe("BUTTON");
  expect(read).toHaveBeenCalledWith(sessionId, "a1", expect.any(AbortSignal));
});
