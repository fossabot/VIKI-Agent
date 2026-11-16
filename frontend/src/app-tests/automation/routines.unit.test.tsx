/**
 * Routines as App wires them. With a routines client the rail lists `/api/v2/routines` in place
 * of the `/api/rails` planned rows, the Routines tab opens the view and counts the routines that
 * need the user, a rail row opens its routine, a dirty routine editor asks before the tab leaves
 * the view, and a recovery window reads no routines and keeps the tab inert.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki, GENERATION, REQUEST } from "../../accept/m3b/fakes";
import { ShellApiError } from "../../clients/app/shellApi";
import type { RoutineItem, RoutinesApi } from "../../clients/automation/routinesApi";
import type { Envelope } from "../../protocol";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  type CapabilitiesOverrides,
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const RUN_SESSION = "run-session-1";
const PLANNED = "/daily · morning brief";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function item(overrides: Partial<RoutineItem> & { routine_id: string; name: string }): RoutineItem {
  return {
    state: "enabled",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: "Asia/Tokyo" },
    next_run: "2099-01-01T00:00:00Z",
    last_run: null,
    last_attempt: overrides.last_run?.attempt ? overrides.last_run : null,
    active_run: null,
    ...overrides,
  };
}

const DIGEST = item({ routine_id: "r-digest", name: "Morning digest" });
const RUNNING = item({
  routine_id: "r-running",
  name: "Inbox sweep",
  active_run: { attempt_id: "a-1", session_id: RUN_SESSION, state: "running", stop_by: null },
});

function routinesApi(routines: RoutineItem[] = [DIGEST, RUNNING]) {
  const pending = () => new Promise<never>(() => {});
  const list = vi.fn(async () => ({
    routines,
    scheduler: { state: "running", attended: true, paused: false },
  }));
  const api: RoutinesApi = {
    list,
    get: pending,
    runs: pending,
    preview: pending,
    create: pending,
    revise: pending,
    setState: pending,
    run: pending,
  };
  return { api, list };
}

function vaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
  };
}

/** `effective` carries the routine keys, which the shell fixture's key list does not know. */
function shellApi(
  capabilities: CapabilitiesOverrides = {},
  effective: Record<string, unknown> = {},
) {
  const settings = settingsFixture({ "onboarding.completed": true });
  return fakeShellApi({
    capabilities: capabilitiesFixture(capabilities),
    settings: { ...settings, effective: { ...settings.effective, ...effective } },
    workspaces: {
      ...listingFixture([workspaceFixture({ root_physical_id: "12:34" })]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

/** The routine run's session asks for a permission: the routine turns to waiting. */
async function openCard({ viki, socket }: ReturnType<typeof fakeViki>): Promise<void> {
  await act(async () => {
    viki.actions.openSession(RUN_SESSION);
  });
  await act(async () => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "permission.requested",
      session_id: RUN_SESSION,
      seq: 1,
      data: {
        tool_use_id: "tu-1",
        kind: "tool",
        tool: "Write",
        summary: "a.md",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
      native_request: { runtime_id: GENERATION, request_id: "req-1", turn_id: REQUEST },
    } as Envelope);
  });
}

interface FakeNotice {
  onclick: (() => void) | null;
  close: () => void;
}

/** A granted `Notification` that records each notice, on a page that is not focused. */
function stubNotification(): FakeNotice[] {
  const shown: FakeNotice[] = [];
  class FakeNotification implements FakeNotice {
    static permission = "granted";
    onclick: (() => void) | null = null;
    close = vi.fn();
    constructor() {
      shown.push(this);
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  vi.spyOn(window, "focus").mockImplementation(() => {});
  return shown;
}

/** Mounts App on a ready routines list, then lets the running routine turn to waiting. */
async function waitingTransition(notificationsEnabled: boolean): Promise<FakeNotice[]> {
  const shown = stubNotification();
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi({}, { "notifications.enabled": notificationsEnabled })}
      routinesApi={routinesApi().api}
    />,
  );
  act(() => fake.socket.open());
  await screen.findByText("Morning digest");
  await settled();
  expect(shown).toHaveLength(0);
  await openCard(fake);
  expect(routinesTab().textContent).toBe("Routines1");
  expect(routinesTab().getAttribute("aria-label")).toBe("Routines, 1 need you");
  return shown;
}

const routinesTab = () =>
  within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole("button", {
    name: /^Routines/,
  });
const inRoutinesView = () => screen.queryByRole("heading", { level: 1, name: "Routines" });

test("the planned rows never render with the routines client", async () => {
  const rest = fakeRest({
    rails: { ...emptyRails(), routines: [{ when: "07:30", name: PLANNED, planned: true }] },
  });
  const { viki } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} routinesApi={routinesApi().api} />,
  );
  await settled();
  expect(rest.calls).toContain("rails");
  expect(screen.queryByText(PLANNED)).toBeNull();
  expect(screen.queryByText("planned")).toBeNull();
  await screen.findByText("Morning digest");
  const rows = [...container.querySelectorAll(".rail-section--routines .rt")];
  expect(rows.map((row) => row.querySelector(".routines-rail-name")?.textContent)).toEqual([
    "Inbox sweep",
    "Morning digest",
  ]);
});

test("the Routines tab opens the Routines view", async () => {
  const { viki } = fakeViki();
  render(
    <App viki={viki} rest={fakeRest()} durable={fakeDurable()} routinesApi={routinesApi().api} />,
  );
  await settled();
  expect(inRoutinesView()).toBeNull();
  await click(routinesTab());
  expect(inRoutinesView()).not.toBeNull();
  expect(routinesTab().getAttribute("aria-current")).toBe("page");
  expect(screen.getByRole("button", { name: "Morning digest" })).toBeTruthy();
});

test("the tab counts a routine whose run session holds an open card", async () => {
  const { viki, socket } = fakeViki();
  render(
    <App viki={viki} rest={fakeRest()} durable={fakeDurable()} routinesApi={routinesApi().api} />,
  );
  act(() => socket.open());
  await screen.findByText("Morning digest");
  expect(routinesTab().textContent).toBe("Routines");
  await openCard({ viki, socket });
  expect(routinesTab().textContent).toBe("Routines1");
  expect(routinesTab().getAttribute("aria-label")).toBe("Routines, 1 need you");
});

test("a rail row opens the Routines view with that routine selected", async () => {
  const { viki } = fakeViki();
  render(
    <App viki={viki} rest={fakeRest()} durable={fakeDurable()} routinesApi={routinesApi().api} />,
  );
  // Not "Run Morning digest now" (the routines rail row's own button, name /Morning digest/ too): the row's
  // own open button never starts with "Run ".
  await click(await screen.findByRole("button", { name: /^(?!Run ).*Morning digest/ }));
  expect(inRoutinesView()).not.toBeNull();
  expect(screen.getByRole("button", { name: "Morning digest" }).getAttribute("aria-current")).toBe(
    "true",
  );
  expect(
    screen.getByRole("button", { name: "Inbox sweep" }).getAttribute("aria-current"),
  ).toBeNull();
});

test("a dirty routine editor asks before the Chat tab leaves the view", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { viki } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      vaultApi={vaultApi()}
      routinesApi={routinesApi().api}
    />,
  );
  await settled();
  await click(routinesTab());
  await click(screen.getByRole("button", { name: "New routine" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Evening review" } });
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(inRoutinesView()).not.toBeNull();
  expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Evening review");

  confirm.mockReturnValue(true);
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(inRoutinesView()).toBeNull();
});

test("a recovery window reads no routines and omits navigation; a normal one reads them", async () => {
  const recovery = routinesApi();
  const { viki } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi({
        app: { mode: "recovery" },
        storage: { state: "unavailable", reason: "busy" },
      })}
      routinesApi={recovery.api}
    />,
  );
  await settled();
  expect(screen.getByRole("heading", { level: 1 })).toBeTruthy();
  expect(recovery.list).not.toHaveBeenCalled();
  expect(screen.queryByRole("navigation", { name: "Navigation and sessions" })).toBeNull();
  expect(screen.queryByRole("button", { name: /^Routines/ })).toBeNull();
  cleanup();

  const normal = routinesApi();
  const second = fakeViki();
  render(
    <App
      viki={second.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi()}
      routinesApi={normal.api}
    />,
  );
  await settled();
  expect(normal.list).toHaveBeenCalledTimes(1);
  expect(routinesTab().getAttribute("aria-disabled")).toBeNull();
});

test("unmounting App stops the routines store: the read in flight is aborted", async () => {
  const signals: (AbortSignal | undefined)[] = [];
  const { api } = routinesApi();
  api.list = (signal) => {
    signals.push(signal);
    return new Promise<never>(() => {});
  };
  const { viki } = fakeViki();
  const view = render(
    <App viki={viki} rest={fakeRest()} durable={fakeDurable()} routinesApi={api} />,
  );
  await settled();
  expect(signals).toHaveLength(1);
  expect(signals[0]?.aborted).toBe(false);
  view.unmount();
  expect(signals[0]?.aborted).toBe(true);
});

test("a routine turning to waiting shows one notice when notifications are on; its click opens that routine", async () => {
  const shown = await waitingTransition(true);
  expect(shown).toHaveLength(1);
  expect(inRoutinesView()).toBeNull();
  await act(async () => {
    shown[0]?.onclick?.();
  });
  expect(inRoutinesView()).not.toBeNull();
  expect(screen.getByRole("button", { name: "Inbox sweep" }).getAttribute("aria-current")).toBe(
    "true",
  );
  expect(
    screen.getByRole("button", { name: "Morning digest" }).getAttribute("aria-current"),
  ).toBeNull();
});

test("the same transition shows no notice while notifications are off", async () => {
  const shown = await waitingTransition(false);
  expect(shown).toHaveLength(0);
});

test("a rejected settings patch shows that the settings were not saved", async () => {
  const shell = shellApi();
  const { viki } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      routinesApi={routinesApi().api}
    />,
  );
  await settled();
  await click(routinesTab());
  shell.fail("patchSettings", new ShellApiError(503, "busy"));
  await click(screen.getByRole("button", { name: "Pause all" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not save the settings.");
  expect(shell.calls.filter((call) => call.method === "patchSettings")).toHaveLength(1);
});
