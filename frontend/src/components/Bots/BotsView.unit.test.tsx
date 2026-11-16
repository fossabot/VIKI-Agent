import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, type Mock, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { RoutinesApi } from "../../clients/automation/routinesApi";
import {
  type BotDetail,
  type BotRow,
  type BotsApi,
  BotsApiError,
} from "../../clients/messaging/botsApi";
import {
  type ProfileApi,
  ProfileApiError,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { t } from "../../i18n/t";
import { setUnsavedWork } from "../../state/unsavedWork";
import type { SessionView } from "../../state/view";
import type { BotRoutinesProps } from "./BotRoutines";
import { BotsView, type BotsViewProps } from "./BotsView";
import type { TeamLive } from "./botStatus";
import { TEAM, type TeamSeat } from "./team";
import { READINESS_TEXT, type TeamReadiness } from "./teamReadiness";

afterEach(() => {
  cleanup();
});
const botProfile = {
  revision: 2,
  archived: false,
  identity: {
    name: "Planner",
    purpose: "Plan tasks",
    avatar_asset_id: null,
    accent: "blue",
    shape: "circle",
  },
} as const;
const bot: BotRow = {
  bot_id: "b1",
  profile_id: "p1",
  workspace_id: "w1",
  chat_session_id: "s1",
  state_revision: 1,
  created_at: "2026-10-14T00:00:00.000Z",
  updated_at: "2026-10-14T00:00:00.000Z",
  profile: {
    revision: 2,
    archived: false,
    identity: {
      name: "Planner",
      purpose: "Plan tasks",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
    },
  },
  workspace: { name: "Main vault", policy: "ordinary", active: true },
  chat: { profile_revision: 1, started: true, archived: false },
  routine_ids: [],
  delivery: { enabled: false, available: true, reason: "off" },
  latest_delivery: null,
};
const prepared = {
  launchable: true,
  fields: [],
  blockers: [],
  warnings: [],
  prefs: { mode: "default", effort: "high", model: null },
  session_id: "s1",
  profile_id: "p1",
  profile_revision: 1,
  generation: "g1",
  workspace_id: "w1",
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: "s1", revision: 0, text: "", saved_at: null, attachments: [] },
} satisfies PreparedProfilePreview;
const profile = {
  profile_id: "p1",
  revision: 2,
  current_revision: 2,
  archived: false,
  identity: bot.profile?.identity,
} as ProfileSummary;
const workspaces = [
  { workspace_id: "w1", name: "Main vault", kind: "vault", persistent: true },
] as WorkspaceJson[];
function detail(value = bot): BotDetail {
  return { bot: value, chat_status: { state: "reopenable", blockers: [] } };
}
function setup(over: Partial<BotsViewProps> = {}) {
  const api = {
    list: vi.fn().mockResolvedValue({
      bots: [bot],
      scheduler: { state: "running", attended: true, paused: false },
    }),
    get: vi.fn().mockImplementation((id) =>
      Promise.resolve(
        detail(
          id === "b1"
            ? bot
            : {
                ...bot,
                bot_id: id,
                profile_id: "p2",
                chat_session_id: "s2",
                profile: {
                  ...botProfile,
                  identity: { ...botProfile.identity, name: "Researcher" },
                },
              },
        ),
      ),
    ),
    create: vi
      .fn()
      .mockImplementation((id) =>
        Promise.resolve({ bot: { ...bot, bot_id: id }, replayed: false }),
      ),
    chat: vi.fn().mockResolvedValue({ bot_id: "b1", state_revision: 1, preview: prepared }),
    newChat: vi.fn().mockResolvedValue({
      bot: {
        ...bot,
        state_revision: 2,
        chat_session_id: "s-new",
        chat: { profile_revision: null, started: false, archived: false },
      },
    }),
    setDelivery: vi.fn().mockResolvedValue({
      bot: {
        ...bot,
        state_revision: 2,
        delivery: { enabled: true, available: true, reason: "ready" },
      },
    }),
    remove: vi.fn().mockResolvedValue({ removed: true, bot_id: "b1", paused_routine_ids: [] }),
    reset: vi
      .fn()
      .mockResolvedValue({ workspace_id: "w1", removed_bot_ids: ["b1"], paused_routine_ids: [] }),
  } satisfies BotsApi;
  const all: BotsViewProps = {
    api,
    profiles: { list: vi.fn().mockResolvedValue([profile]) } as unknown as ProfileApi,
    workspaces,
    activeWorkspaceId: "w1",
    selectedId: null,
    onSelect: vi.fn(),
    onBack: vi.fn(),
    onCreateProfile: vi.fn(),
    onOpenChat: vi.fn(),
    onOpenSession: vi.fn(),
    confirm: vi.fn().mockReturnValue(true),
    ...over,
  };
  function Controlled() {
    const [selectedId, select] = useState<string | null>(all.selectedId);
    return (
      <BotsView
        {...all}
        selectedId={selectedId}
        onSelect={(id) => {
          all.onSelect(id);
          select(id);
        }}
      />
    );
  }
  const view = render(<Controlled />);
  return {
    api: all.api as typeof api,
    all,
    view,
    rerender(next: Partial<BotsViewProps>) {
      Object.assign(all, next);
      view.rerender(<Controlled />);
    },
  };
}
function routineProps(): Omit<BotRoutinesProps, "bot"> {
  return {
    api: { preview: vi.fn(), create: vi.fn() } as unknown as RoutinesApi,
    snapshot: { status: "ready", items: [], scheduler: null, error: null },
    sessions: {},
    nowMs: 0,
    workspaces,
    controls: {
      paused: false,
      notificationsEnabled: false,
      quietStart: null,
      quietEnd: null,
      permission: "granted",
    },
    onSettings: vi.fn(),
    onRequestPermission: vi.fn(),
    onRefresh: vi.fn(),
    onOpenSession: vi.fn(),
    onStop: vi.fn(),
    timers: { set: vi.fn().mockReturnValue(1), clear: vi.fn() },
  };
}
function changedRoutines(): Omit<BotRoutinesProps, "bot"> {
  const props = routineProps();
  props.snapshot.items = [
    {
      routine_id: "trigger",
      current_revision: 1,
      state_revision: 1,
      active_run: null,
      last_attempt: null,
    },
  ] as never[];
  return props;
}
async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
  });
}

async function confirmAfter(opener: string, confirmLabel: string) {
  await click(opener);
  await click2(screen.getByRole("alertdialog"), confirmLabel);
}

async function click2(scope: HTMLElement, name: string) {
  await act(async () => {
    fireEvent.click(within(scope).getByRole("button", { name }));
  });
}

describe("BotsView", () => {
  it.each(["missing", "wrong Bot", "network"] as const)(
    "keeps routine admission closed after a %s refresh until accepted detail recovers",
    async (failure) => {
      const routines = routineProps();
      vi.mocked(routines.api.create).mockResolvedValue({
        routine_id: "r-new",
        revision: 1,
        state: "draft",
        state_revision: 1,
      });
      const { api } = setup({ selectedId: "b1", routines });
      const verified = detail({ ...bot, state_revision: 2 });
      api.get.mockResolvedValue(verified);
      await click("Refresh Bots");
      await click("New routine");
      const name = screen.getByLabelText("Name", { exact: true });
      fireEvent.change(name, { target: { value: "Unsaved routine draft" } });
      fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Review tasks" } });
      if (failure === "wrong Bot")
        api.get.mockResolvedValueOnce(detail({ ...bot, bot_id: "b-other", state_revision: 3 }));
      else
        api.get.mockRejectedValueOnce(
          failure === "missing" ? new BotsApiError(404, "not_found") : new Error("offline"),
        );
      await click("Refresh Bots");
      expect(screen.getByLabelText("Name", { exact: true })).toBe(name);
      expect(name).toHaveProperty("value", "Unsaved routine draft");
      expect(name.matches(":disabled")).toBe(false);
      await act(async () => fireEvent.submit(name.closest("form") as HTMLFormElement));
      expect(routines.api.create).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);

      let resolve!: (value: BotDetail) => void;
      api.get.mockImplementationOnce(
        () =>
          new Promise<BotDetail>((ready) => {
            resolve = ready;
          }),
      );
      await click("Refresh Bots");
      fireEvent.change(name, { target: { value: "Copyable updated draft" } });
      expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
      await act(async () => fireEvent.submit(name.closest("form") as HTMLFormElement));
      expect(routines.api.create).not.toHaveBeenCalled();
      await act(async () => resolve(detail()));
      expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
      await act(async () => fireEvent.submit(name.closest("form") as HTMLFormElement));
      expect(routines.api.create).not.toHaveBeenCalled();

      await click("Refresh Bots");
      expect(screen.getByLabelText("Name", { exact: true })).toBe(name);
      expect(name).toHaveProperty("value", "Copyable updated draft");
      expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", false);
      await act(async () => fireEvent.submit(name.closest("form") as HTMLFormElement));
      expect(routines.api.create).toHaveBeenCalledExactlyOnceWith(
        expect.any(String),
        expect.objectContaining({
          name: "Copyable updated draft",
          profile_id: "p1",
          workspace_id: "w1",
        }),
      );
    },
  );

  it.each(["archived", "transcript_missing", "blocked", "unavailable"] as const)(
    "routine admission accepts verified detail when its conversation is %s",
    async (state) => {
      const routines = routineProps();
      const { api } = setup({ selectedId: "b1", routines });
      api.get.mockResolvedValue({ bot, chat_status: { state, blockers: [state] } });
      await click("Refresh Bots");
      await click("New routine");
      fireEvent.change(screen.getByLabelText("Name", { exact: true }), {
        target: { value: "Routine draft" },
      });
      fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Review tasks" } });
      await click("Save");
      expect(routines.api.create).toHaveBeenCalledTimes(1);
    },
  );

  it("asks to open the Bot's vault, without reading, after a workspace switch", async () => {
    const memoryApi = {
      entries: vi.fn(),
      note: vi.fn(),
      metadata: vi.fn(),
    };
    setup({
      selectedId: "b1",
      activeWorkspaceId: "w2",
      workspaces: [
        ...workspaces,
        { workspace_id: "w2", name: "Other vault", kind: "vault", persistent: true },
      ] as WorkspaceJson[],
      memoryApi: memoryApi as never,
      launch: { prepare: vi.fn() } as unknown as ProfileLaunchApi,
      onOpenThread: vi.fn(),
    });
    await act(async () => {});
    expect(screen.getByText(t("bots.memory.openVault"))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("bots.memory.review") })).toBeNull();
    expect(memoryApi.entries).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Kanban" })).toBeNull();
  });

  it("says memory cannot be shown, not to open the vault, when the Bot's own vault is unavailable", async () => {
    const memoryApi = { entries: vi.fn(), note: vi.fn(), metadata: vi.fn() };
    setup({
      selectedId: "b1",
      workspaces: [
        {
          workspace_id: "w1",
          name: "Main vault",
          kind: "vault",
          persistent: true,
          root_available: false,
          policy: "ordinary",
        },
      ] as WorkspaceJson[],
      memoryApi: memoryApi as never,
      launch: { prepare: vi.fn() } as unknown as ProfileLaunchApi,
      onOpenThread: vi.fn(),
    });
    await act(async () => {});
    expect(screen.getByText(t("bots.memory.unavailable"))).toBeTruthy();
    expect(screen.queryByText(t("bots.memory.openVault"))).toBeNull();
    expect(memoryApi.entries).not.toHaveBeenCalled();
  });

  it("reads app-kept memory for a Bot whose policy equals the active protected vault's", async () => {
    const memoryApi = {
      entries: vi.fn().mockResolvedValue({ entries: [], partial: false, partial_reasons: [] }),
      note: vi.fn(),
      metadata: vi.fn(),
    };
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const botId = "22222222-2222-4222-8222-222222222222";
    const protectedBot = {
      ...bot,
      bot_id: botId,
      workspace_id: workspaceId,
      workspace: { name: "Main vault", policy: "protected", active: true },
    } as BotRow;
    const { api } = setup({
      selectedId: botId,
      activeWorkspaceId: workspaceId,
      workspaces: [
        {
          workspace_id: workspaceId,
          name: "Main vault",
          kind: "vault",
          persistent: true,
          root_available: true,
          policy: "protected",
        },
      ] as WorkspaceJson[],
      memoryApi: memoryApi as never,
      launch: { prepare: vi.fn() } as unknown as ProfileLaunchApi,
      onOpenThread: vi.fn(),
    });
    api.list.mockResolvedValue({
      bots: [protectedBot],
      scheduler: { state: "running", attended: true, paused: false },
    });
    api.get.mockResolvedValue(detail(protectedBot));
    await click("Refresh Bots");
    await act(async () => {});
    expect(screen.getByText(t("bots.memory.keptApp"))).toBeTruthy();
    expect(memoryApi.entries).toHaveBeenCalled();
  });

  function memorySetup(over: Partial<BotsViewProps> = {}) {
    return setup({
      selectedId: "b1",
      workspaces: [
        {
          workspace_id: "w1",
          name: "Main vault",
          kind: "vault",
          persistent: true,
          root_available: true,
          policy: "ordinary",
        },
      ] as WorkspaceJson[],
      memoryApi: { entries: vi.fn(), note: vi.fn(), metadata: vi.fn() } as never,
      launch: { prepare: vi.fn() } as unknown as ProfileLaunchApi,
      onOpenThread: vi.fn(),
      onAskInMainChat: vi.fn().mockResolvedValue(undefined),
      routines: routineProps(),
      ...over,
    });
  }

  it("passes Open in VS Code to the memory dialog on the Bot page", async () => {
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const botId = "22222222-2222-4222-8222-222222222222";
    const ref = `bot-memory/${botId}/MEMORY.md`;
    const uuidBot = { ...bot, bot_id: botId, workspace_id: workspaceId };
    const onOpenInEditor = vi.fn();
    const memoryApi = {
      entries: vi.fn().mockResolvedValue({
        entries: [{ ref, title: "MEMORY.md", modified: 1 }],
        partial: false,
        partial_reasons: [],
        omitted: 0,
      }),
      note: vi.fn().mockResolvedValue({ text: "x" }),
    };
    const { api } = memorySetup({
      selectedId: botId,
      activeWorkspaceId: workspaceId,
      workspaces: [
        {
          workspace_id: workspaceId,
          name: "Main vault",
          kind: "vault",
          persistent: true,
          root_available: true,
          policy: "ordinary",
        },
      ] as WorkspaceJson[],
      memoryApi: memoryApi as never,
      onOpenInEditor,
    });
    api.list.mockResolvedValue({
      bots: [uuidBot],
      scheduler: { state: "running", attended: true, paused: false },
    });
    api.get.mockResolvedValue(detail(uuidBot));
    await click("Refresh Bots");
    await act(async () => {});
    await click(t("bots.memory.view"));
    await click("Open in VS Code");
    expect(onOpenInEditor).toHaveBeenCalledExactlyOnceWith(ref);
  });

  it("keeps a memory edit and reports no draft when the leave guard is declined, then places it on retry", async () => {
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const botId = "22222222-2222-4222-8222-222222222222";
    const ref = `bot-memory/${botId}/MEMORY.md`;
    const uuidBot = { ...bot, bot_id: botId, workspace_id: workspaceId };
    const memoryApi = {
      entries: vi.fn().mockResolvedValue({
        entries: [{ ref, title: "MEMORY.md", modified: 1 }],
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
      note: vi.fn().mockResolvedValue({
        note: {
          ref,
          title: "MEMORY.md",
          text: "old line",
          source: "old line",
          byte_size: 8,
          sha256: "a".repeat(64),
          frontmatter: {},
          frontmatter_error: null,
        },
        resolved_links: [],
      }),
    };
    const launch = {
      prepare: vi.fn(async (profileId, _command, sessionId, revision, workspace) => ({
        ...prepared,
        session_id: sessionId,
        profile_id: profileId,
        profile_revision: revision,
        workspace_id: workspace,
        bot_memory: { bot_id: botId, ref: `bot-memory/${botId}`, routine: false },
      })),
    } as unknown as ProfileLaunchApi;
    const onOpenThread = vi.fn(async (_preview: unknown, _bot: unknown, _request?: string) => true);
    const { api, all } = memorySetup({
      selectedId: botId,
      activeWorkspaceId: workspaceId,
      workspaces: [
        {
          workspace_id: workspaceId,
          name: "Main vault",
          kind: "vault",
          persistent: true,
          root_available: true,
          policy: "ordinary",
        },
      ] as WorkspaceJson[],
      memoryApi: memoryApi as never,
      launch,
      onOpenThread,
    });
    api.list.mockResolvedValue({
      bots: [uuidBot],
      scheduler: { state: "running", attended: true, paused: false },
    });
    api.get.mockResolvedValue(detail(uuidBot));
    await click("Refresh Bots");
    await act(async () => {});
    await click("New routine");
    fireEvent.change(screen.getByLabelText("Name", { exact: true }), {
      target: { value: "Unsaved routine draft" },
    });
    await click(t("bots.memory.view"));
    await act(async () => {});
    const dialog = screen.getByRole("dialog", { name: "Bot memory" });
    await act(async () => fireEvent.click(within(dialog).getByRole("button", { name: "Edit" })));
    fireEvent.change(screen.getByRole("textbox", { name: "New text of this file" }), {
      target: { value: "new line" },
    });
    (all.confirm as Mock).mockReturnValueOnce(false);

    await act(async () => fireEvent.click(within(dialog).getByRole("button", { name: "Save" })));
    await act(async () => {});
    expect(all.confirm).toHaveBeenCalledWith("Discard the unsaved routine draft?");
    expect(onOpenThread).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/Nothing was changed/)).toBeTruthy();
    expect(within(dialog).queryByText(/unsent draft for/)).toBeNull();
    expect(
      (
        within(dialog).getByRole("textbox", {
          name: "New text of this file",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("new line");

    await act(async () => fireEvent.click(within(dialog).getByRole("button", { name: "Save" })));
    await act(async () => {});
    expect(onOpenThread).toHaveBeenCalledTimes(1);
    expect(onOpenThread.mock.calls[0]?.[2]).toContain("new line");
    expect(within(dialog).getByText(/unsent draft for/)).toBeTruthy();
  });

  it("asks the unsaved-work guard before Ask in main chat leaves the page", async () => {
    const { all } = memorySetup();
    await act(async () => {});
    await click("New routine");
    const name = screen.getByLabelText("Name", { exact: true });
    fireEvent.change(name, { target: { value: "Unsaved routine draft" } });
    (all.confirm as Mock).mockReturnValueOnce(false);

    await click("Ask Planner to change…");

    expect(all.confirm).toHaveBeenCalledWith("Discard the unsaved routine draft?");
    expect(all.onAskInMainChat).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name", { exact: true })).toBe(name);
    expect(name).toHaveProperty("value", "Unsaved routine draft");

    await click("Ask Planner to change…");
    expect(all.onAskInMainChat).toHaveBeenCalledTimes(1);
  });

  it("keeps a remounted memory card disabled while its earlier request is pending", async () => {
    let finish!: () => void;
    const onAskInMainChat = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    const other: BotRow = {
      ...bot,
      bot_id: "b2",
      profile_id: "p2",
      chat_session_id: "s2",
      profile: { ...botProfile, identity: { ...botProfile.identity, name: "Researcher" } },
    };
    const { api } = memorySetup({ onAskInMainChat });
    api.list.mockResolvedValue({
      bots: [bot, other],
      scheduler: { state: "running", attended: true, paused: false },
    });
    await click("Refresh Bots");
    const ask = (botName: string) =>
      screen.getByRole("button", { name: `Ask ${botName} to change…` });
    await click("Ask Planner to change…");
    expect(onAskInMainChat).toHaveBeenCalledTimes(1);

    await click("Researcher");
    await click("Planner");

    expect(ask("Planner")).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Ask to review" })).toHaveProperty("disabled", true);
    await click("Ask Planner to change…");
    expect(onAskInMainChat).toHaveBeenCalledTimes(1);

    await act(async () => finish());
    expect(ask("Planner")).toHaveProperty("disabled", false);
  });

  it("waits for current-scope Bot detail before reading memory after a workspace roundtrip", async () => {
    const memoryApi = {
      entries: vi.fn().mockResolvedValue({ entries: [], partial: false, omitted: 0 }),
      note: vi.fn(),
      metadata: vi.fn(),
    };
    const ordinaryVaults = [
      {
        workspace_id: "11111111-1111-4111-8111-111111111111",
        name: "Main vault",
        kind: "vault",
        persistent: true,
        root_available: true,
        policy: "ordinary",
      },
      {
        workspace_id: "33333333-3333-4333-8333-333333333333",
        name: "Other vault",
        kind: "vault",
        persistent: true,
        root_available: true,
        policy: "ordinary",
      },
    ] as WorkspaceJson[];
    const memoryBot: BotRow = {
      ...bot,
      bot_id: "22222222-2222-4222-8222-222222222222",
      workspace_id: "11111111-1111-4111-8111-111111111111",
      workspace: { name: "Main vault", policy: "ordinary", active: true },
    };
    let resolveReturned!: (answer: BotDetail) => void;
    const apiStub = {
      list: vi.fn().mockResolvedValue({
        bots: [memoryBot],
        scheduler: { state: "running", attended: true, paused: false },
      }),
      get: vi
        .fn()
        .mockResolvedValueOnce(detail(memoryBot))
        .mockImplementationOnce(() => Promise.resolve(detail(memoryBot)))
        .mockImplementationOnce(() => new Promise((resolve) => (resolveReturned = resolve))),
    } as unknown as BotsApi;
    const { api, rerender } = setup({
      selectedId: memoryBot.bot_id,
      memoryApi: memoryApi as never,
      workspaces: ordinaryVaults,
      activeWorkspaceId: "11111111-1111-4111-8111-111111111111",
      api: apiStub,
      launch: { prepare: vi.fn() } as unknown as ProfileLaunchApi,
      onOpenThread: vi.fn(),
    });
    await act(async () => {});
    expect(screen.getByRole("heading", { name: t("bots.memory") })).toBeTruthy();
    expect(memoryApi.entries).toHaveBeenCalledTimes(1);

    rerender({ activeWorkspaceId: "33333333-3333-4333-8333-333333333333" });
    await act(async () => {});
    api.get.mockImplementationOnce(() => new Promise((resolve) => (resolveReturned = resolve)));
    rerender({ activeWorkspaceId: "11111111-1111-4111-8111-111111111111" });
    await act(async () => {});
    expect(memoryApi.entries).toHaveBeenCalledTimes(1);
    await act(async () => resolveReturned(detail(memoryBot)));
    expect(memoryApi.entries).toHaveBeenCalledTimes(2);
  });

  it("allows a pinned chat after a role-only current-profile revision changes", async () => {
    const { api, all, rerender } = setup({ selectedId: "b1", routines: routineProps() });
    await act(async () => {});
    let resolve!: (value: {
      bot_id: string;
      state_revision: number;
      preview: PreparedProfilePreview;
    }) => void;
    api.chat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await click("Open conversation");
    api.get.mockResolvedValue(
      detail({
        ...bot,
        profile: {
          ...botProfile,
          revision: 3,
          identity: { ...botProfile.identity, purpose: "Updated role" },
        },
      }),
    );
    rerender({ routines: changedRoutines() });
    await act(async () => {});
    await act(async () => resolve({ bot_id: "b1", state_revision: 1, preview: prepared }));
    expect(all.onOpenChat).toHaveBeenCalledWith(prepared, bot);
  });
  it.each(["archived", "transcript_missing", "blocked", "unavailable"] as const)(
    "rejects a delayed Open when current same-revision admission becomes %s",
    async (state) => {
      const { api, all, rerender } = setup({ selectedId: "b1", routines: routineProps() });
      await act(async () => {});
      let resolve!: (value: {
        bot_id: string;
        state_revision: number;
        preview: PreparedProfilePreview;
      }) => void;
      api.chat.mockImplementation(
        () =>
          new Promise((ready) => {
            resolve = ready;
          }),
      );
      await click("Open conversation");
      api.get.mockResolvedValue({ bot, chat_status: { state, blockers: [state] } });
      rerender({ routines: changedRoutines() });
      await act(async () => {});
      await act(async () => resolve({ bot_id: "b1", state_revision: 1, preview: prepared }));
      expect(all.onOpenChat).not.toHaveBeenCalled();
    },
  );

  it("invalidates pending Open when a current detail read definitively refuses the Bot", async () => {
    const { api, all, rerender } = setup({ selectedId: "b1", routines: routineProps() });
    await act(async () => {});
    let resolve!: (value: {
      bot_id: string;
      state_revision: number;
      preview: PreparedProfilePreview;
    }) => void;
    api.chat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await click("Open conversation");
    api.get.mockRejectedValue(new BotsApiError(404, "not_found"));
    rerender({ routines: changedRoutines() });
    await act(async () => {});
    await act(async () => resolve({ bot_id: "b1", state_revision: 1, preview: prepared }));
    expect(all.onOpenChat).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
      "disabled",
      true,
    );
  });
  it("keeps an archived conversation blocked until unarchived or replaced", async () => {
    const { api } = setup({ selectedId: "b1" });
    api.get.mockResolvedValue({
      bot: { ...bot, chat: { ...bot.chat, archived: true } },
      chat_status: { state: "archived", blockers: [] },
    });
    await click("Refresh Bots");
    expect(
      screen.getByText("The Bot chat is archived. Create a new chat to continue."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
      "disabled",
      true,
    );
    await click("Open conversation");
    expect(api.chat).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "New conversation" })).toHaveProperty(
      "disabled",
      false,
    );
  });
  it.each([
    { state_revision: 2, chat_session_id: "s-new" },
    { state_revision: 2, chat_session_id: "s1" },
  ])("rejects a delayed Open after current Bot identity advances to %j", async (change) => {
    const { api, all, rerender } = setup({ selectedId: "b1", routines: routineProps() });
    await act(async () => {});
    let resolve!: (value: {
      bot_id: string;
      state_revision: number;
      preview: PreparedProfilePreview;
    }) => void;
    api.chat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await click("Open conversation");
    api.get.mockResolvedValue(detail({ ...bot, ...change }));
    rerender({ routines: changedRoutines() });
    await act(async () => {});
    expect(api.get).toHaveBeenCalledTimes(2);
    await act(async () => resolve({ bot_id: "b1", state_revision: 1, preview: prepared }));
    expect(all.onOpenChat).not.toHaveBeenCalled();
  });

  it("preserves a newer read when a delayed mutation returns an older row", async () => {
    const { api, rerender } = setup({ selectedId: "b1", routines: routineProps() });
    await act(async () => {});
    let resolve!: (value: { bot: BotRow }) => void;
    api.newChat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await confirmAfter("New conversation", "New conversation");
    const newer = { ...bot, state_revision: 3, chat_session_id: "s-current" };
    api.get.mockResolvedValue(detail(newer));
    rerender({ routines: changedRoutines() });
    await act(async () => {});
    await act(async () =>
      resolve({ bot: { ...bot, state_revision: 2, chat_session_id: "s-obsolete" } }),
    );
    await click("Open conversation");
    expect(api.chat).toHaveBeenCalledWith(newer, expect.any(AbortSignal));
    expect(screen.getByText("The saved conversation can be reopened.")).toBeTruthy();
  });
  it.each(["Remove Bot", "Reset Bots in this workspace"])(
    "pauses routine editing while %s awaits and restores the draft after failure",
    async (action) => {
      const routineApi = { preview: vi.fn(), create: vi.fn() } as unknown as RoutinesApi;
      const { api } = setup({
        selectedId: "b1",
        routines: {
          api: routineApi,
          snapshot: { status: "ready", items: [], scheduler: null, error: null },
          sessions: {},
          nowMs: 0,
          workspaces,
          controls: {
            paused: false,
            notificationsEnabled: false,
            quietStart: null,
            quietEnd: null,
            permission: "granted",
          },
          onSettings: vi.fn(),
          onRequestPermission: vi.fn(),
          onRefresh: vi.fn(),
          onOpenSession: vi.fn(),
          onStop: vi.fn(),
          timers: { set: vi.fn().mockReturnValue(1), clear: vi.fn() },
        },
      });
      await act(async () => {});
      await click("New routine");
      const name = screen.getByLabelText("Name") as HTMLInputElement;
      fireEvent.change(name, { target: { value: "My draft" } });
      let reject!: (reason: unknown) => void;
      const deferred = () =>
        new Promise<never>((_resolve, fail) => {
          reject = fail;
        });
      if (action === "Remove Bot") api.remove.mockImplementation(deferred);
      else api.reset.mockImplementation(deferred);
      await confirmAfter(action, action === "Remove Bot" ? "Remove" : "Reset Bots");
      expect(
        screen.getByText(
          "Applying Bot change… Routine editing is paused until this request finishes.",
        ),
      ).toBeTruthy();
      expect(name).toHaveProperty("value", "My draft");
      expect(name.matches(":disabled")).toBe(true);
      expect(screen.getByRole("button", { name: "Save" }).matches(":disabled")).toBe(true);
      await act(async () => reject(new BotsApiError(503, "unavailable")));
      expect(name.matches(":disabled")).toBe(false);
      expect(name).toHaveProperty("value", "My draft");
      expect(routineApi.create).not.toHaveBeenCalled();
    },
  );
  it("renders avatar/role/workspace and opens only its selected canonical chat", async () => {
    const { api, all } = setup();
    await act(async () => {});
    expect(screen.getByRole("img", { name: "Planner" })).toBeTruthy();
    expect(screen.getByText("Plan tasks")).toBeTruthy();
    await click("Planner");
    expect(screen.getByText("Current profile revision 2 · Conversation revision 1")).toBeTruthy();
    await click("Open conversation");
    expect(api.chat).toHaveBeenCalledWith(bot, expect.any(AbortSignal));
    expect(all.onOpenChat).toHaveBeenCalledWith(prepared, bot);
  });

  it("shows a Bot without an avatar as its bot icon, in colour", async () => {
    setup();
    await act(async () => {});
    const avatar = screen.getByRole("img", { name: "Planner" });
    expect(avatar.getAttribute("data-color")).toBe("true");
    expect(avatar.querySelector("svg.bot-icon")).not.toBeNull();
  });

  it("creates from an existing profile and selected persistent workspace", async () => {
    const { api, all } = setup();
    (api.list as Mock).mockResolvedValue({
      bots: [],
      scheduler: { state: "running", attended: true, paused: false },
    });
    await click("Refresh Bots");
    await act(async () => {});
    await click("New Bot");
    await click("Create Bot");
    expect(api.create).toHaveBeenCalledWith(
      expect.stringMatching(/^[0-9a-f-]{36}$/),
      "p1",
      2,
      "w1",
      expect.any(AbortSignal),
    );
    expect(all.onSelect).toHaveBeenCalledWith(expect.stringMatching(/^[0-9a-f-]{36}$/));
  });

  it("opens the existing Bot for a profile and vault without creating a duplicate", async () => {
    const { api, all } = setup();
    await act(async () => {});
    await click("New Bot");
    expect(screen.getByText("This profile already has a Bot in the selected vault.")).toBeTruthy();
    await click("Open Bot");
    expect(api.create).not.toHaveBeenCalled();
    expect(all.onSelect).toHaveBeenCalledWith("b1");
  });

  it("offers Create profile when none exist without manufacturing a Bot", async () => {
    const { api, all } = setup({
      profiles: { list: vi.fn().mockResolvedValue([]) } as unknown as ProfileApi,
    });
    await act(async () => {});
    await click("New Bot");
    await click("Create profile");
    expect(all.onCreateProfile).toHaveBeenCalled();
    expect(api.create).not.toHaveBeenCalled();
  });

  it("requires explicit launch-only delivery consent and uses CAS revision", async () => {
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    expect(screen.getByText(/Delivery is off at each launch/)).toBeTruthy();
    expect(screen.getByText(/server's retention policy/)).toBeTruthy();
    await click("Enable Mattermost delivery for this launch");
    expect(api.setDelivery).toHaveBeenCalledWith("b1", 1, true, expect.any(AbortSignal));
  });

  it("shows truthful unavailable and conflict states without automatic retries", async () => {
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    api.setDelivery.mockRejectedValue(new BotsApiError(409, "conflict"));
    await click("Enable Mattermost delivery for this launch");
    expect(screen.getByRole("alert").textContent).toContain("Refresh Bots");
    expect(api.setDelivery).toHaveBeenCalledTimes(1);
  });

  it("explains New conversation and Remove/Reset consequences in a dialog before mutations", async () => {
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    await click("New conversation");
    let dialog = screen.getByRole("alertdialog", { name: "New conversation" });
    expect(dialog.textContent).toContain("keeps the previous conversation");
    expect(api.newChat).not.toHaveBeenCalled();
    await click2(dialog, "New conversation");
    expect(api.newChat).toHaveBeenCalledWith("b1", 1, expect.any(AbortSignal));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await click("Remove Bot");
    dialog = screen.getByRole("alertdialog", { name: "Remove Planner" });
    expect(dialog.textContent).toContain("pauses its owned routines");
    expect(api.remove).not.toHaveBeenCalled();
    await click2(dialog, "Remove");
    expect(api.remove).toHaveBeenCalled();
    await click("Reset Bots in this workspace");
    dialog = screen.getByRole("alertdialog", { name: "Reset Bots" });
    expect(dialog.textContent).toContain("profiles, conversations and routine history");
    expect(api.reset).not.toHaveBeenCalled();
    await click2(dialog, "Reset Bots");
    expect(api.reset).toHaveBeenCalledWith("w1", expect.any(AbortSignal));
  });

  it("focuses Cancel and calls no API when a confirmation is cancelled", async () => {
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    for (const opener of ["New conversation", "Remove Bot", "Reset Bots in this workspace"]) {
      await click(opener);
      const dialog = screen.getByRole("alertdialog");
      expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" }));
      await click2(dialog, "Cancel");
      expect(screen.queryByRole("alertdialog")).toBeNull();
    }
    await click("Remove Bot");
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.newChat).not.toHaveBeenCalled();
    expect(api.remove).not.toHaveBeenCalled();
    expect(api.reset).not.toHaveBeenCalled();
  });

  it("opens each confirmation next to its opener and scrolls it into view", async () => {
    const scrollIntoView = vi.fn();
    const previous = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      setup({ selectedId: "b1", routines: routineProps() });
      await act(async () => {});
      for (const opener of ["New conversation", "Remove Bot"]) {
        scrollIntoView.mockClear();
        const row = screen.getByRole("button", { name: opener }).closest(".bots-actions");
        await click(opener);
        const dialog = screen.getByRole("alertdialog");
        // Directly under the desk's last action row, not under the page head a screen above it.
        expect(row?.nextElementSibling).toBe(dialog);
        expect(row?.parentElement?.className).toBe("bots-detail");
        expect(dialog.classList.contains("confirm-dialog-under-opener")).toBe(true);
        expect(scrollIntoView.mock.contexts[0]).toBe(dialog);
        await click2(dialog, "Cancel");
      }
      scrollIntoView.mockClear();
      await click("Reset Bots in this workspace");
      const dialog = screen.getByRole("alertdialog");
      expect(document.querySelector("header.page-head")?.nextElementSibling).toBe(dialog);
      expect(dialog.classList.contains("confirm-dialog-under-opener")).toBe(false);
      expect(scrollIntoView.mock.contexts[0]).toBe(dialog);
    } finally {
      Element.prototype.scrollIntoView = previous;
    }
  });

  it.each(["Remove Bot", "Reset Bots in this workspace"])(
    "drops a pending %s confirmation when the active vault changes",
    async (opener) => {
      const { api, rerender } = setup({ selectedId: "b1" });
      await act(async () => {});
      await click(opener);
      expect(screen.getByRole("alertdialog")).toBeTruthy();
      await act(async () => rerender({ activeWorkspaceId: "w2" }));
      expect(screen.queryByRole("alertdialog")).toBeNull();
      // Back in the first vault, the dialog must not reopen on its own.
      await act(async () => rerender({ activeWorkspaceId: "w1" }));
      expect(screen.queryByRole("alertdialog")).toBeNull();
      expect(api.remove).not.toHaveBeenCalled();
      expect(api.reset).not.toHaveBeenCalled();
    },
  );

  it("still asks the leave guard before opening a confirmation", async () => {
    const { all } = setup({ selectedId: "b1" });
    await act(async () => {});
    setUnsavedWork("routine", true);
    try {
      (all.confirm as Mock).mockReturnValueOnce(false);
      await click("Remove Bot");
      expect(all.confirm).toHaveBeenCalledWith("Discard the unsaved routine draft?");
      expect(screen.queryByRole("alertdialog")).toBeNull();
    } finally {
      setUnsavedWork("routine", false);
    }
  });

  it("holds the page title, intro and actions in a page head", async () => {
    setup();
    await act(async () => {});
    const head = document.querySelector("header.page-head") as HTMLElement;
    expect(within(head).getByRole("heading", { name: "Bots" }).className).toBe("page-title");
    expect(head.querySelector("p.page-intro")?.textContent).toBe(
      "Manage your Bots, profiles, routines and delivery.",
    );
    const actions = head.querySelector("div.page-actions") as HTMLElement;
    expect(within(actions).getByRole("button", { name: "New Bot" }).className).toBe("btn go");
    expect(within(actions).getByRole("button", { name: "Refresh Bots" }).className).toBe("btn alt");
    expect(
      within(actions).getByRole("button", { name: "Reset Bots in this workspace" }).className,
    ).toBe("btn txt");
  });

  it("leaves existing history usable when the canonical source is missing", async () => {
    const { api } = setup({ selectedId: "b1" });
    api.get.mockResolvedValue({
      bot,
      chat_status: { state: "transcript_missing", blockers: ["transcript_missing"] },
    });
    await click("Refresh Bots");
    expect(
      screen.getAllByText("The Bot chat transcript is missing. Start a new chat."),
    ).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.getByRole("button", { name: "New conversation" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("aborts and rejects late reads or chat handoffs after selecting another Bot", async () => {
    let resolve!: (value: {
      bot_id: string;
      state_revision: number;
      preview: PreparedProfilePreview;
    }) => void;
    const { api, all } = setup({ selectedId: "b1" });
    api.list.mockResolvedValue({
      bots: [
        bot,
        {
          ...bot,
          bot_id: "b2",
          profile_id: "p2",
          chat_session_id: "s2",
          profile: { ...botProfile, identity: { ...botProfile.identity, name: "Researcher" } },
        },
      ],
      scheduler: { state: "running", attended: true, paused: false },
    });
    await act(async () => {});
    await click("Refresh Bots");
    api.chat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await click("Open conversation");
    const signal = api.chat.mock.calls[0]?.[1] as AbortSignal;
    await click("Researcher");
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ bot_id: "b1", state_revision: 1, preview: prepared }));
    expect(all.onOpenChat).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Researcher" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("separates a routine outcome from held delivery and its reason, without any JEV wording", async () => {
    const receipt = {
      attempt_id: "a1",
      routine_id: "r1",
      session_id: "s-run",
      outcome: "succeeded",
      attention: "silent",
      reason: "muted_by_owner",
      state: "held",
      delivery_reason: "held",
      created_at: bot.created_at,
      updated_at: bot.updated_at,
    } as const;
    const { api, all } = setup({ selectedId: "b1" });
    api.get.mockResolvedValue(detail({ ...bot, latest_delivery: receipt }));
    await click("Refresh Bots");
    expect(screen.getByText("Routine outcome: Succeeded")).toBeTruthy();
    expect(screen.getByText("Mattermost delivery: Held")).toBeTruthy();
    expect(screen.getByText("Held by Decision's rules.")).toBeTruthy();
    expect(screen.getByText("You muted this topic.")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Latest routine result" }).textContent).not.toMatch(
      /jev/i,
    );
    await click("Open latest run chat");
    expect(all.onOpenSession).toHaveBeenCalledWith("s-run");
  });

  it("does not let a late pre-mutation read replace the new canonical conversation", async () => {
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    let resolve!: (value: BotDetail) => void;
    api.get.mockImplementationOnce(
      () =>
        new Promise<BotDetail>((ready) => {
          resolve = ready;
        }),
    );
    await click("Refresh Bots");
    await confirmAfter("New conversation", "New conversation");
    await act(async () => resolve(detail()));
    await click("Open conversation");
    expect(api.chat).toHaveBeenCalledWith(
      expect.objectContaining({ chat_session_id: "s-new", state_revision: 2 }),
      expect.any(AbortSignal),
    );
  });

  it("shows a profile read failure without claiming that no profiles exist", async () => {
    setup({
      profiles: { list: vi.fn().mockRejectedValue(new Error("private")) } as unknown as ProfileApi,
    });
    await act(async () => {});
    await click("New Bot");
    expect(
      screen.getByText("Profiles could not be loaded. Refresh Bots to try again."),
    ).toBeTruthy();
    expect(screen.queryByText("Create a profile in Agents, then return to Bots.")).toBeNull();
  });
});

it("renders Bot chat status and blocker explanations in plain words", async () => {
  const blockers = [
    "transcript_missing",
    "profile_archived",
    "workspace_restart_required",
    "workspace_unavailable",
    "profile_blocked",
    "launch_blocked",
    "launch_changed",
    "capability_changed",
    "app_closing",
    "provider_unavailable",
    "profile_missing",
    "workspace_missing",
    "session_exists",
    "corrupt",
    "mcp_revoked",
    "not_found",
    "newer_version",
    "protection",
    "workspace_not_allowed",
    "connection_missing",
    "connections_unavailable",
    "connection_invalid",
  ];
  const { api } = setup({ selectedId: "b1" });
  api.get.mockResolvedValue({ bot, chat_status: { state: "blocked", blockers } });
  await act(async () => {});
  await click("Refresh Bots");
  const detail = screen.getByRole("region", { name: "Selected Bot" });
  const items = within(detail).getAllByRole("listitem");
  expect(items).toHaveLength(blockers.length);
  for (const [index, code] of blockers.entries()) {
    const text = items[index]?.textContent ?? "";
    expect(text).not.toBe(code);
    expect(text).not.toContain(`(${code})`);
  }
  expect(detail.textContent).not.toContain("<script>");
});

it.each([
  ["connection_missing", "This Bot's saved model connection was removed."],
  [
    "connections_unavailable",
    "Your model connections could not be read, so this Bot cannot start.",
  ],
])("lists %s as a plain connection sentence in the Bot status", async (code, sentence) => {
  const { api } = setup({ selectedId: "b1" });
  api.get.mockResolvedValue({
    bot,
    chat_status: { state: "blocked", blockers: [code] },
  });
  await act(async () => {});
  await click("Refresh Bots");
  const detail = screen.getByRole("region", { name: "Selected Bot" });
  expect(within(detail).getByText(sentence).tagName).toBe("LI");
  expect(detail.textContent).not.toContain(
    "Claude Code has not confirmed that this model is available.",
  );
});

it("keeps unavailable Bot status sentence while explaining its known blocker", async () => {
  const { api } = setup({ selectedId: "b1" });
  api.get.mockResolvedValue({
    bot,
    chat_status: { state: "unavailable", blockers: ["provider_unavailable"] },
  });
  await act(async () => {});
  await click("Refresh Bots");
  const detail = screen.getByRole("region", { name: "Selected Bot" });
  expect(within(detail).getByText(t("profiles.chat.unavailable"))).toBeTruthy();
  expect(within(detail).getByText(t("profiles.chat.provider_unavailable"))).toBeTruthy();
  expect(detail.textContent).not.toContain("(unavailable)");
});

it("uses blocked preview field reasons when opening a Bot", async () => {
  const { api } = setup({ selectedId: "b1" });
  api.chat.mockResolvedValue({
    bot_id: "b1",
    state_revision: 1,
    preview: {
      launchable: false,
      profile_id: "p1",
      profile_revision: 1,
      blockers: ["workspace.allowed_workspace_ids", "tools.mcp_configuration"],
      fields: [
        {
          field: "workspace.allowed_workspace_ids",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: null,
          source: "profile",
          reason: "workspace_not_allowed",
        },
        {
          field: "tools.mcp_configuration",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: null,
          source: "profile",
          reason: "mcp_policy_blocked",
        },
      ],
    },
  });
  await act(async () => {});
  await click("Open conversation");
  const alert = screen.getByRole("alert").textContent ?? "";
  expect(alert).toContain("This workspace is not allowed by the profile.");
  expect(alert).toContain("This profile's MCP servers cannot run in this workspace.");
  expect(alert).not.toContain("(workspace_not_allowed)");
  expect(alert).not.toContain("(mcp_policy_blocked)");
});

const CHIEF_ID = (TEAM[0] as TeamSeat).profileId as string;
function team(over: Partial<TeamLive> = {}, readiness: TeamReadiness = "ready") {
  return {
    live: {
      online: true,
      sessions: {},
      others: [],
      routines: { status: "ready", items: [], scheduler: null, error: null },
      checker: "listed",
      sessionRows: [],
      nowMs: 0,
      ...over,
    } as TeamLive,
    readiness,
  };
}
const chiefBot: BotRow = {
  ...bot,
  bot_id: "bc",
  profile_id: CHIEF_ID,
  chat_session_id: "sc",
  profile: { ...botProfile, identity: { ...botProfile.identity, name: "Chief" } },
};
const chiefPrepared = { ...prepared, session_id: "sc", profile_id: CHIEF_ID };
function described(name: string): string {
  const id = screen.getByRole("button", { name }).getAttribute("aria-describedby");
  return document.getElementById(id ?? "")?.textContent ?? "";
}
function seatProfiles(rows: ProfileSummary[] = []) {
  return {
    list: vi.fn().mockResolvedValue(rows),
    create: vi.fn().mockResolvedValue({}),
  } as unknown as ProfileApi & { list: Mock; create: Mock };
}

describe("BotsView team", () => {
  it("lists the seats before any profile or Bot exists, creating nothing", async () => {
    const profiles = seatProfiles();
    const { api } = setup({ team: team(), profiles });
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Core team" })).toBeTruthy();
    for (const name of ["Chief", "Ops", "Research", "Writer", "Engineer", "Desk", "Checker"])
      expect(screen.getByRole("button", { name })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Your Bots" })).toBeTruthy();
    expect(screen.getByText("Manage your Bots, profiles, routines and delivery.")).toBeTruthy();
    expect(screen.queryByText(/No Bots yet/)).toBeNull();
    expect(api.create).not.toHaveBeenCalled();
    expect(api.chat).not.toHaveBeenCalled();
    expect(profiles.create).not.toHaveBeenCalled();
  });

  it("opens a seat: profile, Bot, chat, then selects and opens it with exact args", async () => {
    const profiles = seatProfiles();
    const { api, all } = setup({ team: team(), profiles, initialSeat: "chief" });
    api.create.mockResolvedValue({ bot: chiefBot, replayed: false });
    api.chat.mockResolvedValue({ bot_id: "bc", state_revision: 1, preview: chiefPrepared });
    await act(async () => {});
    await click("Open conversation");
    await act(async () => {});
    expect(profiles.create).toHaveBeenCalledWith(
      CHIEF_ID,
      expect.stringMatching(/^[0-9a-f-]{36}$/),
      expect.objectContaining({ identity: expect.objectContaining({ name: "Chief" }) }),
      expect.any(AbortSignal),
    );
    expect(api.create).toHaveBeenCalledWith(
      expect.stringMatching(/^[0-9a-f-]{36}$/),
      CHIEF_ID,
      1,
      "w1",
      expect.any(AbortSignal),
    );
    expect(api.chat).toHaveBeenCalledWith(chiefBot, expect.any(AbortSignal));
    expect(all.onSelect).toHaveBeenCalledWith("bc");
    expect(all.onOpenChat).toHaveBeenCalledWith(chiefPrepared, chiefBot);
    const order = [
      profiles.create,
      api.create,
      api.chat,
      all.onSelect as Mock,
      all.onOpenChat as Mock,
    ].map((fn) => fn.mock.invocationCallOrder[0] ?? 0);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(order[0]).toBeGreaterThan(0);
  });

  it("shows the capacity message when a seat's profile cannot be stored", async () => {
    const profiles = seatProfiles();
    profiles.create.mockRejectedValue(new ProfileApiError(409, "capacity"));
    const { all } = setup({ team: team(), profiles, initialSeat: "chief" });
    await act(async () => {});
    await click("Open conversation");
    await act(async () => {});
    expect(screen.getByRole("alert").textContent).toBe(
      "VIKI Agent cannot store another profile. Add a Bot for one of your profiles with New Bot instead.",
    );
    expect(all.onOpenChat).not.toHaveBeenCalled();
  });

  it("selects the seat's Bot without opening when its chat is blocked", async () => {
    const profiles = seatProfiles();
    const { api, all } = setup({ team: team(), profiles, initialSeat: "chief" });
    api.create.mockResolvedValue({ bot: chiefBot, replayed: false });
    api.chat.mockResolvedValue({
      bot_id: "bc",
      state_revision: 1,
      preview: {
        launchable: false,
        fields: [],
        blockers: ["capability"],
        profile_id: CHIEF_ID,
        profile_revision: 1,
      },
    });
    await act(async () => {});
    await click("Open conversation");
    await act(async () => {});
    expect(all.onSelect).toHaveBeenCalledWith("bc");
    expect(all.onOpenChat).not.toHaveBeenCalled();
    expect(api.list).toHaveBeenCalledTimes(3);
  });

  it.each(["loading", "missing", "unknown"] as const)(
    "opens a team conversation when optional Kanban readiness is %s",
    async (readiness) => {
      const profiles = seatProfiles();
      const { api, all } = setup({ team: team({}, readiness), profiles, initialSeat: "chief" });
      api.create.mockResolvedValue({ bot: chiefBot, replayed: false });
      api.chat.mockResolvedValue({ bot_id: "bc", state_revision: 1, preview: chiefPrepared });
      await act(async () => {});
      expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
        "disabled",
        false,
      );
      await click("Open conversation");
      expect(all.onOpenChat).toHaveBeenCalledWith(chiefPrepared, chiefBot);
    },
  );

  it("requires a registered persistent vault for a team conversation", async () => {
    const { api } = setup({
      team: team(),
      profiles: seatProfiles(),
      initialSeat: "chief",
      workspaces: [],
    });
    await act(async () => {});
    const open = screen.getByRole("button", { name: "Open conversation" });
    expect(open).toHaveProperty("disabled", true);
    expect(document.getElementById(open.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
      READINESS_TEXT.no_vault,
    );
    expect(api.create).not.toHaveBeenCalled();
  });

  it("disables a seat without a profiles client", async () => {
    const { all } = setup({ team: team(), initialSeat: "chief" });
    delete all.profiles;
    cleanup();
    render(<BotsView {...all} />);
    await act(async () => {});
    const open = screen.getByRole("button", { name: "Open conversation" });
    expect(open).toHaveProperty("disabled", true);
    expect(
      screen.getByText("Profiles could not be loaded. Refresh Bots to try again."),
    ).toBeTruthy();
  });

  it("keeps a seat disabled when profiles cannot be read", async () => {
    const profiles = seatProfiles();
    profiles.list.mockRejectedValue(new Error("unavailable"));
    const { api } = setup({ team: team({}, "unknown"), profiles, initialSeat: "chief" });
    await act(async () => {});
    expect(screen.getByRole("button", { name: "Open conversation" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(
      screen.getAllByText("Profiles could not be loaded. Refresh Bots to try again.").length,
    ).toBeGreaterThan(0);
    expect(api.create).not.toHaveBeenCalled();
  });

  it("reads Checking… with Open disabled while Bots are listing, then Status unavailable", async () => {
    let reject!: (reason: unknown) => void;
    const api = setup().api;
    cleanup();
    api.list.mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    const { all } = setup({ api, team: team(), profiles: seatProfiles(), initialSeat: "chief" });
    await act(async () => {});
    expect(described("Ops")).toBe("Checking…");
    const open = screen.getByRole("button", { name: "Open conversation" });
    expect(open).toHaveProperty("disabled", true);
    expect(screen.getByText("Bots are still loading.")).toBeTruthy();
    await act(async () => reject(new Error("offline")));
    expect(described("Ops")).toBe("Status unavailable");
    expect(open).toHaveProperty("disabled", true);
    expect(all.onOpenChat).not.toHaveBeenCalled();
  });

  it("aborts a seat open when another seat is chosen mid-open", async () => {
    const profiles = seatProfiles([{ ...profile, profile_id: CHIEF_ID }]);
    const { api, all } = setup({ team: team(), profiles, initialSeat: "chief" });
    api.create.mockResolvedValue({ bot: chiefBot, replayed: false });
    let resolve!: (value: unknown) => void;
    api.chat.mockImplementation(
      () =>
        new Promise((ready) => {
          resolve = ready;
        }),
    );
    await act(async () => {});
    await click("Open conversation");
    await act(async () => {});
    const signal = api.chat.mock.calls[0]?.[1] as AbortSignal;
    await click("Ops");
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ bot_id: "bc", state_revision: 1, preview: chiefPrepared }));
    expect(all.onOpenChat).not.toHaveBeenCalled();
    expect(all.onSelect).not.toHaveBeenCalledWith("bc");
    expect(screen.getByRole("heading", { name: "Ops" })).toBeTruthy();
  });

  it("keeps New Bot open with a seat selected, and Cancel shows the seat again", async () => {
    setup({ team: team(), profiles: seatProfiles([profile]), initialSeat: "chief" });
    await act(async () => {});
    await click("New Bot");
    expect(screen.getByRole("region", { name: "New Bot" })).toBeTruthy();
    await click("Cancel");
    expect(screen.queryByRole("region", { name: "New Bot" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Chief" })).toBeTruthy();
  });

  it("selects the initial seat and focuses its desk heading", async () => {
    setup({ team: team(), profiles: seatProfiles(), initialSeat: "chief" });
    await act(async () => {});
    expect(screen.getByRole("button", { name: "Chief" }).getAttribute("aria-current")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Chief" }));
  });

  it("focuses the desk heading after a roster click only", async () => {
    setup({ team: team() });
    await act(async () => {});
    await click("Planner");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Planner" }));
    await click("Writer");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Writer" }));
    const { all } = setup();
    cleanup();
    const view = render(<BotsView {...all} team={team()} />);
    await act(async () => {});
    view.rerender(<BotsView {...all} team={team()} selectedId="b1" />);
    await act(async () => {});
    const heading = screen.getByRole("heading", { name: "Planner" });
    expect(document.activeElement).not.toBe(heading);
  });

  it("offers Edit profile for the selected Bot's profile", async () => {
    const onEditProfile = vi.fn();
    setup({ selectedId: "b1", team: team(), onEditProfile });
    await act(async () => {});
    await click("Edit profile");
    expect(onEditProfile).toHaveBeenCalledWith("p1");
  });

  it("warns that a removed or reset seat Bot stays as Not started", async () => {
    const { api } = setup({ selectedId: "bc", team: team() });
    api.list.mockResolvedValue({
      bots: [bot, chiefBot],
      scheduler: { state: "running", attended: true, paused: false },
    });
    api.get.mockResolvedValue(detail(chiefBot));
    await click("Refresh Bots");
    await click("Remove Bot");
    let dialog = screen.getByRole("alertdialog");
    expect(dialog.querySelector("p")?.textContent).toMatch(
      / Chief stays in your team as Not started; opening it again starts a new conversation with the same profile\.$/,
    );
    await click2(dialog, "Cancel");
    await click("Reset Bots in this workspace");
    dialog = screen.getByRole("alertdialog");
    expect(dialog.querySelector("p")?.textContent).toMatch(
      / Your team's seats stay listed as Not started\.$/,
    );
  });

  it("keeps plain Remove and Reset copy without a filled seat", async () => {
    setup({ selectedId: "b1", team: team() });
    await act(async () => {});
    for (const opener of ["Remove Bot", "Reset Bots in this workspace"]) {
      await click(opener);
      const dialog = screen.getByRole("alertdialog");
      expect(dialog.textContent).not.toMatch(/Not started/);
      await click2(dialog, "Cancel");
    }
  });

  it("remove and reset say where memory files stay", async () => {
    const kept = () => t("bots.memory.removeKept");
    const keptApp = () => t("bots.memory.removeKeptApp");
    const { api } = setup({ selectedId: "b1" });
    await act(async () => {});
    for (const opener of ["Remove Bot", "Reset Bots in this workspace"]) {
      await click(opener);
      const dialog = screen.getByRole("alertdialog");
      expect(dialog.textContent).toContain(kept());
      expect(dialog.textContent).not.toContain(keptApp());
      expect(kept()).toContain("stay in the vault");
      await click2(dialog, "Cancel");
    }

    const unmanaged = {
      ...bot,
      workspace: { name: "Main vault", policy: "protected", active: true },
    };
    api.list.mockResolvedValue({
      bots: [unmanaged],
      scheduler: { state: "running", attended: true, paused: false },
    });
    api.get.mockResolvedValue(detail(unmanaged as BotRow));
    await click("Refresh Bots");
    for (const opener of ["Remove Bot", "Reset Bots in this workspace"]) {
      await click(opener);
      const dialog = screen.getByRole("alertdialog");
      expect(dialog.textContent).toContain(keptApp());
      expect(dialog.textContent).not.toContain(kept());
      await click2(dialog, "Cancel");
    }
  });

  describe("code folder grants in Remove and Reset", () => {
    const granted: BotRow = { ...bot, code_folder: { workspace_id: "c1", name: "App" } };
    const removal = () => t("bots.code_folder.remove_copies");

    it("adds the copies sentence to Remove only when this Bot has a grant", async () => {
      const { api } = setup({ selectedId: "b1" });
      api.get.mockResolvedValue(detail(granted));
      await click("Refresh Bots");
      await click("Remove Bot");
      let dialog = screen.getByRole("alertdialog");
      expect(dialog.querySelector("p")?.textContent).toMatch(/pauses its owned routines/);
      expect(dialog.querySelector("p")?.textContent?.endsWith(removal())).toBe(true);
      await click2(dialog, "Cancel");

      api.get.mockResolvedValue(detail({ ...bot, code_folder: null }));
      await click("Refresh Bots");
      await click("Remove Bot");
      dialog = screen.getByRole("alertdialog");
      expect(dialog.textContent).not.toContain(removal());
      await click2(dialog, "Cancel");

      api.get.mockResolvedValue(detail(bot));
      await click("Refresh Bots");
      await click("Remove Bot");
      expect(screen.getByRole("alertdialog").textContent).not.toContain(removal());
    });

    it("keeps the seat sentence and then adds the copies sentence", async () => {
      const chief: BotRow = { ...chiefBot, code_folder: granted.code_folder ?? null };
      const { api } = setup({ selectedId: "bc", team: team() });
      api.list.mockResolvedValue({
        bots: [bot, chief],
        scheduler: { state: "running", attended: true, paused: false },
      });
      api.get.mockResolvedValue(detail(chief));
      await click("Refresh Bots");
      await click("Remove Bot");
      const body = screen.getByRole("alertdialog").querySelector("p")?.textContent ?? "";
      expect(body).toContain("stays in your team as Not started");
      expect(body.endsWith(removal())).toBe(true);
    });

    it("adds the copies sentence to Reset when a Bot in this workspace has a grant", async () => {
      const { api } = setup({ selectedId: "b2" });
      api.list.mockResolvedValue({
        bots: [{ ...bot, bot_id: "b2", chat_session_id: "s2" }, granted],
        scheduler: { state: "running", attended: true, paused: false },
      });
      await click("Refresh Bots");
      await click("Reset Bots in this workspace");
      const body = screen.getByRole("alertdialog").querySelector("p")?.textContent ?? "";
      expect(body).toMatch(/removes all Bots in this workspace/);
      expect(body.endsWith(removal())).toBe(true);
    });

    it("leaves Reset alone for grants in other workspaces and for no grant", async () => {
      const { api } = setup({ selectedId: "b1" });
      api.list.mockResolvedValue({
        bots: [
          { ...bot, code_folder: null },
          { ...granted, bot_id: "b3", workspace_id: "w2", chat_session_id: "s3" },
        ],
        scheduler: { state: "running", attended: true, paused: false },
      });
      await click("Refresh Bots");
      await click("Reset Bots in this workspace");
      expect(screen.getByRole("alertdialog").textContent).not.toContain(removal());
    });
  });

  it("shows live status in rows, the desk and the summary, and re-checks on Refresh", async () => {
    const onRecheckTeam = vi.fn();
    const sessions = { s1: { cards: [{}], turns: [] } as unknown as SessionView };
    setup({ selectedId: "b1", team: team({ sessions }), onRecheckTeam });
    await act(async () => {});
    expect(described("Planner")).toBe("Waiting for you in its conversation");
    expect(described("Chief")).toBe("Not started");
    expect(described("Checker")).toBe("Not started");
    expect(document.querySelector(".bots-summary")?.textContent).toBe("1 needs you");
    const desk = screen.getByRole("region", { name: "Selected Bot" });
    expect(desk.textContent).toContain("Waiting for you in its conversation");
    await click("Refresh Bots");
    expect(onRecheckTeam).toHaveBeenCalledTimes(1);
  });

  it("shows the scheduled-routines note for a Bot whose vault is not ordinary", async () => {
    const { api } = setup({ selectedId: "b1", team: team() });
    api.get.mockResolvedValue(
      detail({
        ...bot,
        workspace: { name: "Main vault", policy: "existing_readonly", active: true },
      }),
    );
    await click("Refresh Bots");
    expect(
      screen.getByText(
        "Scheduled routines need a vault whose policy is Ordinary in Settings → Workspaces; this conversation still works here.",
      ),
    ).toBeTruthy();
  });

  it("forgets the seat once a Bot is selected from outside", async () => {
    const { all } = setup();
    cleanup();
    const view = render(<BotsView {...all} team={team()} initialSeat="chief" />);
    await act(async () => {});
    view.rerender(<BotsView {...all} team={team()} initialSeat="chief" selectedId="b1" />);
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "Planner" })).toBeTruthy();
    view.rerender(<BotsView {...all} team={team()} initialSeat="chief" selectedId={null} />);
    await act(async () => {});
    expect(screen.queryByRole("heading", { name: "Chief" })).toBeNull();
    expect(screen.getByText("Choose a Bot for the work")).toBeTruthy();
  });

  it("holds every Bot back and seats at Checking… while the vault id is loading", async () => {
    setup({ activeWorkspaceId: "", team: team({}, "loading") });
    await act(async () => {});
    expect(described("Chief")).toBe("Checking…");
    expect(screen.queryByRole("button", { name: "Planner" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "In other vaults" })).toBeNull();
    expect(screen.queryByText(/In Main vault/)).toBeNull();
  });
});

const SEATS = TEAM.filter((row) => row.profileId !== null);
function seatBot(row: TeamSeat): BotRow {
  return {
    ...bot,
    bot_id: `b-${row.seat}`,
    profile_id: row.profileId as string,
    profile: { ...botProfile, identity: { ...botProfile.identity, name: row.name } },
  };
}
function listOf(bots: BotRow[]) {
  return { bots, scheduler: { state: "running", attended: true, paused: false } };
}
const addButton = () => screen.getByRole("button", { name: "Add missing Bots" });

describe("BotsView Add missing Bots", () => {
  it("creates all seven seats for an empty team and names them", async () => {
    const profiles = seatProfiles();
    const { api, all } = setup({ team: team(), profiles });
    await act(async () => {});
    await click("Add missing Bots");
    await act(async () => {});
    expect(profiles.create).toHaveBeenCalledTimes(7);
    expect(api.create).toHaveBeenCalledTimes(7);
    expect(screen.getByRole("status").textContent).toMatch(
      /^Added Chief, Ops, Research, Writer, Engineer, Desk,? and Checker\.$/,
    );
    expect(api.chat).not.toHaveBeenCalled();
    expect(all.onOpenChat).not.toHaveBeenCalled();
  });

  it("reports the seats that already had a Bot beside the ones added", async () => {
    const profiles = seatProfiles();
    const { api } = setup({ team: team(), profiles });
    api.list.mockResolvedValue(listOf([seatBot(SEATS[0] as TeamSeat)]));
    await act(async () => {});
    await click("Add missing Bots");
    await act(async () => {});
    expect(api.create).toHaveBeenCalledTimes(6);
    expect(screen.getByRole("status").textContent).toMatch(
      /^Added Ops, .* Checker\. Already in your team: Chief\.$/,
    );
  });

  it("names a seat that could not be added while the rest are added", async () => {
    const profiles = seatProfiles();
    const writer = SEATS.find((row) => row.seat === "writer") as TeamSeat;
    profiles.create.mockImplementation((id: string) =>
      id === writer.profileId
        ? Promise.reject(new ProfileApiError(409, "capacity"))
        : Promise.resolve({}),
    );
    const { api } = setup({ team: team(), profiles });
    await act(async () => {});
    await click("Add missing Bots");
    await act(async () => {});
    expect(api.create).toHaveBeenCalledTimes(6);
    const note = screen.getByRole("status").textContent ?? "";
    expect(note).toMatch(/^Added Chief, .* Checker\./);
    expect(note).not.toMatch(/Added[^.]*Writer/);
    expect(note).toContain(
      "Writer was not added: VIKI Agent cannot store another profile. Add a Bot for one of your profiles with New Bot instead.",
    );
  });

  it("says every seat already has a Bot when a run finds nothing to add", async () => {
    const profiles = seatProfiles();
    const { api } = setup({ team: team(), profiles });
    await act(async () => {});
    api.list.mockResolvedValue(listOf(SEATS.map(seatBot)));
    await click("Add missing Bots");
    await act(async () => {});
    expect(screen.getByRole("status").textContent).toBe("Every seat already has a Bot.");
    expect(api.create).not.toHaveBeenCalled();
  });

  function holdFirstCreate() {
    const profiles = seatProfiles();
    const view = setup({ team: team(), profiles });
    let release!: () => void;
    view.api.create.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ bot: { ...bot, bot_id: "new" }, replayed: false });
        }),
    );
    return { ...view, release: () => release() };
  }

  it("is disabled while it runs and keeps running when the selection changes", async () => {
    const { api, release } = holdFirstCreate();
    await act(async () => {});
    await click("Add missing Bots");
    expect(addButton()).toHaveProperty("disabled", true);
    expect(screen.getByRole("status").textContent).toBe("Adding Bots…");
    expect(screen.getByRole("button", { name: "Reset Bots in this workspace" })).toHaveProperty(
      "disabled",
      true,
    );
    const signal = api.create.mock.calls[0]?.[4] as AbortSignal;
    await click("Planner");
    expect(signal.aborted).toBe(false);
    await act(async () => release());
    expect(api.create).toHaveBeenCalledTimes(7);
    expect(screen.getByRole("status").textContent).toMatch(/^Added /);
    expect(addButton()).toHaveProperty("disabled", false);
  });

  it("stops and shows no note when the vault changes mid-run", async () => {
    const { api, rerender, release } = holdFirstCreate();
    await act(async () => {});
    await click("Add missing Bots");
    const signal = api.create.mock.calls[0]?.[4] as AbortSignal;
    rerender({ activeWorkspaceId: "w2" });
    await act(async () => {});
    expect(signal.aborted).toBe(true);
    await act(async () => release());
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is hidden without profiles, outside a persistent vault and when every seat has a Bot", async () => {
    const { all, api } = setup({ team: team(), profiles: seatProfiles() });
    await act(async () => {});
    expect(addButton()).toBeTruthy();
    cleanup();
    const { profiles: _profiles, ...withoutProfiles } = all;
    render(<BotsView {...withoutProfiles} />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
    cleanup();
    render(<BotsView {...all} workspaces={[]} />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
    cleanup();
    api.list.mockResolvedValue(listOf(SEATS.map(seatBot)));
    render(<BotsView {...all} />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
  });

  it("is hidden in a non-persistent vault and when readiness says no vault", async () => {
    const view = setup({ team: team(), profiles: seatProfiles() });
    await act(async () => {});
    expect(addButton()).toBeTruthy();
    view.rerender({ workspaces: [{ ...workspaces[0], persistent: false } as WorkspaceJson] });
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
    cleanup();
    setup({ team: team({}, "no_vault"), profiles: seatProfiles() });
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
  });

  it("stops and shows no note when the client changes mid-run", async () => {
    const { api, rerender, release } = holdFirstCreate();
    await act(async () => {});
    await click("Add missing Bots");
    const signal = api.create.mock.calls[0]?.[4] as AbortSignal;
    rerender({ profiles: seatProfiles() });
    await act(async () => {});
    expect(signal.aborted).toBe(true);
    await act(async () => release());
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows no note and reloads nothing when the vault changes during the last seat's create", async () => {
    const { api, rerender, release } = holdFirstCreate();
    const checker = SEATS.find((row) => row.seat === "checker") as TeamSeat;
    api.list.mockResolvedValue(listOf(SEATS.filter((row) => row !== checker).map(seatBot)));
    await act(async () => {});
    await click("Add missing Bots");
    expect(api.create).toHaveBeenCalledTimes(1);
    rerender({ activeWorkspaceId: "w2" });
    await act(async () => {});
    const listCalls = api.list.mock.calls.length;
    await act(async () => release());
    expect(screen.queryByRole("status")).toBeNull();
    expect(api.list).toHaveBeenCalledTimes(listCalls);
  });

  it("moves focus to the page heading when the run fills the last seat", async () => {
    const { api } = setup({ team: team(), profiles: seatProfiles() });
    await act(async () => {});
    api.list.mockImplementation(() =>
      Promise.resolve(listOf(api.create.mock.calls.length < 7 ? [] : SEATS.map(seatBot))),
    );
    addButton().focus();
    expect(document.activeElement).toBe(addButton());
    await click("Add missing Bots");
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Bots" }));
  });

  it("stays disabled until the reload's list arrives", async () => {
    const { api } = setup({ team: team(), profiles: seatProfiles() });
    await act(async () => {});
    let arrive!: () => void;
    // fillSeat re-reads the list before each create, so only the list after the 7th create is held.
    api.list.mockImplementation(() =>
      api.create.mock.calls.length < 7
        ? Promise.resolve(listOf([]))
        : new Promise((resolve) => {
            arrive = () => resolve(listOf([]));
          }),
    );
    await click("Add missing Bots");
    await act(async () => {});
    expect(screen.getByRole("status").textContent).toMatch(/^Added /);
    expect(addButton()).toHaveProperty("disabled", true);
    await act(async () => arrive());
    expect(addButton()).toHaveProperty("disabled", false);
  });

  it("is disabled while another Bots action runs and enabled when it settles", async () => {
    const { api } = setup({ team: team(), profiles: seatProfiles(), selectedId: "b1" });
    await act(async () => {});
    let settle!: () => void;
    api.remove.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    expect(addButton()).toHaveProperty("disabled", false);
    await confirmAfter("Remove Bot", "Remove");
    expect(addButton()).toHaveProperty("disabled", true);
    await act(async () => settle());
    expect(addButton()).toHaveProperty("disabled", false);
  });
});
