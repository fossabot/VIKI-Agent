/**
 * App-level wiring for the Help menu/`?` shortcut mount and the sources strip
 * under a finished reply. HelpMenu.unit.test.tsx, HelpCenter.unit.test.tsx and
 * SourcesStrip.unit.test.tsx already pin each component's own contract; this file pins only what
 * App itself is responsible for wiring between them.
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { resetHelp } from "../../components/Help/helpStore";
import type { Envelope, EventMap } from "../../protocol";
import { useReportUnsavedWork } from "../../state/unsavedWork";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  workspaceFixture,
} from "../../testShell";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  resetHelp();
  vi.restoreAllMocks();
});

const SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

function frame<K extends keyof EventMap>(seq: number, type: K, data: EventMap[K]): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    session_id: SID,
    seq,
    type,
    data,
  } as Envelope;
}

/** An ordinary, persistent, non-chat-only vault active: `usePublishSources`' own gate for a
 * session's sources to count as read from the vault (state/sources.ts's `vault` flag). Returns
 * the workspace too, so a write-capable caller can assert `createNote`'s exact ids. */
function workspaceShell(overrides: Partial<WorkspaceJson> = {}) {
  const workspace = workspaceFixture(overrides);
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: workspace.workspace_id,
          kind: "vault",
          name: workspace.name,
          policy: workspace.policy,
          layout: "unknown",
          source: workspace.source,
          aliased: workspace.aliased,
          root_available: workspace.root_available,
        },
        chat_only: false,
      },
    }),
    workspaces: {
      ...listingFixture([workspace]),
      active_id: workspace.workspace_id,
      chat_only: false,
    },
  });
  return { shell, workspace };
}

function vaultShell() {
  return workspaceShell().shell;
}

/** Pending reads throughout; a caller overrides only the calls its own test exercises. */
function fakeVaultApi(overrides: Partial<VaultApi> = {}): VaultApi {
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
    ...overrides,
  };
}

/** Renders the app over a vault-backed chat and opens its one live session. */
async function openChat(
  options: { shell?: ReturnType<typeof fakeShellApi>; vaultApi?: VaultApi; strict?: boolean } = {},
) {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SID, title: "Notes" })] });
  const { viki, socket } = fakeViki();
  const app = (
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      shell={options.shell ?? vaultShell()}
      {...(options.vaultApi ? { vaultApi: options.vaultApi } : {})}
    />
  );
  const view = render(options.strict ? <StrictMode>{app}</StrictMode> : app);
  act(() => socket.open());
  const row = await waitFor(() => {
    const found = view.container.querySelector<HTMLElement>(`[data-session-id="${SID}"]`);
    if (!found) throw new Error("the session row has not arrived");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  return { ...view, socket };
}

test("Help menu's Keyboard shortcuts row shows the keyboard-shortcuts page", async () => {
  await openChat();
  fireEvent.click(screen.getByRole("button", { name: "Help" }));
  fireEvent.click(screen.getByText("Keyboard shortcuts"));

  expect(screen.queryByRole("menu")).toBeNull();
  const page = screen.getByRole("button", { name: "Keyboard shortcuts" });
  expect(page.getAttribute("aria-current")).toBe("page");
});

test("a bare ? opens the same Keyboard shortcuts page", async () => {
  await openChat();
  fireEvent.keyDown(window, { key: "?" });

  const page = screen.getByRole("button", { name: "Keyboard shortcuts" });
  expect(page.getAttribute("aria-current")).toBe("page");
});

test("a finished reply with a successful vault Read shows its source chip", async () => {
  const { socket } = await openChat();
  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(
      frame(2, "tool.started", {
        block_id: "b1",
        tool: "Read",
        row: { head: "Read", rest: "30-knowledge/claim.md", tail: "" },
        parent_id: "",
        artifact: {
          artifact_id: "b1",
          kind: "file",
          operation: "read",
          path: "30-knowledge/claim.md",
          path_state: "workspace_relative",
        },
      }),
    );
    socket.receive(
      frame(3, "tool.done", {
        block_id: "b1",
        is_error: false,
        summary: "read",
        preview: "",
        tail: "",
        artifact: {
          artifact_id: "b1",
          observation: "result",
          byte_size: 42,
          sha256: "",
          preview: "",
          truncated: false,
          source_revision: "",
          detail_ref: null,
        },
      }),
    );
    socket.receive(
      frame(4, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });

  // The reply's own Read row; the right rail's Files in this chat counts the same Read as well.
  await waitFor(() => {
    within(screen.getByRole("group", { name: "Read for this reply" })).getByRole("button", {
      name: "Read note 30-knowledge/claim.md",
    });
  });
  expect(screen.queryByText("No vault sources")).toBeNull();
});

test("a finished reply with no vault note read shows the No vault sources badge", async () => {
  const { socket } = await openChat();
  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t2" }));
    socket.receive(frame(2, "text.done", { block_id: "txt1", text: "hi there" }));
    socket.receive(
      frame(3, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });

  await waitFor(() => {
    screen.getByText("No vault sources");
  });
  expect(screen.queryByRole("button", { name: /^Read note / })).toBeNull();
});

test("an ordinary vault on a Windows shell opens a prefilled Vault editor from Save as note, even under StrictMode", async () => {
  const { shell, workspace } = workspaceShell({ root_physical_id: "12:34" });
  const createNote = vi.fn(async () => ({
    operation_id: "11111111-1111-4111-8111-111111111111",
    status: "saved" as const,
    cleanup_pending: false,
  }));
  const { socket } = await openChat({
    shell,
    vaultApi: fakeVaultApi({ createNote }),
    strict: true,
  });

  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(frame(2, "text.done", { block_id: "txt1", text: "Report text" }));
    socket.receive(
      frame(3, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  });

  expect((screen.getByLabelText("Note path") as HTMLInputElement).value).toBe("Report text.md");
  expect(
    (screen.getByLabelText("Markdown") as HTMLTextAreaElement).value.startsWith("# Report text"),
  ).toBe(true);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Create note" }));
  });
  expect(createNote).toHaveBeenCalledWith(
    workspace.workspace_id,
    workspace.policy_revision,
    "12:34",
    "Report text.md",
    expect.stringContaining("# Report text"),
  );
});

test("a protected vault's Save as note drops a chat draft instead of the Vault editor", async () => {
  const { shell } = workspaceShell({ policy: "protected", root_physical_id: "12:34" });
  const createNote = vi.fn();
  const { socket } = await openChat({ shell, vaultApi: fakeVaultApi({ createNote }) });

  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(frame(2, "text.done", { block_id: "txt1", text: "Report text" }));
    socket.receive(
      frame(3, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  });

  expect(screen.queryByLabelText("Note path")).toBeNull();
  expect(createNote).not.toHaveBeenCalled();
  const composer = screen.getByLabelText<HTMLTextAreaElement>("Message");
  expect(composer.value).toContain("Report text");
  expect(composer.value).toContain(
    "Please save the reply below as a knowledge note in this vault.",
  );
});

test("Ask VIKI's cancelled leave keeps the other view's edit, and still drops the asked text as a new draft", async () => {
  await openChat();

  // Settings, not Vault: the Vault's own `canLeave` refuses to leave while any dialog (Help
  // included) is open, regardless of unsaved work, which would make `confirm` below unreachable
  // for the reason this test means to pin.
  fireEvent.keyDown(window, { key: ",", ctrlKey: true });
  const heading = screen.getByRole("heading", { level: 1 });

  // Stands in for a user mid-edit without the whole note-editing UI: the same global hook a
  // real editor reports through (src/app-tests/chat/retry.unit.test.tsx's own pattern for it).
  renderHook(() => useReportUnsavedWork("vault", true));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  fireEvent.keyDown(window, { key: "?" });
  fireEvent.click(screen.getByRole("button", { name: "Ask VIKI" }));

  expect(confirm).toHaveBeenCalledTimes(1);
  // The cancelled leave kept the Settings view up, not the "no room for a draft" refusal.
  expect(screen.getByRole("heading", { level: 1 })).toBe(heading);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(
    screen.queryByText("This window has no room for another draft. Close one, then try again."),
  ).toBeNull();
});

/** The Vault view with the Help dialog open over it: `canLeave` refuses to leave while any
 * dialog (Help included) is open, which is exactly what the Vault's own navigation gate must
 * see through — via `modalOrigin` — for "Ask VIKI" and "Open Settings → Diagnostics" to work. */
async function inVaultWithHelp() {
  await openChat({ vaultApi: fakeVaultApi() });
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  await waitFor(() =>
    expect(screen.getByRole("heading", { level: 1, name: "Vault" })).toBeTruthy(),
  );
  fireEvent.keyDown(window, { key: "?" });
  expect(screen.getByRole("dialog", { name: "Help" })).toBeTruthy();
}

test("from the Vault view, Ask VIKI lands in the chat with the draft", async () => {
  await inVaultWithHelp();
  fireEvent.click(screen.getByRole("button", { name: "Ask VIKI" }));

  // Help closed itself (onAsk returned true) and the Vault view is gone: the user is now
  // looking at the chat composer holding the asked text, not stuck behind a closed dialog.
  expect(screen.queryByRole("dialog", { name: "Help" })).toBeNull();
  expect(screen.queryByRole("heading", { level: 1, name: "Vault" })).toBeNull();
  expect(screen.getByLabelText<HTMLTextAreaElement>("Message").value).toContain(
    "I have a question about VIKI Agent.",
  );
});

test("from the Vault view, Open Settings → Diagnostics lands on Diagnostics", async () => {
  await inVaultWithHelp();
  fireEvent.click(screen.getByRole("button", { name: "Troubleshooting" }));
  fireEvent.click(screen.getByRole("button", { name: "Open Settings → Diagnostics" }));

  expect(screen.queryByRole("dialog", { name: "Help" })).toBeNull();
  expect(screen.queryByRole("heading", { level: 1, name: "Vault" })).toBeNull();
  expect(screen.getByRole("tab", { selected: true }).textContent).toContain("Diagnostics");
});
