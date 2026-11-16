/**
 * Which Bot a selected chat belongs to, when the Bot's thread listing cannot say.
 *
 * The listing stops at a Bot's newest threads and reports `has_more`; the Bot page then sends
 * the user to Search sessions for older chats. A chat opened that way is still that Bot's task
 * thread, so its header, rail and face must be the Bot's, named by the chat's own memory binding.
 * A complete listing stays the only authority: it leaves out the Bot's routine runs, which carry
 * the same binding and must open as plain profile chats.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type { BotRow, BotsApi, BotThreads } from "../../clients/messaging/botsApi";
import { blankProfileDocument, type ProfileApi } from "../../clients/profiles/profileApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const BOT = "00000000-0000-4000-8000-000000000001";
const PROFILE = "00000000-0000-4000-8000-000000000002";
const MAIN = "00000000-0000-4000-8000-000000000003";
const OLDER = "00000000-0000-4000-8000-000000000051";
const REMOVED_BOT = "00000000-0000-4000-8000-000000000009";
const STAMP = "2026-10-14T00:00:00.000Z";

const document = blankProfileDocument();
document.identity.name = "Planner";

const bot: BotRow = {
  bot_id: BOT,
  profile_id: PROFILE,
  workspace_id: FAKE_WORKSPACE_ID,
  chat_session_id: MAIN,
  state_revision: 1,
  created_at: STAMP,
  updated_at: STAMP,
  profile: { revision: 1, archived: false, identity: document.identity },
  workspace: { name: "My notes", policy: "ordinary", active: true },
  chat: { profile_revision: 1, started: true, archived: false },
  routine_ids: [],
  delivery: { enabled: false, available: false, reason: "not_connected" },
  latest_delivery: null,
};

/** The inspect answer for OLDER: a started chat on the Bot's profile, in the Bot's vault. */
const binding: PreparedProfilePreview = {
  launchable: true,
  fields: [],
  blockers: [],
  warnings: [],
  prefs: { mode: "default", effort: "high", model: null },
  session_id: OLDER,
  profile_id: PROFILE,
  profile_revision: 1,
  generation: "00000000-0000-4000-8000-000000000005",
  workspace_id: FAKE_WORKSPACE_ID,
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: OLDER, revision: 0, text: "", saved_at: null, attachments: [] },
  identity: { ...document.identity, profile_revision: 1 },
  started: true,
};

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.useRealTimers();
});

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

/** Mounts the app with one Bot, then opens OLDER the way the Bot page tells the user to: through
 * Search sessions. The listing's own row for it is flagged `bot`, so the sidebar does not offer it. */
async function openOlderChat(threads: BotThreads, inspected: PreparedProfilePreview) {
  const bots = {
    list: vi.fn(async () => ({
      bots: [bot],
      scheduler: { state: "running", attended: true, paused: false },
    })),
    get: vi.fn(async () => ({ bot, chat_status: { state: "reopenable", blockers: [] } })),
    threads: vi.fn(async () => threads),
    create: vi.fn(),
    chat: vi.fn(),
    newChat: vi.fn(),
    setDelivery: vi.fn(),
    remove: vi.fn(),
    reset: vi.fn(),
  } as unknown as BotsApi;
  const profiles = {
    list: vi.fn(async () => []),
    get: vi.fn(),
    create: vi.fn(),
    revise: vi.fn(),
    archive: vi.fn(),
    duplicate: vi.fn(),
    compare: vi.fn(),
  } as unknown as ProfileApi;
  const launch: ProfileLaunchApi = {
    capabilities: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    prepare: vi.fn(),
    inspect: vi.fn(async () => inspected),
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
  const view = render(
    <App
      viki={viki}
      rest={fakeRest({
        sessions: [{ ...sessionRow({ session_id: OLDER, title: "Older task" }), bot: true }],
        hits: [{ session_id: OLDER, title: "Older task", snippet: "", at: "", role: "title" }],
      })}
      durable={fakeDurable()}
      shell={shell}
      botsApi={bots}
      profileApi={profiles}
      profileLaunchApi={launch}
    />,
  );
  act(() => socket.open());
  await settled();
  expect(view.container.querySelector(`.sess[data-session-id="${OLDER}"]`)).toBeNull();

  await act(async () => fireEvent.click(view.container.querySelector(".rail-search") as Element));
  vi.useFakeTimers();
  fireEvent.change(screen.getByPlaceholderText("Search sessions"), { target: { value: "older" } });
  await act(async () => vi.advanceTimersByTimeAsync(150));
  await act(async () =>
    fireEvent.click(view.container.ownerDocument.querySelector(".hit[data-session-id]") as Element),
  );
  vi.useRealTimers();
  await settled();
  expect(sessionStorage.getItem("viki.selected")).toBe(OLDER);
  expect(bots.threads).toHaveBeenCalledWith(BOT, expect.any(AbortSignal));
  return view;
}

const newerOnly: BotThreads = {
  bot_id: BOT,
  threads: [
    {
      session_id: "00000000-0000-4000-8000-000000000050",
      profile_revision: 1,
      created_at: STAMP,
      started: true,
      archived: false,
      title: "Newest task",
    },
  ],
  has_more: true,
};

const memoryOf = (botId: string) => ({ bot_id: botId, ref: `bot-memory/${botId}`, routine: false });

function botChatMeta(): string | null | undefined {
  return screen.queryByRole("navigation", { name: "Bot chat" })?.querySelector(".bot-chat-meta")
    ?.textContent;
}

test("a task thread past the listing's cap opens as its Bot's, named by its memory binding", async () => {
  await openOlderChat(newerOnly, { ...binding, bot_memory: memoryOf(BOT) });
  expect(botChatMeta()).toBe("Task thread · r1");
  expect(screen.getByRole("region", { name: "Bot activity" })).toBeTruthy();
  expect(screen.queryByText("Profile", { selector: ".chip" })).toBeNull();
});

test("a complete listing that leaves the chat out keeps it a profile chat: a routine run carries the same binding", async () => {
  await openOlderChat({ ...newerOnly, has_more: false }, { ...binding, bot_memory: memoryOf(BOT) });
  expect(screen.queryByRole("navigation", { name: "Bot chat" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Bot activity" })).toBeNull();
});

test("past the cap, a routine run of the Bot stays a profile chat", async () => {
  await openOlderChat(newerOnly, { ...binding, bot_memory: { ...memoryOf(BOT), routine: true } });
  expect(screen.queryByRole("navigation", { name: "Bot chat" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Bot activity" })).toBeNull();
});

test("a chat bound to a removed Bot's memory is never handed to the Bot now in its seat", async () => {
  await openOlderChat(newerOnly, { ...binding, bot_memory: memoryOf(REMOVED_BOT) });
  expect(screen.queryByRole("navigation", { name: "Bot chat" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Bot activity" })).toBeNull();
});

test("past the cap, a chat with no memory binding stays a profile chat rather than a guess", async () => {
  await openOlderChat(newerOnly, binding);
  expect(screen.queryByRole("navigation", { name: "Bot chat" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Bot activity" })).toBeNull();
});
