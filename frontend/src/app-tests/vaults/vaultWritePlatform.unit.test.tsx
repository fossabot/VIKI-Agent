/**
 * The Vault view offers its write control on every OS: `writeScope` (App.tsx) gates on the
 * workspace's own admission (ordinary, persistent, bootstrap complete, reachable, with a
 * physical id), not on `platformKind`. Edit note and Save render only inside a selected,
 * opened note, so "New note" stands in for the one `write` prop all three controls hang off.
 * `navigator.platform` is read once per render through `platformKind`, so it must be set before
 * the Vault tab opens.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { VaultApi } from "../../state/vaultApi";
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
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

function vaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
  };
}

function mount() {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({}),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture({ root_physical_id: "12:34" })]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  const rest = fakeRest({});
  const utils = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} shell={shell} vaultApi={vaultApi()} />,
  );
  return { ...utils, viki, socket };
}

/** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property is
 * removed afterwards rather than restored — there is no own descriptor to put back. */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

async function openVault(): Promise<void> {
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
  });
}

test("on macOS, the Vault view offers a write control", async () => {
  const restore = setPlatform("MacIntel");
  try {
    mount();
    await openVault();
    expect(screen.getByRole("button", { name: "New note" })).not.toBeNull();
  } finally {
    restore();
  }
});

test("on Linux, the Vault view offers a write control", async () => {
  const restore = setPlatform("Linux x86_64");
  try {
    mount();
    await openVault();
    expect(screen.getByRole("button", { name: "New note" })).not.toBeNull();
  } finally {
    restore();
  }
});

test("on the default jsdom platform, the Vault view offers a write control", async () => {
  mount();
  await openVault();
  expect(screen.getByRole("button", { name: "New note" })).not.toBeNull();
});
