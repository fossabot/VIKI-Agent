/**
 * What App decides about the setup guide: it opens by itself on a first launch, Help › Setup guide
 * reopens it (one more capabilities read, Back returns to the chat), the guide's two actions
 * reach the open.url command and the login client, and a recovery window offers no row.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { ProviderLoginApi } from "../../clients/app/providerLoginApi";
import { fakeDurable } from "../../testDurable";
import {
  type CapabilitiesOverrides,
  capabilitiesFixture,
  fakeShellApi,
  settingsFixture,
} from "../../testShell";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

function mount(options: {
  capabilities?: CapabilitiesOverrides;
  completed?: boolean;
  providerLoginApi?: ProviderLoginApi;
}) {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture(options.capabilities ?? {}),
    settings: settingsFixture({ "onboarding.completed": options.completed ?? true }),
  });
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      {...(options.providerLoginApi ? { providerLoginApi: options.providerLoginApi } : {})}
    />,
  );
  return { shell, socket };
}

async function settled(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const capabilityReads = (shell: ReturnType<typeof mount>["shell"]): number =>
  shell.calls.filter((call) => call.method === "capabilities").length;

async function openSetupGuide(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Help" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: "Setup guide" }));
  });
}

test("a first launch opens the guide: the steps list and the three vault cards", async () => {
  mount({ completed: false });
  await settled();
  const steps = screen.getByRole("list", { name: "Setup steps" });
  expect(steps.querySelectorAll("li.setup-step")).toHaveLength(3);
  expect(screen.getByRole("group", { name: "Create or open a vault" })).toBeTruthy();
});

test("Help › Setup guide reopens the guide with one more capabilities read; Back returns to the chat", async () => {
  const { shell } = mount({});
  await settled();
  expect(screen.queryByRole("list", { name: "Setup steps" })).toBeNull();
  const reads = capabilityReads(shell);

  await openSetupGuide();
  await settled();
  expect(screen.getByRole("list", { name: "Setup steps" })).toBeTruthy();
  expect(capabilityReads(shell)).toBe(reads + 1);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
  });
  expect(screen.queryByRole("list", { name: "Setup steps" })).toBeNull();
});

test("with Claude Code missing, Anthropic's setup guide is an open.url command", async () => {
  const { socket } = mount({
    completed: false,
    capabilities: { provider: { cli: "missing", can_start: false } },
  });
  act(() => socket.open());
  await settled();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Anthropic's setup guide" }));
  });
  expect(socket.sent).toContainEqual({
    type: "open.url",
    url: "https://code.claude.com/docs/en/setup",
  });
});

test("Sign in in the guide calls the login client once", async () => {
  const open = vi.fn().mockResolvedValue(undefined);
  mount({
    completed: false,
    capabilities: { provider: { login: "unknown" } },
    providerLoginApi: { open },
  });
  await settled();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  });
  expect(open).toHaveBeenCalledTimes(1);
});

test("a recovery window has no Setup guide row", async () => {
  mount({ capabilities: { app: { mode: "recovery" }, storage: { state: "unavailable" } } });
  await settled();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Help" }));
  });
  expect(screen.getByRole("menu", { name: "Help" })).toBeTruthy();
  expect(screen.queryByRole("menuitem", { name: "Setup guide" })).toBeNull();
});
