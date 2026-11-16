/**
 * The note actions, the recovery panel and keep-in-vault hang off the one Save gate, so they
 * render on every OS, for a persistent, bootstrapped, reachable ordinary vault with a physical
 * id. The memory panel has three halves: its full actions behind that gate, only
 * Propose for a protected vault on any platform, and nothing otherwise. Nothing here is offered
 * that the server would only refuse.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { AssetsApi } from "../../clients/media/assetsApi";
import type { VaultApi } from "../../state/vaultApi";
import type { VaultKnowledgeApi } from "../../state/vaultKnowledgeApi";
import type { VaultOpsApi } from "../../state/vaultOpsApi";
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
  delete (window.navigator as { platform?: string }).platform;
});

const NOTE = "n.md";
const complete = (body: Record<string, unknown>) => ({
  ...body,
  partial: false,
  partial_reasons: [],
  omitted: 0,
});

function vaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: (async () => complete({ entries: [{ ref: NOTE, title: "n" }] })) as never,
    note: (async () =>
      complete({
        note: {
          ref: NOTE,
          title: "n",
          text: "text",
          source: "text",
          sha256: "a".repeat(64),
          frontmatter_error: null,
        },
        resolved_links: [],
      })) as never,
    backlinks: (async () => complete({ backlinks: [] })) as never,
    history: (async () => complete({ commits: [], state: "ready" })) as never,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
  };
}

const opsApi = (): VaultOpsApi => ({
  preview: vi.fn(),
  apply: vi.fn(),
  recovery: vi.fn(async () => ({ entries: [], stuck: [] })),
  forgetOperation: vi.fn(),
  resolve: vi.fn(),
});

const knowledgeApi = (): VaultKnowledgeApi =>
  ({
    memories: vi.fn(async () => ({ items: [], invalid: [], partial: false })),
    composeMemory: vi.fn(async () => ({ text: "preview" })),
    keepAttachment: vi.fn(),
  }) as unknown as VaultKnowledgeApi;

const assetsApi = (): AssetsApi => ({
  upload: vi.fn(async () => ({
    asset_id: "123e4567-e89b-12d3-a456-426614174001",
    media_type: "image/png" as const,
    byte_size: 2,
    width: 2,
    height: 2,
    name: "shot.png",
  })),
  read: () => new Promise(() => {}),
});

type Workspace = Parameters<typeof workspaceFixture>[0] & { noPhysicalId?: boolean };

function mount(platform: string, overrides: Workspace = {}, ops: VaultOpsApi = opsApi()) {
  Object.defineProperty(window.navigator, "platform", { value: platform, configurable: true });
  const { noPhysicalId, ...rest } = overrides;
  const workspace = workspaceFixture({ root_physical_id: "12:34", ...rest });
  if (noPhysicalId) delete workspace.root_physical_id;
  const { viki } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({}),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspace]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  const assets = assetsApi();
  render(
    <App
      viki={viki}
      rest={fakeRest({})}
      durable={fakeDurable()}
      shell={shell}
      assets={assets}
      vaultApi={vaultApi()}
      vaultOpsApi={ops}
      vaultKnowledgeApi={knowledgeApi()}
    />,
  );
  return assets;
}

async function attachImage(assets: AssetsApi): Promise<void> {
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, {
      target: { files: [new File([new Uint8Array([1, 2])], "shot.png", { type: "image/png" })] },
    });
  });
  await waitFor(() => expect(assets.upload).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function openNote(): Promise<void> {
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
  });
  fireEvent.click(await screen.findByRole("button", { name: "n" }));
  await screen.findByRole("heading", { name: "n" });
}

const keep = () => screen.queryByRole("button", { name: "Save to vault" });
const actions = () => screen.queryByRole("button", { name: "Note actions" });
const recovery = () => screen.queryByRole("region", { name: "Recovery" });
const memory = () => screen.queryByRole("region", { name: "Memory" });
const create = () => screen.queryByRole("button", { name: "Create" });
const propose = () => screen.queryByRole("button", { name: "Propose" });

type Memory = "full" | "propose" | "none";
const CASES: [name: string, platform: string, ws: Workspace, writes: boolean, memory: Memory][] = [
  ["Windows, an ordinary admitted vault", "Win32", {}, true, "full"],
  ["macOS, an ordinary vault", "MacIntel", {}, true, "full"],
  ["Linux, an ordinary vault", "Linux x86_64", {}, true, "full"],
  ["Windows, a protected vault", "Win32", { policy: "protected" }, false, "propose"],
  ["macOS, a protected vault", "MacIntel", { policy: "protected" }, false, "propose"],
  ["Linux, a protected vault", "Linux x86_64", { policy: "protected" }, false, "propose"],
  [
    "a protected vault without a physical id",
    "Win32",
    { policy: "protected", noPhysicalId: true },
    false,
    "none",
  ],
  ["Windows, a read-only vault", "Win32", { policy: "existing_readonly" }, false, "none"],
  [
    "Windows, a vault whose bootstrap is incomplete",
    "Win32",
    { bootstrap: "pending" },
    false,
    "none",
  ],
  ["Windows, a vault that is not persistent", "Win32", { persistent: false }, false, "none"],
  [
    "Windows, an ordinary vault without a physical id",
    "Win32",
    { noPhysicalId: true },
    false,
    "none",
  ],
];

describe("vault write surfaces follow the Save gate", () => {
  it.each(CASES)("%s", async (_name, platform, ws, writes, expectedMemory) => {
    const assets = mount(platform, ws);
    await attachImage(assets);
    if (writes) expect(keep()).not.toBeNull();
    else expect(keep()).toBeNull();

    await openNote();
    await act(async () => {});
    if (writes) {
      expect(actions()).not.toBeNull();
      expect(recovery()).not.toBeNull();
    } else {
      expect(actions()).toBeNull();
      expect(recovery()).toBeNull();
    }

    if (expectedMemory === "none") expect(memory()).toBeNull();
    else expect(memory()).not.toBeNull();
    if (expectedMemory !== "none") {
      // The create or propose button appears only once a draft has been previewed.
      fireEvent.change(screen.getByRole("textbox", { name: "Content" }), {
        target: { value: "x" },
      });
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Review" })));
    }
    if (expectedMemory === "full") {
      expect(create()).not.toBeNull();
      expect(propose()).toBeNull();
    } else if (expectedMemory === "propose") {
      expect(propose()).not.toBeNull();
      expect(create()).toBeNull();
    }
  });
});

// A save or operation row left for a vault whose folder is gone freezes every registration change,
// and clearing it needs no folder. So a stranded ordinary vault gets a panel that only
// forgets records; no other write surface follows it, on every OS.
describe("the forget-only panel follows the stranded scope", () => {
  const STUCK = {
    operation_id: "00000000-0000-4000-8000-0000000000bb",
    kind: "move" as const,
    state: "recovery_required",
    paths: ["a.md", "b.md"],
  };
  const forgetOnlyNote = () => screen.queryByText(/The vault's folder can't be opened/);
  const stuckOps = () => {
    const ops = opsApi();
    ops.recovery = vi.fn(async () => ({ entries: [], stuck: [STUCK] }));
    return ops;
  };
  async function openVault(): Promise<void> {
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
          "button",
          { name: "Vault" },
        ),
      );
    });
    await act(async () => {});
  }

  const STRANDED: [name: string, platform: string][] = [
    ["Windows", "Win32"],
    ["macOS", "MacIntel"],
    ["Linux", "Linux x86_64"],
  ];
  it.each(STRANDED)(
    "shows it for a stranded ordinary vault on %s, with no other write surface",
    async (_name, platform) => {
      mount(platform, { root_available: false }, stuckOps());
      await openVault();
      expect(forgetOnlyNote()).not.toBeNull();
      expect(screen.getByText("a.md → b.md")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Discard record" })).toBeTruthy();
      expect(screen.queryByText("Deleted notes")).toBeNull();
      expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Delete previous version" })).toBeNull();
      expect(screen.queryByRole("button", { name: "New note" })).toBeNull();
      expect(memory()).toBeNull();
      expect(keep()).toBeNull();
      fireEvent.click(await screen.findByRole("button", { name: "n" }));
      await screen.findByRole("heading", { name: "n" });
      expect(actions()).toBeNull();
      expect(screen.queryByRole("button", { name: "Edit note" })).toBeNull();
    },
  );

  const NOT_STRANDED: [name: string, platform: string, ws: Workspace][] = [
    ["a protected vault", "Win32", { policy: "protected", root_available: false }],
    ["a vault that is not persistent", "Win32", { persistent: false, root_available: false }],
    [
      "a vault whose bootstrap is incomplete",
      "Win32",
      { bootstrap: "pending", root_available: false },
    ],
    ["a vault whose folder is available", "Win32", {}],
  ];
  it.each(NOT_STRANDED)("shows none for %s", async (_name, platform, ws) => {
    mount(platform, ws, stuckOps());
    await openVault();
    expect(forgetOnlyNote()).toBeNull();
  });

  it("keeps the full panel for an available vault", async () => {
    mount("Win32", {}, stuckOps());
    await openVault();
    expect(recovery()).not.toBeNull();
    expect(screen.getByText("Deleted notes")).toBeTruthy();
  });
});
