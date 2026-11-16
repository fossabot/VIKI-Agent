import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  ContextApiError,
  type ContextReason,
  type ContextReport,
} from "../../clients/conversations/contextApi";
import { ContextMeter } from "./ContextMeter";

afterEach(cleanup);

const SESSION = "11111111-1111-4111-8111-111111111111";
const OTHER_SESSION = "22222222-2222-4222-8222-222222222222";
const OBSERVED_MS = 1_770_000_000_000;

function expectedTime(ms: number): string {
  const at = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

function reportFixture(overrides: Partial<ContextReport> = {}): ContextReport {
  return {
    observed_at: new Date(OBSERVED_MS).toISOString(),
    total_tokens: 127_700,
    max_tokens: 200_000,
    raw_max_tokens: 200_000,
    percentage: 63.85,
    auto_compact: { enabled: true, threshold: 180_000, source: "cli" },
    categories: [
      { name: "System prompt", tokens: 4_200, kind: "used" },
      { name: "Messages", tokens: 120_000, kind: "used" },
      { name: "Skills", tokens: 1_000, kind: "used" },
      { name: "MCP tools", tokens: 500, kind: "used" },
      { name: "Autocompact buffer", tokens: 2_000, kind: "buffer" },
      { name: "Free space", tokens: 72_300, kind: "free" },
    ],
    memory_files: null,
    mcp_servers: null,
    skills: null,
    agents: null,
    reasons: {},
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    ...overrides,
  };
}

/** A promise plus its own settlers, for pinning the component mid-fetch. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function pendingApi() {
  const calls: (AbortSignal | undefined)[] = [];
  const get = vi.fn((_sessionId: string, signal?: AbortSignal) => {
    calls.push(signal);
    return new Promise<ContextReport>(() => {});
  });
  return { get, calls };
}

function openToggle(percent: number): void {
  fireEvent.click(screen.getByRole("button", { name: `context ${percent}%` }));
}

// --- opens and fetches once ---

test("closed by default; opening fetches the report exactly once", () => {
  const { get } = pendingApi();
  render(<ContextMeter percent={42} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  const toggle = screen.getByRole("button", { name: "context 42%" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(get).not.toHaveBeenCalled();

  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  screen.getByRole("dialog"); // throws if absent
  expect(get).toHaveBeenCalledTimes(1);
  expect(get).toHaveBeenCalledWith(SESSION, expect.anything());
});

test("re-rendering while open (e.g. a new percent) does not refetch", () => {
  const { get } = pendingApi();
  const api = { get };
  const { rerender } = render(
    <ContextMeter percent={10} sessionId={SESSION} api={api} onOpenPage={vi.fn()} />,
  );
  openToggle(10);
  expect(get).toHaveBeenCalledTimes(1);
  rerender(<ContextMeter percent={55} sessionId={SESSION} api={api} onOpenPage={vi.fn()} />);
  expect(get).toHaveBeenCalledTimes(1);
  screen.getByRole("button", { name: "context 55%" });
});

test("closing aborts the in-flight request", () => {
  const { get, calls } = pendingApi();
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  const toggle = screen.getByRole("button", { name: "context 1%" });
  fireEvent.click(toggle);
  fireEvent.click(toggle);
  expect(calls[0]?.aborted).toBe(true);
});

test("unmounting aborts the in-flight request", () => {
  const { get, calls } = pendingApi();
  const { unmount } = render(
    <ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />,
  );
  openToggle(1);
  unmount();
  expect(calls[0]?.aborted).toBe(true);
});

test("a session change while open aborts the old request and fetches the new session", () => {
  const { get, calls } = pendingApi();
  const api = { get };
  const { rerender } = render(
    <ContextMeter percent={1} sessionId={SESSION} api={api} onOpenPage={vi.fn()} />,
  );
  openToggle(1);
  rerender(<ContextMeter percent={1} sessionId={OTHER_SESSION} api={api} onOpenPage={vi.fn()} />);
  expect(calls[0]?.aborted).toBe(true);
  expect(get).toHaveBeenCalledTimes(2);
  expect(get).toHaveBeenLastCalledWith(OTHER_SESSION, expect.anything());
});

// --- content ---

test("shows a reading message before the report resolves", () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  render(<ContextMeter percent={62} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(62);
  screen.getByText("Reading the context…");
});

test("the loaded panel shows the bar, the totals, auto-compact and the top used categories", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  render(<ContextMeter percent={62} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(62);

  await act(async () => {
    pending.resolve(reportFixture());
  });

  expect(screen.queryByText("Reading the context…")).toBeNull();
  const dialog = screen.getByRole("dialog", { name: "Context" });
  within(dialog).getByRole("img", { name: "127,700 of 200,000 tokens" });
  within(dialog).getByText("127,700 of 200,000 tokens");
  within(dialog).getByText("Auto-compact at 180,000 tokens");
  within(dialog).getByText("Messages 120,000 · System prompt 4,200 · Skills 1,000");
  within(dialog).getByText(`Reported by Claude Code at ${expectedTime(OBSERVED_MS)}`);
});

test("auto-compact reads as not reported, not off, when auto_compact itself is missing", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(1);
  await act(async () => {
    pending.resolve(reportFixture({ auto_compact: null }));
  });
  screen.getByText("Claude Code did not report this.");
  expect(screen.queryByText("Auto-compact is off")).toBeNull();
});

// `failed_report(reason)` (context_report.py): every field null, every part's reason the same
// one value — the route's exact body shape, parameterized over the reason. A respawn
// (effort changed on a live chat, then sent) keeps the old process's percent on the trigger while
// the new process answers `failed_report("not_observed")`; a first read that itself timed out or
// errored answers `failed_report("timeout"/"unavailable")` instead — same null shape, different
// reason, so it must not read as "no reply has ended yet" when a reply did end.
function failedReport(reason: ContextReason): ContextReport {
  return {
    observed_at: null,
    total_tokens: null,
    max_tokens: null,
    raw_max_tokens: null,
    percentage: null,
    auto_compact: null,
    categories: null,
    memory_files: null,
    mcp_servers: null,
    skills: null,
    agents: null,
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    reasons: {
      categories: reason,
      memory_files: reason,
      mcp_servers: reason,
      skills: reason,
      agents: reason,
    },
  };
}

test("an unobserved report shows the not-observed reason, not zeros, while the trigger keeps its percent", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  render(<ContextMeter percent={42} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(42);
  await act(async () => {
    pending.resolve(failedReport("not_observed"));
  });

  const dialog = screen.getByRole("dialog", { name: "Context" });
  const paragraphs = [...dialog.querySelectorAll("p")].map((p) => p.textContent);
  expect(paragraphs).toEqual(["No reply has ended yet in this Claude Code process."]);
  expect(dialog.querySelector(".context-bar")).toBeNull();
  screen.getByRole("button", { name: "context 42%" });
});

test.each([
  ["timeout", "The request timed out."],
  ["unavailable", "This is unavailable."],
] as const)(
  "a failed first read (%s) shows its own reason, not the not-observed sentence",
  async (reason, text) => {
    const pending = deferred<ContextReport>();
    const get = vi.fn().mockReturnValue(pending.promise);
    render(<ContextMeter percent={7} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
    openToggle(7);
    await act(async () => {
      pending.resolve(failedReport(reason));
    });

    const dialog = screen.getByRole("dialog", { name: "Context" });
    const paragraphs = [...dialog.querySelectorAll("p")].map((p) => p.textContent);
    expect(paragraphs).toEqual([text]);
    expect(dialog.querySelector(".context-bar")).toBeNull();
  },
);

// --- auto-compact: on / off / not reported — the same three reports ContextView.unit.test.tsx
// pins, so the popover and the page never again read one report two different ways ---

test("auto-compact: enabled with a known threshold reads on, with a mark", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  const { container } = render(
    <ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />,
  );
  openToggle(1);
  await act(async () => {
    pending.resolve(
      reportFixture({ auto_compact: { enabled: true, threshold: 167_000, source: "cli" } }),
    );
  });
  screen.getByText("Auto-compact at 167,000 tokens");
  expect(container.querySelector(".context-bar-mark")).not.toBeNull();
});

test("auto-compact: disabled reads off even with a threshold present, and omits the mark", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  const { container } = render(
    <ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />,
  );
  openToggle(1);
  await act(async () => {
    pending.resolve(
      reportFixture({ auto_compact: { enabled: false, threshold: 167_000, source: "cli" } }),
    );
  });
  screen.getByText("Auto-compact is off");
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

test("auto-compact: enabled with no threshold reads not reported, not off, and omits the mark", async () => {
  const pending = deferred<ContextReport>();
  const get = vi.fn().mockReturnValue(pending.promise);
  const { container } = render(
    <ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />,
  );
  openToggle(1);
  await act(async () => {
    pending.resolve(
      reportFixture({ auto_compact: { enabled: true, threshold: null, source: "cli" } }),
    );
  });
  screen.getByText("Claude Code did not report this.");
  expect(screen.queryByText("Auto-compact is off")).toBeNull();
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

// --- each error ---

test.each([
  [409, "Claude Code is not running for this chat."],
  [404, "This chat is not open."],
  [500, "Could not read the context."],
])("a %i response renders %s", async (status, message) => {
  const get = vi.fn().mockRejectedValue(new ContextApiError(status, "code"));
  render(<ContextMeter percent={5} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(5);
  await screen.findByText(message);
});

test("a non-ContextApiError rejection also reads as unavailable", async () => {
  const get = vi.fn().mockRejectedValue(new Error("network down"));
  render(<ContextMeter percent={5} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(5);
  await screen.findByText("Could not read the context.");
});

// --- Escape and focus ---

test("Escape closes the popover and returns focus to the toggle", () => {
  const { get } = pendingApi();
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  const toggle = screen.getByRole("button", { name: "context 1%" });
  fireEvent.click(toggle);
  expect(document.activeElement).not.toBe(toggle);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(toggle);
});

test("Escape is ignored while composing or for keyCode 229", () => {
  const { get } = pendingApi();
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(1);
  fireEvent.keyDown(window, { key: "Escape", isComposing: true });
  screen.getByRole("dialog");
  fireEvent.keyDown(window, { key: "Escape", keyCode: 229 });
  screen.getByRole("dialog");
});

test("Escape reaches only the popover: a bubble-phase window listener (e.g. App's own) never runs", () => {
  const outerCalls: string[] = [];
  function onWindowKeyDownBubble(event: KeyboardEvent): void {
    if (event.key === "Escape") outerCalls.push("outer");
  }
  // Mounted before the popover opens, mirroring App's own listener added once at mount, well
  // before any popover exists to open.
  window.addEventListener("keydown", onWindowKeyDownBubble);
  try {
    const { get } = pendingApi();
    render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: "context 1%" });
    fireEvent.click(toggle);
    const action = screen.getByRole("button", { name: "Open Context & loadout" });
    expect(document.activeElement).toBe(action);

    // Dispatched on the focused control, not window itself, so the event genuinely captures down
    // from window before it bubbles back up — the same path a real keypress takes.
    fireEvent.keyDown(action, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(toggle);
    expect(outerCalls).toEqual([]);
  } finally {
    window.removeEventListener("keydown", onWindowKeyDownBubble);
  }
});

// --- outside click ---

test("a mousedown outside the popover closes it", () => {
  const { get } = pendingApi();
  render(
    <div>
      <ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />
      <button type="button">elsewhere</button>
    </div>,
  );
  openToggle(1);
  fireEvent.mouseDown(screen.getByText("elsewhere"));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("Tab while focus is inside the popover closes it", () => {
  const { get } = pendingApi();
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={vi.fn()} />);
  openToggle(1);
  const action = screen.getByRole("button", { name: "Open Context & loadout" });
  expect(document.activeElement).toBe(action);
  fireEvent.keyDown(action, { key: "Tab" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

// --- Open ---

test("Open Context & loadout calls onOpenPage and closes, even while still loading", () => {
  const { get } = pendingApi();
  const onOpenPage = vi.fn();
  render(<ContextMeter percent={1} sessionId={SESSION} api={{ get }} onOpenPage={onOpenPage} />);
  openToggle(1);
  fireEvent.click(screen.getByRole("button", { name: "Open Context & loadout" }));
  expect(onOpenPage).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("dialog")).toBeNull();
});
