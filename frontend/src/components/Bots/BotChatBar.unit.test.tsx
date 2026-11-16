import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { VaultApi } from "../../state/vaultApi";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { BotChatBar } from "./BotChatBar";

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
    revision: 3,
    archived: false,
    identity: {
      name: "Main Bot",
      purpose: "Review tasks",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
    },
  },
  workspace: { name: "Main", policy: "ordinary", active: true },
  chat: { profile_revision: 3, started: true, archived: false },
  routine_ids: [],
  delivery: { enabled: false, available: false, reason: "not_connected" },
  latest_delivery: null,
};

const identity = {
  ...DEFAULT_IDENTITY,
  profile: {
    name: "Main Bot",
    avatarId: null,
    accent: "blue" as const,
    shape: "circle" as const,
    revision: 3,
    botProfileId: "p1",
  },
};

function renderBar(props: { main: boolean; role?: string } = { main: true }) {
  const onManage = vi.fn();
  const view = render(
    <IdentityContext.Provider value={identity}>
      <BotChatBar bot={bot} {...props} onManage={onManage} />
    </IdentityContext.Provider>,
  );
  return { ...view, onManage };
}

test("shows the main Bot name, kind and pinned revision", () => {
  const { container } = renderBar({ main: true });
  expect(screen.getByRole("navigation", { name: "Bot chat" })).toBeTruthy();
  expect(container.querySelector("strong")?.textContent).toBe("Main Bot");
  expect(screen.getByText("Main Bot chat", { exact: true })).toBeTruthy();
  expect(container.querySelector(".bot-chat-meta")?.textContent).toBe("Main Bot chat · r3");
});

test("shows the task-thread kind and pinned revision", () => {
  const { container } = renderBar({ main: false });
  expect(screen.getByText("Task thread", { exact: true })).toBeTruthy();
  expect(container.querySelector(".bot-chat-meta")?.textContent).toBe("Task thread · r3");
});

test("shows the seat role only when given", () => {
  const { container, rerender } = renderBar({ main: true, role: "Front door" });
  expect(container.querySelector(".bots-role")?.textContent).toBe("Front door");
  rerender(
    <IdentityContext.Provider value={identity}>
      <BotChatBar bot={bot} main onManage={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".bots-role")).toBeNull();
});

test("omits revision when identity is unresolved", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...identity, unresolved: true }}>
      <BotChatBar bot={bot} main onManage={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".bot-chat-meta")?.textContent).toBe("Main Bot chat");
});

test("Chats & routines keeps its callback without an All Bots button", () => {
  const { onManage } = renderBar({ main: true });
  fireEvent.click(screen.getByRole("button", { name: "Chats & routines" }));
  expect(onManage).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: "All Bots" })).toBeNull();
});

const vaultApi = {
  entries: vi
    .fn()
    .mockResolvedValue({ entries: [], partial: false, partial_reasons: [], omitted: 0 }),
  note: vi.fn(),
  metadata: vi.fn(),
} as unknown as VaultApi;
const memory = {
  api: vaultApi,
  sessionId: "s1",
  botId: "b1",
  workspaceId: "w1",
  activeWorkspaceId: "w1",
  workspacePolicy: "ordinary",
  onOpenInVault: vi.fn(),
  onOpenUrl: vi.fn(),
};

function renderWithMemory(over: Partial<typeof memory> = {}) {
  return render(
    <IdentityContext.Provider value={identity}>
      <BotChatBar bot={bot} main memory={{ ...memory, ...over }} onManage={vi.fn()} />
    </IdentityContext.Provider>,
  );
}

test("Memory opens the memory dialog for this Bot", async () => {
  renderWithMemory();
  fireEvent.click(screen.getByRole("button", { name: "Memory" }));
  expect(await screen.findByRole("dialog", { name: "Bot memory" })).toBeTruthy();
});

test("Memory is absent without memory access, for another Bot, or off the active ordinary vault", () => {
  renderBar({ main: true });
  expect(screen.queryByRole("button", { name: "Memory" })).toBeNull();
  cleanup();
  for (const over of [
    { botId: "other" },
    { activeWorkspaceId: "w2" },
    { workspacePolicy: "protected" },
  ]) {
    renderWithMemory(over);
    expect(screen.queryByRole("button", { name: "Memory" })).toBeNull();
    cleanup();
  }
});

test("Memory shows the app-kept memory of a Bot in a protected active vault", async () => {
  const botId = "22222222-2222-4222-8222-222222222222";
  const workspaceId = "11111111-1111-4111-8111-111111111111";
  const protectedBot = {
    ...bot,
    bot_id: botId,
    workspace_id: workspaceId,
    workspace: { name: "Main", policy: "protected" as const, active: true },
  };
  const { onOpenInVault: _vaultOnly, ...appMemory } = memory;
  vi.mocked(vaultApi.entries).mockClear();
  render(
    <IdentityContext.Provider value={identity}>
      <BotChatBar
        bot={protectedBot}
        main
        memory={{
          ...appMemory,
          botId,
          workspaceId,
          activeWorkspaceId: workspaceId,
          workspacePolicy: "protected",
        }}
        onManage={vi.fn()}
      />
    </IdentityContext.Provider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Memory" }));
  expect(await screen.findByRole("dialog", { name: "Bot memory" })).toBeTruthy();
  expect(await screen.findByText(/outside the vault/)).toBeTruthy();
  await waitFor(() =>
    expect(vaultApi.entries).toHaveBeenCalledExactlyOnceWith(
      workspaceId,
      undefined,
      expect.any(AbortSignal),
    ),
  );
  expect(screen.queryByRole("button", { name: "Open in Vault" })).toBeNull();
});

test("a closed-off Memory dialog does not come back when the context returns", async () => {
  const bar = (over: Partial<typeof memory> = {}) => (
    <IdentityContext.Provider value={identity}>
      <BotChatBar bot={bot} main memory={{ ...memory, ...over }} onManage={vi.fn()} />
    </IdentityContext.Provider>
  );
  const view = render(bar());
  fireEvent.click(screen.getByRole("button", { name: "Memory" }));
  expect(await screen.findByRole("dialog", { name: "Bot memory" })).toBeTruthy();

  view.rerender(bar({ workspacePolicy: "protected" }));
  expect(screen.queryByRole("dialog", { name: "Bot memory" })).toBeNull();
  view.rerender(bar());
  expect(screen.queryByRole("dialog", { name: "Bot memory" })).toBeNull();
  expect(screen.getByRole("button", { name: "Memory" })).toBeTruthy();
});

test("an open Memory dialog closes when the chat or workspace it was opened for changes", async () => {
  const bar = (over: Partial<typeof memory> = {}) => (
    <IdentityContext.Provider value={identity}>
      <BotChatBar bot={bot} main memory={{ ...memory, ...over }} onManage={vi.fn()} />
    </IdentityContext.Provider>
  );
  const view = render(bar());
  fireEvent.click(screen.getByRole("button", { name: "Memory" }));
  expect(await screen.findByRole("dialog", { name: "Bot memory" })).toBeTruthy();
  view.rerender(bar({ sessionId: "s2" }));
  expect(screen.queryByRole("dialog", { name: "Bot memory" })).toBeNull();
  view.rerender(bar());
  expect(screen.queryByRole("dialog", { name: "Bot memory" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Memory" }));
  expect(await screen.findByRole("dialog", { name: "Bot memory" })).toBeTruthy();
  view.rerender(bar({ workspaceId: "w2", activeWorkspaceId: "w2" }));
  expect(screen.queryByRole("dialog", { name: "Bot memory" })).toBeNull();
});
