import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  type ContextApi,
  ContextApiError,
  type ContextReason,
  type ContextReport,
} from "../../clients/conversations/contextApi";
import { ContextView, type ContextViewProps } from "./ContextView";

afterEach(cleanup);

function report(overrides: Partial<ContextReport> = {}): ContextReport {
  return {
    observed_at: "2026-10-09T02:00:00.000Z",
    total_tokens: 1200,
    max_tokens: 200000,
    raw_max_tokens: 200000,
    percentage: 0.6,
    auto_compact: { enabled: true, threshold: 180000, source: "cli" },
    categories: [
      { name: "System prompt", tokens: 400, kind: "used" },
      { name: "Messages", tokens: 800, kind: "used" },
      { name: "Autocompact buffer", tokens: 2000, kind: "buffer" },
      { name: "Free space", tokens: 197000, kind: "free" },
    ],
    memory_files: [],
    mcp_servers: [],
    skills: { total: 0, included: 0, tokens: 0, items: [] },
    agents: [],
    reasons: {},
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    ...overrides,
  };
}

// Computed the same way the component reads a timestamp (local HH:MM:SS), so this assertion
// never depends on the test runner's own timezone.
function clock(iso: string): string {
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

function neverApi(): ContextApi {
  return { get: vi.fn(() => new Promise<ContextReport>(() => {})) };
}

function renderView(overrides: Partial<ContextViewProps> = {}) {
  return render(
    <ContextView
      api={neverApi()}
      chat={null}
      busy={false}
      refreshKey={0}
      onBack={vi.fn()}
      {...overrides}
    />,
  );
}

it("shows a message and never calls the api when no chat is open", () => {
  const get = vi.fn();
  renderView({ api: { get } });
  expect(screen.getByText("Open a chat to see its context.")).toBeTruthy();
  expect(get).not.toHaveBeenCalled();
});

it("loads the chat's report on mount, passing its session id and an abort signal", async () => {
  const get = vi.fn().mockResolvedValue(report());
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText(/1,200 of/);
  expect(get).toHaveBeenCalledWith("a", expect.any(AbortSignal));
});

it("409 reports Claude Code is not running yet", async () => {
  const get = vi.fn().mockRejectedValue(new ContextApiError(409, "not_running"));
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  expect(
    await screen.findByText(
      "Claude Code is not running for this chat yet. Send a message; the breakdown appears after the reply.",
    ),
  ).toBeTruthy();
});

it("404 reports the chat is not open in this window", async () => {
  const get = vi.fn().mockRejectedValue(new ContextApiError(404, "not_found"));
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  expect(await screen.findByText("This chat is not open in this window.")).toBeTruthy();
});

it("shows the generic failure for any other error, and Try again reloads", async () => {
  const get = vi
    .fn()
    .mockRejectedValueOnce(new ContextApiError(500, "server_error"))
    .mockResolvedValueOnce(report({ total_tokens: 999 }));
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("Could not read the context.");
  expect(get).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByText(/999 of/);
  expect(get).toHaveBeenCalledTimes(2);
});

it("drops a late report for a session that is no longer selected", async () => {
  let resolveA!: (value: ContextReport) => void;
  const get = vi.fn((sessionId: string) =>
    sessionId === "a"
      ? new Promise<ContextReport>((resolve) => {
          resolveA = resolve;
        })
      : Promise.resolve(report({ total_tokens: 222 })),
  );
  const api: ContextApi = { get };
  const view = renderView({ api, chat: { sessionId: "a", title: "Chat A" } });
  view.rerender(
    <ContextView
      api={api}
      chat={{ sessionId: "b", title: "Chat B" }}
      busy={false}
      refreshKey={0}
      onBack={vi.fn()}
    />,
  );
  await screen.findByText(/222 of/);
  await act(async () => resolveA(report({ total_tokens: 111 })));
  expect(screen.queryByText(/111 of/)).toBeNull();
});

it("reloads the same session only when refreshKey changes, keeping the prior report visible meanwhile", async () => {
  let resolveSecond!: (value: ContextReport) => void;
  const get = vi
    .fn()
    .mockResolvedValueOnce(report({ total_tokens: 100 }))
    .mockImplementationOnce(
      () =>
        new Promise<ContextReport>((resolve) => {
          resolveSecond = resolve;
        }),
    );
  const api: ContextApi = { get };
  const chat = { sessionId: "a", title: "Chat A" };
  const view = render(
    <ContextView api={api} chat={chat} busy={false} refreshKey={1} onBack={vi.fn()} />,
  );
  await screen.findByText(/100 of/);
  expect(get).toHaveBeenCalledTimes(1);

  view.rerender(<ContextView api={api} chat={chat} busy={false} refreshKey={1} onBack={vi.fn()} />);
  expect(get).toHaveBeenCalledTimes(1);

  view.rerender(<ContextView api={api} chat={chat} busy={false} refreshKey={2} onBack={vi.fn()} />);
  expect(get).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/100 of/)).toBeTruthy();

  await act(async () => resolveSecond(report({ total_tokens: 200 })));
  expect(await screen.findByText(/200 of/)).toBeTruthy();
});

it("has no tablist when decision is omitted", () => {
  renderView();
  expect(screen.queryByRole("tablist")).toBeNull();
  expect(screen.getByText("Open a chat to see its context.")).toBeTruthy();
});

it("shows a Context/Decision tablist when decision is given; switching tabs swaps panels without losing the loaded report", async () => {
  const get = vi.fn().mockResolvedValue(report({ total_tokens: 555 }));
  renderView({
    api: { get },
    chat: { sessionId: "a", title: "Chat A" },
    decision: <p>Decision panel</p>,
  });
  expect(screen.getByRole("tab", { name: "Context" }).getAttribute("aria-selected")).toBe("true");
  await screen.findByText(/555 of/);
  expect(screen.queryByText("Decision panel")).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "Decision" }));
  expect(screen.getByText("Decision panel")).toBeTruthy();
  expect(screen.queryByText(/555 of/)).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "Context" }));
  expect(screen.getByText(/555 of/)).toBeTruthy();
  expect(get).toHaveBeenCalledTimes(1);
});

it("a fresh decisionRequest selects the Decision tab, even once the view is already open on Context", () => {
  const chat = { sessionId: "a", title: "Chat A" };
  const view = renderView({ api: neverApi(), chat, decision: <p>Decision panel</p> });
  expect(screen.getByRole("tab", { name: "Context" }).getAttribute("aria-selected")).toBe("true");

  view.rerender(
    <ContextView
      api={neverApi()}
      chat={chat}
      busy={false}
      refreshKey={0}
      onBack={vi.fn()}
      decision={<p>Decision panel</p>}
      decisionRequest={{}}
    />,
  );

  expect(screen.getByRole("tab", { name: "Decision" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByText("Decision panel")).toBeTruthy();
});

it("focuses the heading on mount", () => {
  renderView();
  expect(screen.getByRole("heading", { name: "Context & loadout" })).toBe(document.activeElement);
});

it("shows a default Back to chat button that calls onBack, and the chat title under the heading", () => {
  const onBack = vi.fn();
  renderView({ chat: { sessionId: "a", title: "Chat A" }, onBack });
  fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(onBack).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Chat A")).toBeTruthy();
});

it("a supplied renderBack replaces the default button", () => {
  const onBack = vi.fn();
  renderView({
    onBack,
    renderBack: (back) => (
      <button type="button" onClick={back}>
        Back
      </button>
    ),
  });
  expect(screen.queryByRole("button", { name: "Back to chat" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(onBack).toHaveBeenCalledTimes(1);
});

it("the bar carries the auto-compact mark and the headline reads total of max (percentage%)", async () => {
  const get = vi.fn().mockResolvedValue(report());
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("1,200 of 200,000 tokens (0.6%)");
  const bar = container.querySelector(".context-bar");
  expect(bar?.getAttribute("aria-label")).toBe("1,200 of 200,000 tokens (0.6%)");
  const mark = container.querySelector<HTMLElement>(".context-bar-mark");
  expect(mark).not.toBeNull();
  expect(parseFloat(mark?.style.left ?? "")).toBeCloseTo((180000 / 200000) * 100);
});

it("says auto-compact is off when disabled, and omits the mark", async () => {
  const get = vi
    .fn()
    .mockResolvedValue(
      report({ auto_compact: { enabled: false, threshold: 180000, source: "cli" } }),
    );
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("Auto-compact is off");
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

// `failed_report(reason)` (context_report.py): every field null, every part's reason the same
// one value — the route's exact body shape, parameterized over the reason, same
// fixture ContextMeter.unit.test.tsx pins for the popover. A respawn (effort changed on a live
// chat, then sent) keeps the old process's percent on the status-bar trigger while the new
// process answers `failed_report("not_observed")`; a first read that itself timed out or errored
// answers `failed_report("timeout"/"unavailable")` instead — same null shape, different reason,
// so it must not read as "no reply has ended yet" when a reply did end.
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

// ContextItems' own per-part reason repeats the same sentence five times further down the page;
// wait on its heading instead of the sentence so the query never trips over its own multiple
// matches, then read only the Report's own direct paragraphs.
function directParagraphs(container: HTMLElement): (string | null)[] {
  const reportDiv = container.querySelector(".context-view-report");
  return [...(reportDiv?.children ?? [])]
    .filter((el) => el.tagName === "P")
    .map((el) => el.textContent);
}

it("an unobserved report shows the not-observed reason, not dashes, in place of the figures", async () => {
  const get = vi.fn().mockResolvedValue(failedReport("not_observed"));
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByRole("heading", { name: "Breakdown", level: 2 });

  expect(directParagraphs(container)).toEqual([
    "No reply has ended yet in this Claude Code process.",
  ]);
  expect(container.querySelector(".context-bar")).toBeNull();
});

it.each([
  ["timeout", "The request timed out."],
  ["unavailable", "This is unavailable."],
] as const)(
  "a failed first read (%s) shows its own reason, not the not-observed sentence",
  async (reason, text) => {
    const get = vi.fn().mockResolvedValue(failedReport(reason));
    const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
    await screen.findByRole("heading", { name: "Breakdown", level: 2 });

    expect(directParagraphs(container)).toEqual([text]);
    expect(container.querySelector(".context-bar")).toBeNull();
  },
);

// --- auto-compact: on / off / not reported — the same three reports ContextMeter.unit.test.tsx
// pins, so the popover and the page never again read one report two different ways ---

it("auto-compact: enabled with a known threshold reads on, with a mark", async () => {
  const get = vi
    .fn()
    .mockResolvedValue(
      report({ auto_compact: { enabled: true, threshold: 167_000, source: "cli" } }),
    );
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("Auto-compact at 167,000 tokens (source: cli)");
  expect(container.querySelector(".context-bar-mark")).not.toBeNull();
});

it("auto-compact: disabled reads off even with a threshold present, and omits the mark", async () => {
  const get = vi
    .fn()
    .mockResolvedValue(
      report({ auto_compact: { enabled: false, threshold: 167_000, source: "cli" } }),
    );
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("Auto-compact is off");
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

it("auto-compact: enabled with no threshold reads not reported, not off, and omits the mark", async () => {
  const get = vi
    .fn()
    .mockResolvedValue(report({ auto_compact: { enabled: true, threshold: null, source: "cli" } }));
  const { container } = renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText("Claude Code did not report this.");
  expect(screen.queryByText("Auto-compact is off")).toBeNull();
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

it("shows the effective-window line only when raw_max_tokens differs from max_tokens", async () => {
  const get = vi.fn().mockResolvedValue(report({ raw_max_tokens: 250000 }));
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  expect(
    await screen.findByText("Effective window 200,000 of the model's 250,000 tokens"),
  ).toBeTruthy();
});

it("omits the effective-window line when raw_max_tokens equals max_tokens", async () => {
  const get = vi.fn().mockResolvedValue(report());
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  await screen.findByText(/1,200 of/);
  expect(screen.queryByText(/Effective window/)).toBeNull();
});

it("shows when Claude Code reported it, and a breathing dot only while busy", async () => {
  const get = vi.fn().mockResolvedValue(report({ observed_at: "2026-10-09T02:00:00.000Z" }));
  const api: ContextApi = { get };
  const chat = { sessionId: "a", title: "Chat A" };
  const { container, rerender } = renderView({ api, chat, busy: true });
  const expected = clock("2026-10-09T02:00:00.000Z");
  await screen.findByText(`Reported by Claude Code at ${expected}`);
  expect(screen.getByText("A reply is running; this updates when it ends.")).toBeTruthy();
  expect(container.querySelector(".context-view-busy-dot")?.classList.contains("breathe")).toBe(
    true,
  );

  rerender(<ContextView api={api} chat={chat} busy={false} refreshKey={0} onBack={vi.fn()} />);
  expect(screen.queryByText("A reply is running; this updates when it ends.")).toBeNull();
});

it("renders ContextItems below the bar", async () => {
  const get = vi.fn().mockResolvedValue(report());
  renderView({ api: { get }, chat: { sessionId: "a", title: "Chat A" } });
  expect(await screen.findByRole("heading", { name: "Breakdown", level: 2 })).toBeTruthy();
});
