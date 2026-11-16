/**
 * App wiring for a run's own desktop notice, the hidden window title, the narrow layout's
 * VIKI panel dot and the routines rail's Run now. `state/runNotifications.ts` and
 * `state/runGroups.ts` each prove their own logic in isolation already; what only a full App
 * render can prove is that `groups`, `waiting`, the notice's stable-ref `onOpen` and `onRunNow`
 * actually reach there from a real socket-driven session (same reasoning as src/app-tests/chat/backgroundRuns.unit.test.tsx).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  emptyRails,
  type FakeSocket,
  fakeRest,
  fakeViki,
  GENERATION,
  REQUEST,
} from "../../accept/m3b/fakes";
import type { RoutineItem, RoutinesApi, RunStarted } from "../../clients/automation/routinesApi";
import type { Envelope, EventMap } from "../../protocol";
import { NOTICE_TITLE } from "../../state/runNotifications";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

beforeEach(() => {});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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

function userMessage(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "user.message", {
    text: "hi",
    command: "",
    at: "2026-10-08T09:00:00.000Z",
  });
}

/** A fixed native request, the same on a card and on the `requestAnswered` that later closes it
 * — the fake socket auto-fills a fresh random one otherwise (accept/m3b/fakes.ts's `receive`),
 * which an outside `request.answered` could never match. */
const NATIVE_REQUEST = { runtime_id: GENERATION, request_id: "req-1", turn_id: REQUEST };

/** A main-thread "tool" permission card: new in an already-known session, it is what makes a
 * card frame and the waiting count move. */
function permissionRequested(sessionId: string, seq: number, toolUseId: string): Envelope {
  return {
    ...frame(sessionId, seq, "permission.requested", {
      tool_use_id: toolUseId,
      kind: "tool",
      tool: "Write",
      summary: "a.md",
      question: "",
      reason: "",
      agent: "",
      plan: "",
    }),
    native_request: NATIVE_REQUEST,
  } as Envelope;
}

/** Closes the card `permissionRequested` opened (state/sessionReducer.ts's `request.answered`). */
function requestAnswered(sessionId: string, seq: number, toolUseId: string): Envelope {
  return {
    ...frame(sessionId, seq, "request.answered", { tool_use_id: toolUseId }),
    native_request: NATIVE_REQUEST,
  } as Envelope;
}

/** Clicks New session and returns the new chat's id — copied from src/app-tests/chat/backgroundRuns.unit.test.tsx (every
 * App test file here scripts its own frames). */
function newSession(socket: FakeSocket): string {
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  return opened.session_id;
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

interface FakeNotice {
  title: string;
  body: string;
  onclick: (() => void) | null;
  close: () => void;
}

/** A granted `Notification` that records each notice's title and body, on a page that is not
 * focused — same stubbing src/app-tests/automation/routines.unit.test.tsx uses for its own routine notices. */
function stubNotification(): FakeNotice[] {
  const shown: FakeNotice[] = [];
  class FakeNotification implements FakeNotice {
    static permission = "granted";
    title: string;
    body: string;
    onclick: (() => void) | null = null;
    close = vi.fn();
    constructor(title: string, options?: { body?: string }) {
      this.title = title;
      this.body = options?.body ?? "";
      shown.push(this);
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  vi.spyOn(window, "focus").mockImplementation(() => {});
  return shown;
}

/** One matchMedia answering `(min-width: Npx)` for a 400px window, so both rails are drawers —
 * trimmed from src/app-tests/shell/narrowLayout.unit.test.tsx's stubViewport to the one width this file needs. */
function stubNarrowViewport(): void {
  vi.stubGlobal("matchMedia", (query: string) => {
    const min = Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? "0");
    return {
      media: query,
      matches: 400 >= min,
      addEventListener: () => {},
      removeEventListener: () => {},
    };
  });
}

/** A routines client with no live call pending but `list` (and optionally `run`). */
function routinesApi(routines: RoutineItem[]) {
  const pending = () => new Promise<never>(() => {});
  const list = vi.fn(async () => ({
    routines,
    scheduler: { state: "running", attended: true, paused: false },
  }));
  const run = vi.fn(
    async (): Promise<RunStarted> => ({
      occurrence_id: "o-1",
      attempt_id: "a-1",
      session_id: "s-run-1",
    }),
  );
  const api: RoutinesApi = {
    list,
    get: pending,
    runs: pending,
    preview: pending,
    create: pending,
    revise: pending,
    setState: pending,
    run,
  };
  return { api, list, run };
}

const routinesTab = () =>
  within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole("button", {
    name: /^Routines/,
  });
const inRoutinesView = () => screen.queryByRole("heading", { level: 1, name: "Routines" });

/** A shell with the one switch a run's own notice shares with a routine's turned on —
 * copied from src/app-tests/automation/routines.unit.test.tsx's own `shellApi`. */
function shellApi() {
  const settings = settingsFixture({ "onboarding.completed": true });
  return fakeShellApi({
    capabilities: capabilitiesFixture({}),
    settings: {
      ...settings,
      effective: { ...settings.effective, "notifications.enabled": true },
    },
    workspaces: {
      ...listingFixture([workspaceFixture({ root_physical_id: "12:34" })]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
}

test('a new card notifies once as "VIKI Agent"; its click opens the chat', async () => {
  const shown = stubNotification();
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      shell={shellApi()}
      routinesApi={routinesApi([]).api}
    />,
  );
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  expect(shown).toHaveLength(0);

  // Away from the chat when the notice fires, same as a routine's own notice in
  // src/app-tests/automation/routines.unit.test.tsx — its click must bring the chat back on screen.
  await click(routinesTab());
  expect(inRoutinesView()).not.toBeNull();

  act(() => socket.receive(permissionRequested(sessionId, 2, "tu1")));
  await waitFor(() => expect(shown).toHaveLength(1));
  expect(shown[0]?.title).toBe(NOTICE_TITLE);

  await act(async () => {
    shown[0]?.onclick?.();
  });
  expect(inRoutinesView()).toBeNull();
});

test("a repeated card arrival replaces the status child even when its text is unchanged", async () => {
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={fakeRest()} durable={fakeDurable()} routinesApi={routinesApi([]).api} />,
  );
  act(() => socket.open());
  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  await click(routinesTab());
  act(() => socket.receive(permissionRequested(sessionId, 2, "tu1")));
  const status = container.querySelector<HTMLElement>('[role="status"][aria-live="polite"]');
  expect(status?.textContent).toContain("needs your approval");
  const first = status?.firstElementChild;
  act(() => socket.receive(requestAnswered(sessionId, 3, "tu1")));
  act(() => socket.receive(permissionRequested(sessionId, 4, "tu2")));
  expect(status?.textContent).toContain("needs your approval");
  expect(status?.firstElementChild).not.toBe(first);
});

test("notice permission rejection keeps settings off and one rapid double click sends one request", async () => {
  stubNarrowViewport();
  let rejectPermission: (reason: Error) => void = () => {};
  const requestPermission = vi.fn(
    () =>
      new Promise<NotificationPermission>((_resolve, reject) => {
        rejectPermission = reject;
      }),
  );
  vi.stubGlobal("Notification", { permission: "default", requestPermission });
  const shell = shellApi();
  shell.data.settings = {
    ...shell.data.settings,
    effective: { ...shell.data.settings.effective, "notifications.enabled": false },
  };
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={fakeDurable()} shell={shell} />);
  act(() => socket.open());
  fireEvent.click(screen.getByRole("button", { name: "Sessions and agents" }));
  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(permissionRequested(sessionId, 2, "tu1")));
  fireEvent.click(screen.getByRole("button", { name: "VIKI panel" }));
  const enable = screen.getByRole("button", { name: "Turn on desktop notices" });
  act(() => {
    enable.click();
    enable.click();
  });
  expect(requestPermission).toHaveBeenCalledTimes(1);
  expect((enable as HTMLButtonElement).disabled).toBe(true);
  await act(async () => rejectPermission(new Error("permission failed")));
  expect(screen.getByText("Could not request notification permission. Try again.")).toBeTruthy();
  expect(shell.calls.filter((call) => call.method === "patchSettings")).toHaveLength(0);
  expect((enable as HTMLButtonElement).disabled).toBe(false);

  let finishSave: () => void = () => {};
  const originalPatch = shell.patchSettings.bind(shell);
  vi.spyOn(shell, "patchSettings").mockImplementation(async (...args) => {
    await new Promise<void>((resolve) => {
      finishSave = resolve;
    });
    return originalPatch(...args);
  });
  requestPermission.mockResolvedValueOnce("granted");
  fireEvent.click(enable);
  await waitFor(() => expect(shell.patchSettings).toHaveBeenCalledTimes(1));
  fireEvent.click(enable);
  expect(requestPermission).toHaveBeenCalledTimes(2);
  expect((enable as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finishSave());
  expect(screen.queryByRole("button", { name: "Turn on desktop notices" })).toBeNull();
});

test("the window title stays visually blank while cards open and are answered", async () => {
  const base = "\u2060";
  document.title = base;
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(permissionRequested(sessionId, 2, "tu1")));
  await waitFor(() => expect(document.title).toBe(base));

  act(() => socket.receive(requestAnswered(sessionId, 3, "tu1")));
  await waitFor(() => expect(document.title).toBe(base));

  // Ctrl+, opens Settings.
  await act(async () => {
    fireEvent.keyDown(document.body, { key: ",", ctrlKey: true });
  });
  await waitFor(() => expect(document.title).toBe(base));
});

test("a run ready for review and Mark reviewed leave the window title visually blank", async () => {
  const base = "\u2060";
  document.title = base;
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() =>
    socket.receive(
      frame(sessionId, 2, "subagent.started", {
        block_id: "a1",
        agent: "researcher",
        model: "",
        description: "Survey sources",
        reason: "",
      }),
    ),
  );
  act(() =>
    socket.receive(frame(sessionId, 3, "subagent.done", { block_id: "a1", status: "completed" })),
  );
  await waitFor(() => expect(document.title).toBe(base));

  fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
  await waitFor(() => expect(document.title).toBe(base));
});

test("the narrow layout's VIKI panel toggle shows a dot while something is waiting", async () => {
  stubNarrowViewport();
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  const panelToggle = () => screen.getByRole("button", { name: "VIKI panel" });
  expect(panelToggle().querySelector(".rail-toggle-dot")).toBeNull();

  await click(screen.getByRole("button", { name: "Sessions and agents" }));
  const sessionId = newSession(socket);
  act(() => socket.receive(userMessage(sessionId, 1)));
  act(() => socket.receive(permissionRequested(sessionId, 2, "tu1")));
  await waitFor(() => expect(panelToggle().querySelector(".rail-toggle-dot")).not.toBeNull());
  // Its name stays "VIKI panel": the query above would already fail otherwise.
});

test("Run now in the rail passes the routine's current revision, then refreshes", async () => {
  const digest: RoutineItem = {
    routine_id: "r-digest",
    name: "Morning digest",
    state: "enabled",
    current_revision: 7,
    state_revision: 7,
    schedule: { kind: "daily", timezone: "Asia/Tokyo" },
    next_run: "2099-01-01T00:00:00Z",
    last_run: null,
    last_attempt: null,
    active_run: null,
  };
  const fx = routinesApi([digest]);
  const rest = fakeRest({ rails: emptyRails() });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} routinesApi={fx.api} />);
  act(() => socket.open());
  await screen.findByText("Morning digest");
  await waitFor(() => expect(fx.list).toHaveBeenCalledTimes(1));

  await click(screen.getByRole("button", { name: "Run Morning digest now" }));

  await waitFor(() => expect(fx.run).toHaveBeenCalledWith("r-digest", expect.any(String), 7));
  await waitFor(() => expect(fx.list).toHaveBeenCalledTimes(2));
});
