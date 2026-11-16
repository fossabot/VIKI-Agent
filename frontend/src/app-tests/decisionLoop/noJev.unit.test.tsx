/**
 * The guarantee that no Decision surface ever shows the provider's own name ("jev", any case)
 * or "System 1" — renders every Decision surface and scans its rendered text plus the four
 * attributes a user can perceive (aria-label, title, placeholder, alt) for both strings. Most
 * surfaces mount through the real `App` with `createFakeDecisionLoopApi`, the way
 * `launcher.unit.test.tsx` and `page.unit.test.tsx` already do, so nothing App wires in gets
 * missed; the launcher's own eight looks and
 * HelpCenter's topics are rendered directly instead (`Launcher.unit.test.tsx` and
 * `HelpCenter.unit.test.tsx` already do the same for those two components), since neither needs
 * App's wiring to show its text and a direct render covers every state without the timing a live
 * nudge pulse would otherwise need.
 *
 * `assertNoJev` is the one matcher every case below shares; the last test proves it still catches
 * a planted "JEV" and a planted "System 1" label, so a change to the regex (or to how it is
 * applied) cannot silently stop guarding anything.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../../help/helpDrift.unit.test.ts
import { readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { SettingsView } from "../../clients/app/shellApi";
import type { ContextApi } from "../../clients/conversations/contextApi";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "../../clients/decisionLoop/fake";
import {
  CANDIDATES,
  type DecisionCandidate,
  type DecisionLoopApi,
  type ItemView,
  LAUNCHER_LEVELS,
  LAUNCHER_STATES,
  type LauncherView,
} from "../../clients/decisionLoop/types";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import {
  blankProfileDocument,
  type ProfileApi,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import { Launcher } from "../../components/DecisionLoop/Launcher";
import type { UseLauncherPositionResult } from "../../components/DecisionLoop/useLauncherPosition";
import { HelpCenter } from "../../components/Help/HelpCenter";
import { openHelp, resetHelp } from "../../components/Help/helpStore";
import { HELP_TOPICS } from "../../help/helpTopics";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

beforeEach(() => {
  sessionStorage.clear();
  resetHelp();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  resetHelp();
});

const JEV_PATTERN = /jev/i;
const SYSTEM_ONE_PATTERN = /system 1/i;
/** The attributes a user can perceive besides an element's own text (the rule "every text is a
 * text node" is about textContent; these are the other places user-facing words land). */
const WATCHED_ATTRIBUTES = ["aria-label", "title", "placeholder", "alt"] as const;

/** Fails if `root`'s rendered text, or any of `WATCHED_ATTRIBUTES` on `root` or a descendant,
 * names "jev" (any case) or "System 1". `root.textContent` alone already covers every descendant
 * text node, so only the attribute scan needs to walk the tree. */
function assertNoJev(root: HTMLElement): void {
  const elements = [root, ...root.querySelectorAll<HTMLElement>("*")];
  const attributeText = elements.flatMap((element) =>
    WATCHED_ATTRIBUTES.map((attribute) => element.getAttribute(attribute)).filter(
      (value): value is string => value !== null,
    ),
  );
  const text = [root.textContent ?? "", ...attributeText].join("\n");
  expect(text).not.toMatch(JEV_PATTERN);
  expect(text).not.toMatch(SYSTEM_ONE_PATTERN);
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 8; index++) await Promise.resolve();
  });
}

function sidebar() {
  return within(screen.getByRole("navigation", { name: "Navigation and sessions" }));
}

// ---------------------------------------------------------------------------
// The launcher: every state, every level, and the working arc
// ---------------------------------------------------------------------------

const EXAMPLE_LAUNCHER = decisionLoopExamples().launcher;

function launcherView(overrides: Partial<LauncherView>): LauncherView {
  return { ...EXAMPLE_LAUNCHER, working: 0, count: 0, ...overrides };
}

const LAUNCHER_POSITION: UseLauncherPositionResult = {
  right: 20,
  bottom: 72,
  dragging: false,
  bind: {
    onPointerDown: vi.fn(),
    onPointerMove: vi.fn(),
    onPointerUp: vi.fn(),
    onPointerCancel: vi.fn(),
    onKeyDown: vi.fn(),
  },
  endedDrag: vi.fn(() => false),
};

/** off and paused (state alone decides the look), each of the six levels while on, and one extra
 * case for the working arc overlaying idle — driven from the types' own vocabularies rather than
 * a hand-written list, so a future state or level is covered automatically. A bare `level: "nudge"`
 * with no live pulse reads as the badge look (Launcher.tsx's own doc: "held while typing ... then
 * the badge" — there is no separate resting ring), so its text is the same already-covered "N
 * items need you"; this still renders it, it just does not need a timer to prove the point. */
const LAUNCHER_CASES: readonly Partial<LauncherView>[] = [
  ...LAUNCHER_STATES.filter((state) => state !== "on").map(
    (state): Partial<LauncherView> => ({ state }),
  ),
  ...LAUNCHER_LEVELS.map((level): Partial<LauncherView> => ({ state: "on", level, count: 2 })),
  { state: "on", level: "idle", working: 2 },
];

test("the launcher shows no jev or System 1 in any state, level or the working arc", () => {
  for (const overrides of LAUNCHER_CASES) {
    const { container, unmount } = render(
      <Launcher
        view={launcherView(overrides)}
        open={false}
        onToggle={() => {}}
        position={LAUNCHER_POSITION}
        typingUntilMs={0}
        reducedMotion={false}
      />,
    );
    assertNoJev(container);
    unmount();
  }
});

// ---------------------------------------------------------------------------
// App-mounted Decision surfaces: the open panel, VIKI's chat, the Decision page and
// Context & loadout's Decision tab
// ---------------------------------------------------------------------------

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

function readySettings(effective: Record<string, unknown> = {}): SettingsView {
  const base = settingsFixture({ "onboarding.completed": true });
  return { ...base, effective: { ...base.effective, ...effective } };
}

function mountDecision(decisionApi: DecisionLoopApi, effective: Record<string, unknown> = {}) {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({ settings: readySettings(effective) });
  const view = render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      profileApi={profiles}
      decisionApi={decisionApi}
      contextApi={context}
    />,
  );
  act(() => socket.open());
  return { socket, ...view };
}

async function openPanel() {
  const button = document.querySelector<HTMLButtonElement>("button.dloop-launcher");
  if (!button) throw new Error("no launcher");
  await act(async () => void fireEvent.click(button));
  await settled();
  return within(screen.getByRole("dialog", { name: "VIKI" }));
}

/** Opens every item's "⋯" menu (ItemCard.tsx ~205), one at a time, then the panel header's own
 * "⋯" menu (LauncherPanel.tsx ~314) — each renders its contents only while open, and the scan
 * had never opened either, so a label planted inside one (the four feedback reasons, or a header
 * row such as "Pause for 1 hour") was never scanned. Opening a later menu
 * closes whichever was open before it — its first item takes focus, and the other menu's own
 * outside-focus handler (copied from TopBar/HelpMenu.tsx) treats that focus move as a dismiss,
 * same as a real click elsewhere would — so this scans the whole document right as each one
 * opens, rather than trying to hold them all open together. */
async function scanEveryMenu(): Promise<void> {
  for (const toggle of document.querySelectorAll<HTMLButtonElement>(".dloop-item-menu-toggle")) {
    await act(async () => void fireEvent.click(toggle));
    assertNoJev(document.body);
  }
  const headerToggle = document.querySelector<HTMLButtonElement>(".dloop-panel-menu-toggle");
  if (!headerToggle) throw new Error("no header menu toggle");
  await act(async () => void fireEvent.click(headerToggle));
  assertNoJev(document.body);
}

/** `examples.json` seeds no "uncommitted" item (the fixture has one of every other candidate
 * already); this is the one item this file adds so every `CANDIDATES` entry has a card to
 * render. Shaped like its neighbours in examples.json, not from any real backend output. */
const UNCOMMITTED_ITEM: ItemView = {
  item_key: "4a".repeat(32),
  episode_id: "4a4a4a4a-4a4a-4a4a-8a4a-4a4a4a4a4a4a",
  candidate: "uncommitted",
  kind: "inform",
  level: "list",
  what: "The vault has uncommitted changes",
  why: "the vault has uncommitted changes",
  evidence: [{ label: "Vault changes" }],
  actions: [
    {
      type: "open_note",
      label: "Open the vault note",
      primary: true,
      workspace_id: "315d31aa-25ed-45a7-a4b8-5c582ce841fb",
      ref: "00-inbox/vault-changes.md",
    },
  ],
  created_at: "2026-10-21T09:30:00.000Z",
  mission_id: null,
};

/** Vault-change proposal types (`contracts.CHANGES`, Python side): these `IMPROVE` candidates
 * never reach the launcher (`service.py::_visible`; the "approvals" candidate already counts
 * their open rows), so the every-candidate walk below must not seed one, and no example item may
 * name one. */
const CHANGE_CANDIDATES: readonly DecisionCandidate[] = [
  "note_link",
  "note_superseded",
  "tasks_ticked",
];

function itemFor(candidate: DecisionCandidate, items: readonly ItemView[]): ItemView {
  if (candidate === "uncommitted") return UNCOMMITTED_ITEM;
  const found = items.find((item) => item.candidate === candidate);
  if (!found) throw new Error(`no example item for candidate ${candidate}`);
  return found;
}

/** One item per `CANDIDATES` entry that can reach the panel (the examples' own six plus
 * `UNCOMMITTED_ITEM`, skipping `CHANGE_CANDIDATES`), so the open panel renders every candidate
 * type the fake can seed in one pass. */
function everyCandidateLauncher(): LauncherView {
  const base = decisionLoopExamples().launcher;
  const items = CANDIDATES.filter((candidate) => !CHANGE_CANDIDATES.includes(candidate)).map(
    (candidate) => itemFor(candidate, base.items),
  );
  return { ...base, items, open: items.length };
}

test("the open panel lists every candidate's item with its Why now and actions, with no jev or System 1", async () => {
  const noChangeItems = decisionLoopExamples().launcher.items.filter((item) =>
    CHANGE_CANDIDATES.includes(item.candidate),
  );
  expect(noChangeItems).toHaveLength(0);
  const seeded = everyCandidateLauncher();
  mountDecision(createFakeDecisionLoopApi({ launcher: seeded }));
  await settled();
  const dialog = await openPanel();
  for (const item of seeded.items) {
    expect(dialog.getByText(item.what)).toBeTruthy();
    expect(document.body.textContent).toContain(`Why now: ${item.why}`);
  }
  expect(document.querySelectorAll(".dloop-item")).toHaveLength(seeded.items.length);
  const totalActions = seeded.items.reduce((sum, item) => sum + item.actions.length, 0);
  expect(document.querySelectorAll(".dloop-item-action")).toHaveLength(totalActions);
  await scanEveryMenu();
  assertNoJev(document.body);
});

/** Off and paused (the scan had only ever opened the panel against the example view's default
 * "on" state, so "Turn on in Decision" and the paused header menu's "Resume" row were never
 * scanned). `items: []` matches how LauncherPanel.unit.test.tsx's own
 * off/header-menu cases seed these two states, since neither is about the item list. */
test("the open panel shows no jev or System 1 while off, with its header menu open", async () => {
  const seeded: LauncherView = {
    ...decisionLoopExamples().launcher,
    items: [],
    state: "off",
    paused_until: null,
    focus_until: null,
  };
  mountDecision(createFakeDecisionLoopApi({ launcher: seeded }));
  await settled();
  await openPanel();
  expect(screen.getByRole("button", { name: "Turn on in Decision" })).toBeTruthy();
  await scanEveryMenu();
  assertNoJev(document.body);
});

test("the open panel shows no jev or System 1 while paused, with its header menu open", async () => {
  const seeded: LauncherView = {
    ...decisionLoopExamples().launcher,
    items: [],
    state: "paused",
    paused_until: "2026-10-21T15:00:00.000Z",
    focus_until: null,
  };
  mountDecision(createFakeDecisionLoopApi({ launcher: seeded }));
  await settled();
  await openPanel();
  await scanEveryMenu();
  // Confirms this really is the paused branch of the header menu (Resume replaces the two pause
  // rows, LauncherPanel.tsx's own `menuEntries`), not just a state field nothing reads.
  expect(screen.getByRole("menuitem", { name: "Resume" })).toBeTruthy();
  assertNoJev(document.body);
});

test("VIKI's chat in the panel shows no jev or System 1, empty and after a send", async () => {
  const { socket } = mountDecision(createFakeDecisionLoopApi());
  await settled();
  const dialog = await openPanel();
  const chat = document.querySelector<HTMLElement>(".dloop-chat");
  if (!chat) throw new Error("no chat");
  expect(dialog.getByText("Ask VIKI anything.")).toBeTruthy();
  assertNoJev(chat);

  const box = dialog.getByRole("textbox", { name: "Message VIKI" });
  fireEvent.change(box, { target: { value: "What is due today?" } });
  await act(async () => void fireEvent.keyDown(box, { key: "Enter" }));
  await settled();
  expect(socket.sent.some((frame) => frame.type === "session.open")).toBe(true);
  assertNoJev(chat);
});

/** The Decision page's eight rows, in the page's own order, plus Learning between How VIKI is
 * doing and Data. The Proactive VIKI card and the How VIKI works strip have no row toggle. */
const DECISION_ROW_NAMES = [
  "What VIKI watches",
  "Decision model",
  "What leaves this computer",
  "How VIKI may reach you",
  "What VIKI may do",
  "How VIKI is doing",
  "Learning",
  "Data",
];

test("the Decision page shows no jev or System 1 with every row open", async () => {
  mountDecision(createFakeDecisionLoopApi());
  await settled();
  await act(async () => void fireEvent.click(sidebar().getByRole("button", { name: "Decision" })));
  await settled();
  for (const name of DECISION_ROW_NAMES) {
    const row = screen.getByRole("region", { name });
    const toggle = within(row).getByRole("button", { name });
    // Every row defaults closed (DecisionLoopPage.tsx's `defaultOpen`); only clicking a closed
    // row's toggle opens it without re-closing another.
    if (toggle.getAttribute("aria-expanded") === "false") {
      await act(async () => void fireEvent.click(toggle));
    }
  }
  assertNoJev(document.body);
});

test("Context & loadout's Decision tab, opened with a call, shows no jev or System 1", async () => {
  mountDecision(createFakeDecisionLoopApi(), { "decision.enabled": true });
  await settled();
  const dialog = await openPanel();
  await act(
    async () => void fireEvent.click(dialog.getByRole("button", { name: "See what was sent" })),
  );
  await settled();
  expect(screen.getByRole("heading", { level: 1, name: "Context & loadout" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "What was sent" })).toBeTruthy();
  assertNoJev(document.body);
});

// ---------------------------------------------------------------------------
// The Bots summary (messaging/bots.py, BotChatActivity.tsx): a held delivery, Decision's own
// hold reasoning surfacing on a Bot's activity panel
// ---------------------------------------------------------------------------

test("the Bots summary shows a held delivery with no jev or System 1", async () => {
  const BOT = "10000000-0000-4000-8000-000000000001";
  const PROFILE = "10000000-0000-4000-8000-000000000002";
  const SESSION = "10000000-0000-4000-8000-000000000003";
  const STAMP = "2026-10-21T00:00:00.000Z";
  const profileDoc = blankProfileDocument();
  profileDoc.identity.name = "Planner";
  const bot: BotRow = {
    bot_id: BOT,
    profile_id: PROFILE,
    workspace_id: FAKE_WORKSPACE_ID,
    chat_session_id: SESSION,
    state_revision: 1,
    created_at: STAMP,
    updated_at: STAMP,
    profile: { revision: 1, archived: false, identity: profileDoc.identity },
    workspace: { name: "My notes", policy: "ordinary", active: true },
    chat: { profile_revision: 1, started: false, archived: false },
    routine_ids: [],
    delivery: { enabled: false, available: false, reason: "not_connected" },
    latest_delivery: {
      attempt_id: "20000000-0000-4000-8000-000000000001",
      routine_id: "20000000-0000-4000-8000-000000000002",
      session_id: "20000000-0000-4000-8000-000000000003",
      outcome: "succeeded",
      attention: "silent",
      reason: "muted_by_owner",
      state: "held",
      delivery_reason: "held",
      created_at: STAMP,
      updated_at: STAMP,
    },
  };
  const prepared: PreparedProfilePreview = {
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: null },
    session_id: SESSION,
    profile_id: PROFILE,
    profile_revision: 1,
    generation: "20000000-0000-4000-8000-000000000004",
    workspace_id: FAKE_WORKSPACE_ID,
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 0,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: SESSION, revision: 0, text: "", saved_at: null, attachments: [] },
    identity: { ...profileDoc.identity, profile_revision: 1 },
    started: false,
  };
  const botsApi: BotsApi = {
    list: vi.fn().mockResolvedValue({
      bots: [bot],
      scheduler: { state: "running", attended: true, paused: false },
    }),
    get: vi.fn().mockResolvedValue({ bot, chat_status: { state: "prepared", blockers: [] } }),
    create: vi.fn(),
    chat: vi.fn().mockResolvedValue({ bot_id: BOT, state_revision: 1, preview: prepared }),
    newChat: vi.fn(),
    setDelivery: vi.fn(),
    remove: vi.fn(),
    reset: vi.fn(),
  };
  const profileApi: ProfileApi = {
    list: vi.fn().mockResolvedValue([
      {
        profile_id: PROFILE,
        revision: 1,
        current_revision: 1,
        state_revision: 1,
        archived: false,
        created_at: STAMP,
        updated_at: STAMP,
        identity: profileDoc.identity,
        runtime: profileDoc.runtime,
        provenance: profileDoc.provenance,
      } satisfies ProfileSummary,
    ]),
    get: vi.fn(),
    create: vi.fn(),
    revise: vi.fn(),
    archive: vi.fn(),
    duplicate: vi.fn(),
    compare: vi.fn(),
  };
  const profileLaunchApi: ProfileLaunchApi = {
    capabilities: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    prepare: vi.fn(),
    inspect: vi
      .fn()
      .mockImplementation((id: string) =>
        id === SESSION
          ? Promise.resolve(prepared)
          : Promise.reject(new ProfileLaunchApiError(404, "not_found")),
      ),
  };
  const workspace = workspaceFixture();
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
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
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      botsApi={botsApi}
      profileApi={profileApi}
      profileLaunchApi={profileLaunchApi}
    />,
  );
  act(() => socket.open());
  await settled();
  await act(async () => void fireEvent.click(sidebar().getByRole("button", { name: "Bots" })));
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Planner" })));
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Open conversation" })),
  );
  await settled();
  expect(screen.getByRole("region", { name: "Latest routine result" })).toBeTruthy();
  expect(screen.getByText("Mattermost delivery: Held")).toBeTruthy();
  assertNoJev(document.body);
});

// ---------------------------------------------------------------------------
// HelpCenter (every topic) and the raw markdown behind it
// ---------------------------------------------------------------------------

test("HelpCenter shows no jev or System 1 on every topic", () => {
  expect(HELP_TOPICS.length).toBeGreaterThan(0);
  for (const topic of HELP_TOPICS) {
    act(() => openHelp({ kind: "topic", id: topic.id }));
    const { container, unmount } = render(
      <HelpCenter onOpenUrl={() => {}} onOpenDiagnostics={() => {}} onAsk={() => true} />,
    );
    assertNoJev(container);
    unmount();
  }
});

// `here` must be a variable: `new URL("...", import.meta.url)` written inline is Vite's own
// asset-URL pattern, rewritten at build time into a dev-server URL rather than a real path
// (src/testing/css.ts's own doc; helpDrift.unit.test.ts's `here` does the same).
const here = import.meta.url;
const HELP_TOPICS_DIR = fileURLToPath(new URL("../../help/topics/", here)) as string;

/** Every `.md` file in `help/topics/`, read raw — the same files `helpTopics.ts` loads through
 * Vite's `?raw` import, read here through Node's fs instead (as `helpDrift.unit.test.ts` already
 * does) so this scan covers a future topic file too, without a new import line for each one. */
function helpTopicMarkdownFiles(): string[] {
  const entries = readdirSync(HELP_TOPICS_DIR) as string[];
  return entries.filter((file) => file.endsWith(".md"));
}

test("every help topic's raw markdown file names neither jev nor System 1", () => {
  const files = helpTopicMarkdownFiles();
  expect(files.length).toBeGreaterThan(0);
  const offenders = files.filter((file) => {
    const content = readFileSync(HELP_TOPICS_DIR + file, "utf-8") as string;
    return JEV_PATTERN.test(content) || SYSTEM_ONE_PATTERN.test(content);
  });
  expect(offenders).toEqual([]);
});

// ---------------------------------------------------------------------------
// Negative control: the matcher itself must still catch a planted label
// ---------------------------------------------------------------------------

test("the matcher catches a planted JEV label and a planted System 1 label", () => {
  const jevCase = render(
    <button type="button" aria-label="Ask JEV for help">
      Go
    </button>,
  );
  expect(() => assertNoJev(jevCase.container)).toThrow();
  jevCase.unmount();

  const systemOneCase = render(<p>Decisions come from System 1 instincts.</p>);
  expect(() => assertNoJev(systemOneCase.container)).toThrow();
  systemOneCase.unmount();
});
