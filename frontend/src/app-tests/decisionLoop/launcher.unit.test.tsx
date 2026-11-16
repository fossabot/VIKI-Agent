/**
 * VIKI's launcher as App mounts it: with a `decisionApi`, the launcher sits beside the help
 * center in both of App's returns and keeps its place across views; its panel re-checks, lists
 * the items, answers them, follows their actions through App's own openers, writes the menu's
 * settings and holds VIKI's own chat; the window reports its presence; Context & loadout gains
 * its Decision tab; and the session list marks the chats VIKI started.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { SettingsView } from "../../clients/app/shellApi";
import type { ContextApi } from "../../clients/conversations/contextApi";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
  type FakeDecisionLoopApi,
} from "../../clients/decisionLoop/fake";
import type { LauncherView, PresenceBody } from "../../clients/decisionLoop/types";
import type { MessagingApi } from "../../clients/messaging/messagingApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { CapturesApi } from "../../clients/vaults/capturesApi";
import type { SessionRow } from "../../protocol";
import type { DecisionsApi } from "../../state/decisionsApi";
import type { SkillProposalsApi } from "../../state/skillProposalsApi";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  type FakeShellApi,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const STAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/;
const HOUR_MS = 3_600_000;

const pending = () => new Promise<never>(() => {});
const profiles: ProfileApi = {
  list: async () => [],
  get: pending,
  create: pending,
  revise: pending,
  archive: pending,
  duplicate: pending,
  compare: pending,
};
const context: ContextApi = { get: pending };

async function settled() {
  await act(async () => {
    for (let index = 0; index < 8; index++) await Promise.resolve();
  });
}
/** A ready settings document, with `effective` values the fixture's own key list lacks. */
function readySettings(effective: Record<string, unknown> = {}, read_only = false): SettingsView {
  const base = settingsFixture({ "onboarding.completed": true });
  return { ...base, read_only, effective: { ...base.effective, ...effective } };
}
function mount(
  options: {
    decisionApi?: FakeDecisionLoopApi;
    shell?: FakeShellApi;
    contextApi?: ContextApi;
    sessions?: SessionRow[];
    vaultApi?: VaultApi;
    capturesApi?: CapturesApi;
    messagingApi?: MessagingApi;
    skillProposals?: SkillProposalsApi;
    decisions?: DecisionsApi;
  } = {},
) {
  const { viki, socket } = fakeViki();
  const shell = options.shell ?? fakeShellApi({ settings: readySettings() });
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: options.sessions ?? [] })}
      durable={fakeDurable()}
      shell={shell}
      profileApi={profiles}
      {...(options.decisionApi ? { decisionApi: options.decisionApi } : {})}
      {...(options.contextApi ? { contextApi: options.contextApi } : {})}
      {...(options.vaultApi ? { vaultApi: options.vaultApi } : {})}
      {...(options.capturesApi ? { capturesApi: options.capturesApi } : {})}
      {...(options.messagingApi ? { messagingApi: options.messagingApi } : {})}
      {...(options.skillProposals ? { skillProposals: options.skillProposals } : {})}
      {...(options.decisions ? { decisions: options.decisions } : {})}
    />,
  );
  act(() => socket.open());
  return { socket, shell, ...view };
}
const click = async (element: Element) => act(async () => void fireEvent.click(element));
const launcher = () => document.querySelector<HTMLButtonElement>("button.dloop-launcher");
const panel = () => screen.queryByRole("dialog", { name: "VIKI" });
const pageHeading = () => screen.queryByRole("heading", { level: 1, name: "Decision" });
const sidebar = () => within(screen.getByRole("navigation", { name: "Navigation and sessions" }));
const examples = () => decisionLoopExamples().launcher;
const patches = (shell: FakeShellApi) =>
  shell.calls.filter((call) => call.method === "patchSettings").map((call) => call.args[2]);
const presences = (api: FakeDecisionLoopApi) =>
  api.requests
    .filter((request) => request.method === "presence")
    .map((request) => request.args[0] as PresenceBody);

/** Opens the panel the way a click does, the launcher taking the focus first. */
async function openPanel() {
  const button = launcher();
  if (!button) throw new Error("no launcher");
  button.focus();
  await click(button);
  await settled();
  return within(screen.getByRole("dialog", { name: "VIKI" }));
}
/** The panel's one card for `what`. */
function card(what: string): HTMLElement {
  const found = screen.getByText(what).closest("article");
  if (!(found instanceof HTMLElement)) throw new Error(`no card for ${what}`);
  return found;
}

test("the launcher shows only with a decisionApi, and stays the same button across views", async () => {
  const without = mount();
  await settled();
  expect(launcher()).toBeNull();
  without.unmount();
  sessionStorage.clear();

  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  const button = launcher();
  expect(button?.getAttribute("aria-label")).toBe("VIKI, a question for you, working on 1 chat");
  expect(button?.getAttribute("aria-expanded")).toBe("false");
  await click(sidebar().getByRole("button", { name: "Decision" }));
  await settled();
  expect(pageHeading()).toBeTruthy();
  expect(launcher()).toBe(button);
});

test("the launcher never mounts in recovery mode, even with a decisionApi", async () => {
  const api = createFakeDecisionLoopApi();
  const shell = fakeShellApi({
    settings: readySettings(),
    capabilities: capabilitiesFixture({ app: { mode: "recovery" } }),
  });
  mount({ decisionApi: api, shell });
  await settled();
  expect(launcher()).toBeNull();
  expect(api.requests).toEqual([]);
});

test("the launcher never mounts once this window's own token is already refused", async () => {
  // Built without the shared mount() helper, which always opens the socket: this scenario needs
  // appSnapshot.status to already read "unauthorized" on App's very first render, the way a
  // relaunch with a dead token would, so the launcher — and the decisionApi it would call — never
  // mounts at all, not even once before settling.
  const api = createFakeDecisionLoopApi();
  const { viki, socket } = fakeViki();
  socket.onclose?.({ code: 4401 }); // socket.ts's own AUTH_REFUSED, before App ever renders
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [] })}
      durable={fakeDurable()}
      shell={fakeShellApi({ settings: readySettings() })}
      profileApi={profiles}
      decisionApi={api}
    />,
  );
  await settled();
  expect(launcher()).toBeNull();
  expect(api.requests).toEqual([]);
});

test("opening the launcher re-checks behind the loading kit, then lists every item and marks them shown", async () => {
  const api = createFakeDecisionLoopApi();
  const openLauncher = api.openLauncher;
  let release = () => {};
  api.openLauncher = () =>
    new Promise((resolve) => {
      release = () => resolve(openLauncher());
    });
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  expect(dialog.getByText("Checking…")).toBeTruthy();
  expect(dialog.queryByText(examples().items[0]?.what ?? "")).toBeNull();
  expect(api.requests.map((request) => request.method)).not.toContain("observe");
  await act(async () => release());
  await settled();
  expect(api.requests.map((request) => request.method)).toContain("openLauncher");
  expect(launcher()?.getAttribute("aria-expanded")).toBe("true");
  const items = examples().items;
  for (const item of items) expect(dialog.getByText(item.what)).toBeTruthy();
  expect(api.requests).toContainEqual({
    method: "observe",
    args: ["shown", items.map((item) => item.item_key)],
  });
});

test("Escape closes the panel and gives the focus back to the launcher", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  expect(panel()).toBeTruthy();
  act(() => {
    fireEvent.keyDown(window, { key: "Escape" });
  });
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(launcher());
  expect(launcher()?.getAttribute("aria-expanded")).toBe("false");
});

test("Not now and a reason answer their items from the launcher, and the items go", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  await openPanel();
  const [item, other] = examples().items;
  if (!item || !other) throw new Error("the example launcher lacks items");
  await click(within(card(item.what)).getByRole("button", { name: "Not now" }));
  await settled();
  expect(api.requests).toContainEqual({
    method: "feedback",
    args: [
      {
        item_key: item.item_key,
        episode_id: null,
        value: "later",
        reason: null,
        source: "launcher",
      },
    ],
  });
  expect(screen.queryByText(item.what)).toBeNull();

  await click(within(card(other.what)).getByRole("button", { name: "More" }));
  await click(screen.getByRole("menuitem", { name: "Out of date" }));
  await settled();
  expect(api.requests).toContainEqual({
    method: "feedback",
    args: [
      {
        item_key: other.item_key,
        episode_id: null,
        value: "wrong",
        reason: "out_of_date",
        source: "launcher",
      },
    ],
  });
  expect(screen.queryByText(other.what)).toBeNull();
});

test("Open Approvals closes the panel, marks the item opened and opens Approvals", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const item = examples().items[1];
  if (item?.candidate !== "approvals") throw new Error("the second example item is not approvals");
  await click(dialog.getByRole("button", { name: "Open Approvals" }));
  await settled();
  expect(panel()).toBeNull();
  expect(screen.getByRole("dialog", { name: "Approvals" })).toBeTruthy();
  expect(api.requests).toContainEqual({ method: "observe", args: ["opened", [item.item_key]] });
});

/** A vault API whose lists answer empty and whose note read waits on `note`. */
function vaultApiWith(note: ReturnType<typeof vi.fn>) {
  return {
    entries: vi.fn().mockResolvedValue({
      entries: [{ ref: "kanban/find-sources.md", title: "Find three sources" }],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    }),
    metadata: vi.fn().mockResolvedValue({
      items: [],
      partial: false,
      partial_reasons: [],
      read_bytes: 0,
      omitted: 0,
    }),
    note,
    backlinks: vi.fn(() => new Promise<never>(() => {})),
    history: vi.fn(() => new Promise<never>(() => {})),
    bookmarks: vi.fn().mockResolvedValue({ bookmarks: [] }),
  } as unknown as VaultApi;
}
/** A shell whose active workspace is an ordinary vault, FAKE_WORKSPACE_ID. */
function vaultShell() {
  const workspace = workspaceFixture();
  return fakeShellApi({
    settings: readySettings(),
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: FAKE_WORKSPACE_ID,
          name: workspace.name,
          kind: "vault",
          policy: "ordinary",
          source: "created",
          layout: "unknown",
          aliased: false,
          root_available: true,
        },
        chat_only: false,
        shared_scratch: false,
        selected_id: FAKE_WORKSPACE_ID,
      },
    }),
    workspaces: {
      ...listingFixture([workspace]),
      active_id: FAKE_WORKSPACE_ID,
      selected_id: FAKE_WORKSPACE_ID,
      selected_by: "registration",
      chat_only: false,
    },
  });
}

test("Open card on a Kanban item opens its note in the vault view", async () => {
  const seed = decisionLoopExamples();
  const needsYou = seed.launcher.items.find((item) => item.candidate === "card_needs_you");
  if (!needsYou) throw new Error("the example launcher lacks the card_needs_you item");
  const open = needsYou.actions.find((action) => action.type === "open_note");
  if (open?.ref !== "kanban/find-sources.md") throw new Error("the item opens no Kanban note");
  const inVault = (item: typeof needsYou) => ({
    ...item,
    evidence: item.evidence.map((e) => ({ ...e, workspace_id: FAKE_WORKSPACE_ID })),
    actions: item.actions.map((a) => ({ ...a, workspace_id: FAKE_WORKSPACE_ID })),
  });
  const api = createFakeDecisionLoopApi({
    launcher: {
      ...seed.launcher,
      items: seed.launcher.items.map((item) => (item === needsYou ? inVault(item) : item)),
    },
  });
  const note = vi.fn(() => new Promise<never>(() => {}));
  const vaultApi = vaultApiWith(note);
  const shell = vaultShell();
  mount({ decisionApi: api, shell, vaultApi });
  await settled();
  await openPanel();
  await click(within(card(needsYou.what)).getByRole("button", { name: "Open card" }));
  await settled();
  expect(panel()).toBeNull();
  await waitFor(() =>
    expect(note).toHaveBeenCalledWith(
      FAKE_WORKSPACE_ID,
      "kanban/find-sources.md",
      expect.anything(),
    ),
  );
});

test("an open_chat action opens that chat in the chat view", async () => {
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await click(sidebar().getByRole("button", { name: "Decision" }));
  await settled();
  await openPanel();
  const chat = examples().items[2]?.actions.find((action) => action.type === "open_chat");
  if (!chat?.session_id) throw new Error("the third example item opens no chat");
  const owner = examples().items[2];
  if (!owner) throw new Error("no third example item");
  await click(within(card(owner.what)).getByRole("button", { name: chat.label }));
  await settled();
  expect(panel()).toBeNull();
  expect(pageHeading()).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).toBe(chat.session_id);
  expect(
    socket.sent.some(
      (frame) => frame.type === "session.open" && frame.session_id === chat.session_id,
    ),
  ).toBe(true);
});

/** The example launcher with its vault_recovery item's Open vault pointed at `workspaceId`. */
function recoveryLauncher(workspaceId: string) {
  const seed = decisionLoopExamples();
  const item = seed.launcher.items.find((found) => found.candidate === "vault_recovery");
  if (!item) throw new Error("the example launcher lacks the vault_recovery item");
  const launcherView = {
    ...seed.launcher,
    items: seed.launcher.items.map((found) =>
      found === item
        ? {
            ...found,
            actions: found.actions.map((a) => ({ ...a, workspace_id: workspaceId })),
          }
        : found,
    ),
  };
  return { item, launcherView };
}

test("Open vault opens the vault view for the active workspace, and nothing for another id", async () => {
  const other = "0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f";
  for (const [workspaceId, opens] of [
    [other, false],
    [FAKE_WORKSPACE_ID, true],
  ] as const) {
    const { item, launcherView } = recoveryLauncher(workspaceId);
    const vaultApi = vaultApiWith(vi.fn(() => new Promise<never>(() => {})));
    mount({
      decisionApi: createFakeDecisionLoopApi({ launcher: launcherView }),
      shell: vaultShell(),
      vaultApi,
    });
    await settled();
    await openPanel();
    await click(within(card(item.what)).getByRole("button", { name: "Open vault" }));
    await settled();
    const browser = () => screen.queryByRole("region", { name: "Vault browser" });
    if (opens) await waitFor(() => expect(browser()).toBeTruthy());
    else {
      expect(browser()).toBeNull();
      expect(vaultApi.entries).not.toHaveBeenCalled();
    }
    cleanup();
    sessionStorage.clear();
  }
});

test("Open Messaging opens the Messaging view when the build has one", async () => {
  const status = {
    configured: false,
    state: "not_configured",
    restart_required: false,
    reason: null,
  };
  const messagingApi: MessagingApi = {
    get: async () => ({ mattermost: status, slack: status, discord: status }),
    enable: vi.fn(),
    disable: vi.fn(),
    disableMattermost: vi.fn(),
    config: pending,
    save: pending,
    remove: pending,
  } as unknown as MessagingApi;
  mount({ decisionApi: createFakeDecisionLoopApi(), messagingApi });
  await settled();
  await openPanel();
  const item = examples().items.find((found) => found.candidate === "bridge_stopped");
  if (!item) throw new Error("the example launcher lacks the bridge_stopped item");
  await click(within(card(item.what)).getByRole("button", { name: "Open Messaging" }));
  await settled();
  expect(panel()).toBeNull();
  expect(screen.getByRole("heading", { name: "Messaging" })).toBeTruthy();
});

test("Open Messaging changes nothing in a build without Messaging", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  const item = examples().items.find((found) => found.candidate === "bridge_stopped");
  if (!item) throw new Error("the example launcher lacks the bridge_stopped item");
  await click(within(card(item.what)).getByRole("button", { name: "Open Messaging" }));
  await settled();
  expect(screen.queryByRole("heading", { name: "Messaging" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
  expect(pageHeading()).toBeNull();
});

test("a code_look item: Open in Code sends open.code for its folder, Take a look starts the mission", async () => {
  const api = createFakeDecisionLoopApi();
  const { socket } = mount({ decisionApi: api });
  await settled();
  await openPanel();
  const item = examples().items.find((found) => found.candidate === "code_look");
  const open = item?.actions.find((action) => action.type === "open_code");
  if (!item || !open?.workspace_id)
    throw new Error("the example launcher lacks the code_look item");
  await click(within(card(item.what)).getByRole("button", { name: "Open in Code" }));
  await settled();
  expect(socket.sent).toContainEqual({
    type: "open.code",
    workspace_id: open.workspace_id,
    path: "",
  });
  // The panel closes on a navigation; reopen it for the second action.
  await openPanel();
  await click(within(card(item.what)).getByRole("button", { name: "Take a look" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "startMission", args: [item.item_key, "prepare"] });
});

test("a long_task_look offer: Take a second look starts the prepare mission", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  await openPanel();
  const item = examples().items.find((found) => found.candidate === "long_task_look");
  if (!item) throw new Error("the example launcher lacks the long_task_look item");
  await click(within(card(item.what)).getByRole("button", { name: "Take a second look" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "startMission", args: [item.item_key, "prepare"] });
});

test("a correction_look finding: Open plan opens its chat", async () => {
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  const item = examples().items.find((found) => found.candidate === "correction_look");
  const open = item?.actions.find((action) => action.type === "open_chat");
  if (!item || !open?.session_id) throw new Error("the example launcher lacks the finding");
  await click(within(card(item.what)).getByRole("button", { name: "Open plan" }));
  await settled();
  expect(sessionStorage.getItem("viki.selected")).toBe(open.session_id);
  expect(
    socket.sent.some(
      (frame) => frame.type === "session.open" && frame.session_id === open.session_id,
    ),
  ).toBe(true);
});

test.each([
  ["stalled_project", "Plan next steps"],
  ["overdue_tasks", "Check these tasks"],
])("a %s offer: %s starts the prepare mission", async (candidate, name) => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  await openPanel();
  const item = examples().items.find(
    (found) =>
      found.candidate === candidate && found.actions.some((a) => a.type === "start_prepare"),
  );
  if (!item) throw new Error(`the example launcher lacks the ${candidate} offer`);
  await click(within(card(item.what)).getByRole("button", { name }));
  await settled();
  expect(api.requests).toContainEqual({ method: "startMission", args: [item.item_key, "prepare"] });
});

test("a stalled_project finding: Open plan opens its chat", async () => {
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  const item = examples().items.find(
    (found) =>
      found.candidate === "stalled_project" && found.actions.some((a) => a.type === "open_chat"),
  );
  const open = item?.actions.find((action) => action.type === "open_chat");
  if (!item || !open?.session_id) throw new Error("the example launcher lacks the finding");
  await click(within(card(item.what)).getByRole("button", { name: "Open plan" }));
  await settled();
  expect(sessionStorage.getItem("viki.selected")).toBe(open.session_id);
  expect(
    socket.sent.some(
      (frame) => frame.type === "session.open" && frame.session_id === open.session_id,
    ),
  ).toBe(true);
});

test("a start action starts VIKI's chat and marks the item acted; a refusal shows a line", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const [prepare, , running, inbox] = examples().items;
  if (!prepare || !running || !inbox) throw new Error("the example launcher lacks items");
  await click(within(card(prepare.what)).getByRole("button", { name: "Draft a plan" }));
  await settled();
  expect(api.requests).toContainEqual({
    method: "startMission",
    args: [prepare.item_key, "prepare"],
  });
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [prepare.item_key]] });
  expect(panel()).toBeTruthy();
  expect(dialog.queryByRole("alert")).toBeNull();

  await click(within(card(running.what)).getByRole("button", { name: "Stop" }));
  await settled();
  expect(api.requests).toContainEqual({
    method: "stopMission",
    args: [running.mission_id],
  });

  // busy (429): two of VIKI's chats already running, or this item already has one.
  api.fail("startMission", "busy");
  await click(within(card(inbox.what)).getByRole("button", { name: "File them in a chat" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "startMission", args: [inbox.item_key, "run"] });
  expect(dialog.getByRole("alert").textContent).toBe(
    "VIKI can't start that chat yet: two are already running, or this item has one. Try again soon.",
  );
  expect(api.requests).not.toContainEqual({ method: "observe", args: ["acted", [inbox.item_key]] });

  // limit (409): today's own start limit, not the two-running cap above — its own line.
  api.fail("startMission", "limit");
  await click(within(card(inbox.what)).getByRole("button", { name: "File them in a chat" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe(
    "VIKI can't start another chat today: today's limit is used.",
  );
  expect(api.requests).not.toContainEqual({ method: "observe", args: ["acted", [inbox.item_key]] });
});

test("Start and Stop on a Bot's seat call startSeat and stopSeat; a refusal shows a line", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const start = examples().items.find((item) => item.candidate === "seat_start");
  const working = examples().items.find((item) => item.candidate === "seat_working");
  const startAction = start?.actions.find((action) => action.type === "start_seat");
  const stopAction = working?.actions.find((action) => action.type === "stop_seat");
  if (!start || !working || !startAction?.routine_id || !stopAction?.routine_id)
    throw new Error("the example launcher lacks the seat items");

  await click(within(card(start.what)).getByRole("button", { name: "Start" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "startSeat", args: [startAction.routine_id] });
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [start.item_key]] });
  expect(dialog.queryByRole("alert")).toBeNull();

  await click(within(card(working.what)).getByRole("button", { name: "Stop" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "stopSeat", args: [stopAction.routine_id] });

  api.fail("startSeat", "busy");
  await click(within(card(start.what)).getByRole("button", { name: "Start" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe(
    "VIKI can't start that Bot yet: it is already running, or too many Bots are. Try again soon.",
  );

  api.fail("startSeat", "unavailable");
  await click(within(card(start.what)).getByRole("button", { name: "Start" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe("VIKI couldn't start that Bot.");

  api.fail("stopSeat", "busy");
  await click(within(card(working.what)).getByRole("button", { name: "Stop" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe("VIKI couldn't stop that Bot.");
});

test("Run checks and Stop on a commit's checks call runChecks and stopChecks; a refusal shows a line", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const checks = examples().items.filter((item) => item.candidate === "commit_checks");
  const waiting = checks.find((item) => item.actions.some((a) => a.type === "run_checks"));
  const running = checks.find((item) => item.actions.some((a) => a.type === "stop_checks"));
  const runAction = waiting?.actions.find((action) => action.type === "run_checks");
  const stopAction = running?.actions.find((action) => action.type === "stop_checks");
  if (!waiting || !running || !runAction?.workspace_id || !stopAction?.workspace_id)
    throw new Error("the example launcher lacks the checks items");

  const reads = () => api.requests.filter((request) => request.method === "launcher").length;

  const readsBeforeRun = reads();
  await click(within(card(waiting.what)).getByRole("button", { name: "Run checks" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "runChecks", args: [runAction.workspace_id] });
  expect(reads()).toBeGreaterThan(readsBeforeRun);
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [waiting.item_key]] });
  expect(dialog.queryByRole("alert")).toBeNull();

  const readsBeforeStop = reads();
  await click(within(card(running.what)).getByRole("button", { name: "Stop" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "stopChecks", args: [stopAction.workspace_id] });
  expect(reads()).toBeGreaterThan(readsBeforeStop);

  api.fail("runChecks", "busy");
  await click(within(card(waiting.what)).getByRole("button", { name: "Run checks" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe(
    "VIKI can't run those checks yet: they are already running, or too many checks are. Try again soon.",
  );

  api.fail("runChecks", "unavailable");
  await click(within(card(waiting.what)).getByRole("button", { name: "Run checks" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe("VIKI couldn't run those checks.");

  api.fail("stopChecks", "busy");
  await click(within(card(running.what)).getByRole("button", { name: "Stop" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe("VIKI couldn't stop those checks.");
});

test("Triage inbox observes acted, closes the panel and opens Inbox triage in a vault shell", async () => {
  const api = createFakeDecisionLoopApi();
  const capturesApi: CapturesApi = {
    list: async () => ({ captures: [], projects: [], partial: false, omitted: 0, run: null }),
    run: async () => null,
    suggest: pending,
  };
  mount({
    decisionApi: api,
    shell: vaultShell(),
    vaultApi: vaultApiWith(vi.fn(pending)),
    capturesApi,
  });
  await settled();
  await openPanel();
  const item = examples().items.find((candidate) => candidate.candidate === "inbox_triage");
  if (!item) throw new Error("the example launcher lacks the inbox_triage item");
  await click(within(card(item.what)).getByRole("button", { name: "Triage inbox" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
  expect(panel()).toBeNull();
  expect(screen.getByRole("dialog", { name: "Inbox triage" })).toBeTruthy();
});

test("Suggest destinations calls suggestInbox, observes acted and refreshes; a refusal shows a line", async () => {
  const api = createFakeDecisionLoopApi();
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const item = examples().items.find((candidate) => candidate.candidate === "inbox_triage");
  if (!item) throw new Error("the example launcher lacks the inbox_triage item");
  const reads = () => api.requests.filter((request) => request.method === "launcher").length;

  const readsBefore = reads();
  await click(within(card(item.what)).getByRole("button", { name: "Suggest destinations" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "suggestInbox", args: [item.item_key] });
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
  expect(reads()).toBeGreaterThan(readsBefore);
  expect(dialog.queryByRole("alert")).toBeNull();

  api.fail("suggestInbox", "busy");
  const requestsBefore = api.requests.length;
  await click(within(card(item.what)).getByRole("button", { name: "Suggest destinations" }));
  await settled();
  expect(dialog.getByRole("alert").textContent).toBe(
    "VIKI can't ask for those suggestions yet: a suggestion run is already going. Try again soon.",
  );
  expect(api.requests.slice(requestsBefore)).not.toContainEqual({
    method: "observe",
    args: ["acted", [item.item_key]],
  });
});

test("the menu writes decision.paused_until an hour out, 08:00 tomorrow, and ends Focus", async () => {
  const api = createFakeDecisionLoopApi();
  const { shell } = mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  const choose = async (label: string) => {
    await click(dialog.getAllByRole("button", { name: "More" })[0] as HTMLElement);
    await click(screen.getByRole("menuitem", { name: label }));
    await settled();
  };
  const reads = () => api.requests.filter((request) => request.method === "launcher").length;

  const before = Date.now();
  const readsBefore = reads();
  await choose("Pause for 1 hour");
  const hour = patches(shell)[0] as Record<string, string>;
  expect(Object.keys(hour)).toEqual(["decision.paused_until"]);
  expect(hour["decision.paused_until"]).toMatch(STAMP);
  const at = Date.parse(hour["decision.paused_until"] ?? "");
  expect(at - before).toBeGreaterThanOrEqual(HOUR_MS);
  expect(at - Date.now()).toBeLessThanOrEqual(HOUR_MS);
  expect(reads()).toBeGreaterThan(readsBefore);

  await choose("Pause until tomorrow");
  const tomorrow = new Date(
    (patches(shell)[1] as Record<string, string>)["decision.paused_until"] ?? "",
  );
  const today = new Date();
  expect([tomorrow.getHours(), tomorrow.getMinutes()]).toEqual([8, 0]);
  expect(tomorrow.getDate()).toBe(
    new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1).getDate(),
  );

  await choose("End focus");
  expect(patches(shell)[2]).toEqual({ "decision.focus_until": "" });
  expect(panel()).toBeTruthy();
});

test("off, the launcher still opens the panel with VIKI's chat and Turn on in Decision", async () => {
  const off: LauncherView = {
    ...examples(),
    state: "off",
    level: "idle",
    count: 0,
    open: 0,
    working: 0,
    focus_until: null,
    items: [],
    started: [],
  };
  mount({ decisionApi: createFakeDecisionLoopApi({ launcher: off }) });
  await settled();
  expect(launcher()?.getAttribute("aria-label")).toBe("VIKI, proactive VIKI is off");
  const dialog = await openPanel();
  expect(dialog.getByText("Proactive VIKI is off")).toBeTruthy();
  expect(dialog.getByRole("textbox", { name: "Message VIKI" })).toBeTruthy();
  await click(dialog.getByRole("button", { name: "Turn on in Decision" }));
  await settled();
  expect(panel()).toBeNull();
  expect(pageHeading()).toBeTruthy();
  expect(screen.getByRole("region", { name: "Proactive VIKI" })).toBeTruthy();
});

test("the weekly check card shows only while review.due", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  const dialog = await openPanel();
  expect(dialog.queryByText("Check")).toBeNull();
  expect(dialog.queryByText("Skip this week")).toBeNull();
});

test("the weekly check card's Check closes the panel and opens Decision at How VIKI is doing, open", async () => {
  const seed = decisionLoopExamples();
  seed.launcher.review = { due: true, count: 3 };
  const api = createFakeDecisionLoopApi({ launcher: seed.launcher });
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  expect(
    dialog.getByText("Weekly check: 3 things VIKI held back last week. Was it right?"),
  ).toBeTruthy();
  await click(dialog.getByRole("button", { name: "Check" }));
  await settled();
  expect(panel()).toBeNull();
  expect(pageHeading()).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "How VIKI is doing" }).getAttribute("aria-expanded"),
  ).toBe("true");
});

test("the weekly check card's Skip this week posts review_seen, keeps the panel open, and the card goes", async () => {
  const seed = decisionLoopExamples();
  seed.launcher.review = { due: true, count: 2 };
  const api = createFakeDecisionLoopApi({ launcher: seed.launcher });
  mount({ decisionApi: api });
  await settled();
  const dialog = await openPanel();
  await click(dialog.getByRole("button", { name: "Skip this week" }));
  await settled();
  expect(api.requests).toContainEqual({ method: "preference", args: [{ name: "review_seen" }] });
  expect(panel()).toBeTruthy();
  expect(screen.queryByText("Skip this week")).toBeNull();
});

test("VIKI's chat: the first send starts a chat, saves it and sends; Open in main window selects it", async () => {
  const { socket, shell } = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  const dialog = await openPanel();
  expect(dialog.getByText("Ask VIKI anything.")).toBeTruthy();
  const box = dialog.getByRole("textbox", { name: "Message VIKI" });
  fireEvent.change(box, { target: { value: "What is due today?" } });
  await act(async () => void fireEvent.keyDown(box, { key: "Enter" }));
  await settled();

  const saved = patches(shell).find((changes) =>
    Object.hasOwn(changes as object, "decision.launcher_session"),
  ) as Record<string, string> | undefined;
  const id = saved?.["decision.launcher_session"] ?? "";
  expect(id).toMatch(UUID);
  expect(
    socket.sent.some((frame) => frame.type === "session.open" && frame.session_id === id),
  ).toBe(true);
  expect(socket.sent).toContainEqual(
    expect.objectContaining({ type: "turn.send", session_id: id, text: "What is due today?" }),
  );
  expect(dialog.queryByText("Ask VIKI anything.")).toBeNull();
  expect(dialog.queryByRole("alert")).toBeNull();

  await click(dialog.getByRole("button", { name: "Open in main window" }));
  await settled();
  expect(panel()).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).toBe(id);
});

test("VIKI's chat reopens the saved chat, and New conversation clears it", async () => {
  const savedId = "7d3b1c52-5c1e-4a43-9d55-2f0c4a1f6e10";
  const shell = fakeShellApi({
    settings: readySettings({ "decision.launcher_session": savedId }),
  });
  const { socket } = mount({ decisionApi: createFakeDecisionLoopApi(), shell });
  await settled();
  expect(socket.sent.some((frame) => frame.session_id === savedId)).toBe(false);
  const dialog = await openPanel();
  expect(
    socket.sent.some((frame) => frame.type === "session.open" && frame.session_id === savedId),
  ).toBe(true);
  expect(dialog.queryByText("Ask VIKI anything.")).toBeNull();
  await click(dialog.getByRole("button", { name: "New conversation" }));
  await settled();
  expect(patches(shell)).toEqual([{ "decision.launcher_session": "" }]);
  expect(dialog.getByText("Ask VIKI anything.")).toBeTruthy();
});

test("See what was sent opens Context & loadout on its Decision tab", async () => {
  const api = createFakeDecisionLoopApi();
  const shell = fakeShellApi({ settings: readySettings({ "decision.enabled": true }) });
  mount({ decisionApi: api, shell, contextApi: context });
  await settled();
  const dialog = await openPanel();
  await click(dialog.getByRole("button", { name: "See what was sent" }));
  await settled();
  expect(panel()).toBeNull();
  expect(screen.getByRole("heading", { level: 1, name: "Context & loadout" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Decision" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("heading", { name: "Decision model context window" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "What was sent" })).toBeTruthy();
  const first = decisionLoopExamples().calls.calls[0]?.call_id;
  expect(api.requests).toContainEqual({ method: "call", args: [first] });
});

test("See why on a panel item opens the Decision tab on that item's own call, not the newest", async () => {
  // The default fixture's first item already names the newest call (d2df3dad...), which could
  // never catch this gap: re-point its own open_context action at the second call instead.
  const seed = decisionLoopExamples();
  const item = seed.launcher.items[0];
  const targetCall = seed.calls.calls[1];
  if (!item || !targetCall) throw new Error("the example fixtures lack what this test needs");
  const openContext = item.actions.find((action) => action.type === "open_context");
  if (!openContext) throw new Error("the first example item has no open_context action");
  const seededLauncher: LauncherView = {
    ...seed.launcher,
    items: [
      {
        ...item,
        actions: item.actions.map((action) =>
          action === openContext ? { ...action, call_id: targetCall.call_id } : action,
        ),
      },
      ...seed.launcher.items.slice(1),
    ],
  };
  const api = createFakeDecisionLoopApi({ launcher: seededLauncher });
  const shell = fakeShellApi({ settings: readySettings({ "decision.enabled": true }) });
  mount({ decisionApi: api, shell, contextApi: context });
  await settled();
  await openPanel();
  await click(within(card(item.what)).getByRole("button", { name: openContext.label }));
  await settled();
  expect(panel()).toBeNull();
  expect(screen.getByRole("heading", { level: 1, name: "Context & loadout" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Decision" }).getAttribute("aria-selected")).toBe("true");
  expect(api.requests).toContainEqual({ method: "call", args: [targetCall.call_id] });
  const picker = (await screen.findByRole("combobox", { name: "Call" })) as HTMLSelectElement;
  expect(picker.value).toBe(targetCall.call_id);
});

test("the Decision page's See the last state sent lands on Context's Decision tab", async () => {
  const shell = fakeShellApi({ settings: readySettings({ "decision.enabled": true }) });
  mount({ decisionApi: createFakeDecisionLoopApi(), shell, contextApi: context });
  await settled();
  await click(sidebar().getByRole("button", { name: "Decision" }));
  await settled();
  const privacy = screen.getByRole("region", { name: "What leaves this computer" });
  await click(within(privacy).getByRole("button", { name: "What leaves this computer" }));
  await click(within(privacy).getByRole("button", { name: "See the last state sent" }));
  await settled();
  expect(screen.getByRole("tab", { name: "Decision" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("heading", { name: "Decision model context window" })).toBeTruthy();
});

test("a chat the launcher lists as started shows Started by VIKI in the session list", async () => {
  const started = examples().started[0] ?? "";
  const other = "4c0f8d36-3c43-4a5e-9a2b-0d1f2e3c4b5a";
  mount({
    decisionApi: createFakeDecisionLoopApi(),
    sessions: [
      sessionRow({ session_id: started, title: "Trip budget plan" }),
      sessionRow({ session_id: other, title: "Groceries" }),
    ],
  });
  await settled();
  const row = (id: string) => {
    const found = document.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row for ${id}`);
    return within(found);
  };
  expect(row(started).getByText("Started by VIKI")).toBeTruthy();
  expect(row(other).queryByText("Started by VIKI")).toBeNull();
});

test("presence: one window id, focus, the chat on screen with its title, the open panel and the page", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  const api = createFakeDecisionLoopApi();
  const chatId = "0b8c3a8e-9d2f-4f6b-8e1a-5c7d9e0f1a2b";
  mount({ decisionApi: api, sessions: [sessionRow({ session_id: chatId, title: "Garden plan" })] });
  await settled();
  const debounce = async () => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
  };
  await debounce();
  const first = presences(api).at(-1);
  expect(first?.window).toMatch(UUID);
  expect(first).toMatchObject({ chat: null, chat_title: null, note: null, launcher: false });
  expect(first?.page).toEqual({
    native_waiting: 0,
    runs: { working: 0, review: 0 },
    git: { state: "not_repository", dirty: null },
    routine: null,
    skill: null,
    suggest: null,
    second_look: null,
    context: null,
    sources: null,
  });

  act(() => {
    fireEvent.blur(window);
  });
  await debounce();
  expect(presences(api).at(-1)?.focused).toBe(false);
  act(() => {
    fireEvent.focus(window);
  });
  await debounce();
  expect(presences(api).at(-1)?.focused).toBe(true);

  const row = document.querySelector<HTMLElement>(`[data-session-id="${chatId}"]`);
  if (!row) throw new Error("no session row");
  await click(row);
  await settled();
  await debounce();
  expect(presences(api).at(-1)).toMatchObject({ chat: chatId, chat_title: "Garden plan" });

  await openPanel();
  await debounce();
  const last = presences(api).at(-1);
  expect(last?.launcher).toBe(true);
  expect(new Set(presences(api).map((body) => body.window))).toEqual(new Set([first?.window]));
});

test("presence never reports an untitled chat's first message as its title", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  const api = createFakeDecisionLoopApi();
  const untitledId = "3c9d2e5a-7b41-4f8c-9a2d-6e1f0b3c7a85";
  const customId = "5f1a8b3c-2d47-4e9a-8c6b-1a4d7e9f0c23";
  const firstMessage = "what time does the bookshop on Main Street close today";
  mount({
    decisionApi: api,
    sessions: [
      // Untitled: the server's row.title is only first_prompt's own clip (catalog.py's
      // fallback), so the user's first message must never reach presence as a "title".
      sessionRow({ session_id: untitledId, title: firstMessage, first_prompt: firstMessage }),
      // A real custom/AI title, unrelated to the first message, is sent as the chat title.
      sessionRow({ session_id: customId, title: "Bookshop hours", first_prompt: firstMessage }),
    ],
  });
  await settled();
  const debounce = async () => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
  };
  await debounce();

  const untitledRow = document.querySelector<HTMLElement>(`[data-session-id="${untitledId}"]`);
  if (!untitledRow) throw new Error("no session row for the untitled chat");
  await click(untitledRow);
  await settled();
  await debounce();
  expect(presences(api).at(-1)).toMatchObject({ chat: untitledId, chat_title: null });

  const customRow = document.querySelector<HTMLElement>(`[data-session-id="${customId}"]`);
  if (!customRow) throw new Error("no session row for the custom-titled chat");
  await click(customRow);
  await settled();
  await debounce();
  expect(presences(api).at(-1)).toMatchObject({ chat: customId, chat_title: "Bookshop hours" });
});

test("presence reports a second look for a corrected vault chat, and Not now on the skill hint keeps it", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  const api = createFakeDecisionLoopApi();
  const chatId = "6a1d5c2e-8b3f-4a7d-9e0c-2f4b6d8a1c35";
  const turnId = "c4e8a2b6-1d3f-4b5a-8c7e-9a0b2d4f6e18";
  const { socket } = mount({
    decisionApi: api,
    shell: vaultShell(),
    // The inline skill hint (and its Not now) renders only with both of these.
    skillProposals: {
      create: vi.fn(),
      detail: vi.fn(),
      act: vi.fn(),
    } as unknown as SkillProposalsApi,
    decisions: { decisions: pending } as unknown as DecisionsApi,
    sessions: [sessionRow({ session_id: chatId, title: "Garden plan" })],
  });
  await settled();
  const debounce = async () => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
  };
  const row = document.querySelector<HTMLElement>(`[data-session-id="${chatId}"]`);
  if (!row) throw new Error("no session row");
  await click(row);
  await settled();

  // Three turns: the middle one opens with a correction; the last edits notes/x.md and succeeds.
  const usage = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
  };
  const done = { status: "success", usage, context_percent: null, duration_ms: 1 };
  const at = "2026-10-09T00:00:00.000Z";
  const items: [string, Record<string, unknown>][] = [
    ["user.message", { text: "write it", command: "", at }],
    ["turn.done", done],
    ["user.message", { text: "No, use the other note", command: "", at }],
    ["turn.done", done],
    ["user.message", { text: "go on", command: "", at }],
    ["turn.started", { turn_id: turnId }],
    [
      "tool.started",
      {
        block_id: "b1",
        tool: "Edit",
        row: { head: "Edit", rest: "notes/x.md", tail: "" },
        parent_id: "",
      },
    ],
    ["tool.done", { block_id: "b1", is_error: false, summary: "ok", preview: "", tail: "" }],
    ["turn.done", done],
  ];
  await act(async () => {
    for (const [index, [type, data]] of items.entries())
      socket.receive({
        v: 2,
        generation: GENERATION,
        request_id: "",
        type,
        session_id: chatId,
        seq: index + 1,
        data,
      } as never);
  });
  await settled();
  await debounce();
  const secondLook = {
    session: chatId,
    turn: turnId,
    trigger: "correction",
    title: "Garden plan",
    refs: ["notes/x.md"],
  };
  expect(presences(api).at(-1)?.page?.second_look).toEqual(secondLook);
  expect(presences(api).at(-1)?.page?.skill).toMatchObject({ trigger: "correction" });

  await click(screen.getByRole("button", { name: "Not now" }));
  await settled();
  await debounce();
  expect(presences(api).at(-1)?.page?.skill).toBeNull();
  expect(presences(api).at(-1)?.page?.second_look).toEqual(secondLook);
});

test("presence reports the context share and a fetched-pages reply, and Not now keeps them", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  const api = createFakeDecisionLoopApi();
  const chatId = "6a1d5c2e-8b3f-4a7d-9e0c-2f4b6d8a1c35";
  const turnId = "c4e8a2b6-1d3f-4b5a-8c7e-9a0b2d4f6e18";
  const { socket } = mount({
    decisionApi: api,
    shell: vaultShell(),
    skillProposals: {
      create: vi.fn(),
      detail: vi.fn(),
      act: vi.fn(),
    } as unknown as SkillProposalsApi,
    decisions: { decisions: pending } as unknown as DecisionsApi,
    sessions: [sessionRow({ session_id: chatId, title: "Garden plan" })],
  });
  await settled();
  const row = document.querySelector<HTMLElement>(`[data-session-id="${chatId}"]`);
  if (!row) throw new Error("no session row");
  await click(row);
  await settled();

  const usage = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
  };
  const done = (context_percent: number | null) => ({
    status: "success",
    usage,
    context_percent,
    duration_ms: 1,
  });
  const at = "2026-10-09T00:00:00.000Z";
  // The correction turn makes the skill hint (and its Not now) appear; the last reply fetches a
  // page, reads no note and ends at 75% of the context.
  const items: [string, Record<string, unknown>][] = [
    ["user.message", { text: "write it", command: "", at }],
    ["turn.done", done(null)],
    ["user.message", { text: "No, use the other note", command: "", at }],
    ["turn.done", done(null)],
    ["user.message", { text: "go on", command: "", at }],
    ["turn.started", { turn_id: turnId }],
    [
      "tool.started",
      {
        block_id: "b1",
        tool: "WebFetch",
        row: { head: "", rest: "example.com", tail: "" },
        parent_id: "",
      },
    ],
    ["tool.done", { block_id: "b1", is_error: false, summary: "ok", preview: "", tail: "" }],
    ["turn.done", done(75)],
  ];
  await act(async () => {
    for (const [index, [type, data]] of items.entries())
      socket.receive({
        v: 2,
        generation: GENERATION,
        request_id: "",
        type,
        session_id: chatId,
        seq: index + 1,
        data,
      } as never);
  });
  await settled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });
  const facts = { context: { session: chatId, percent: 75 } };
  const sources = { session: chatId, turn: turnId, notes: false };
  expect(presences(api).at(-1)?.page).toMatchObject({ ...facts, sources });
  expect(presences(api).at(-1)?.page?.skill).toMatchObject({ trigger: "correction" });

  await click(screen.getByRole("button", { name: "Not now" }));
  await settled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });
  expect(presences(api).at(-1)?.page?.skill).toBeNull();
  expect(presences(api).at(-1)?.page).toMatchObject({ ...facts, sources });
});

test("Draft a handoff appends its text to that chat's message box, marks the item acted and sends nothing", async () => {
  const api = createFakeDecisionLoopApi();
  const item = examples().items.find((found) => found.candidate === "context_handoff");
  const draft = item?.actions.find((action) => action.type === "draft_chat");
  if (!item || !draft?.session_id || !draft.text)
    throw new Error("the example launcher lacks the context_handoff offer");
  const { socket } = mount({
    decisionApi: api,
    sessions: [sessionRow({ session_id: draft.session_id, title: "Garden plan" })],
  });
  await settled();
  await openPanel();
  await click(within(card(item.what)).getByRole("button", { name: "Draft a handoff" }));
  await settled();
  expect(panel()).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).toBe(draft.session_id);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.value).toBe(`${draft.text}\n\n`);
  expect(api.requests).toContainEqual({ method: "observe", args: ["acted", [item.item_key]] });
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("moving the launcher saves layout.launcher, and only while settings are writable", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  const moved = async () => {
    const button = launcher();
    if (!button) throw new Error("no launcher");
    act(() => {
      fireEvent.keyDown(button, { key: "ArrowUp", altKey: true });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
  };
  const writable = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await moved();
  expect(patches(writable.shell)).toEqual([{ "layout.launcher": "20,192" }]);
  writable.unmount();

  sessionStorage.clear();
  const readOnly = fakeShellApi({ settings: readySettings({}, true) });
  mount({ decisionApi: createFakeDecisionLoopApi(), shell: readOnly });
  await settled();
  await moved();
  expect(patches(readOnly)).toEqual([]);
});

// --- the 100px launcher, the panel's saved size, and Alt+V wired into App ---

test("the launcher carries aria-keyshortcuts for Alt+V", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  expect(launcher()?.getAttribute("aria-keyshortcuts")).toBe("Alt+V");
});

test("the panel opens beside the 100px launcher at its default position and size", async () => {
  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 900);
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  const dialog = panel();
  if (!dialog) throw new Error("no panel");
  expect(dialog.style.left).toBe("624px");
  expect(dialog.style.top).toBe("136px");
  expect(dialog.style.width).toBe("380px");
  expect(dialog.style.height).toBe("480px");
});

test("a saved layout.launcher_panel size places and sizes the panel", async () => {
  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 900);
  const shell = fakeShellApi({ settings: readySettings({ "layout.launcher_panel": "500,560" }) });
  mount({ decisionApi: createFakeDecisionLoopApi(), shell });
  await settled();
  await openPanel();
  const dialog = panel();
  if (!dialog) throw new Error("no panel");
  expect(dialog.style.left).toBe("504px");
  expect(dialog.style.top).toBe("56px");
  expect(dialog.style.width).toBe("500px");
  expect(dialog.style.height).toBe("560px");
});

test("the width separator's ArrowLeft saves layout.launcher_panel, only while settings are writable", async () => {
  vi.stubGlobal("innerWidth", 1024);
  vi.stubGlobal("innerHeight", 768);
  const writable = mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  await openPanel();
  act(() => {
    fireEvent.keyDown(screen.getByRole("separator", { name: "Resize panel width" }), {
      key: "ArrowLeft",
    });
  });
  expect(patches(writable.shell)).toEqual([{ "layout.launcher_panel": "390,480" }]);
  writable.unmount();

  sessionStorage.clear();
  const readOnly = fakeShellApi({ settings: readySettings({}, true) });
  mount({ decisionApi: createFakeDecisionLoopApi(), shell: readOnly });
  await settled();
  await openPanel();
  act(() => {
    fireEvent.keyDown(screen.getByRole("separator", { name: "Resize panel width" }), {
      key: "ArrowLeft",
    });
  });
  expect(patches(readOnly)).toEqual([]);
});

test("Alt+V from the Message box opens the panel with focus in Message VIKI, and closes back to it", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  const messageBox = screen.getByRole("textbox", { name: "Message" });
  messageBox.focus();
  expect(document.activeElement).toBe(messageBox);

  act(() => {
    fireEvent.keyDown(messageBox, { altKey: true, code: "KeyV" });
  });
  const dialog = within(screen.getByRole("dialog", { name: "VIKI" }));
  expect(document.activeElement).toBe(dialog.getByRole("textbox", { name: "Message VIKI" }));

  act(() => {
    fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  });
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(messageBox);
});

test("Alt+V does nothing while the Ctrl+K palette is open", async () => {
  mount({ decisionApi: createFakeDecisionLoopApi() });
  await settled();
  act(() => {
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  });
  const search = screen.getByRole("dialog", { name: "Search sessions" });
  act(() => {
    fireEvent.keyDown(window, { altKey: true, code: "KeyV" });
  });
  expect(panel()).toBeNull();
  expect(screen.getByRole("dialog", { name: "Search sessions" })).toBe(search);
});
