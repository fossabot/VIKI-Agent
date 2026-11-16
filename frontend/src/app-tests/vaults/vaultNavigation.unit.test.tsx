/**
 * A rail tile opens the Vault view with its folder preselected, and the select keeps that folder
 * from the first render until the user changes it. A chat-only workspace leaves the tiles inert.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki } from "../../accept/m3b/fakes";
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

const listing = {
  entries: [
    { ref: "00-inbox/a.md", title: "a" },
    { ref: "30-knowledge/b.md", title: "b" },
    { ref: "40-sources/c.md", title: "c" },
  ],
  partial: false,
  partial_reasons: [],
  omitted: 0,
};

function vaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: (async () => listing) as unknown as VaultApi["entries"],
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

async function mount(chatOnly = false) {
  const { viki } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({ workspace: { shared_scratch: false } }),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture({ root_physical_id: "12:34" })]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: chatOnly,
    },
  });
  const rest = fakeRest({
    rails: { ...emptyRails(), counts: { knowledge: 3, sources: 2, inbox: 1 } },
  });
  render(
    <App viki={viki} rest={rest} durable={fakeDurable()} shell={shell} vaultApi={vaultApi()} />,
  );
  await act(async () => {});
}

const tile = (name: string) =>
  document.querySelector(`.tile[data-tile="${name}"]`) as HTMLButtonElement;

test.each([
  ["knowledge", "30-knowledge"],
  ["sources", "40-sources"],
  ["inbox", "00-inbox"],
])(
  "the %s tile opens the Vault view with %s selected, kept after the first render",
  async (name, folder) => {
    await mount();
    await act(async () => {
      fireEvent.click(tile(name));
    });
    const select = (await screen.findByLabelText("Folder")) as HTMLSelectElement;
    expect(select.value).toBe(folder);
    await act(async () => {});
    expect(select.value).toBe(folder);
    await act(async () => {
      fireEvent.change(select, { target: { value: "" } });
    });
    expect(select.value).toBe("");
  },
);

test("a tile of a chat-only workspace is inert", async () => {
  await mount(true);
  expect(tile("knowledge").disabled).toBe(true);
  await act(async () => {
    fireEvent.click(tile("knowledge"));
  });
  expect(screen.queryByLabelText("Folder")).toBeNull();
});
