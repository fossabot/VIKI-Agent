/**
 * RunPeek: a read-only, polling view of one run's own transcript. These tests use a fake
 * `SubagentsApi` whose `read()` returns a controllable (deferred) promise, and fake timers, so
 * each poll can be driven one step at a time — the same technique runStats.unit.test.tsx uses
 * for its own polling hook.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type {
  PeekLine,
  RunTranscript,
  SubagentsApi,
} from "../../clients/conversations/subagentsApi";
import type { PermissionRequestedData } from "../../protocol";
import type { RunRow } from "../../state/runGroups";
import type { RoutingBlock } from "../../state/view";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { PEEK_POLL_MS, RunPeek } from "./RunPeek";
import { ANSWER_SETTLE_MS } from "./useAnswerHold";

const SESSION_ID = "11111111-1111-1111-1111-111111111111";

interface Deferred {
  resolve: (value: RunTranscript) => void;
  reject: (error: unknown) => void;
}

interface Call {
  sessionId: string;
  blockId: string;
  signal: AbortSignal | undefined;
}

function fakeApi(): { api: SubagentsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: SubagentsApi = {
    read(sessionId, blockId, signal) {
      calls.push({ sessionId, blockId, signal });
      return new Promise<RunTranscript>((resolve, reject) => pending.push({ resolve, reject }));
    },
  };
  return { api, calls, pending };
}

function permissionRequest(
  overrides: Partial<PermissionRequestedData> = {},
): PermissionRequestedData {
  return {
    tool_use_id: "tu-1",
    kind: "tool",
    tool: "Write",
    summary: "notes.md",
    question: "",
    reason: "",
    agent: "researcher",
    plan: "",
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

function makeRow(blockOverrides: Partial<RoutingBlock> = {}): RunRow {
  return {
    key: "s1:b1",
    sessionId: SESSION_ID,
    turnId: "t1",
    background: false,
    live: true,
    ended: false,
    waitingSince: null,
    requests: [],
    block: {
      kind: "routing",
      id: "b1",
      agent: "researcher",
      model: "opus",
      description: "Investigate the thing",
      reason: "",
      status: "running",
      tools: [],
      ...blockOverrides,
    },
  };
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

test("shows the pending note, then the transcript once the read resolves", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow()}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  expect(screen.getByText("Reading the transcript…")).toBeTruthy();

  pending[0]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Hello" }] }));
  await settle();

  expect(screen.queryByText("Reading the transcript…")).toBeNull();
  expect(screen.getByText("Hello")).toBeTruthy();
});

test("polls again after pollMs while the run is still running", async () => {
  const { api, calls, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  expect(calls).toHaveLength(1);
  pending[0]?.resolve(transcript());
  await settle();
  expect(calls).toHaveLength(1);

  await advance(PEEK_POLL_MS);
  expect(calls).toHaveLength(2);
  expect(calls[1]).toMatchObject({ sessionId: SESSION_ID, blockId: "b1" });
});

test("a rerender to a completed block issues exactly one more read, then stops", async () => {
  const { api, calls, pending } = fakeApi();
  const { rerender } = render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();
  expect(calls).toHaveLength(1);

  rerender(
    <RunPeek
      row={makeRow({ status: "completed" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await advance(PEEK_POLL_MS);
  expect(calls).toHaveLength(2);
  pending[1]?.resolve(transcript());
  await settle();

  await advance(PEEK_POLL_MS * 5);
  expect(calls).toHaveLength(2);
});

test("once the run is no longer running, shows Result with the last text line", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "completed" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  const lines: PeekLine[] = [
    { kind: "text", id: "t1", text: "First line" },
    { kind: "tool", id: "x1", head: "Read", rest: "a.md", tail: "", done: true, error: false },
    { kind: "text", id: "t2", text: "Final answer" },
  ];
  pending[0]?.resolve(transcript({ lines }));
  await settle();

  expect(screen.getByText("Result")).toBeTruthy();
  expect(screen.getAllByText("Final answer")).toHaveLength(2);
});

test("a not_found read while running reads differently than once finished", async () => {
  const { api: runningApi, pending: runningPending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={runningApi}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  runningPending[0]?.reject(new Error("not_found"));
  await settle();
  expect(screen.getByText("No transcript for this run yet.")).toBeTruthy();
  cleanup();

  const { api: doneApi, pending: donePending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "completed" })}
      chat="Research session"
      api={doneApi}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  donePending[0]?.reject(new Error("not_found"));
  await settle();
  expect(screen.getByText("This run's transcript was not found.")).toBeTruthy();
});

test("a non-not_found failure before any success shows one generic note", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.reject(new Error("unavailable"));
  await settle();
  expect(screen.getByText("Couldn't read this run's transcript.")).toBeTruthy();
});

test("a later failure keeps the transcript and adds a paused note", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Still here" }] }));
  await settle();
  expect(screen.getByText("Still here")).toBeTruthy();

  await advance(PEEK_POLL_MS);
  pending[1]?.reject(new Error("unavailable"));
  await settle();

  expect(screen.getByText("Still here")).toBeTruthy();
  expect(screen.getByText("Live updates paused: the transcript could not be read.")).toBeTruthy();
});

test("Back is focused on mount and calls onBack; Jump to chat calls onJump", async () => {
  const { api, pending } = fakeApi();
  const onBack = vi.fn();
  const onJump = vi.fn();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={onBack}
      onJump={onJump}
    />,
  );
  const back = screen.getByText("Back");
  expect(document.activeElement).toBe(back);

  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  fireEvent.click(back);
  expect(onBack).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText("Jump to chat"));
  expect(onJump).toHaveBeenCalledOnce();
});

test("hostile markup in the prompt and a text line renders as literal text, not an element", async () => {
  const { api, pending } = fakeApi();
  const hostile = "<img src=x onerror=alert(1)>";
  render(
    <RunPeek
      row={makeRow({ status: "completed" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(
    transcript({ prompt: hostile, lines: [{ kind: "text", id: "t1", text: hostile }] }),
  );
  await settle();

  expect(document.querySelector("img")).toBeNull();
  expect(screen.getAllByText(hostile).length).toBeGreaterThan(0);
});

test("a finish landing while a read is in flight is still followed by one more read", async () => {
  const { api, calls, pending } = fakeApi();
  const { rerender } = render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Interim" }] }));
  await settle();
  await advance(PEEK_POLL_MS);
  expect(calls).toHaveLength(2); // read #2 issued while the row was still running

  // SubagentDone arrives while read #2 is in flight.
  rerender(
    <RunPeek
      row={makeRow({ status: "completed" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  // Read #2 was issued before the run ended, so it still carries the interim text.
  pending[1]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Interim" }] }));
  await settle();
  await advance(PEEK_POLL_MS); // the read that must still follow the finish
  expect(calls).toHaveLength(3); // one more read follows the finish

  pending[2]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Final" }] }));
  await settle();
  await advance(PEEK_POLL_MS * 5);

  expect(calls).toHaveLength(3); // the first read issued once no longer running schedules none
  expect(document.querySelector(".run-peek-result")?.textContent).toContain("Final");
  expect(screen.queryByText("Interim")).toBeNull();
});

test("hostile markup in a message, tool and agent line renders as literal text, not an element", async () => {
  const { api, pending } = fakeApi();
  const hostile = "<img src=x onerror=alert(1)>";
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(
    transcript({
      lines: [
        { kind: "message", id: "m1", text: hostile },
        {
          kind: "tool",
          id: "x1",
          head: hostile,
          rest: hostile,
          tail: hostile,
          done: true,
          error: false,
        },
        { kind: "agent", id: "a1", agent: hostile, description: hostile, status: "running" },
      ],
    }),
  );
  await settle();

  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector(".run-peek-message")?.textContent).toBe(hostile);
  expect(document.querySelector(".tool-head")?.textContent).toBe(hostile);
  expect(document.querySelector(".run-peek-tool-rest")?.textContent?.trim()).toBe(hostile);
  expect(document.querySelector(".run-peek-tool-tail")?.textContent?.trim()).toBe(hostile);
  expect(document.querySelector(".run-peek-agent")?.textContent).toContain(hostile);
});

test("hostile markup in the agent, model and chat renders as literal text in the header", async () => {
  const { api, pending } = fakeApi();
  const hostile = "<img src=x onerror=alert(1)>";
  render(
    <RunPeek
      row={makeRow({ agent: hostile, model: hostile })}
      chat={hostile}
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector(".run-peek-title")?.textContent).toBe(`${hostile} · ${hostile}`);
  expect(document.querySelector(".run-peek-meta")?.textContent).toContain(hostile);
});

test("unmounting aborts the in-flight read", async () => {
  const { api, calls } = fakeApi();
  const { unmount } = render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(false);

  unmount();

  expect(calls[0]?.signal?.aborted).toBe(true);
});

// --- stop ------------------------------------------------------------------------------------

test("stop renders RunStop right after Jump to chat, wired to onAsk/onCancel/onSend", async () => {
  const { api, pending } = fakeApi();
  const onAsk = vi.fn();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
      stop={{ phase: undefined, onAsk, onCancel: vi.fn(), onSend: vi.fn() }}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  const jump = screen.getByText("Jump to chat");
  const header = jump.closest("header") as HTMLElement;
  const children = [...header.children];
  const stopButton = screen.getByRole("button", { name: "Stop researcher in Research session" });
  expect(children.indexOf(stopButton)).toBeGreaterThan(children.indexOf(jump));

  fireEvent.click(stopButton);
  expect(onAsk).toHaveBeenCalledTimes(1);
});

test("without stop, no Stop control renders", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  expect(screen.queryByRole("button", { name: /^Stop /i })).toBeNull();
});

// --- requests ----------------------------------------------------------------------------------

test("requests renders one RequestActions per tool-kind item, labelled Waiting for you", async () => {
  const { api, pending } = fakeApi();
  const onDecide = vi.fn(async () => "applied" as const);
  const toolRequest = permissionRequest({ tool_use_id: "tu-1" });
  const planRequest = permissionRequest({ tool_use_id: "tu-2", kind: "plan" });
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={1000}
      onBack={vi.fn()}
      onJump={vi.fn()}
      requests={{ items: [toolRequest, planRequest], waitingSince: 500, onDecide }}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  const section = document.querySelector("section.run-peek-requests") as HTMLElement;
  expect(section.getAttribute("aria-label")).toBe("Waiting for you");
  expect(section.querySelectorAll(".request-actions")).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: "Allow once: Write notes.md" }));
  expect(onDecide).toHaveBeenCalledWith("tu-1", true);
});

test("requests with no tool-kind item renders no section", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
      requests={{
        items: [permissionRequest({ kind: "plan" })],
        waitingSince: null,
        onDecide: vi.fn(),
      }}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  expect(document.querySelector(".run-peek-requests")).toBeNull();
});

test("without the requests prop, no section renders", async () => {
  const { api, pending } = fakeApi();
  render(
    <RunPeek
      row={makeRow({ status: "running" })}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  pending[0]?.resolve(transcript());
  await settle();

  expect(document.querySelector(".run-peek-requests")).toBeNull();
});

// --- ended counts as not running ---------------------------------------------------------------

test("an ended row counts as not running: face inactive, Result shown, polling stops for good", async () => {
  const { api, calls, pending } = fakeApi();
  const row: RunRow = { ...makeRow({ status: "running" }), ended: true };
  render(
    <RunPeek
      row={row}
      chat="Research session"
      api={api}
      nowMs={0}
      onBack={vi.fn()}
      onJump={vi.fn()}
    />,
  );
  await settle();
  expect(calls).toHaveLength(1); // the one read issued on mount

  pending[0]?.resolve(transcript({ lines: [{ kind: "text", id: "t1", text: "Last words" }] }));
  await settle();

  expect(document.querySelector(".face-presence")).toBeNull();
  expect(screen.getByText("Result")).toBeTruthy();
  expect(screen.getAllByText("Last words")).toHaveLength(2);

  await advance(PEEK_POLL_MS * 5);
  expect(calls).toHaveLength(1); // ended counts as not running, so no further poll is scheduled
});

// --- which request an answer reaches -----------------------------------------------------------
//
// jsdom does not lay anything out and does not move focus as a browser does. `stackRequests`
// stands for the layout; each focus test starts from a button that could still answer (its
// request withdrawn, or answered in the chat, while the user was on it), which is the same start
// in a browser.

function peekProps(api: SubagentsApi) {
  return {
    row: makeRow({ status: "completed" }),
    chat: "Synthetic session",
    api,
    nowMs: 0,
    onBack: vi.fn(),
    onJump: vi.fn(),
  };
}

const waitingList = (): HTMLElement =>
  screen.getByRole("region", { name: "Waiting for you" }) as HTMLElement;

test.each(["idle", "pending", "unknown"])(
  "a focused request leaving hands focus to the list, never to an earlier %s request's buttons",
  async (phase) => {
    const { api } = fakeApi();
    const onDecide = vi.fn(() =>
      phase === "pending" ? new Promise<"unknown">(() => {}) : Promise.resolve("unknown" as const),
    );
    const earlier = permissionRequest({ tool_use_id: "earlier", summary: "earlier" });
    const removed = permissionRequest({ tool_use_id: "removed", summary: "removed" });
    const props = peekProps(api);
    const { rerender } = render(
      <RunPeek {...props} requests={{ items: [earlier, removed], waitingSince: null, onDecide }} />,
    );
    const allowEarlier = screen.getByRole("button", { name: "Allow once: Write earlier" });
    if (phase !== "idle") {
      await act(async () => {
        fireEvent.click(allowEarlier);
      });
      expect(allowEarlier.getAttribute("aria-disabled")).toBe("true");
    }
    const denyRemoved = screen.getByRole("button", { name: "Deny: Write removed" });
    denyRemoved.focus();
    expect(document.activeElement).toBe(denyRemoved);

    rerender(<RunPeek {...props} requests={{ items: [earlier], waitingSince: null, onDecide }} />);
    await settle();

    // "Allow once: Write earlier" is the first control left in the list, and one key press on
    // it would answer a request the user was not on.
    expect(document.activeElement).toBe(waitingList());
    expect(waitingList().getAttribute("tabindex")).toBe("-1");
    expect(onDecide).toHaveBeenCalledTimes(phase === "idle" ? 0 : 1);
  },
);

test("the last request leaving takes the list with it, and focus goes to Back", async () => {
  const { api } = fakeApi();
  const props = peekProps(api);
  const only = permissionRequest({ tool_use_id: "only", summary: "only" });
  const { rerender } = render(
    <RunPeek {...props} requests={{ items: [only], waitingSince: null, onDecide: vi.fn() }} />,
  );
  screen.getByRole("button", { name: "Allow once: Write only" }).focus();

  rerender(<RunPeek {...props} requests={{ items: [], waitingSince: null, onDecide: vi.fn() }} />);
  await settle();

  expect(document.querySelector(".run-peek-requests")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
});

test("the list shows where focus was put: the rail's ring, inside the box Peek clips, and Highlight in forced colours", () => {
  const css = readCss(import.meta.url, "RunPeek.css");
  const forced = narrowBlock(css, "@media (forced-colors: active) {");
  const ring = ruleIn(css.replace(forced, ""), ".run-peek-requests:focus-visible");
  expect(ring).toMatch(/outline:\s*2px solid var\(--or\)/);
  // `.run-peek` hides its overflow, so a ring drawn outside the list would lose its sides.
  expect(ruleIn(css, ".run-peek")).toMatch(/overflow:\s*hidden/);
  expect(ring).toMatch(/outline-offset:\s*-\d+px/);
  expect(ruleIn(forced, ".run-peek-requests:focus-visible")).toMatch(
    /outline:\s*2px solid Highlight/,
  );
});

/** Stands for the browser's layout: the list's requests are stacked a hundred pixels each. */
function stackRequests(): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    const mine = this.closest(".request-actions");
    const all = [...document.querySelectorAll(".run-peek-requests .request-actions")];
    return new DOMRect(0, 100 * Math.max(0, all.indexOf(mine as Element)), 200, 24);
  });
}

test("Peek's own buttons are held when they appear: the second click of the double-click that opened it answers nothing", async () => {
  const { api } = fakeApi();
  const onDecide = vi.fn(async () => "applied" as const);
  const only = permissionRequest({ tool_use_id: "only", summary: "only" });
  render(
    <RunPeek {...peekProps(api)} requests={{ items: [only], waitingSince: null, onDecide }} />,
  );
  const allow = screen.getByRole("button", { name: "Allow once: Write only" });

  // `detail` 2: the second click of a double-click, landing where Peek has just put a button.
  fireEvent.click(allow, { detail: 2 });
  expect(onDecide).not.toHaveBeenCalled();
  expect(document.querySelectorAll(".request-actions-buttons[data-settling]")).toHaveLength(1);

  await advance(ANSWER_SETTLE_MS);
  expect(document.querySelector(".request-actions-buttons[data-settling]")).toBeNull();
  fireEvent.click(allow, { detail: 1 });
  expect(onDecide).toHaveBeenCalledWith("only", true);
});

test("a request answered in Peek leaves, and the one that moves up under the pointer is held; the keyboard is not", async () => {
  stackRequests();
  const { api } = fakeApi();
  const onDecide = vi.fn(async () => "applied" as const);
  const first = permissionRequest({ tool_use_id: "first", summary: "first" });
  const second = permissionRequest({ tool_use_id: "second", summary: "second" });
  const props = peekProps(api);
  const { rerender } = render(
    <RunPeek {...props} requests={{ items: [first, second], waitingSince: null, onDecide }} />,
  );
  await advance(ANSWER_SETTLE_MS);
  expect(document.querySelector(".request-actions-buttons[data-settling]")).toBeNull();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Allow once: Write first" }), { detail: 1 });
  });
  expect(onDecide.mock.calls).toEqual([["first", true]]);

  // request.answered: the first request leaves and the second takes its place.
  rerender(<RunPeek {...props} requests={{ items: [second], waitingSince: null, onDecide }} />);
  const allowSecond = screen.getByRole("button", { name: "Allow once: Write second" });
  fireEvent.click(allowSecond, { detail: 2 });
  expect(onDecide.mock.calls).toEqual([["first", true]]);

  // Enter on the focused button: focus did not move with the layout.
  fireEvent.click(allowSecond, { detail: 0 });
  expect(onDecide.mock.calls).toEqual([
    ["first", true],
    ["second", true],
  ]);
});

test("a request in Peek shows hidden characters as codes, with the line that says so", async () => {
  const { api } = fakeApi();
  const hostile = permissionRequest({
    tool: "Bash",
    summary: `echo ok${String.fromCodePoint(0x202e)}\nrm -rf ./build`,
  });
  render(
    <RunPeek
      {...peekProps(api)}
      requests={{ items: [hostile], waitingSince: null, onDecide: vi.fn() }}
    />,
  );
  const list = waitingList();
  expect(list.querySelector(".request-actions-head")?.textContent).toBe(
    "Wants to run: Bash echo ok<U+202E>\nrm -rf ./build",
  );
  expect(list.querySelector(".decision-notes")?.textContent).toContain(
    "Hidden or text-reordering characters are in this text.",
  );
});
