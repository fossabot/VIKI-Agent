/**
 * Vault, Routines and Settings leave Help in the top bar. VaultBrowser's `initialDraft` seeds
 * the New note editor only when a write context is given.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { RoutinesApi } from "../../clients/automation/routinesApi";
import type { RoutinesSnapshot } from "../../state/routines";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import type { VaultBookmarkApi, VaultReadApi, VaultWriteApi } from "../../state/vaultApi";
import { fakeShellApi } from "../../testShell";
import { RoutinesView, type RoutinesViewProps } from "../Routines/RoutinesView";
import { SettingsView } from "../Settings/SettingsView";
import { VaultBrowser } from "../VaultBrowser/VaultBrowser";
import { resetHelp, useHelpHost } from "./helpStore";

const W1 = "00000000-0000-4000-8000-000000000001";

/** Mount the same help host registration that HelpCenter supplies in the app. */
function Host(): null {
  useHelpHost();
  return null;
}

function vaultApi(): VaultReadApi & VaultBookmarkApi {
  return {
    entries: vi.fn(async () => ({ entries: [], partial: false, partial_reasons: [], omitted: 0 })),
    note: vi.fn(async (_id, ref) => ({
      note: {
        ref,
        title: ref,
        text: "",
        source: "",
        sha256: "a".repeat(64),
        frontmatter_error: null,
      },
      resolved_links: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    })),
    backlinks: vi.fn(async () => ({
      backlinks: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    })),
    history: vi.fn(async () => ({
      commits: [],
      state: "ready",
      partial: false,
      partial_reasons: [],
      omitted: 0,
    })),
    bookmarks: vi.fn(async () => ({ bookmarks: [] })),
    addBookmark: vi.fn(async (_id, ref) => ({
      bookmark: { ref, created_at: "2026-10-09T00:00:00.000Z" },
    })),
    removeBookmark: vi.fn(async () => undefined),
  };
}

function writeApi(overrides: Partial<VaultWriteApi> = {}): VaultWriteApi {
  return {
    createNote: vi.fn(async () => ({
      operation_id: W1,
      status: "saved" as const,
      cleanup_pending: false,
    })),
    saveNote: vi.fn(async () => ({
      operation_id: W1,
      status: "saved" as const,
      cleanup_pending: false,
    })),
    recovery: vi.fn(async () => []),
    ...overrides,
  };
}

async function settingsStore() {
  const store = createShellStore(fakeShellApi(), new AppStore());
  await act(async () => {
    await store.refreshSettings();
  });
  return store;
}

function routinesProps(): RoutinesViewProps {
  const snapshot: RoutinesSnapshot = { status: "idle", items: [], scheduler: null, error: null };
  return {
    api: {} as unknown as RoutinesApi,
    snapshot,
    sessions: {},
    nowMs: Date.now(),
    selectedId: null,
    onSelect: vi.fn(),
    onRefresh: vi.fn(),
    onBack: vi.fn(),
    workspaces: [] as WorkspaceJson[],
    activeWorkspaceId: "",
    controls: {
      paused: false,
      notificationsEnabled: false,
      quietStart: null,
      quietEnd: null,
      permission: "granted",
    },
    onSettings: vi.fn(async () => {}),
    onRequestPermission: vi.fn(async () => {}),
    onOpenSession: vi.fn(),
    onStop: vi.fn(),
  };
}

beforeEach(() => {
  resetHelp();
});
afterEach(cleanup);

describe("VaultBrowser's initialDraft", () => {
  test("prefills Note path and Markdown; Create note saves it", async () => {
    const createNote = vi.fn(async () => ({
      operation_id: W1,
      status: "saved" as const,
      cleanup_pending: false,
    }));
    render(
      <VaultBrowser
        workspaceId={W1}
        api={vaultApi()}
        onOpenUrl={vi.fn()}
        write={{ api: writeApi({ createNote }), policyRevision: 4, rootPhysicalId: "12:34" }}
        initialDraft={{ ref: "new/note.md", content: "# New" }}
      />,
    );
    await act(async () => {});
    expect((screen.getByLabelText("Note path") as HTMLInputElement).value).toBe("new/note.md");
    expect((screen.getByLabelText("Markdown") as HTMLTextAreaElement).value).toBe("# New");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create note" })));
    expect(createNote).toHaveBeenCalledWith(W1, 4, "12:34", "new/note.md", "# New");
  });

  test("prefills Note path and Markdown under StrictMode's double-invoked effects", async () => {
    const createNote = vi.fn(async () => ({
      operation_id: W1,
      status: "saved" as const,
      cleanup_pending: false,
    }));
    render(
      <StrictMode>
        <VaultBrowser
          workspaceId={W1}
          api={vaultApi()}
          onOpenUrl={vi.fn()}
          write={{ api: writeApi({ createNote }), policyRevision: 4, rootPhysicalId: "12:34" }}
          initialDraft={{ ref: "new/note.md", content: "# New" }}
        />
      </StrictMode>,
    );
    await act(async () => {});
    expect((screen.getByLabelText("Note path") as HTMLInputElement).value).toBe("new/note.md");
    expect((screen.getByLabelText("Markdown") as HTMLTextAreaElement).value).toBe("# New");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create note" })));
    expect(createNote).toHaveBeenCalledWith(W1, 4, "12:34", "new/note.md", "# New");
  });

  test("does nothing without a write context", async () => {
    render(
      <VaultBrowser
        workspaceId={W1}
        api={vaultApi()}
        onOpenUrl={vi.fn()}
        initialDraft={{ ref: "new/note.md", content: "# New" }}
      />,
    );
    await act(async () => {});
    expect(screen.queryByLabelText("Note path")).toBeNull();
    expect(screen.queryByRole("button", { name: "Create note" })).toBeNull();
  });
});

describe("Help stays in the top bar", () => {
  test("render nothing without a mounted help host", async () => {
    const store = await settingsStore();
    render(<VaultBrowser workspaceId={W1} api={vaultApi()} onOpenUrl={vi.fn()} />);
    render(<SettingsView store={store} page="general" onPage={vi.fn()} onNavigate={vi.fn()} />);
    render(<RoutinesView {...routinesProps()} />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Help: Vault" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Help: Settings" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Help: Routines" })).toBeNull();
  });

  test("Vault, Routines and Settings add no Help button with a help host mounted", async () => {
    const store = await settingsStore();
    render(<Host />);
    render(<VaultBrowser workspaceId={W1} api={vaultApi()} onOpenUrl={vi.fn()} />);
    render(<SettingsView store={store} page="general" onPage={vi.fn()} onNavigate={vi.fn()} />);
    render(<RoutinesView {...routinesProps()} />);
    await act(async () => {});

    expect(screen.queryAllByRole("button", { name: /^Help(?:$|:)/ })).toHaveLength(0);
  });
});
