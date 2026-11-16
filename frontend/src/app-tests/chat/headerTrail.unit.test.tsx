/**
 * App-level wiring for the header's trail. HeaderTrail's own pill, mode chip
 * and menu are pinned at the component level (components/Header/HeaderTrail.unit.test.tsx); this
 * file is the part only a full App render can prove: the chip tracks a session's real mode
 * (session.started, then every status), picking a mode sends session.set_mode over the real
 * socket while the label keeps reading the session, the pill's own count comes from the real run
 * groups, its click reaches the right rail's own Agents list (docked, or through the VIKI panel
 * drawer below 1280px), and every pane keeps its own pill and chip, selected or not, so the
 * pointerdown that activates an unfocused pane never unmounts its button before the click lands.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { Envelope, EventMap } from "../../protocol";
import { serializeLayout } from "../../state/paneFormat";
import { moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

/** One fixed-width matchMedia stub, trimmed from app-tests/shell/panes.unit.test.tsx's own
 * stubViewport: this file never resizes mid-test, so no listener bookkeeping is needed. */
function stubViewport(width: number): void {
  vi.stubGlobal("matchMedia", (query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query)?.[1];
    return {
      media: query,
      matches: min !== undefined && width >= Number(min),
      addEventListener: () => {},
      removeEventListener: () => {},
    };
  });
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
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

function started(sessionId: string, seq: number, mode: string): Envelope {
  return frame(sessionId, seq, "session.started", {
    model: "sonnet",
    mode,
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
    at: "2026-10-20T09:00:00.000Z",
  });
}

/** A "researcher" run, block a1, starting in the session's current turn — the same shape
 * app-tests/agents/runPanel.unit.test.tsx uses to put a row in the `working` list of run groups. */
function subagentStarted(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "subagent.started", {
    block_id: "a1",
    agent: "researcher",
    model: "",
    description: "Survey sources",
    reason: "",
  });
}

function status(sessionId: string, seq: number, state: string, mode: string): Envelope {
  return frame(sessionId, seq, "status", { state, mode });
}

function modeChip(root: HTMLElement): HTMLElement {
  return root.querySelector(".mode-chip") as HTMLElement;
}

function pill(root: HTMLElement): HTMLElement | null {
  return root.querySelector(".agents-pill");
}

/** No patch/remove is ever exercised here: this fake only has to be truthy, so App keeps its
 * stored split rather than collapsing it to the single-tab fallback (App.tsx's `multiPanes`). */
function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

test("one pane: the mode chip follows session.started and status, a pick sends session.set_mode, and the pill opens the agents list", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  act(() => socket.receive(started(sessionId, 1, "auto")));
  expect(modeChip(container).textContent).toContain("Auto");

  fireEvent.click(modeChip(container));
  fireEvent.click(within(screen.getByRole("menu")).getByText("Manual"));
  expect(
    socket.sent.some((sent) => sent.type === "session.set_mode" && sent.mode === "default"),
  ).toBe(true);
  // Same rule as the model chip: the label only ever reads the session, never the pick.
  expect(modeChip(container).textContent).toContain("Auto");

  act(() => socket.receive(status(sessionId, 2, "", "default")));
  expect(modeChip(container).textContent).toContain("Manual");

  act(() => socket.receive(userMessage(sessionId, 3)));
  act(() => socket.receive(subagentStarted(sessionId, 4)));
  expect(pill(container)?.textContent).toContain("1 subagent running");

  fireEvent.click(pill(container) as HTMLElement);
  expect(document.activeElement).toBe(container.querySelector("section.rail-agents"));
});

test("below 1280px the pill opens the VIKI panel drawer instead of focusing an already-docked list", async () => {
  stubViewport(1000);
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;
  act(() => socket.receive(started(sessionId, 1, "auto")));
  act(() => socket.receive(userMessage(sessionId, 2)));
  act(() => socket.receive(subagentStarted(sessionId, 3)));

  fireEvent.click(pill(container) as HTMLElement);
  expect(screen.getByRole("dialog", { name: "VIKI panel" })).toBeTruthy();
});

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

test("two panes: each keeps its own pill and chip; only the focused one gets Session actions, and a pointerdown-then-click on the unfocused pill still reaches it", async () => {
  const layout = moveTab(openTab(singlePane(), A), B, { group: "g1", zone: "right" });
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  const rest = fakeRest({
    sessions: [A, B].map((id) => sessionRow({ session_id: id, title: id })),
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={organization()} />,
  );
  act(() => socket.open());
  await settled();

  act(() => socket.receive(started(A, 1, "auto")));
  act(() => socket.receive(userMessage(A, 2)));
  act(() => socket.receive(subagentStarted(A, 3)));
  act(() => socket.receive(started(B, 1, "plan")));

  const panes = () => [...container.querySelectorAll<HTMLElement>("section.conversation.pane")];
  const paneA = panes()[0] as HTMLElement;
  const paneB = panes()[1] as HTMLElement;

  const paneAPill = within(paneA).getByRole("button", { name: "1 subagent running" });
  expect(modeChip(paneA).textContent).toContain("Auto");
  expect(within(paneA).queryByRole("button", { name: "Session actions" })).toBeNull();

  expect(within(paneB).queryByRole("button", { name: /subagent running/ })).toBeNull();
  expect(modeChip(paneB).textContent).toContain("Plan");
  expect(within(paneB).getByRole("button", { name: "Session actions" })).toBeTruthy();

  // A real press: pointerdown activates pane A (it was unfocused) before the click lands on the
  // very same pill — proving it was never unmounted in between.
  await act(async () => {
    fireEvent.pointerDown(paneAPill);
  });
  await act(async () => {
    fireEvent.click(paneAPill);
  });
  expect(document.activeElement).toBe(container.querySelector("section.rail-agents"));
});
