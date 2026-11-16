import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type BotDetail,
  type BotRow,
  type BotsApi,
  BotsApiError,
} from "../../clients/messaging/botsApi";
import type { VaultApi } from "../../state/vaultApi";
import { BotChatActivity, BotConversationHelp, BotDeliverySummary } from "./BotChatActivity";

afterEach(cleanup);
const bot: BotRow = {
  bot_id: "b1",
  profile_id: "p1",
  workspace_id: "w1",
  chat_session_id: "s1",
  state_revision: 1,
  created_at: "2026-10-14T00:00:00.000Z",
  updated_at: "2026-10-14T00:00:00.000Z",
  profile: {
    revision: 1,
    archived: false,
    identity: {
      name: "Planner",
      purpose: "Review tasks",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
    },
  },
  workspace: { name: "Main", policy: "ordinary", active: true },
  chat: { profile_revision: 1, started: true, archived: false },
  routine_ids: [],
  delivery: { enabled: false, available: false, reason: "not_connected" },
  latest_delivery: null,
};
function fixture(over: Partial<BotRow> = {}): BotDetail {
  return {
    bot: { ...bot, ...over },
    chat_status: { state: "reopenable", blockers: [] },
  };
}
function activity(
  get = vi.fn().mockResolvedValue(fixture()),
  extra: Partial<React.ComponentProps<typeof BotChatActivity>> = {},
) {
  const onOpenSession = vi.fn();
  const onRefresh = vi.fn();
  const api = { get } as unknown as BotsApi;
  const view = render(
    <BotChatActivity
      api={api}
      botId="b1"
      sessionId="s1"
      main
      onOpenSession={onOpenSession}
      onRefresh={onRefresh}
      {...extra}
    />,
  );
  return { ...view, api, get, onOpenSession, onRefresh };
}
describe("BotChatActivity", () => {
  const memoryApi = {
    entries: vi
      .fn()
      .mockResolvedValue({ entries: [], partial: false, partial_reasons: [], omitted: 0 }),
    metadata: vi.fn(),
    note: vi.fn(),
  } as unknown as Pick<VaultApi, "entries" | "metadata" | "note">;

  it("opens memory only for the selected ordinary workspace Bot and returns focus on close", async () => {
    vi.mocked(memoryApi.entries).mockClear();
    const botId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const get = vi.fn().mockResolvedValue(fixture({ bot_id: botId, workspace_id: workspaceId }));
    activity(get, {
      botId,
      memory: {
        api: memoryApi,
        sessionId: "s1",
        botId,
        workspaceId,
        activeWorkspaceId: workspaceId,
        workspacePolicy: "ordinary",
        onOpenInVault: vi.fn(),
        onOpenUrl: vi.fn(),
      },
    });
    await act(async () => {});
    const opener = screen.getByRole("button", { name: "View memory" });
    opener.focus();
    fireEvent.click(opener);
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    await waitFor(
      () =>
        expect(memoryApi.entries).toHaveBeenCalledExactlyOnceWith(
          workspaceId,
          undefined,
          expect.any(AbortSignal),
        ),
      { timeout: 50 },
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("shows app-kept memory for a Bot in a protected active vault", async () => {
    vi.mocked(memoryApi.entries).mockClear();
    const botId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const get = vi.fn().mockResolvedValue(
      fixture({
        bot_id: botId,
        workspace_id: workspaceId,
        workspace: { name: "Main", policy: "protected", active: true },
      }),
    );
    activity(get, {
      botId,
      memory: {
        api: memoryApi,
        sessionId: "s1",
        botId,
        workspaceId,
        activeWorkspaceId: workspaceId,
        workspacePolicy: "protected",
        onOpenUrl: vi.fn(),
      },
    });
    await act(async () => {});
    expect(screen.getByText(/outside the vault/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View memory" }));
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    await waitFor(() => expect(memoryApi.entries).toHaveBeenCalledOnce(), { timeout: 50 });
    expect(screen.queryByRole("button", { name: "Open in Vault" })).toBeNull();
  });

  it("hides memory for absent, wrong-workspace, and stale session or Bot props", async () => {
    vi.mocked(memoryApi.entries).mockClear();
    const valid = {
      api: memoryApi,
      sessionId: "s1",
      botId: "b1",
      workspaceId: "w1",
      activeWorkspaceId: "w1",
      workspacePolicy: "ordinary",
      onOpenInVault: vi.fn(),
      onOpenUrl: vi.fn(),
    };
    const { rerender } = activity(undefined, { memory: valid });
    await act(async () => {});
    expect(screen.getByRole("button", { name: "View memory" })).toBeTruthy();
    for (const memory of [
      undefined,
      { ...valid, activeWorkspaceId: "w2" },
      { ...valid, sessionId: "old" },
      { ...valid, botId: "b2" },
      { ...valid, workspacePolicy: "protected" },
    ]) {
      rerender(
        <BotChatActivity
          api={{ get: vi.fn().mockResolvedValue(fixture()) } as unknown as BotsApi}
          botId="b1"
          sessionId="s1"
          main
          onOpenSession={vi.fn()}
          {...(memory ? { memory } : {})}
        />,
      );
      await act(async () => {});
      expect(screen.queryByRole("button", { name: "View memory" })).toBeNull();
    }
    expect(memoryApi.entries).not.toHaveBeenCalled();
    rerender(
      <BotChatActivity
        api={
          { get: vi.fn().mockResolvedValue(fixture({ workspace_id: "w2" })) } as unknown as BotsApi
        }
        botId="b1"
        sessionId="s1"
        main
        onOpenSession={vi.fn()}
        memory={valid}
      />,
    );
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "View memory" })).toBeNull();
    expect(memoryApi.entries).not.toHaveBeenCalled();
  });

  it("closes and cancels memory reads when the selected session changes", async () => {
    const botId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    let resolveEntries!: (value: unknown) => void;
    const entries = vi.fn(
      (_workspace: string, _query?: string, _signal?: AbortSignal) =>
        new Promise<unknown>((resolve) => (resolveEntries = resolve)),
    );
    const api = {
      entries,
      metadata: vi.fn(),
      note: vi.fn(),
    } as unknown as Pick<VaultApi, "entries" | "metadata" | "note">;
    const selected = {
      api,
      sessionId: "s1",
      botId,
      workspaceId,
      activeWorkspaceId: workspaceId,
      workspacePolicy: "ordinary",
      onOpenInVault: vi.fn(),
      onOpenUrl: vi.fn(),
    };
    const view = activity(
      vi.fn().mockResolvedValue(fixture({ bot_id: botId, workspace_id: workspaceId })),
      { botId, memory: selected },
    );
    const opener = await screen.findByRole("button", { name: "View memory" }, { timeout: 50 });
    fireEvent.click(opener);
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    expect(entries).toHaveBeenCalledOnce();
    expect(entries.mock.calls[0]?.[2]?.aborted).toBe(false);
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s2"
        main
        onOpenSession={view.onOpenSession}
        memory={selected}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(entries.mock.calls[0]?.[2]?.aborted).toBe(true);
    resolveEntries({ entries: [], partial: false, partial_reasons: [], omitted: 0 });
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={selected}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(entries).toHaveBeenCalledOnce();
  });

  it("clears open memory when workspace, API, or eligibility changes", async () => {
    const botId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const entries = vi.fn().mockResolvedValue({
      entries: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    });
    const api = { entries, metadata: vi.fn(), note: vi.fn() } as unknown as Pick<
      VaultApi,
      "entries" | "metadata" | "note"
    >;
    const otherEntries = vi.fn().mockResolvedValue({
      entries: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    });
    const otherApi = { entries: otherEntries, metadata: vi.fn(), note: vi.fn() } as unknown as Pick<
      VaultApi,
      "entries" | "metadata" | "note"
    >;
    const memory = {
      api,
      sessionId: "s1",
      botId,
      workspaceId,
      activeWorkspaceId: workspaceId,
      workspacePolicy: "ordinary" as const,
      onOpenInVault: vi.fn(),
      onOpenUrl: vi.fn(),
    };
    const view = activity(
      vi.fn().mockResolvedValue(fixture({ bot_id: botId, workspace_id: workspaceId })),
      { botId, memory },
    );
    const opener = await screen.findByRole("button", { name: "View memory" }, { timeout: 50 });
    fireEvent.click(opener);
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    expect(entries).toHaveBeenCalledOnce();

    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={{ ...memory, activeWorkspaceId: "w2" }}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={memory}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(entries).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "View memory" }));
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={{ ...memory, api: otherApi }}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(otherEntries).not.toHaveBeenCalled();
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={memory}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(entries).toHaveBeenCalledTimes(2);
    expect(otherEntries).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "View memory" }));
    expect(await screen.findByRole("dialog", { name: "Bot memory" }, { timeout: 50 })).toBeTruthy();
    expect(entries).toHaveBeenCalledTimes(3);
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={{ ...memory, workspacePolicy: "protected" }}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
        memory={memory}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(entries).toHaveBeenCalledTimes(3);
  });

  it("requires the selected session to remain the Bot main conversation", async () => {
    const botId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const entries = vi.fn();
    const memory = {
      api: { entries, metadata: vi.fn(), note: vi.fn() } as unknown as Pick<
        VaultApi,
        "entries" | "metadata" | "note"
      >,
      sessionId: "s1",
      botId,
      workspaceId,
      activeWorkspaceId: workspaceId,
      workspacePolicy: "ordinary" as const,
      onOpenInVault: vi.fn(),
      onOpenUrl: vi.fn(),
    };
    const get = vi
      .fn()
      .mockResolvedValue(
        fixture({ bot_id: botId, workspace_id: workspaceId, chat_session_id: "s-other" }),
      );
    const view = activity(get, { botId, memory });
    await act(async () => {});
    expect(screen.getByText("This Bot now uses a different conversation.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "View memory" })).toBeNull();
    expect(entries).not.toHaveBeenCalled();

    view.rerender(
      <BotChatActivity
        api={view.api}
        botId={botId}
        sessionId="s1"
        main={false}
        onOpenSession={view.onOpenSession}
        memory={memory}
      />,
    );
    expect(screen.getByRole("button", { name: "View memory" })).toBeTruthy();
  });

  it("shows slim activity without Bot identity, purpose, or navigation", async () => {
    const { container } = activity();
    await act(async () => {});
    expect(screen.getByRole("heading", { level: 2, name: "Bot activity" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Refresh activity" })).toHaveLength(1);
    const section = screen.getByRole("region", { name: "Bot activity" });
    expect(section.querySelector(".btn.txt.sm")).toBeTruthy();
    expect(section.querySelector(".btn.txt.sm")?.textContent).toBe("Refresh activity");
    for (const text of [
      "Planner",
      "Review tasks",
      "Open Bot",
      "Context & loadout",
      "Conversation history and context",
      "New routine",
    ])
      expect(section.textContent).not.toContain(text);
    expect(container.querySelector(".chip, .card")).toBeNull();
  });

  it("renders routines after state lines and before the latest run", async () => {
    const { container } = activity(
      vi.fn().mockResolvedValue(
        fixture({
          chat_session_id: "s-new",
          latest_delivery: {
            attempt_id: "a1",
            routine_id: "r1",
            session_id: "s-run",
            outcome: "succeeded",
            attention: "silent",
            reason: "show_all",
            state: "held",
            delivery_reason: "held",
            created_at: bot.created_at,
            updated_at: bot.updated_at,
          },
        }),
      ),
      { routines: <div>Routine slot</div> },
    );
    await act(async () => {});
    const section = screen.getByRole("region", { name: "Bot activity" });
    const text = section.textContent ?? "";
    expect(text.indexOf("This Bot now uses a different conversation.")).toBeLessThan(
      text.indexOf("Routine slot"),
    );
    expect(text.indexOf("Routine slot")).toBeLessThan(text.indexOf("Routine outcome: Succeeded"));
    expect(container.querySelector(".chip, .card")).toBeNull();
  });

  it("shows conversation mismatch only in the main Bot chat", async () => {
    const get = vi.fn().mockResolvedValue(fixture({ chat_session_id: "s-new" }));
    const view = activity(get, { main: false });
    await act(async () => {});
    expect(screen.queryByText("This Bot now uses a different conversation.")).toBeNull();
    view.rerender(
      <BotChatActivity
        api={{ get } as unknown as BotsApi}
        botId="b1"
        sessionId="s1"
        main
        onOpenSession={view.onOpenSession}
      />,
    );
    await act(async () => {});
    expect(screen.getByText("This Bot now uses a different conversation.")).toBeTruthy();
  });

  it("shows loading before its first answer and maps read failures to an alert", async () => {
    let reject!: (reason: unknown) => void;
    const get = vi.fn().mockImplementation(() => new Promise((_resolve, fail) => (reject = fail)));
    activity(get);
    expect(screen.getByText("Loading Bot activity…")).toBeTruthy();
    await act(async () => reject(new BotsApiError(503, "busy")));
    expect(screen.getByRole("alert").textContent).toBe(
      "This conversation is busy. Wait or stop its current turn before trying again.",
    );
  });

  it("shows latest receipt, omits off delivery, and opens its session", async () => {
    const receipt = {
      attempt_id: "a1",
      routine_id: "r1",
      session_id: "s-run",
      outcome: "succeeded",
      attention: "silent",
      reason: "show_all",
      state: "held",
      delivery_reason: "held",
      created_at: bot.created_at,
      updated_at: bot.updated_at,
    } as const;
    const get = vi.fn().mockResolvedValue(fixture({ latest_delivery: receipt }));
    const { api, onOpenSession, rerender } = activity(get);
    await act(async () => {});
    expect(screen.getByRole("region", { name: "Latest routine result" })).toBeTruthy();
    expect(screen.getByText("Routine outcome: Succeeded")).toBeTruthy();
    expect(screen.getByText("Mattermost delivery: Held")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open latest run chat" }));
    expect(onOpenSession).toHaveBeenCalledExactlyOnceWith("s-run");
    get.mockResolvedValue(fixture({ latest_delivery: { ...receipt, state: "off" } }));
    rerender(
      <BotChatActivity
        api={api}
        botId="b1"
        sessionId="s1"
        main
        refreshKey="off"
        onOpenSession={onOpenSession}
      />,
    );
    await act(async () => {});
    expect(screen.queryByText(/^Mattermost delivery:/)).toBeNull();
    get.mockResolvedValue(fixture());
    rerender(
      <BotChatActivity
        api={api}
        botId="b1"
        sessionId="s1"
        main
        refreshKey="none"
        onOpenSession={onOpenSession}
      />,
    );
    await act(async () => {});
    expect(screen.queryByRole("region", { name: "Latest routine result" })).toBeNull();
  });

  it("refreshes on click and changed refreshKey, not unchanged rerenders", async () => {
    const get = vi.fn().mockResolvedValue(fixture());
    const { api, onRefresh, rerender } = activity(get, { refreshKey: "r1" });
    await act(async () => {});
    expect(get).toHaveBeenCalledTimes(1);
    rerender(
      <BotChatActivity
        api={api}
        botId="b1"
        sessionId="s1"
        main
        refreshKey="r1"
        onOpenSession={vi.fn()}
        onRefresh={onRefresh}
      />,
    );
    await act(async () => {});
    expect(get).toHaveBeenCalledTimes(1);
    rerender(
      <BotChatActivity
        api={api}
        botId="b1"
        sessionId="s1"
        main
        refreshKey="r2"
        onOpenSession={vi.fn()}
        onRefresh={onRefresh}
      />,
    );
    await act(async () => {});
    expect(get).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Refresh activity" }));
    await act(async () => {});
    expect(onRefresh).toHaveBeenCalledExactlyOnceWith();
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("aborts old identity reads and ignores late answers", async () => {
    let resolveOld!: (value: BotDetail) => void;
    const get = vi
      .fn()
      .mockImplementationOnce(() => new Promise<BotDetail>((ready) => (resolveOld = ready)))
      .mockResolvedValueOnce(
        fixture({
          bot_id: "b2",
          chat_session_id: "s2",
          latest_delivery: {
            attempt_id: "a2",
            routine_id: "r2",
            session_id: "s-run-2",
            outcome: "failed",
            attention: "silent",
            reason: "show_all",
            state: "held",
            delivery_reason: "held",
            created_at: bot.created_at,
            updated_at: bot.updated_at,
          },
        }),
      );
    const api = { get } as unknown as BotsApi;
    const onOpenSession = vi.fn();
    const view = render(
      <BotChatActivity api={api} botId="b1" sessionId="s1" main onOpenSession={onOpenSession} />,
    );
    view.rerender(
      <BotChatActivity api={api} botId="b2" sessionId="s2" main onOpenSession={onOpenSession} />,
    );
    expect(get.mock.calls[0]?.[1]).toHaveProperty("aborted", true);
    await act(async () =>
      resolveOld(
        fixture({
          latest_delivery: {
            attempt_id: "a1",
            routine_id: "r1",
            session_id: "s-old",
            outcome: "succeeded",
            attention: "silent",
            reason: "show_all",
            state: "held",
            delivery_reason: "held",
            created_at: bot.created_at,
            updated_at: bot.updated_at,
          },
        }),
      ),
    );
    expect(screen.queryByText("Routine outcome: Succeeded")).toBeNull();
    expect(screen.getByText("Routine outcome: Failed")).toBeTruthy();
    expect(screen.queryByText("s-old")).toBeNull();
  });

  it("rejects a response for another Bot without showing its routine result", async () => {
    const receipt = {
      attempt_id: "foreign",
      routine_id: "r2",
      session_id: "foreign-session",
      outcome: "failed",
      attention: "silent",
      reason: "show_all",
      state: "held",
      delivery_reason: "held",
      created_at: bot.created_at,
      updated_at: bot.updated_at,
    } as const;
    activity(vi.fn().mockResolvedValue(fixture({ bot_id: "b2", latest_delivery: receipt })));
    await act(async () => {});
    expect(screen.getByRole("alert").textContent).toBe(
      "Bot activity is unavailable. Refresh to try again.",
    );
    expect(screen.queryByRole("region", { name: "Latest routine result" })).toBeNull();
  });

  it("keeps the newer routine result when a refresh returns an older Bot revision", async () => {
    const receipt = {
      attempt_id: "new",
      routine_id: "r1",
      session_id: "new-session",
      outcome: "succeeded",
      attention: "silent",
      reason: "show_all",
      state: "held",
      delivery_reason: "held",
      created_at: bot.created_at,
      updated_at: bot.updated_at,
    } as const;
    const get = vi
      .fn()
      .mockResolvedValueOnce(fixture({ state_revision: 2, latest_delivery: receipt }))
      .mockResolvedValueOnce(
        fixture({
          state_revision: 1,
          latest_delivery: { ...receipt, outcome: "failed" },
        }),
      );
    activity(get);
    await act(async () => {});
    expect(screen.getByText("Routine outcome: Succeeded")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh activity" }));
    await act(async () => {});
    expect(screen.getByText("Routine outcome: Succeeded")).toBeTruthy();
    expect(screen.queryByText("Routine outcome: Failed")).toBeNull();
  });
});

describe("BotDeliverySummary", () => {
  it("renders no study-era wording for an old stored show_all delivery reason", () => {
    const receipt = {
      attempt_id: "a1",
      routine_id: "r1",
      session_id: "s-run",
      outcome: "succeeded",
      attention: "silent",
      reason: "show_all",
      state: "held",
      delivery_reason: "held",
      created_at: bot.created_at,
      updated_at: bot.updated_at,
    } as const;
    render(
      <BotDeliverySummary bot={{ ...bot, latest_delivery: receipt }} onOpenSession={vi.fn()} />,
    );
    expect(screen.getByRole("region", { name: "Latest routine result" }).textContent).not.toMatch(
      /study|\bb0\b/i,
    );
    expect(screen.getByText("Shown because VIKI was showing every routine result.")).toBeTruthy();
  });
});

it("describes per-Bot memory in the conversation help", () => {
  render(<BotConversationHelp />);
  expect(screen.getByText(/each Bot has its own memory folder/)).toBeTruthy();
  expect(screen.queryByText(/provider memory behavior/)).toBeNull();
});
