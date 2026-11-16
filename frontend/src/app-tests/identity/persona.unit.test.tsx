/**
 * App wires the Persona page into Settings: its own nav tab opens it with the
 * active vault and `vaultApi` as props, `onDraft` lands in a new chat editor through `restoreText`,
 * and `onBack` leads straight to the chat that editor landed in. PersonaPage's own read states,
 * diff and validation are its own suite; this is the one seam from Settings to the chat.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { PERSONA_CLOSE, PERSONA_OPEN, renderPersona } from "../../components/Settings/persona";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

afterEach(cleanup);

const VALID_PERSONA = {
  name: "VIKI",
  address: "you",
  tone: "calm",
  language: "English",
  proactivity: "coach",
  dos: ["Lead with the answer."],
  donts: ["Pad replies."],
};
const VALID_TEXT = [
  "# AGENTS",
  "",
  PERSONA_OPEN,
  renderPersona(VALID_PERSONA),
  PERSONA_CLOSE,
  "",
].join("\n");

const pending = () => new Promise<never>(() => {});
async function settled() {
  await act(async () => {
    for (let n = 0; n < 5; n++) await Promise.resolve();
  });
}
const click = (element: HTMLElement) => act(async () => void fireEvent.click(element));

function fakeVaultApi(notes: Record<string, Record<string, string>>): VaultApi {
  return {
    entries: pending,
    note: vi.fn(async (workspaceId: string, ref: string) => ({
      note: { ref, text: notes[workspaceId]?.[ref] ?? "" },
      resolved_links: [],
    })),
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

/** A persistent, ordinary workspace open as the active vault — the shape App needs before it
 * will resolve `activeVault` and pass it to PersonaPage. */
function mount(
  vaultApi: VaultApi,
  options: {
    selected?: WorkspaceJson;
    selectedId?: string | null;
    restartRequired?: boolean;
    assistantName?: string;
    displayName?: string;
  } = {},
) {
  const fake = fakeViki();
  const workspace = workspaceFixture();
  const selectedId = options.selectedId ?? options.selected?.workspace_id ?? null;
  const shell = fakeShellApi({
    settings: settingsFixture({
      "onboarding.completed": true,
      ...(options.assistantName ? { "identity.assistant_name": options.assistantName } : {}),
      ...(options.displayName ? { "identity.display_name": options.displayName } : {}),
    }),
    workspaces: {
      ...listingFixture(options.selected ? [workspace, options.selected] : [workspace]),
      active_id: workspace.workspace_id,
      selected_id: selectedId,
      chat_only: false,
    },
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: workspace.workspace_id,
          kind: workspace.kind,
          name: workspace.name,
          policy: workspace.policy,
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
        selected_id: selectedId,
        restart_required: options.restartRequired ?? false,
      },
    }),
  });
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      vaultApi={vaultApi}
    />,
  );
  act(() => fake.socket.open());
  return fake;
}

test("Settings → Persona: editing the assistant name and drafting lands the markers and the new name in a new chat editor, with nothing sent", async () => {
  const workspace = workspaceFixture();
  const { socket } = mount(fakeVaultApi({ [workspace.workspace_id]: { "AGENTS.md": VALID_TEXT } }));
  await settled();

  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  await click(screen.getByRole("tab", { name: "Instructions and Persona" }));
  await waitFor(() => expect(screen.getByLabelText("Assistant name in this vault")).toBeTruthy());

  fireEvent.change(screen.getByLabelText("Assistant name in this vault"), {
    target: { value: "Nova" },
  });
  await click(screen.getByRole("button", { name: "Draft the change in a new chat" }));
  await click(screen.getByRole("button", { name: "Go to the chat" }));

  const box = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
  expect(box.value).toContain(PERSONA_OPEN);
  expect(box.value).toContain(PERSONA_CLOSE);
  expect(box.value).toContain("Nova");
  expect(socket.sent.filter((message) => message.type === "turn.send")).toEqual([]);
});

test("unmarked heading drafts conversion from the active vault, not the selected next vault", async () => {
  const active = workspaceFixture();
  const selected = workspaceFixture({
    workspace_id: "44444444-4444-4444-8444-444444444444",
    name: "Next vault",
    root: "E:\\Next vault",
  });
  const api = fakeVaultApi({
    [active.workspace_id]: {
      "AGENTS.md":
        "# Instructions\n\n## Persona\nKeep answers clear.\n\n## Other\nLeave this alone.\n",
      "CLAUDE.md": "@AGENTS.md\n",
    },
    [selected.workspace_id]: {
      "AGENTS.md": "## Persona\nWrong vault section.\n",
      "CLAUDE.md": "Wrong vault import.\n",
    },
  });
  const { socket } = mount(api, { selected, restartRequired: true });
  await settled();

  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  await click(screen.getByRole("tab", { name: "Instructions and Persona" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Draft the conversion in a new chat" })).toBeTruthy(),
  );

  expect(api.note).toHaveBeenCalledWith(active.workspace_id, "AGENTS.md");
  expect(api.note).toHaveBeenCalledWith(active.workspace_id, "CLAUDE.md");
  expect(api.note).not.toHaveBeenCalledWith(selected.workspace_id, expect.any(String));
  expect(
    screen.getByText("Selected for next launch: Next vault. Restart VIKI Agent to switch."),
  ).toBeTruthy();
  expect(
    screen.getByText("These are the instruction files in the running vault, My notes."),
  ).toBeTruthy();
  await click(screen.getByRole("button", { name: "Draft the conversion in a new chat" }));
  await click(screen.getByRole("button", { name: "Go to the chat" }));
  const box = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
  expect(box.value).toContain("Keep answers clear.");
  expect(box.value).not.toContain("Wrong vault section.");
  expect(socket.sent.filter((message) => message.type === "turn.send")).toEqual([]);
});

test("absent Persona section seeds add form from the app identity", async () => {
  const workspace = workspaceFixture();
  const { socket } = mount(
    fakeVaultApi({ [workspace.workspace_id]: { "AGENTS.md": "# Instructions\n" } }),
    { assistantName: "Aster", displayName: "Captain" },
  );
  await settled();

  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  await click(screen.getByRole("tab", { name: "Instructions and Persona" }));
  await click(await screen.findByRole("button", { name: "Add a Persona section…" }));
  expect(screen.getByLabelText<HTMLInputElement>("Assistant name in this vault").value).toBe(
    "Aster",
  );
  expect(screen.getByLabelText<HTMLInputElement>("Address me as").value).toBe("Captain");
  await click(screen.getByRole("button", { name: "Draft adding it in a new chat" }));
  await click(screen.getByRole("button", { name: "Go to the chat" }));
  const box = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
  expect(box.value).toContain("Aster");
  expect(box.value).toContain("Captain");
  expect(socket.sent.filter((message) => message.type === "turn.send")).toEqual([]);
});

test.each([
  ["active selection", workspaceFixture().workspace_id],
  ["no selection", null],
  ["missing selected row", "55555555-5555-4555-8555-555555555555"],
])("restart flag with %s does not show a next-launch notice", async (_case, selectedId) => {
  const workspace = workspaceFixture();
  mount(fakeVaultApi({ [workspace.workspace_id]: { "AGENTS.md": VALID_TEXT } }), {
    selectedId,
    restartRequired: true,
  });
  await settled();

  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  await click(screen.getByRole("tab", { name: "Instructions and Persona" }));
  await waitFor(() => expect(screen.getByLabelText("Assistant name in this vault")).toBeTruthy());
  expect(screen.queryByText(/Selected for next launch:/)).toBeNull();
  expect(screen.queryByText(/These are the instruction files in the running vault/)).toBeNull();
});

test("a different selection without restart does not show a next-launch notice", async () => {
  const active = workspaceFixture();
  const selected = workspaceFixture({
    workspace_id: "44444444-4444-4444-8444-444444444444",
    name: "Next vault",
  });
  mount(fakeVaultApi({ [active.workspace_id]: { "AGENTS.md": VALID_TEXT } }), { selected });
  await settled();

  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  await click(screen.getByRole("tab", { name: "Instructions and Persona" }));
  await waitFor(() => expect(screen.getByLabelText("Assistant name in this vault")).toBeTruthy());
  expect(screen.queryByText(/Selected for next launch:/)).toBeNull();
  expect(screen.queryByText(/These are the instruction files in the running vault/)).toBeNull();
});
