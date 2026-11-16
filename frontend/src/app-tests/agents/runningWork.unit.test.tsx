/**
 * App-level wiring for the Agents view's Running work section: the last vault.sessions report
 * becomes its rows, and Open, Stop and Close go through the same actions the rest of the app uses.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type {
  ProfileApi,
  ProfileDefinition,
  ProfileDocumentV1,
} from "../../clients/profiles/profileApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import type { Envelope, EventMap, ModelOption } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

const MODELS: ModelOption[] = [{ value: "sonnet", label: "Sonnet", description: "" }];

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
    models: MODELS,
    cli_version: "2.1.259",
    resumed: true,
  });
}

function userMessage(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "user.message", {
    text: "hi",
    command: "",
    at: "2026-09-27T10:00:00.000Z",
  });
}

function textDelta(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "text.delta", { block_id: "b1", text: "hello" });
}

function vaultSessions(seq: number, live: string[]): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq,
    data: { others: [], live },
  } as Envelope;
}

/** The vault names a session live, this window discovers it and opens it (as any window would),
 * and the first synchronize answers idle (no fixture controls that reply) and retracts it from
 * the live report on its own. A second, identical report is what a real reconnect also sends, and
 * this time nothing reopens an already-tracked session, so the live report stands. */
function makeLive(socket: { receive: (message: unknown) => void }, sessionId: string): void {
  act(() => socket.receive(vaultSessions(1, [sessionId])));
  act(() => socket.receive(vaultSessions(2, [sessionId])));
}

/** Empty (no profiles), so Running work never resolves a profile identity for these rows. */
function emptyProfileApi(): ProfileApi {
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [],
    get: pending,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
  };
}

function profileSessionPreview(sessionId: string): PreparedProfilePreview {
  return {
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: "sonnet" },
    session_id: sessionId,
    profile_id: "profile",
    profile_revision: 3,
    generation: null,
    workspace_id: "workspace",
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 1,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: sessionId, revision: 1, text: "x", saved_at: null, attachments: [] },
    identity: {
      name: "Planner",
      avatar_asset_id: null,
      accent: "orange",
      shape: "circle",
      profile_revision: 3,
    },
  };
}

async function openAgentsRunningWork(): Promise<void> {
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  fireEvent.click(await screen.findByRole("tab", { name: "Running work" }));
}

test("a live session lists in Running work, busy; Stop interrupts it and Close ends it", async () => {
  const SESSION = "live-1";
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SESSION, title: "Live chat" })] });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} profileApi={emptyProfileApi()} />);
  act(() => socket.open());
  makeLive(socket, SESSION);
  act(() => socket.receive(started(SESSION, 1)));
  act(() => socket.receive(userMessage(SESSION, 2)));
  act(() => socket.receive(textDelta(SESSION, 3)));

  await openAgentsRunningWork();

  const row = await within(screen.getByRole("tabpanel", { name: "Running work" })).findByText(
    "Live chat",
  );
  const item = row.closest("li");
  if (!item) throw new Error("no row");
  expect(item.textContent).toContain("busy");

  socket.sent.length = 0;
  fireEvent.click(within(item).getByText("Stop"));
  await waitFor(() =>
    expect(
      socket.sent.filter((m) => m.type === "turn.interrupt" && m.session_id === SESSION).length,
    ).toBe(1),
  );

  socket.sent.length = 0;
  fireEvent.click(within(item).getByText("Close"));
  await waitFor(() =>
    expect(
      socket.sent.filter((m) => m.type === "session.close" && m.session_id === SESSION).length,
    ).toBe(1),
  );

  viki.client.close();
});

test("a tracked, not-live session stays out of Running work; a live profile session shows its identity through App's inspect wiring", async () => {
  const LIVE = "live-4";
  const PREPARED = "prepared-4";
  const rest = fakeRest({
    sessions: [
      sessionRow({ session_id: LIVE, title: "Live chat" }),
      sessionRow({ session_id: PREPARED, title: "Prepared chat" }),
    ],
  });
  const { viki, socket } = fakeViki();
  const inspect = vi.fn(async (id: string) => {
    if (id === LIVE) return profileSessionPreview(LIVE);
    throw new ProfileLaunchApiError(404, "not_found");
  });
  const launch = {
    capabilities: vi.fn(),
    models: vi.fn(),
    inspect,
    prepare: vi.fn(),
  } as unknown as ProfileLaunchApi;
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileApi={emptyProfileApi()}
      profileLaunchApi={launch}
    />,
  );
  act(() => socket.open());
  makeLive(socket, LIVE);

  // Open the prepared session so the window tracks it, while no frame ever lists it live.
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${PREPARED}"]`);
    if (!found) throw new Error("no row yet");
    return found;
  });
  fireEvent.click(row);
  await waitFor(() =>
    expect(socket.sent.some((m) => m.type === "session.open" && m.session_id === PREPARED)).toBe(
      true,
    ),
  );

  await openAgentsRunningWork();
  const live = await within(screen.getByRole("tabpanel", { name: "Running work" })).findByText(
    "Live chat",
  );
  const item = live.closest("li");
  if (!item) throw new Error("no row");
  const list = item.parentElement as HTMLElement;
  expect(within(list).queryByText("Prepared chat")).toBeNull();
  expect(list.querySelectorAll("li").length).toBe(1);
  await waitFor(() => expect(item.textContent).toContain("Planner"));
  expect(item.textContent).toContain("r3");
  expect(item.textContent).toContain("Profile");

  viki.client.close();
});

test("Open selects the running session in the chat", async () => {
  const SESSION = "live-2";
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SESSION, title: "Other chat" })] });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} profileApi={emptyProfileApi()} />);
  act(() => socket.open());
  makeLive(socket, SESSION);

  await openAgentsRunningWork();

  const row = await within(screen.getByRole("tabpanel", { name: "Running work" })).findByText(
    "Other chat",
  );
  const item = row.closest("li");
  if (!item) throw new Error("no row");
  fireEvent.click(within(item).getByText("Open"));

  // The Agents view is gone (Open navigated to Chat) and the session's own title now names the
  // conversation header.
  await waitFor(() => expect(screen.queryByRole("tablist", { name: "Agents" })).toBeNull());
  expect(document.querySelector(".conversation-title")?.textContent).toBe("Other chat");

  viki.client.close();
});

/** A single profile, so a click into it opens the editor with an existing document to dirty. */
function oneProfileApi(): ProfileApi {
  const doc: ProfileDocumentV1 = {
    format_version: 1,
    identity: {
      name: "Alpha",
      purpose: "",
      avatar_asset_id: null,
      accent: "blue",
      shape: "rounded",
    },
    runtime: { provider: "claude", model: null, effort: "high", permission_mode: null },
    instructions: "",
    workspace: { allowed_workspace_ids: [] },
    context: { items: [] },
    native_assets: [],
    tools: { required: [], denied: [], mcp_configuration: null },
    memory: { mode: "provider_inherited", sources: [] },
    limits: {
      timeout_seconds: null,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    },
    provenance: { kind: "user", derived_from: null, native_source: null },
  };
  const definition: ProfileDefinition = {
    profile_id: "00000000-0000-4000-8000-000000000001",
    revision: 1,
    current_revision: 1,
    state_revision: 1,
    created_at: "2026-09-21T00:00:00.000Z",
    updated_at: "2026-09-21T00:00:00.000Z",
    archived: false,
    document: doc,
  };
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [
      {
        profile_id: definition.profile_id,
        revision: definition.revision,
        current_revision: definition.current_revision,
        state_revision: definition.state_revision,
        created_at: definition.created_at,
        updated_at: definition.updated_at,
        archived: definition.archived,
        identity: doc.identity,
        runtime: doc.runtime,
        provenance: doc.provenance,
      },
    ],
    get: async () => definition,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
  };
}

test("Close on a session that wrote in the Agents view closes it at once, with no dialog", async () => {
  const SESSION = "live-3";
  // No `rails` given: `fakeRest`'s default git chip is `not_repository` — closing still goes
  // straight through, regardless of git state.
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SESSION, title: "Written chat" })] });
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} profileApi={emptyProfileApi()} />);
  act(() => socket.open());
  makeLive(socket, SESSION);
  act(() => socket.receive(started(SESSION, 1)));
  act(() => socket.receive(userMessage(SESSION, 2)));
  act(() => socket.receive(frame(SESSION, 3, "turn.started", { turn_id: "t1" })));
  act(() =>
    socket.receive(
      frame(SESSION, 4, "tool.started", {
        block_id: "b1",
        tool: "Write",
        row: { head: "", rest: "x.md", tail: "" },
        parent_id: "",
      }),
    ),
  );

  await openAgentsRunningWork();
  const row = await within(screen.getByRole("tabpanel", { name: "Running work" })).findByText(
    "Written chat",
  );
  const item = row.closest("li");
  if (!item) throw new Error("no row");

  fireEvent.click(within(item).getByText("Close"));
  expect(screen.queryByRole("alertdialog")).toBeNull();

  await waitFor(() =>
    expect(
      socket.sent.filter((m) => m.type === "session.close" && m.session_id === SESSION).length,
    ).toBe(1),
  );

  viki.client.close();
});

test("switching to Running work with an unsaved profile edit asks; cancelling keeps the edit and the Profiles tab", async () => {
  const rest = fakeRest({ sessions: [] });
  const { viki, socket } = fakeViki();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App viki={viki} rest={rest} durable={fakeDurable()} profileApi={oneProfileApi()} />);
  act(() => socket.open());

  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  fireEvent.click(await screen.findByRole("button", { name: /Alpha/ }));
  const name = await screen.findByRole("textbox", { name: "Name" });
  fireEvent.change(name, { target: { value: "Changed" } });

  fireEvent.click(screen.getByRole("tab", { name: "Running work" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe("true");
  expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Changed");

  viki.client.close();
});
