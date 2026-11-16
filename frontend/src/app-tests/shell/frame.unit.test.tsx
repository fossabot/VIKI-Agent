/**
 * The frame as App wires it: the tab strip as the header's title row, the git chip leading the
 * status bar (the vault's, or the selected chat's own code folder's), and a changed file opened
 * from that chip in the center pane. GitChip, StatusBar, ConversationHeader and SessionTabs have
 * their own suites; this one pins what App passes them.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki, localIso, sessionRow } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import { type RootsApi, RootsApiError } from "../../clients/code/rootsApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { Rails } from "../../protocol";
import { resetRoots } from "../../state/sessionRoots";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CODE_ID = "55555555-5555-4555-8555-555555555555";

beforeEach(() => {
  window.sessionStorage.clear();
  resetRoots();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  resetRoots();
});

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

/** Selects a listed session from its rail row, as the user does. */
async function openRow(id: string): Promise<void> {
  await settled();
  const row = document.querySelector(`.sess[data-session-id="${id}"]`);
  if (!row) throw new Error("the session row has not arrived");
  await act(async () => {
    fireEvent.click(row);
  });
}

test("with organization, the selected tab carries the one title inside the header", async () => {
  const organization: SessionMetadataApi = {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest({ sessions: [sessionRow({ session_id: SID, title: "Alpha" })] })}
      durable={fakeDurable()}
      organization={organization}
    />,
  );
  act(() => fake.socket.open());
  await openRow(SID);

  const titles = document.querySelectorAll(".conversation-title");
  expect(titles).toHaveLength(1);
  expect(titles[0]?.textContent).toBe("Alpha");
  const tab = titles[0]?.closest('.session-tab[data-selected="true"]');
  expect(tab).toBeInstanceOf(HTMLElement);
  expect(tab?.closest('[data-region="conversation-header"]')).toBeInstanceOf(HTMLElement);
  const strips = [...document.querySelectorAll(".session-tabs")];
  expect(strips).toHaveLength(1);
  expect(strips.filter((strip) => !strip.closest('[data-region="conversation-header"]'))).toEqual(
    [],
  );
});

test("a vault window's git chip leads the status bar with its last commit, and the top bar has none", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(localIso(2026, 9, 12, 20, 0)));
  const rails: Rails = {
    ...emptyRails(),
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: localIso(2026, 9, 12, 19, 16),
      dirty: 3,
    },
  };
  const { container } = render(
    <App viki={fakeViki().viki} rest={fakeRest({ rails })} durable={fakeDurable()} />,
  );

  const leadingChip = () =>
    container
      .querySelector('[data-region="statusbar"]')
      ?.firstElementChild?.querySelector(".git-chip")?.textContent ?? "";
  await waitFor(() => expect(leadingChip()).toContain("main · dirty · c8868e6"));
  expect(container.querySelectorAll(".git-chip")).toHaveLength(1);
  expect(container.querySelector('[data-region="topbar"] .git-chip')).toBeNull();
  expect(screen.getByText("last commit c8868e6 19:16")).toBeTruthy();
});

test("a chat rooted in a code folder shows that folder's chip, and its changed file opens in the center", async () => {
  const vault = workspaceFixture();
  const folder = workspaceFixture({
    workspace_id: CODE_ID,
    kind: "code",
    name: "app code",
    root: "D:\\Code\\app",
    root_physical_id: "physical-app",
  });
  const stored = settingsFixture({ "onboarding.completed": true });
  const shell = fakeShellApi({
    settings: stored,
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: vault.workspace_id,
          kind: "vault",
          name: vault.name,
          policy: vault.policy,
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
      },
    }),
    workspaces: {
      ...listingFixture([vault, folder]),
      active_id: vault.workspace_id,
      chat_only: false,
    },
  });
  const pending = () => new Promise<never>(() => {});
  const code: CodeApi = {
    tree: vi.fn(pending),
    file: vi.fn(pending),
    search: vi.fn(pending),
    diff: vi.fn(pending),
    save: vi.fn(pending),
    changes: vi.fn<CodeApi["changes"]>(async () => ({
      state: "ready",
      branch: "feat/x",
      head: null,
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      entries: [
        {
          path: "src/a.ts",
          orig_path: null,
          index: ".",
          worktree: "M",
          kind: "tracked",
          private: false,
        },
      ],
      truncated: false,
    })),
  };
  const roots: RootsApi = {
    roots: async () => ({
      roots: [{ session_id: SID, workspace_id: CODE_ID, worktree_id: null }],
      truncated: false,
    }),
    worktrees: async () => ({ worktrees: [], truncated: false }),
    record: vi.fn(),
    create: vi.fn(),
    remove: vi.fn(),
    archives: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
    cleanup: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
    restore: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
  };
  const fake = fakeViki();
  const { container } = render(
    <App
      viki={fake.viki}
      rest={fakeRest({ sessions: [sessionRow({ session_id: SID, title: "Code chat" })] })}
      durable={fakeDurable()}
      shell={shell}
      codeApi={code}
      rootsApi={roots}
    />,
  );
  act(() => fake.socket.open());
  await openRow(SID);

  const chip = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-region="statusbar"] .git-chip');
    expect(found?.textContent ?? "").toContain("feat/x · 1 changed");
    return found as HTMLElement;
  });
  expect(code.changes).toHaveBeenCalledWith(CODE_ID, expect.anything());
  expect(container.querySelector('[data-region="center-pane"]')).toBeNull();
  await act(async () => {
    fireEvent.click(chip);
  });
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Git" })).getByRole("button", {
        name: /src\/a\.ts/,
      }),
    );
  });
  expect(container.querySelector('[data-region="center-pane"]')).not.toBeNull();
  expect(code.file).toHaveBeenCalledWith(CODE_ID, "src/a.ts", expect.anything());
  expect(code.diff).toHaveBeenCalledWith(CODE_ID, "src/a.ts", expect.anything());
});
