/**
 * App-level wiring for the Agents view's optional Native and Tools sections: each tab shows only
 * when its client is supplied, creating a profile from a native agent returns to the Profiles
 * tab, and the Tools chat select lists the window's own live sessions.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { InventoryApi } from "../../clients/conversations/inventoryApi";
import type {
  NativeAssetsApi,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { Envelope } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
});

function emptyProfileApi(overrides: Partial<ProfileApi> = {}): ProfileApi {
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [],
    get: pending,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
    ...overrides,
  };
}

const USER_SOURCE: NativeSource = { source_id: "claude:user", provider: "claude", scope: "user" };

function fakeNativeAssetsApi(): NativeAssetsApi {
  const catalog: NativeCatalog = {
    source_id: "claude:user",
    provider: "claude",
    source_scope: "user",
    assets: [
      {
        source_id: "claude:user",
        source_scope: "user",
        provider: "claude",
        relative_id: ".claude/agents/example-agent.md",
        kind: "agent",
        name: "example-agent",
        description: null,
        model: null,
        tools: [],
        digest: "a".repeat(64),
        observed_at: "2026-09-28T00:00:00.000Z",
        supported_fields: ["name"],
        unsupported_fields: [],
        launch_supported: false,
      },
    ],
    partial: false,
    partial_reasons: [],
    scanned_entries: 1,
    scanned_bytes: 64,
    omitted: 0,
  };
  return {
    sources: async () => [USER_SOURCE],
    catalog: async () => catalog,
  };
}

function fakeInventoryApi(): InventoryApi {
  return { get: () => new Promise<never>(() => {}) };
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

function makeLive(socket: { receive: (message: unknown) => void }, sessionId: string): void {
  act(() => socket.receive(vaultSessions(1, [sessionId])));
  act(() => socket.receive(vaultSessions(2, [sessionId])));
}

async function openAgentsSection(label: string): Promise<void> {
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  fireEvent.click(await screen.findByRole("tab", { name: label }));
}

test("the Native and Tools tabs stay hidden without their clients", async () => {
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} profileApi={emptyProfileApi()} />);
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await screen.findByRole("tab", { name: "Profiles" });
  expect(screen.queryByRole("tab", { name: "Subagents & skills" })).toBeNull();
  expect(screen.queryByRole("tab", { name: "Tools" })).toBeNull();
  viki.client.close();
});

test("the Native and Tools tabs appear when their clients are supplied", async () => {
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileApi={emptyProfileApi()}
      nativeAssetsApi={fakeNativeAssetsApi()}
      inventoryApi={fakeInventoryApi()}
    />,
  );
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await screen.findByRole("tab", { name: "Subagents & skills" });
  expect(screen.getByRole("tab", { name: "Tools" })).not.toBeNull();
  viki.client.close();
});

test("creating a profile from a native agent returns Agents to the Profiles tab", async () => {
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  const create = vi.fn(async () => ({ profile_id: "created-1", revision: 1, state_revision: 1 }));
  render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileApi={emptyProfileApi({ create })}
      nativeAssetsApi={fakeNativeAssetsApi()}
    />,
  );
  await openAgentsSection("Subagents & skills");
  const button = await screen.findByRole("button", {
    name: "Create an app profile from this agent",
  });
  fireEvent.click(button);
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe(
      "true",
    ),
  );
  viki.client.close();
});

test("the Tools chat select lists this window's own live sessions", async () => {
  const SESSION = "live-9";
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SESSION, title: "Live chat" })] });
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileApi={emptyProfileApi()}
      inventoryApi={fakeInventoryApi()}
    />,
  );
  act(() => socket.open());
  makeLive(socket, SESSION);
  await openAgentsSection("Tools");
  expect(await screen.findByRole("option", { name: "Live chat" })).not.toBeNull();
  viki.client.close();
});
