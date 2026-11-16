/**
 * The inbox tile opens VIKI's capture triage once the app is wired with a CapturesApi, and
 * falls back to the plain Vault browser at 00-inbox without one.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { Capture, CaptureListing, CapturesApi } from "../../clients/vaults/capturesApi";
import { captureHandoffText, type TriageChoice } from "../../components/Captures/triage";
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

const vaultListing = {
  entries: [{ ref: "00-inbox/a.md", title: "a" }],
  partial: false,
  partial_reasons: [],
  omitted: 0,
};

function vaultApi(overrides: Partial<VaultApi> = {}): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: (async () => vaultListing) as unknown as VaultApi["entries"],
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
    ...overrides,
  };
}

function capture(overrides: Partial<Capture> = {}): Capture {
  return {
    ref: "00-inbox/a.md",
    revision: "a".repeat(64),
    modified: "2026-10-09T12:00:00.000Z",
    captured: null,
    preview: "A loose idea.",
    cut: false,
    unread: null,
    suggestion: null,
    ...overrides,
  };
}

function listing(overrides: Partial<CaptureListing> = {}): CaptureListing {
  return { captures: [], projects: [], partial: false, omitted: 0, run: null, ...overrides };
}

function fakeCapturesApi(result: CaptureListing): CapturesApi {
  return {
    list: async () => result,
    run: async () => null,
    async suggest() {
      throw new Error("suggest() is not used in this test");
    },
  };
}

async function mount(opts: { capturesApi?: CapturesApi; vault?: Partial<VaultApi> } = {}) {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({ workspace: { shared_scratch: false } }),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture()]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  const rest = fakeRest();
  const utils = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      shell={shell}
      vaultApi={vaultApi(opts.vault)}
      {...(opts.capturesApi ? { capturesApi: opts.capturesApi } : {})}
    />,
  );
  act(() => socket.open());
  await act(async () => {});
  return { ...utils, socket };
}

const tile = () => document.querySelector('.tile[data-tile="inbox"]') as HTMLButtonElement;
const box = () => screen.getByLabelText<HTMLTextAreaElement>("Message");

test("the inbox tile opens the triage, not the Vault, when capturesApi is wired", async () => {
  await mount({ capturesApi: fakeCapturesApi(listing()) });
  await act(async () => {
    fireEvent.click(tile());
  });
  expect(screen.getByRole("dialog", { name: "Inbox triage" })).toBeTruthy();
  expect(screen.queryByLabelText(/^Folder/)).toBeNull();
});

test("without capturesApi the inbox tile opens the Vault at 00-inbox", async () => {
  await mount();
  await act(async () => {
    fireEvent.click(tile());
  });
  const select = (await screen.findByLabelText(/^Folder/)) as HTMLSelectElement;
  expect(select.value).toBe("00-inbox");
  expect(screen.queryByRole("dialog", { name: "Inbox triage" })).toBeNull();
});

test("Discard then File puts the exact hand-off text in a new composer, and sends nothing", async () => {
  const theCapture = capture();
  const { socket } = await mount({
    capturesApi: fakeCapturesApi(listing({ captures: [theCapture] })),
  });
  await act(async () => {
    fireEvent.click(tile());
  });
  const dialog = await screen.findByRole("dialog", { name: "Inbox triage" });
  const discard = await within(dialog).findByRole("button", { name: "Discard" });
  await act(async () => {
    fireEvent.click(discard);
  });
  await act(async () => {
    fireEvent.click(within(dialog).getByRole("button", { name: "File 1 with VIKI" }));
  });

  const choices: TriageChoice[] = [
    {
      ref: theCapture.ref,
      revision: theCapture.revision,
      modified: theCapture.modified,
      destination: { to: "discard", project: null },
      via: "owner",
    },
  ];
  expect(box().value).toBe(captureHandoffText(choices));
  expect(socket.sent.filter((message) => message.type === "turn.send")).toEqual([]);
  expect(screen.queryByRole("dialog", { name: "Inbox triage" })).toBeNull();
});

test("Triage inbox in the Vault Overview opens Inbox triage with no Browse in Vault, and Open note opens the note and closes the dialog", async () => {
  const theCapture = capture();
  await mount({
    capturesApi: fakeCapturesApi(listing({ captures: [theCapture] })),
    vault: {
      overview: async () => ({
        notes: [
          {
            ref: "30-knowledge/x.md",
            state: "ready",
            title: "X",
            type: null,
            status: null,
            confidence: null,
            tags: null,
            created: null,
            modified: null,
            summary: null,
            links: null,
            complete: true,
          },
        ],
        partial: false,
        partial_reasons: [],
        omitted: 0,
        read_bytes: 0,
      }),
    },
  });
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
  });
  const triage = await screen.findByRole("button", { name: "Triage inbox" });
  await act(async () => {
    fireEvent.click(triage);
  });
  const dialog = await screen.findByRole("dialog", { name: "Inbox triage" });
  expect(within(dialog).queryByRole("button", { name: "Browse in Vault" })).toBeNull();
  await act(async () => {
    fireEvent.click(within(dialog).getByRole("button", { name: "Open note" }));
  });
  expect(screen.queryByRole("dialog", { name: "Inbox triage" })).toBeNull();
});
