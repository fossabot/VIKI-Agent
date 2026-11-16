/**
 * AgentRuns: the right rail's run list. Builds its own small session/turn/routing-block fixtures
 * (the same shapes runGroups.unit.test.ts uses) rather than going through a live socket, since
 * grouping itself is already covered there — these tests cover what this component renders and
 * wires on top of that groups.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { RunsApi } from "../../clients/conversations/runsApi";
import type { RunTranscript, SubagentsApi } from "../../clients/conversations/subagentsApi";
import type { PermissionRequestedData } from "../../protocol";
import type { RunRequests } from "../../state/runGroups";
import type { Card, RoutingBlock, SessionView, Turn } from "../../state/view";
import { emptyView } from "../../state/view";
import { AgentRuns, type AgentRunsProps } from "./AgentRuns";
import type { RunStops } from "./RunStop";

afterEach(cleanup);

const RUNTIME = "33333333-3333-4333-8333-333333333333";
const TURN_REQ = "55555555-5555-4555-8555-555555555555";

function routing(id: string, overrides: Partial<RoutingBlock> = {}): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: "researcher",
    model: "sonnet",
    description: "Find the daily note template",
    reason: "",
    status: "running",
    tools: [],
    ...overrides,
  };
}

function turn(id: string, blocks: RoutingBlock[], overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks,
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function view(sessionId: string, overrides: Partial<SessionView> = {}): SessionView {
  return { ...emptyView(sessionId), ...overrides };
}

function permissionCard(
  requestId: string,
  agentName: string,
  overrides: Partial<PermissionRequestedData> = {},
): Extract<Card, { kind: "permission" }> {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "",
      reason: "",
      agent: agentName,
      plan: "",
      ...overrides,
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN_REQ },
  };
}

function baseProps(overrides: Partial<AgentRunsProps> = {}): AgentRunsProps {
  return {
    sessions: {},
    selectedId: null,
    labelOf: (id) => `Chat ${id}`,
    nowMs: 0,
    reviewed: new Set<string>(),
    onReviewed: vi.fn(),
    onJump: vi.fn(),
    api: undefined,
    ...overrides,
  };
}

function transcript(overrides: Partial<RunTranscript> = {}): RunTranscript {
  return {
    prompt: "",
    lines: [],
    omitted: 0,
    partial: false,
    startedAt: null,
    tokens: null,
    ...overrides,
  };
}

function fakeApi(result: Partial<RunTranscript> = {}): SubagentsApi {
  return { read: vi.fn().mockResolvedValue(transcript(result)) };
}

function fakeRunsApi(stop: RunsApi["stop"] = vi.fn().mockResolvedValue(undefined)): RunsApi {
  return { requests: vi.fn().mockResolvedValue([]), stop };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("shows the empty line when no open chat holds a run", () => {
  render(<AgentRuns {...baseProps()} />);
  expect(screen.getByText("No agent runs in open chats.")).toBeTruthy();
  expect(document.querySelector(".run-group")).toBeNull();
});

test("renders only the non-empty groups, in order, each counted; an empty one is hidden", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1"), routing("r2", { status: "completed" })])],
      cards: [permissionCard("req-1", "researcher")],
    }),
    s2: view("s2", {
      turns: [turn("t0", [routing("r3", { status: "completed" })], { history: true })],
    }),
  };
  // r1 (running, claimed by the card) -> needs; r2 (completed, live, unreviewed) -> review;
  // r3 (completed, from history) -> done. Nothing is left running without a request, so
  // Working has no rows at all and must not render its heading.
  const { container } = render(<AgentRuns {...baseProps({ sessions })} />);

  const headings = [...container.querySelectorAll(".run-group .lbl")].map((el) => el.textContent);
  expect(headings).toEqual(["Needs you (1)", "Ready for review (1)"]);
  expect(screen.queryByText(/^Working/)).toBeNull();
  expect(container.querySelector(".run-done summary")?.textContent).toBe("Done (1)");
  expect(container.querySelector('[data-run-key="s1:r1"]')).toBeTruthy();
  expect(container.querySelector('[data-run-key="s1:r2"]')).toBeTruthy();
  expect(container.querySelector('[data-run-key="s2:r3"]')).toBeTruthy();
});

test("a chat-level need names the sole agent its requests agree on, never the generic fallback", () => {
  const named = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "connector")], // no running "connector" row -> chat level
    }),
  };
  const onJump = vi.fn();
  const { container } = render(<AgentRuns {...baseProps({ sessions: named, onJump })} />);
  expect(screen.getByText("A subagent is waiting for approval · connector")).toBeTruthy();
  const need = container.querySelector(".run-need") as HTMLElement;
  fireEvent.click(need.querySelector(".run-jump") as HTMLElement);
  expect(onJump).toHaveBeenCalledWith("s1", null);
  cleanup();

  const fallback = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "subagent")],
    }),
  };
  render(<AgentRuns {...baseProps({ sessions: fallback })} />);
  expect(screen.getByText("A subagent is waiting for approval")).toBeTruthy();
});

test("a running row's counters tick once a second once a fake api reports its stats", async () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(0));
    const api = fakeApi({ startedAt: 0, tokens: 500 });
    const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
    render(<AgentRuns {...baseProps({ sessions, api, selectedId: "s1", nowMs: 0 })} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("this chat · 0s · ~500 tokens")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("this chat · 1s · ~500 tokens")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

test("the doing-now line shows the newest nested tool row, or a finished run's own summary", () => {
  const running = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
      {
        id: "tr2",
        tool: "Grep",
        row: { head: "rg", rest: "-il linter", tail: "" },
        done: false,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  const done = routing("r2", { status: "completed", summary: "Found the template." });
  const sessions = { s1: view("s1", { turns: [turn("t0", [running, done])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions })} />);

  expect(screen.getByText("rg -il linter …")).toBeTruthy();
  expect(screen.getByText("Found the template.")).toBeTruthy();
  expect(container.querySelector('[data-run-key="s1:r1"] .run-open')?.getAttribute("title")).toBe(
    "rg -il linter …",
  );
});

test("a waiting row's line shows its request's hidden characters as codes and draws its line breaks", () => {
  const at = (...points: number[]): string => String.fromCodePoint(...points);
  // Painted raw, the override would reverse the text after it and the one clipped line would
  // show the line break as a space, so the row would name a command other than the one waiting.
  const summary = `echo ok${at(0x202e)}\nrm -rf ./build`;
  // A finished run's closing words are not a request: an emoji's joiner there stays as written.
  const closing = `Done ${at(0x1f469, 0x200d, 0x1f4bb)}\nsecond line`;
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1"),
          routing("r2", { agent: "writer", status: "completed", summary: closing }),
        ]),
      ],
      cards: [permissionCard("req-1", "researcher", { tool: "Bash", summary })],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions })} />);

  const waiting = container.querySelector('[data-run-key="s1:r1"] .run-open');
  const shown = "Waiting for approval · Bash echo ok<U+202E> ↵ rm -rf ./build";
  expect(waiting?.querySelector(".run-line:last-child")?.textContent).toBe(shown);
  expect(waiting?.getAttribute("title")).toBe(shown);

  const finished = container.querySelector('[data-run-key="s1:r2"] .run-open');
  expect(finished?.querySelector(".run-line:last-child")?.textContent).toBe(closing);
});

test("Mark reviewed hands back every Ready for review row's key, newest first", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { status: "completed" }),
          routing("r2", { status: "completed" }),
        ]),
      ],
    }),
  };
  const onReviewed = vi.fn();
  render(<AgentRuns {...baseProps({ sessions, onReviewed })} />);
  fireEvent.click(screen.getByText("Mark reviewed"));
  expect(onReviewed).toHaveBeenCalledWith(["s1:r2", "s1:r1"]);
});

test("Done is folded shut by default and lists newest first", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [routing("r1", { status: "completed" })], { history: true }),
        turn("t1", [routing("r2", { status: "completed" })], { history: true }),
      ],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions })} />);
  const details = container.querySelector(".run-done") as HTMLDetailsElement;
  expect(details.open).toBe(false);
  expect(
    [...details.querySelectorAll(".run")].map((el) => el.getAttribute("data-run-key")),
  ).toEqual(["s1:r2", "s1:r1"]);
});

test("opening Peek replaces the groups; Back returns to them and refocuses the row", async () => {
  const api = fakeApi();
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api })} />);
  const openButton = container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement;

  fireEvent.click(openButton);
  await flush();
  expect(container.querySelector(".run-peek")).toBeTruthy();
  expect(container.querySelector(".run-group")).toBeNull();

  fireEvent.click(screen.getByText("Back"));
  expect(container.querySelector(".run-peek")).toBeNull();
  expect(document.activeElement).toBe(container.querySelector('[data-run-key="s1:r1"] .run-open'));
});

test("the peeked run leaving the groups returns to the list on its own", async () => {
  const api = fakeApi();
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container, rerender } = render(<AgentRuns {...baseProps({ sessions, api })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(container.querySelector(".run-peek")).toBeTruthy();

  rerender(<AgentRuns {...baseProps({ sessions: {}, api })} />);
  expect(container.querySelector(".run-peek")).toBeNull();
  expect(screen.getByText("No agent runs in open chats.")).toBeTruthy();
});

test("Peek marks a row reviewed only once it is finished, whether already so or becoming so", async () => {
  const onReviewed = vi.fn();
  const api = fakeApi();
  const running = {
    s1: view("s1", { turns: [turn("t0", [routing("r1", { status: "running" })])] }),
  };
  const { container, rerender } = render(
    <AgentRuns {...baseProps({ sessions: running, api, onReviewed })} />,
  );
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(onReviewed).not.toHaveBeenCalled();

  const finished = {
    s1: view("s1", { turns: [turn("t0", [routing("r1", { status: "completed" })])] }),
  };
  rerender(<AgentRuns {...baseProps({ sessions: finished, api, onReviewed })} />);
  expect(onReviewed).toHaveBeenCalledWith(["s1:r1"]);
});

test("Peek opened directly on a Ready for review row marks it reviewed right away", async () => {
  const onReviewed = vi.fn();
  const api = fakeApi();
  const sessions = {
    s1: view("s1", { turns: [turn("t0", [routing("r1", { status: "completed" })])] }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api, onReviewed })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(onReviewed).toHaveBeenCalledWith(["s1:r1"]);
});

test("Jump reports the session and block and never reviews a run still running", () => {
  const onJump = vi.fn(() => true);
  const onReviewed = vi.fn();
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions, onJump, onReviewed })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-jump') as HTMLElement);
  expect(onJump).toHaveBeenCalledWith("s1", "r1");
  expect(onReviewed).not.toHaveBeenCalled();
});

test("Jump on a finished row also marks it reviewed", () => {
  const onJump = vi.fn(() => true);
  const onReviewed = vi.fn();
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, onJump, onReviewed })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-jump') as HTMLElement);
  expect(onJump).toHaveBeenCalledWith("s1", "r1");
  expect(onReviewed).toHaveBeenCalledWith(["s1:r1"]);
});

test("a Jump the app refuses never marks a finished row reviewed", () => {
  const onJump = vi.fn(() => false);
  const onReviewed = vi.fn();
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, onJump, onReviewed })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-jump') as HTMLElement);
  expect(onJump).toHaveBeenCalledWith("s1", "r1");
  expect(onReviewed).not.toHaveBeenCalled();
});

test("without a SubagentsApi a row is an inert div: no button, and nothing is ever read", () => {
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api: undefined })} />);
  const openArea = container.querySelector('[data-run-key="s1:r1"] .run-open');
  expect(openArea?.tagName).toBe("DIV");
  expect(container.querySelector("button.run-open")).toBeNull();
  fireEvent.click(openArea as HTMLElement);
  expect(container.querySelector(".run-peek")).toBeNull();
});

test("a run row never carries the roster's own classes or data-agent", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "workflow" }),
          routing("r2", { agent: "general-purpose", status: "completed" }),
        ]),
      ],
      cards: [permissionCard("req-1", "subagent")],
    }),
    s2: view("s2", {
      turns: [
        turn("t0", [routing("r3", { agent: "curator", status: "completed" })], { history: true }),
      ],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, reviewed: new Set() })} />);
  for (const selector of [".agent", ".s", ".s-on", ".q", ".rt", ".chip", "[data-agent]"]) {
    expect(container.querySelector(selector)).toBeNull();
  }
});

test("markup in a run's description renders as literal text, never an element", () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const sessions = {
    s1: view("s1", { turns: [turn("t0", [routing("r1", { description: hostile })])] }),
  };
  render(<AgentRuns {...baseProps({ sessions })} />);
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getAllByText((text) => text.includes(hostile)).length).toBeGreaterThan(0);
});

test("markup in a tool's rest, a finished summary, and the chat label renders as text", () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const running = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "Grep",
        row: { head: "rg", rest: hostile, tail: "" },
        done: false,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  const done = routing("r2", { status: "completed", summary: hostile });
  const sessions = { s1: view("s1", { turns: [turn("t0", [running, done])] }) };
  render(<AgentRuns {...baseProps({ sessions, labelOf: () => hostile })} />);

  expect(document.querySelector("img")).toBeNull();
  expect(screen.getAllByText((text) => text.includes(hostile)).length).toBeGreaterThan(0);
});

test("a need row's card agent and chat label render as literal text, never an element", () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const sessions = {
    s1: view("s1", { cards: [permissionCard("req-1", hostile)] }),
  };
  render(<AgentRuns {...baseProps({ sessions, labelOf: () => hostile })} />);

  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText(hostile)).toBeTruthy();
  expect(screen.getByText(`A subagent is waiting for approval · ${hostile}`)).toBeTruthy();
});

test("a finished row is never read for its stats; only needs and working rows are", async () => {
  const api: SubagentsApi = { read: vi.fn().mockResolvedValue(transcript()) };
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  render(<AgentRuns {...baseProps({ sessions, api })} />);
  await flush();
  expect(api.read).not.toHaveBeenCalled();
});

test("Back after Peek moved a reviewed row into Done opens the fold and focuses it", async () => {
  const onReviewed = vi.fn();
  const api = fakeApi();
  const sessions = {
    s1: view("s1", { turns: [turn("t0", [routing("r1", { status: "completed" })])] }),
  };
  const { container, rerender } = render(
    <AgentRuns {...baseProps({ sessions, api, onReviewed })} />,
  );
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(onReviewed).toHaveBeenCalledWith(["s1:r1"]);

  // App adds the reviewed key and re-renders; the row now sits in Done while Peek
  // stays open (see the module doc's "Reviewing").
  rerender(
    <AgentRuns {...baseProps({ sessions, api, onReviewed, reviewed: new Set(["s1:r1"]) })} />,
  );

  fireEvent.click(screen.getByText("Back"));
  await flush();

  const fold = container.querySelector(".run-done") as HTMLDetailsElement;
  expect(fold.open).toBe(true);
  expect(document.activeElement).toBe(container.querySelector('[data-run-key="s1:r1"] .run-open'));
});

test("a Done fold the user opened stays open after Peek, then Back, on a done row", async () => {
  const api = fakeApi();
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api })} />);
  fireEvent.click(screen.getByText("Done (1)"));
  expect((container.querySelector(".run-done") as HTMLDetailsElement).open).toBe(true);

  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  fireEvent.click(screen.getByText("Back"));
  await flush();

  expect((container.querySelector(".run-done") as HTMLDetailsElement).open).toBe(true);
});

test("a Done fold the user opened stays open after Peek on a row outside Done, then Back", async () => {
  const api = fakeApi();
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [routing("r0", { status: "completed" })], { history: true }),
        turn("t1", [routing("r1")]),
      ],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api })} />);
  fireEvent.click(screen.getByText("Done (1)"));
  expect((container.querySelector(".run-done") as HTMLDetailsElement).open).toBe(true);
  // Let jsdom's queued "toggle" task run, as a browser would long before the user's next
  // click: only then has onToggle's state update landed, before Peek unmounts this details
  // element and Back remounts it. A 0ms timeout is enough, because jsdom already queued that
  // toggle task before this one, so it runs first regardless of this delay's length.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  fireEvent.click(screen.getByText("Back"));
  await flush();

  expect((container.querySelector(".run-done") as HTMLDetailsElement).open).toBe(true);
});

test("hideNeeds hides the needs group and unattributed needs but shows other groups", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1"), routing("r2", { status: "completed" })])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, hideNeeds: true })} />);

  // Needs group should not be rendered
  expect(screen.queryByText(/^Needs you/)).toBeNull();
  // Ready for review should still render
  expect(screen.getByText(/^Ready for review/)).toBeTruthy();
  // Empty message should not show because there are still rows
  expect(screen.queryByText("No agent runs in open chats.")).toBeNull();
  expect(container.querySelector('[data-run-key="s1:r2"]')).toBeTruthy();
});

test("hideNeeds hides the needs group and shows empty only when truly no rows exist", () => {
  const sessions = {
    s1: view("s1", {
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, hideNeeds: true })} />);

  // Only needs rows exist, and needs are hidden; no other groups, so render empty div
  // but NOT the empty message because rows do exist (needs)
  expect(screen.queryByText("No agent runs in open chats.")).toBeNull();
  expect(container.querySelector(".run-group")).toBeNull();
  expect(container.querySelector("[data-run-key]")).toBeNull();
});

// --- runs / Stop -------------------------------------------------------------------------------

test("a running row shows RunStop after Jump once runs is set, not otherwise", () => {
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container, rerender } = render(<AgentRuns {...baseProps({ sessions })} />);
  expect(container.querySelector('[data-run-key="s1:r1"] .run-stop')).toBeNull();

  rerender(<AgentRuns {...baseProps({ sessions, runs: fakeRunsApi() })} />);
  expect(container.querySelector('[data-run-key="s1:r1"] .run-stop')).toBeTruthy();
});

test("Stop asks, then Stop run calls runs.stop with the row's session and block ids", () => {
  const stop = vi.fn().mockResolvedValue(undefined);
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions, runs: fakeRunsApi(stop) })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-stop') as HTMLElement);
  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
  expect(stop).toHaveBeenCalledWith("s1", "r1");
});

test("a finished row never shows RunStop even with runs set", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, runs: fakeRunsApi() })} />);
  expect(container.querySelector('[data-run-key="s1:r1"] .run-stop')).toBeNull();
});

test("an ended row (session closed, block still running) shows no Stop even with runs set", () => {
  const sessions = { s1: view("s1", { closed: true, turns: [turn("t0", [routing("r1")])] }) };
  const { container } = render(<AgentRuns {...baseProps({ sessions, runs: fakeRunsApi() })} />);
  expect(container.querySelector('[data-run-key="s1:r1"] .run-stop')).toBeNull();
  expect(container.querySelector(".run-done summary")?.textContent).toBe("Done (1)");
});

test.each(["idle", "sending"] as const)(
  "a row moving to Needs you after %s leaves focus on an input outside the panel",
  (phase) => {
    const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
    // Never resolves: the test only needs the row to reach and hold idle/sending, never sent.
    const runs = fakeRunsApi(() => new Promise<void>(() => {}));
    const { rerender } = render(
      <div>
        <input aria-label="composer" />
        <AgentRuns {...baseProps({ sessions, runs })} />
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Stop researcher in Chat s1" }));
    fireEvent.click(
      screen.getByRole("button", { name: phase === "idle" ? "Keep running" : "Stop run" }),
    );
    const composer = screen.getByRole("textbox", { name: "composer" });
    composer.focus();
    expect(document.activeElement).toBe(composer);

    // The run raises a request: its row leaves Working for Needs you — a different parent, so
    // its RunStop remounts. The user's own typing target must keep focus regardless.
    const needsYou = { s1: { ...sessions.s1, cards: [permissionCard("req-1", "researcher")] } };
    rerender(
      <div>
        <input aria-label="composer" />
        <AgentRuns {...baseProps({ sessions: needsYou, runs })} />
      </div>,
    );
    expect(document.activeElement).toBe(composer);
  },
);

test("a stops prop is read instead of this component's own hook", () => {
  const sessions = { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) };
  const phase = vi.fn(() => ({ kind: "confirm" }) as const);
  const stops: RunStops = { phase, ask: vi.fn(), cancel: vi.fn(), send: vi.fn() };
  render(<AgentRuns {...baseProps({ sessions, runs: fakeRunsApi(), stops })} />);
  // This component's own hook would start every key idle (undefined), rendering a plain "Stop"
  // button; reading `stops.phase` instead shows the confirm dialog it (falsely) reports.
  expect(screen.getByText("Stop researcher now?")).toBeTruthy();
  expect(phase).toHaveBeenCalledWith("s1:r1");
});

// --- requests feeds groupRuns ---------------------------------------------------------------------

test("requests feeds groupRuns: a needs row reads its own waiting time from it", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(0));
    const sessions = {
      s1: view("s1", {
        turns: [turn("t0", [routing("r1")])],
        cards: [permissionCard("req-1", "researcher")],
      }),
    };
    const requests: RunRequests = new Map([
      ["s1", new Map([["tu-req-1", { run: "r1", raisedAt: 1000 }]])],
    ]);
    render(<AgentRuns {...baseProps({ sessions, requests, nowMs: 5000 })} />);
    expect(screen.getByText((text) => text.includes("waiting 4s"))).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

// --- Peek: stop and onDecide ---------------------------------------------------------------------

test("Peek shows Stop for a live running row once runs is set", async () => {
  const api = fakeApi();
  const { container } = render(
    <AgentRuns
      {...baseProps({
        sessions: { s1: view("s1", { turns: [turn("t0", [routing("r1")])] }) },
        api,
        runs: fakeRunsApi(),
      })}
    />,
  );
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(container.querySelector(".run-peek .run-stop")).toBeTruthy();
});

test("Peek shows no Stop for a finished row even with runs set", async () => {
  const api = fakeApi();
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const { container } = render(
    <AgentRuns {...baseProps({ sessions, api, runs: fakeRunsApi() })} />,
  );
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(container.querySelector(".run-peek .run-stop")).toBeNull();
});

test("Peek offers Allow once/Deny for a needs row's own request once onDecide is set", async () => {
  const api = fakeApi();
  const onDecide = vi.fn(async () => "applied" as const);
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1")])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api, onDecide })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();

  expect(container.querySelector(".run-peek-requests")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /^Allow once:/ }));
  expect(onDecide).toHaveBeenCalledWith("s1", "tu-req-1", true);
});

test("without onDecide, Peek offers no Allow/Deny even for a row with a pending request", async () => {
  const api = fakeApi();
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1")])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const { container } = render(<AgentRuns {...baseProps({ sessions, api })} />);
  fireEvent.click(container.querySelector('[data-run-key="s1:r1"] .run-open') as HTMLElement);
  await flush();
  expect(container.querySelector(".run-peek-requests")).toBeNull();
});
