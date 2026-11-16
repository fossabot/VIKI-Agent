/**
 * Unit tests pinning App-level behaviour that no frozen acceptance test can observe — see each
 * test for why.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "./App";
import { fakeRest, fakeViki, sessionRow } from "./accept/m3b/fakes";
import type { InventoryApi } from "./clients/conversations/inventoryApi";
import type { McpServersApi } from "./clients/conversations/mcpServersApi";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "./clients/decisionLoop/fake";
import { DecisionLoopApiError, type PageMissions } from "./clients/decisionLoop/types";
import type { BotRow, BotsApi } from "./clients/messaging/botsApi";
import type { ProfileApi } from "./clients/profiles/profileApi";
import { STARTER_KANBAN_FILE } from "./components/Bots/seatBots";
import { TEAM } from "./components/Bots/team";
import { WRAP_UP_PROMPT } from "./components/Header/wrapUp";
import type { Envelope, EventMap } from "./protocol";
import { fakeDurable } from "./testDurable";
import {
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  inspectionFixture,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "./testShell";

afterEach(cleanup);

function frame<K extends keyof EventMap>(seq: number, type: K, data: EventMap[K]): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type,
    session_id: "s-1",
    seq,
    data,
  } as Envelope;
}

async function openRow(container: HTMLElement): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("no row yet");
    return found;
  });
  fireEvent.click(row);
}

// frontend-3: Wrap up goes through handleSend, which refuses while a turn runs — so pressing it
// mid-turn wrote the convention into the composer and sent nothing, and the box then read as if
// it had. m4d's frozen test only ever presses it on an idle session.
test("Wrap up pressed while a turn runs leaves the composer alone and says why", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s-1", title: "vault" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  await openRow(container);
  act(() =>
    socket.receive(
      frame(1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: true,
      }),
    ),
  );
  act(() => socket.receive(frame(2, "turn.started", { turn_id: "t1" })));

  fireEvent.click(screen.getByLabelText("Session actions"));
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem"));
  });

  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  expect(box.value).toBe("");
  expect(box.value).not.toBe(WRAP_UP_PROMPT);
  expect(durable.submissions).toEqual([]);
  expect(socket.sent.filter((message) => message.type === "turn.send")).toEqual([]);
  expect(container.querySelector(".composer-refusal")?.textContent).toContain(
    "A reply is in progress. Wait for it to finish, then wrap up.",
  );
});

// Onboarding's Open form must hand PolicyPanel the `workspaces` listing: without it the panel
// falls back to the conservative read-only sentence — even for a vault that already
// admits note writes (ordinary, persistent, bootstrap complete). App must thread the listing
// through the same way WorkspacesPage does.
test("onboarding's Open form shows the editing sentence for an existing ordinary, persistent, bootstrap-complete vault", async () => {
  const rest = fakeRest({});
  const { viki } = fakeViki();
  const durable = fakeDurable();
  const shell = fakeShellApi({
    workspaces: listingFixture([workspaceFixture()]),
    inspection: inspectionFixture({ policy: "ordinary" }),
  });
  render(<App viki={viki} rest={rest} durable={durable} shell={shell} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /Switch workspace/ }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Change vault…" }));
  });

  fireEvent.change(screen.getByLabelText("Folder path"), {
    target: { value: workspaceFixture().root },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
  });

  await waitFor(() => {
    expect(
      screen.getByText("Notes in this vault can be created and edited in the Vault view."),
    ).toBeTruthy();
  });
  expect(screen.queryByText(/VIKI edits notes only in a vault it created/)).toBeNull();
});

function fakeInventoryApi(): InventoryApi {
  return { get: () => Promise.reject(new Error("unused in this test")) };
}

function fakeMcpServersApi(): McpServersApi {
  return { setEnabled: () => Promise.resolve(), reconnect: () => Promise.resolve() };
}

// App passes McpServersButton as MediaComposer's `mcpControl` only once both
// mcpServersApi and inventoryApi are given. Composer.unit.test.tsx pins the rendering contract
// (first child, only-when-given); this is the one place the App-level wiring is observable.
test("an MCP servers button reaches the composer, ahead of the model chip, once mcpServersApi and inventoryApi are both given", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s-1", title: "vault" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={durable}
      inventoryApi={fakeInventoryApi()}
      mcpServersApi={fakeMcpServersApi()}
    />,
  );
  act(() => socket.open());
  await openRow(container);

  const button = screen.getByRole("button", { name: "MCP servers" });
  expect(button.closest(".composer-actions")?.firstElementChild).toBe(button);
});

// ui-2: Start on a Bot, Run checks and Suggest destinations fired a request on every click, so a
// second click while the first was in flight was refused busy and read as a failure, and a Bot
// start refused by today's limit read as the generic line.
const never = () => new Promise<never>(() => {});
const noProfiles: ProfileApi = {
  list: async () => [],
  get: never,
  create: never,
  revise: never,
  archive: never,
  duplicate: never,
  compare: never,
};

// App is the only place `onVaultCreated` can reach `fillStarterTeam` (seatBots.ts): the prop
// exists only once both botsApi and profileApi are given, and CreateVaultForm calls it once after
// a successful create. seatBots.unit.test.ts already covers fillStarterTeam's own seat-filling
// rules; this pins that App's wiring reaches it, with the right arguments.
test("creating the starter vault fills its team's five Bot seats, and starts no chat", async () => {
  const shell = fakeShellApi({
    workspaces: listingFixture([workspaceFixture()]),
    createdFiles: [STARTER_KANBAN_FILE, "Welcome.md"],
  });
  const { viki } = fakeViki();
  const durable = fakeDurable();
  const rejected = (): Promise<never> => Promise.reject(new Error("unused in this test"));
  const botsCreates: { profileId: string; workspaceId: string }[] = [];
  let chatCalled = false;
  const botsApi: BotsApi = {
    async list() {
      return { bots: [], scheduler: { state: "running", attended: true, paused: false } };
    },
    get: rejected,
    async create(botId, profileId, _expectedProfileRevision, workspaceId) {
      botsCreates.push({ profileId, workspaceId });
      return {
        bot: { bot_id: botId, profile_id: profileId, workspace_id: workspaceId } as BotRow,
        replayed: false,
      };
    },
    chat: () => {
      chatCalled = true;
      return rejected();
    },
    newChat: rejected,
    setDelivery: rejected,
    remove: rejected,
    reset: rejected,
  };
  const profileApi: ProfileApi = {
    ...noProfiles,
    async create(profileId) {
      return { profile_id: profileId, revision: 1, state_revision: 1 };
    },
  };

  render(
    <App
      viki={viki}
      rest={fakeRest({})}
      durable={durable}
      shell={shell}
      botsApi={botsApi}
      profileApi={profileApi}
    />,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /Switch workspace/ }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "New vault…" }));
  });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Team vault" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
  });

  const teamIds = TEAM.map((seat) => seat.profileId).filter((id): id is string => id !== null);
  await waitFor(() => expect(botsCreates).toHaveLength(teamIds.length));
  expect(botsCreates.map((call) => call.profileId).sort()).toEqual([...teamIds].sort());
  for (const call of botsCreates) expect(call.workspaceId).toBe(FAKE_WORKSPACE_ID);
  expect(chatCalled).toBe(false);
});

// A vault made without the starter Kanban (no STARTER_KANBAN_FILE among created_files) must not
// touch either client: fillStarterTeam's own early return is covered by seatBots.unit.test.ts,
// this only pins that App's wiring does not bypass it.
test("creating a vault without the starter Kanban fills no seats", async () => {
  const shell = fakeShellApi({
    workspaces: listingFixture([workspaceFixture()]),
    createdFiles: ["Welcome.md"],
  });
  const { viki } = fakeViki();
  const durable = fakeDurable();
  const rejected = (): Promise<never> => Promise.reject(new Error("unused in this test"));
  const botsCreates: { profileId: string; workspaceId: string }[] = [];
  const botsApi: BotsApi = {
    async list() {
      return { bots: [], scheduler: { state: "running", attended: true, paused: false } };
    },
    get: rejected,
    async create(botId, profileId, _expectedProfileRevision, workspaceId) {
      botsCreates.push({ profileId, workspaceId });
      return {
        bot: { bot_id: botId, profile_id: profileId, workspace_id: workspaceId } as BotRow,
        replayed: false,
      };
    },
    chat: rejected,
    newChat: rejected,
    setDelivery: rejected,
    remove: rejected,
    reset: rejected,
  };
  const profileCreates: string[] = [];
  const profileApi: ProfileApi = {
    ...noProfiles,
    async create(profileId) {
      profileCreates.push(profileId);
      return { profile_id: profileId, revision: 1, state_revision: 1 };
    },
  };

  render(
    <App
      viki={viki}
      rest={fakeRest({})}
      durable={durable}
      shell={shell}
      botsApi={botsApi}
      profileApi={profileApi}
    />,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /Switch workspace/ }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "New vault…" }));
  });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Team vault" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
  });
  await waitFor(() => {
    expect(document.querySelector('[data-region="create-vault-result"]')).not.toBeNull();
  });

  expect(botsCreates).toHaveLength(0);
  expect(profileCreates).toHaveLength(0);
});

async function settled(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 8; index++) await Promise.resolve();
  });
}

test.each([
  ["seat_start", "Start", "startSeat"],
  ["commit_checks", "Run checks", "runChecks"],
  ["inbox_triage", "Suggest destinations", "suggestInbox"],
] as const)(
  "ui-2: %s's %s sends one request while it is in flight, its button disabled",
  async (candidate, name, method) => {
    const api = createFakeDecisionLoopApi();
    let answer: (missions: PageMissions) => void = () => {};
    const answered = new Promise<PageMissions>((resolve) => {
      answer = resolve;
    });
    let calls = 0;
    api[method] = () => {
      calls += 1;
      return answered;
    };
    const shell = fakeShellApi({
      settings: { ...settingsFixture({ "onboarding.completed": true }), read_only: false },
    });
    const { viki, socket } = fakeViki();
    render(
      <App
        viki={viki}
        rest={fakeRest({ sessions: [] })}
        durable={fakeDurable()}
        shell={shell}
        profileApi={noProfiles}
        decisionApi={api}
      />,
    );
    act(() => socket.open());
    await settled();
    const launcher = document.querySelector<HTMLButtonElement>("button.dloop-launcher");
    if (!launcher) throw new Error("no launcher");
    await act(async () => void fireEvent.click(launcher));
    await settled();
    const item = decisionLoopExamples().launcher.items.find(
      (one) => one.candidate === candidate && one.actions.some((a) => a.label === name),
    );
    if (!item) throw new Error(`the example launcher lacks ${candidate}`);
    const card = screen.getByText(item.what).closest("article");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    const button = () => within(card).getByRole<HTMLButtonElement>("button", { name });

    await act(async () => void fireEvent.click(button()));
    await act(async () => void fireEvent.click(button()));
    expect(calls).toBe(1);
    expect(button().disabled).toBe(true);

    await act(async () => answer(await createFakeDecisionLoopApi().missions()));
    await settled();
    expect(button().disabled).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

test("ui-2: a Bot's Start refused by today's limit says so; any other refusal keeps its line", async () => {
  const api = createFakeDecisionLoopApi();
  let code = "limit";
  api.startSeat = async () => {
    throw new DecisionLoopApiError(code, 409);
  };
  const shell = fakeShellApi({
    settings: { ...settingsFixture({ "onboarding.completed": true }), read_only: false },
  });
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [] })}
      durable={fakeDurable()}
      shell={shell}
      profileApi={noProfiles}
      decisionApi={api}
    />,
  );
  act(() => socket.open());
  await settled();
  const launcher = document.querySelector<HTMLButtonElement>("button.dloop-launcher");
  if (!launcher) throw new Error("no launcher");
  await act(async () => void fireEvent.click(launcher));
  await settled();
  const item = decisionLoopExamples().launcher.items.find((one) => one.candidate === "seat_start");
  if (!item) throw new Error("the example launcher lacks seat_start");
  const card = screen.getByText(item.what).closest("article");
  if (!(card instanceof HTMLElement)) throw new Error("no card");
  const start = () => within(card).getByRole("button", { name: "Start" });

  await act(async () => void fireEvent.click(start()));
  await settled();
  expect(screen.getByRole("alert").textContent).toBe(
    "VIKI can't start another Bot today: today's limit is used.",
  );
  code = "not_applicable";
  await act(async () => void fireEvent.click(start()));
  await settled();
  expect(screen.getByRole("alert").textContent).toBe("VIKI couldn't start that Bot.");
});
