/**
 * Acceptance (frozen): the app body. Every surface below has its own frozen test proving it
 * renders correctly in isolation — the conversation (m2b/), the cards (m2b2/), the error and
 * empty states (m2b3/), the composer and its menus (m2c/, m2c2/), the row states, the limit
 * toast and the reconnect banner (m3c/). None of those tests mounts App. This file pins the
 * seam, and only the seam: what App mounts, what it feeds each surface, and what a click
 * there puts on the wire.
 *
 * It deliberately does not re-assert wording or layout. If a test here fails it should mean the
 * wiring broke, not that a sentence was reworded — those belong to the surface's own contract.
 *
 * Real timers throughout: the fakes resolve at once. Date is pinned, because App reads the real
 * clock and the fixtures are dated (see m3b/app.accept.test.tsx for the same reason).
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import type { Command, Envelope, EventMap, ModelOption, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, gate, localIso, sessionRow } from "../m3b/fakes";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 12, h, m);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(day(20, 0)));
});

afterEach(() => {
  vi.useRealTimers();
});

const COMMANDS: Command[] = [
  {
    name: "/capture",
    description: "Drop a note into 00-inbox",
    argument_hint: "<text>",
    skill: true,
  },
  { name: "/review", description: "review the diff", argument_hint: "", skill: false },
];

const MODELS: ModelOption[] = [
  { value: "sonnet", label: "Sonnet 5", description: "fast" },
  { value: "opus", label: "Opus 5", description: "deep" },
];

const rows = [
  sessionRow({ session_id: "s-1", title: "Dashboard v1 → reverted", modified: day(17, 20) }),
  sessionRow({ session_id: "s-2", title: "retro H1–H4", modified: day(16, 0) }),
  sessionRow({ session_id: "s-3", title: "Buy milk", modified: day(15, 0) }),
];

function rails(): Rails {
  return {
    agents: [],
    gates: [gate({ id: "H1", item: "/recall skill", label: "medium" })],
    routines: [],
    counts: { knowledge: 116, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: day(19, 16),
      dirty: 3,
    },
  };
}

/** Mounted and connected. `open: false` leaves the socket as a page that has never reached the
 * server, which is a different thing from one that has lost it. */
function mount({ open = true }: { open?: boolean } = {}) {
  const rest = fakeRest({ sessions: rows, rails: rails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  if (open) {
    act(() => socket.open());
    socket.sent.length = 0;
  }
  return { ...utils, durable, viki, socket };
}

/** One envelope for `session`, at `seq`. The reducer keys off seq, so every helper below
 * threads its own counter rather than sharing one. */
function frame<K extends keyof EventMap>(
  session: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type,
    session_id: session,
    seq,
    data,
  } as Envelope;
}

function started(over: Partial<EventMap["session.started"]> = {}): EventMap["session.started"] {
  return {
    model: "sonnet",
    mode: "auto",
    effort: "low",
    commands: COMMANDS,
    agents: [],
    models: MODELS,
    cli_version: "2.1.259",
    resumed: false,
    ...over,
  };
}

/** Open the list's first row and let its session report itself started. */
async function openFirstRow(container: HTMLElement, socket: ReturnType<typeof mount>["socket"]) {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("the list has not arrived");
    return found;
  });
  fireEvent.click(row);
  act(() => socket.receive(frame("s-1", 1, "session.started", started())));
  socket.sent.length = 0;
  return row;
}

const box = () => screen.getByLabelText("Message") as HTMLTextAreaElement;
const sentOf = (socket: ReturnType<typeof mount>["socket"], type: string) =>
  socket.sent.filter((message) => message.type === type);

// --- the conversation body ------------------------------------------------------------------

test("with nothing open the column greets rather than showing an empty box", () => {
  const { container } = mount();
  expect(container.querySelector(".empty")).not.toBeNull();
  expect(container.querySelector(".conversation-list")).toBeNull();
});

test("an open session's turns render in the column, and stream in place", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  act(() =>
    socket.receive(frame("s-1", 2, "user.message", { text: "milk", command: "", at: day(18, 0) })),
  );
  act(() => socket.receive(frame("s-1", 3, "turn.started", { turn_id: "t1" })));
  act(() => socket.receive(frame("s-1", 4, "text.delta", { block_id: "b1", text: "Adding" })));
  expect(container.querySelector(".conversation-list")).not.toBeNull();
  expect(container.querySelector(".user-turn")?.textContent).toContain("milk");
  expect(container.querySelector(".reply")?.textContent).toContain("Adding");
  act(() => socket.receive(frame("s-1", 5, "text.delta", { block_id: "b1", text: " it now" })));
  expect(container.querySelectorAll(".reply")).toHaveLength(1);
  expect(container.querySelector(".reply")?.textContent).toContain("Adding it now");
  expect(container.querySelector(".empty")).toBeNull();
});

test("the header names the open session", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  expect(container.querySelector(".conversation-title")?.textContent).toBe(
    "Dashboard v1 → reverted",
  );
  expect(container.querySelector(".conversation-header .participants")).toBeNull();
});

// --- the composer ---------------------------------------------------------------------------

test("the composer is the real one, and Enter puts the owner's text on the wire", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  expect(container.querySelector(".composer-placeholder")).toBeNull();
  fireEvent.change(box(), { target: { value: "・牛乳\n・卵" } });
  await act(async () => {
    fireEvent.keyDown(box(), { key: "Enter" });
  });
  expect(sentOf(socket, "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([
    {
      session_id: "s-1",
      generation: expect.any(String),
      command_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      draft_revision: expect.any(Number),
    },
  ]);
  expect([...durable.commands.values()][0]?.text).toBe("・牛乳\n・卵");
  expect(box().value).toBe("");
});

test("a running turn shows Stop, and Stop interrupts that session", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  expect(container.querySelector(".composer-stop")).toBeNull();
  act(() => socket.receive(frame("s-1", 2, "turn.started", { turn_id: "t1" })));
  const stop = container.querySelector(".composer-stop") as HTMLElement;
  expect(stop).not.toBeNull();
  expect(screen.queryByLabelText("Send")).toBeNull();
  fireEvent.click(stop);
  expect(sentOf(socket, "turn.interrupt")).toEqual([
    {
      generation: expect.any(String),
      command_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      type: "turn.interrupt",
      session_id: "s-1",
    },
  ]);
});

test("the slash menu is built from the session's own commands, not a hard-coded list", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  fireEvent.change(box(), { target: { value: "/cap" } });
  const menu = container.querySelector(".menu") as HTMLElement;
  expect(menu).not.toBeNull();
  expect(menu.textContent).toContain("/capture");
});

test("the model chip reports the session's model", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  expect(container.querySelector(".model-chip")?.textContent).toContain("Sonnet 5");
});

/* AMENDMENT (2026-10-20). Retiring the retro feed retires this file's own frozen
   "Awaiting decision" pick test too: it asserted a `.q[data-gate-id]`
   pick landing in the composer, and nothing renders that element anymore. Deleted here; every
   other case is unaffected. */

// --- the cards ------------------------------------------------------------------------------

test("a permission request becomes a card, and Allow once answers the request it came from", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  act(() =>
    socket.receive(
      frame("s-1", 2, "permission.requested", {
        tool_use_id: "tu-9",
        kind: "tool",
        tool: "Write",
        summary: "30-knowledge/x.md",
        question: "May I create or overwrite this file?",
        reason: "",
        agent: "",
        plan: "",
      }),
    ),
  );
  const card = container.querySelector(".card") as HTMLElement;
  expect(card).not.toBeNull();
  fireEvent.click(screen.getByText("Allow once"));
  // The App answers through the durable REST ledger; the socket carries no card choice.
  await waitFor(() => expect(durable.approvals.size).toBe(1));
  expect([...durable.approvals.values()][0]).toMatchObject({
    choice_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    session_id: "s-1",
    generation: expect.any(String),
    tool_use_id: "tu-9",
    native_request: {
      runtime_id: expect.any(String),
      request_id: expect.any(String),
      turn_id: expect.any(String),
    },
    choice: { kind: "permission", allow: true },
  });
  expect(sentOf(socket, "permission.respond")).toEqual([]);
});

// --- the reconnect banner ---------------------------------------------------------------------

test("the banner appears only once a socket that was open has dropped", () => {
  const { container, socket } = mount({ open: false });
  // Never connected: there is no interrupted reply to promise, so nothing is said.
  expect(container.querySelector(".banner")).toBeNull();
  act(() => socket.open());
  expect(container.querySelector(".banner")).toBeNull();
  act(() => socket.onclose?.({ code: 1006 }));
  expect(container.querySelector(".banner")).not.toBeNull();
  // It sits between the header and the scroll, where it pushes the conversation rather than
  // covering it.
  const conversation = container.querySelector(".conversation") as HTMLElement;
  const kids = [...conversation.children].map((el) => el.className);
  expect(kids.indexOf("banner jp")).toBe(kids.indexOf("conversation-header") + 1);
});

// --- the session limit ------------------------------------------------------------------------

test("a fourth live session is refused by name, and the toast closes nothing itself", async () => {
  const { container, socket } = mount();
  const newSession = container.querySelector(".new-session") as HTMLElement;
  expect(newSession).not.toBeNull();
  for (let i = 0; i < 3; i++) fireEvent.click(newSession);
  expect(container.querySelector(".toast")).toBeNull();

  fireEvent.click(newSession);
  const toast = container.querySelector(".toast") as HTMLElement;
  expect(toast).not.toBeNull();
  const closes = toast.querySelectorAll(".toast-row");
  expect(closes).toHaveLength(3);
  // The refused session was never opened: only the three that existed are tracked.
  expect(sentOf(socket, "session.open")).toHaveLength(3);

  socket.sent.length = 0;
  fireEvent.click(closes[0]?.querySelector("button") as HTMLElement);
  expect(sentOf(socket, "session.close")).toHaveLength(1);
  // Nothing auto-dismissed: the close can be refused, and App dismisses on the close landing.
  expect(container.querySelector(".toast")).not.toBeNull();
});

// --- the row menu ------------------------------------------------------------------------------

test("the row menu asks the server to open that session's own transcript", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  const menuButton = container.querySelector('[data-session-id="s-1"] .sess-menu') as HTMLElement;
  expect(menuButton).not.toBeNull();
  fireEvent.click(menuButton);
  fireEvent.click(screen.getByText("Open transcript in VS Code"));
  expect(sentOf(socket, "open.transcript")).toEqual([
    { type: "open.transcript", session_id: "s-1" },
  ]);
});

test("the rail's rows read their state from the app, not from liveIds alone", async () => {
  const { container, socket } = mount();
  await openFirstRow(container, socket);
  const row = container.querySelector('[data-session-id="s-1"]') as HTMLElement;
  expect(row.querySelector(".m")?.textContent).toContain("live · idle");
  act(() => socket.receive(frame("s-1", 2, "turn.started", { turn_id: "t1" })));
  act(() => socket.receive(frame("s-1", 3, "text.delta", { block_id: "b1", text: "…" })));
  expect(row.querySelector(".m")?.textContent).toContain("writing…");
});

/* Added 2026-09-13 (no assertion changed): the fork flow is three steps across
   three components — arm from a card or a row menu, tell the user the next message will fork,
   then send through actions.fork instead of actions.send — and App is the only place that holds
   all three. Each piece was covered; the wiring between them was not. */

// --- the fork ---------------------------------------------------------------------------------

test("a blocked session forks on the next message, and says so before it does", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  act(() =>
    socket.receive(
      frame("s-1", 2, "session.blocked", {
        reason: "live_elsewhere",
        holder: { session_id: "s-1", entrypoint: "claude-vscode", pid: 4242, title: "retro" },
      }),
    ),
  );
  // Nothing is armed yet: the card is an offer, not a decision.
  expect(container.querySelector(".fork-note")).toBeNull();

  fireEvent.click(screen.getByText("Continue in a fork"));
  expect(container.querySelector(".fork-note")).not.toBeNull();
  expect(sentOf(socket, "turn.send")).toEqual([]);

  fireEvent.change(box(), { target: { value: "Carry on" } });
  await act(async () => {
    fireEvent.keyDown(box(), { key: "Enter" });
  });
  expect(sentOf(socket, "turn.send")).toEqual([]);
  const sends = [...durable.commands.values()];
  expect(sends).toHaveLength(1);
  const fork = sentOf(socket, "session.fork")[0];
  expect(fork?.session_id).toBe("s-1");
  expect(sends[0]?.session_id).toBe(fork?.fork_id);
  expect(sends[0]?.text).toBe("Carry on");
  expect(typeof fork?.fork_id).toBe("string");
  // The arm is spent: the hint is gone and the next message is an ordinary send.
  expect(container.querySelector(".fork-note")).toBeNull();
});

test("picking a different session drops an arm rather than forking the wrong one", async () => {
  const { container, socket, durable } = mount();
  await openFirstRow(container, socket);
  act(() =>
    socket.receive(
      frame("s-1", 2, "session.blocked", {
        reason: "live_elsewhere",
        holder: { session_id: "s-1", entrypoint: "cli", pid: 99, title: "" },
      }),
    ),
  );
  fireEvent.click(screen.getByText("Continue in a fork"));
  expect(container.querySelector(".fork-note")).not.toBeNull();

  fireEvent.click(container.querySelector('[data-session-id="s-2"]') as HTMLElement);
  act(() => socket.receive(frame("s-2", 1, "session.started", started())));
  expect(container.querySelector(".fork-note")).toBeNull();
  socket.sent.length = 0;

  fireEvent.change(box(), { target: { value: "Something else" } });
  await act(async () => {
    fireEvent.keyDown(box(), { key: "Enter" });
  });
  expect(sentOf(socket, "turn.send")).toEqual([]);
  expect(durable.submissions).toEqual([
    {
      session_id: "s-2",
      generation: expect.any(String),
      command_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      draft_revision: expect.any(Number),
    },
  ]);
  expect([...durable.commands.values()][0]?.text).toBe("Something else");
});
