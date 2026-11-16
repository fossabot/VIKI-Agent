/**
 * App-level wiring for the runs client, and for Needs you.
 * `runsApi` feeds `useRunRequests`, whose per-session attribution reaches both `needsYou()`'s and
 * AgentRuns' own `requests`; `onDecide` is `decideRequest`, a fresh read of `viki.store` that
 * answers through the same durable `approvals.decide` the chat's own cards use; `runs` reaches
 * both AgentRuns' own Stop and, for a run that needs the user (hidden from AgentRuns by its own
 * `hideNeeds`), the matching Stop in Needs you. The attribution rule, the Stop state machine and
 * RequestActions' own rendering are proven at the component level already
 * (state/runGroups.unit.test.ts, components/RightRail/AgentRuns.unit.test.tsx, RunStop.unit.test.tsx,
 * RequestActions.unit.test.tsx, NeedsYou.unit.test.tsx); what only a full App render can prove is
 * that these props, and `decideRequest`'s own guard, actually reach there from a real
 * socket-driven session — including that AgentRuns and Needs you share one
 * `useRunStops` (App's own, over the groups), so a run Stopped from either keeps its phase if it
 * moves to the other list before it ends.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  emptyRails,
  type FakeSocket,
  fakeRest,
  fakeViki,
  GENERATION,
} from "../../accept/m3b/fakes";
import type { OpenRequest, RunsApi } from "../../clients/conversations/runsApi";
import type { RunTranscript, SubagentsApi } from "../../clients/conversations/subagentsApi";
import type { Envelope, EventMap, NativeRequest } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.useRealTimers();
});

function frame<K extends keyof EventMap>(
  sessionId: string,
  seq: number,
  type: K,
  data: EventMap[K],
  nativeRequest?: NativeRequest,
): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq,
    data,
    ...(nativeRequest ? { native_request: nativeRequest } : {}),
  } as Envelope;
}

function userMessage(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "user.message", {
    text: "hi",
    command: "",
    at: "2026-10-08T09:00:00.000Z",
  });
}

/** A "researcher" run on `blockId`, starting in the session's current turn. */
function subagentStarted(
  sessionId: string,
  seq: number,
  blockId: string,
  description: string,
): Envelope {
  return frame(sessionId, seq, "subagent.started", {
    block_id: blockId,
    agent: "researcher",
    model: "",
    description,
    reason: "",
  });
}

function subagentDone(
  sessionId: string,
  seq: number,
  blockId: string,
  status: "completed" | "failed" | "stopped",
): Envelope {
  return frame(sessionId, seq, "subagent.done", { block_id: blockId, status });
}

/** A "tool" permission card naming the "researcher" agent, unattributed until a caller's own
 * `RunsApi.requests()` or the single-running-row fallback (runGroups.ts) joins it to a block.
 * Without `nativeRequest`, the fake socket stamps its own (a fresh id each time — fine here,
 * since nothing then tries to cancel or answer this exact request by id); a caller that does
 * must pass the same binding to both frames (FakeSocket only self-stamps `permission.requested`
 * and `question.asked`, never a withdrawal or an answer). */
function permissionRequested(
  sessionId: string,
  seq: number,
  toolUseId: string,
  nativeRequest?: NativeRequest,
): Envelope {
  return frame(
    sessionId,
    seq,
    "permission.requested",
    {
      tool_use_id: toolUseId,
      kind: "tool",
      tool: "Bash",
      summary: "run tests",
      question: "",
      reason: "",
      agent: "researcher",
      plan: "",
    },
    nativeRequest,
  );
}

/** The CLI withdraws an open request (e.g. the run moved on) without the run itself ending.
 * `nativeRequest` must match the request's own `permissionRequested` binding exactly
 * (sessionReducer's `sameRequest`), or the card it names is kept, not removed. */
function requestCancelled(
  sessionId: string,
  seq: number,
  toolUseId: string,
  nativeRequest?: NativeRequest,
): Envelope {
  return frame(sessionId, seq, "request.cancelled", { tool_use_id: toolUseId }, nativeRequest);
}

/** Clicks New session and returns the new chat's id: the chat on screen from then on — copied
 * from src/app-tests/agents/runPanel.unit.test.tsx (every App test file here scripts its own frames). */
function newSession(socket: FakeSocket): string {
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  return opened.session_id;
}

test("a card the requests read joins to the second of two same-type runs lists under Needs you", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const requests = vi.fn(
    async (): Promise<OpenRequest[]> => [{ toolUseId: "tu1", run: "a2", raisedAt: Date.now() }],
  );
  const stop = vi.fn(async () => {});
  const runsApi: RunsApi = { requests, stop };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} runsApi={runsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(subagentStarted(sessionId, 3, "a2", "Draft outline")));
  act(() => socket.receive(permissionRequested(sessionId, 4, "tu1")));

  await waitFor(() => expect(requests).toHaveBeenCalledWith(sessionId, expect.any(AbortSignal)));

  // The card now lists only under Needs you (AgentRuns mounts with hideNeeds); its own RunStop,
  // sent, names the run it joined to directly — proving the join to a2, not a1, by id.
  const row = () => container.querySelector(".need-native") as HTMLElement;
  await waitFor(() => expect(row()).not.toBeNull());
  fireEvent.click(within(row()).getByRole("button", { name: "Stop researcher in this chat" }));
  fireEvent.click(within(row()).getByRole("button", { name: "Stop run" }));
  expect(stop).toHaveBeenCalledWith(sessionId, "a2");
});

test("Allow once in Needs you reaches the durable ledger as allow: true", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
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
  render(<App durable={durable} viki={viki} rest={rest} subagentsApi={subagentsApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1")));

  // The card is in the selected column until a new chat makes it off screen.
  fireEvent.click(screen.getByText("New session"));
  const allowOnce = await screen.findByRole("button", { name: "Allow once: Bash run tests" });
  fireEvent.click(allowOnce);

  await waitFor(() => expect(durable.approvals.size).toBe(1));
  expect([...durable.approvals.values()][0]?.choice).toEqual({ kind: "permission", allow: true });
});

test("Deny in Needs you reaches the durable ledger as allow: false", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
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
  render(<App durable={durable} viki={viki} rest={rest} subagentsApi={subagentsApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1")));

  fireEvent.click(screen.getByText("New session"));
  const deny = await screen.findByRole("button", { name: "Deny: Bash run tests" });
  fireEvent.click(deny);

  await waitFor(() => expect(durable.approvals.size).toBe(1));
  expect([...durable.approvals.values()][0]?.choice).toEqual({ kind: "permission", allow: false });
});

test("answering while the socket is closed shows the not-ready error, never the durable ledger", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
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
  render(<App durable={durable} viki={viki} rest={rest} subagentsApi={subagentsApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1")));

  fireEvent.click(screen.getByText("New session"));
  const allowOnce = await screen.findByRole("button", { name: "Allow once: Bash run tests" });

  // decideRequest's guard: snapshot.status !== "open" rejects with not_ready before approvals.decide.
  act(() => socket.onclose?.({ code: 1006 }));

  await act(async () => {
    fireEvent.click(allowOnce);
  });

  expect((await screen.findByRole("alert")).textContent).toBe(
    "Could not send your answer. Try again.",
  );
  expect(durable.approvals.size).toBe(0);
});

test("Stop, then Stop run, calls stop(sid, blockId) and shows Stopping…; a stopped subagent.done moves the row to Done", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const stop = vi.fn(async () => {});
  const runsApi: RunsApi = { requests: vi.fn(async () => []), stop };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} runsApi={runsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));

  const row = () => container.querySelector(`[data-run-key="${sessionId}:a1"]`) as HTMLElement;
  fireEvent.click(within(row()).getByRole("button", { name: "Stop researcher in this chat" }));
  fireEvent.click(within(row()).getByRole("button", { name: "Stop run" }));

  expect(stop).toHaveBeenCalledWith(sessionId, "a1");
  expect(within(row()).getByRole("status").textContent).toContain("Stopping…");

  act(() => socket.receive(subagentDone(sessionId, 3, "a1", "stopped")));

  await waitFor(() => {
    expect(container.querySelector(".run-done > summary")?.textContent).toBe("Done (1)");
  });
});

test("no runsApi means a running row offers no Stop", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));

  const row = container.querySelector(`[data-run-key="${sessionId}:a1"]`) as HTMLElement;
  expect(within(row).queryByRole("button", { name: "Stop researcher in this chat" })).toBeNull();
  expect(row.querySelector(".run-stop")).toBeNull();
});

test("a tool request raised a minute and a little ago shows waiting 1m 5s on its Needs you row", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-08T09:01:05.000Z"));

  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const requests = vi.fn(
    async (): Promise<OpenRequest[]> => [
      { toolUseId: "tu1", run: "a1", raisedAt: Date.parse("2026-10-08T09:00:00.000Z") },
    ],
  );
  const runsApi: RunsApi = { requests, stop: vi.fn(async () => {}) };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} runsApi={runsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1")));

  await waitFor(() => expect(requests).toHaveBeenCalledWith(sessionId, expect.any(AbortSignal)));
  const row = () => container.querySelector(".need-native") as HTMLElement;
  await waitFor(() => expect(row()).not.toBeNull());

  expect(row().querySelector(".need-meta")?.textContent).toBe(
    "this chat · waiting for approval · researcher · waiting 1m 5s",
  );
});

// --- one stop state for a run wherever it lists -----------------------------------------------

test("a stop sent from Needs you, then the request cancelled before the run ends: the Working row shows Stopping…, and after 30 s the unconfirmed line with Try again", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date("2026-10-08T09:00:00.000Z"));
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const stop = vi.fn(async () => {});
  const runsApi: RunsApi = { requests: vi.fn(async () => []), stop };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} runsApi={runsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  // request.cancelled only withdraws the card whose binding it names exactly (sessionReducer's
  // sameRequest) — the fake socket would otherwise stamp permission.requested with a fresh,
  // unpredictable id of its own, so this pins one both frames share.
  const nr: NativeRequest = { runtime_id: GENERATION, request_id: "tu1-request", turn_id: "t1" };
  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1", nr)));

  const need = () => container.querySelector(".need-native") as HTMLElement;
  await waitFor(() => expect(need()).not.toBeNull());
  fireEvent.click(within(need()).getByRole("button", { name: "Stop researcher in this chat" }));
  await act(async () => {
    fireEvent.click(within(need()).getByRole("button", { name: "Stop run" }));
  });
  expect(stop).toHaveBeenCalledWith(sessionId, "a1");
  expect(within(need()).getByRole("status").textContent).toContain("Stopping…");

  // The request is withdrawn before the run itself ends: the row leaves Needs you for
  // AgentRuns' own Working group. Without a shared RunStops, Working would start it idle again
  // (the unconfirmed line could then never appear) instead of keeping the same "Stopping…".
  act(() => socket.receive(requestCancelled(sessionId, 4, "tu1", nr)));
  await waitFor(() => expect(container.querySelector(".need-native")).toBeNull());
  const row = () => container.querySelector(`[data-run-key="${sessionId}:a1"]`) as HTMLElement;
  expect(within(row()).getByRole("status").textContent).toContain("Stopping…");

  // 31 s pass with no new frame; AgentRuns' own 1 s interval updates the elapsed stop state.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(31_000);
  });
  expect(within(row()).getByText("Stop sent; Claude Code has not confirmed it yet.")).toBeTruthy();
  expect(within(row()).getByRole("button", { name: "Try again" })).toBeTruthy();
});

test("a stop sent from Working, then the run asks: Needs you's row shows Stopping…", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const stop = vi.fn(async () => {});
  const runsApi: RunsApi = { requests: vi.fn(async () => []), stop };
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} runsApi={runsApi} />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));

  const row = () => container.querySelector(`[data-run-key="${sessionId}:a1"]`) as HTMLElement;
  fireEvent.click(within(row()).getByRole("button", { name: "Stop researcher in this chat" }));
  fireEvent.click(within(row()).getByRole("button", { name: "Stop run" }));
  expect(stop).toHaveBeenCalledWith(sessionId, "a1");
  expect(within(row()).getByRole("status").textContent).toContain("Stopping…");

  // The run raises its own request while the stop is still in flight (the reverse order): the
  // row leaves AgentRuns' own Working (hidden there by hideNeeds) for Needs you instead.
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1")));
  const need = () => container.querySelector(".need-native") as HTMLElement;
  await waitFor(() => expect(need()).not.toBeNull());
  expect(within(need()).getByRole("status").textContent).toContain("Stopping…");
});

// --- focus survives the section's own last row ------------------------------------------------

test("an applied Allow once that empties Needs you lands focus on the run's own row in Working, not the page", async () => {
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  // request.answered only drops the card whose binding it names exactly (sessionReducer's
  // sameRequest), same as requestCancelled above: this pins one both frames share.
  const nr: NativeRequest = { runtime_id: GENERATION, request_id: "tu1-request", turn_id: "t1" };
  const { container } = render(<App durable={durable} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(subagentStarted(sessionId, 2, "a1", "Survey sources")));
  act(() => socket.receive(permissionRequested(sessionId, 3, "tu1", nr)));

  fireEvent.click(screen.getByText("New session"));
  const allowOnce = await screen.findByRole("button", { name: "Allow once: Bash run tests" });
  allowOnce.focus();
  fireEvent.click(allowOnce);
  await waitFor(() => expect(durable.approvals.size).toBe(1));

  // The server confirms the answer: Needs you drops to zero rows and the whole section unmounts
  // with it, Open Approvals included — this is the section's own last-row case, not its sibling one.
  act(() => socket.receive(frame(sessionId, 4, "request.answered", { tool_use_id: "tu1" }, nr)));
  await waitFor(() => expect(container.querySelector(".need-native")).toBeNull());

  const workingRow = container.querySelector(`[data-run-key="${sessionId}:a1"]`);
  expect(workingRow).not.toBeNull();
  expect(document.activeElement).not.toBe(document.body);
  expect(workingRow?.contains(document.activeElement)).toBe(true);
});
