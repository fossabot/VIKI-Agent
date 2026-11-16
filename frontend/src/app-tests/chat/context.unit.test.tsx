/**
 * App-level wiring for the context clients, the status bar's two meters and the Context & loadout
 * page: a turn.done's context_percent swaps the status bar's plain span for the live ContextMeter;
 * opening that meter fetches the report once, and its own Open button reaches the Context &
 * loadout page; a limits reading shows the plan meter beside it.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION } from "../../accept/m3b/fakes";
import type { ContextApi, ContextReport } from "../../clients/conversations/contextApi";
import type { LimitsApi, PlanLimitReading } from "../../clients/conversations/limitsApi";
import type { Envelope, EventMap } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

/** One matchMedia for the whole App, answering `(min-width: Npx)` for a viewport of `width`,
 * able to announce a change the way Edge does when the window is resized or zoomed. Copied from
 * app-tests/shell/narrowLayout.unit.test.tsx. */
function stubViewport(width: number) {
  let current = width;
  const listeners = new Map<string, Set<(event: MediaQueryListEvent) => void>>();
  const matches = (query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query)?.[1];
    return min !== undefined && current >= Number(min);
  };
  vi.stubGlobal("matchMedia", (query: string) => {
    return {
      media: query,
      get matches(): boolean {
        return matches(query);
      },
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        const registered = listeners.get(query) ?? new Set();
        registered.add(listener);
        listeners.set(query, registered);
      },
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        listeners.get(query)?.delete(listener);
      },
    };
  });
  return {
    resizeTo(next: number): void {
      const previous = new Map([...listeners.keys()].map((query) => [query, matches(query)]));
      current = next;
      act(() => {
        for (const [query, registered] of listeners) {
          if (previous.get(query) === matches(query)) continue;
          const event = Object.assign(new Event("change"), {
            media: query,
            matches: matches(query),
          });
          for (const listener of [...registered]) listener(event);
        }
      });
    },
  };
}

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

function turnDone(sessionId: string, seq: number, contextPercent: number): Envelope {
  return frame(sessionId, seq, "turn.done", {
    status: "success",
    usage: { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_creation_tokens: 0 },
    context_percent: contextPercent,
    duration_ms: 10,
  });
}

function contextReport(overrides: Partial<ContextReport> = {}): ContextReport {
  return {
    observed_at: "2026-10-09T02:00:00.000Z",
    total_tokens: 1200,
    max_tokens: 200000,
    raw_max_tokens: 200000,
    percentage: 0.6,
    auto_compact: { enabled: true, threshold: 180000, source: "cli" },
    categories: [{ name: "Messages", tokens: 1200, kind: "used" }],
    memory_files: [],
    mcp_servers: [],
    skills: { total: 0, included: 0, tokens: 0, items: [] },
    agents: [],
    reasons: {},
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    ...overrides,
  };
}

/** Clicks "New session", lets the fake socket's automatic session.open handshake run, then
 * delivers session.started so `info` (and the generation every later frame needs) is set. */
function openSession(socket: {
  sent: Record<string, unknown>[];
  receive: (m: unknown) => void;
}): string {
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((f) => f.type === "session.open") as { session_id: string };
  act(() => socket.receive(started(opened.session_id, 1)));
  return opened.session_id;
}

test("a turn.done with context_percent shows the context meter", async () => {
  // The context meter is a status-bar field: it shows only while the right panel is off
  // screen, so this needs a narrower-than-1280px viewport to find it there at all.
  stubViewport(1100);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const contextApi: ContextApi = { get: vi.fn() };
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={contextApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(await screen.findByRole("button", { name: /context 37%/ })).toBeTruthy();
  viki.client.close();
});

test("the rail context footer is available before a measurement and uses only the selected chat", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const get = vi.fn().mockResolvedValue(contextReport());
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={{ get }} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  expect(screen.getByRole("button", { name: "Context & loadout: Not reported" })).toBeTruthy();
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(screen.getByRole("button", { name: "Context & loadout: 37% used" })).toBeTruthy();
  expect(get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("New session"));
  const next = socket.sent.findLast((item) => item.type === "session.open") as {
    session_id: string;
  };
  act(() => socket.receive(started(next.session_id, 1)));
  expect(screen.getByRole("button", { name: "Context & loadout: Not reported" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Context & loadout: 37% used" })).toBeNull();
  expect(get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Context & loadout: Not reported" }));
  expect(await screen.findByRole("heading", { name: "Context & loadout" })).toBeTruthy();
  expect(get).toHaveBeenCalledWith(next.session_id, expect.any(AbortSignal));
  viki.client.close();
});

test("the left rail leaves Context & loadout to the right panel footer", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const get = vi.fn().mockResolvedValue(contextReport());
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={{ get }} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const nav = screen.getByRole("navigation", { name: "Navigation and sessions" });
  expect(within(nav).getByRole("button", { name: "Chat" })).toBeTruthy();
  expect(within(nav).queryByRole("button", { name: "Context" })).toBeNull();
  expect(screen.getByRole("button", { name: "Context & loadout: Not reported" })).toBeTruthy();
  viki.client.close();
});

test("opening the context meter fetches once, and Open shows the Context & loadout page", async () => {
  // Same as above: the status bar only carries the meter while the right panel is off screen.
  stubViewport(1100);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const get = vi.fn().mockResolvedValue(contextReport());
  const contextApi: ContextApi = { get };
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={contextApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 10)));
  const toggle = await screen.findByRole("button", { name: /context 10%/ });
  fireEvent.click(toggle);
  expect(get).toHaveBeenCalledTimes(1);
  expect(get).toHaveBeenCalledWith(sessionId, expect.any(AbortSignal));
  fireEvent.click(await screen.findByRole("button", { name: "Open Context & loadout" }));
  expect(await screen.findByRole("heading", { name: "Context & loadout" })).toBeTruthy();
  viki.client.close();
});

test("a limits reading shows the plan meter", async () => {
  const resetsAt = Math.floor(Date.now() / 1000) + 3600;
  const reading: PlanLimitReading = {
    observed_at: "2026-10-09T02:00:00.000Z",
    status: "allowed_warning",
    limiting: "five_hour",
    utilization: 0.42,
    resets_at: resetsAt,
    windows: {
      five_hour: { utilization: 0.42, resets_at: resetsAt },
      seven_day: null,
    },
  };
  const limitsApi: LimitsApi = { get: vi.fn().mockResolvedValue(reading) };
  const rest = fakeRest();
  const { viki } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} limitsApi={limitsApi} />);
  expect(await screen.findByText(/5-hour window/)).toBeTruthy();
  viki.client.close();
});

/** The status bar carries the vault counts and the context reading only while the
 * right panel (the "VIKI panel") is off screen — narrower than the 1280px dock, and its drawer
 * closed. Docked or drawer-open, that data lives in the right panel's own tiles and footer
 * instead, never both places at once. */
function statusbar(): HTMLElement {
  const found = document.querySelector<HTMLElement>('[data-region="statusbar"]');
  if (!found) throw new Error("no status bar");
  return found;
}

test("at 1100, with no context client, the status bar carries the counts and the plain percent", async () => {
  stubViewport(1100);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(statusbar().textContent).toContain("knowledge");
  expect(statusbar().querySelector(".statusbar-context")?.textContent).toContain("context 37%");
  viki.client.close();
});

test("at 1100, a context client's meter lands in the status bar beside the counts", async () => {
  stubViewport(1100);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const contextApi: ContextApi = { get: vi.fn() };
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={contextApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(statusbar().textContent).toContain("knowledge");
  expect(within(statusbar()).getByRole("button", { name: /context 37%/ })).toBeTruthy();
  viki.client.close();
});

test("opening the VIKI panel drawer at 1100 moves the counts and the meter out of the status bar", async () => {
  stubViewport(1100);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const contextApi: ContextApi = { get: vi.fn() };
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={contextApi} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(statusbar().textContent).toContain("knowledge");
  fireEvent.click(screen.getByRole("button", { name: "VIKI panel" }));
  expect(statusbar().textContent).not.toContain("knowledge");
  expect(within(statusbar()).queryByRole("button", { name: /context 37%/ })).toBeNull();
  viki.client.close();
});

test("docked (the default width), neither the counts nor the meter reach the status bar, and the footer keeps the percent", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const get = vi.fn().mockResolvedValue(contextReport());
  render(<App viki={viki} rest={rest} durable={fakeDurable()} contextApi={{ get }} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const sessionId = openSession(socket);
  act(() => socket.receive(turnDone(sessionId, 2, 37)));
  expect(statusbar().textContent).not.toContain("knowledge");
  expect(within(statusbar()).queryByRole("button", { name: /context 37%/ })).toBeNull();
  expect(screen.getByRole("button", { name: "Context & loadout: 37% used" })).toBeTruthy();
  viki.client.close();
});
