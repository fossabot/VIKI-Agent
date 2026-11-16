import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ArchiveSummary, ChatArchivesApi } from "../../clients/conversations/chatArchivesApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import {
  exportConversationArchive,
  type ReadOnlyConversationArchive,
} from "../../context/conversationTools";
import { ArchivePreview, ArchiveTools } from "./ConversationTools";

afterEach(cleanup);

const archive: ReadOnlyConversationArchive = {
  formatVersion: 1,
  kind: "viki-conversation-archive",
  readOnly: true,
  session: { sessionId: "123e4567-e89b-12d3-a456-426614174000", title: "Chat" },
  turns: [
    {
      sessionId: "123e4567-e89b-12d3-a456-426614174000",
      turnId: "t1",
      role: "user",
      text: "look",
      attachments: [
        {
          asset_id: "a1",
          media_type: "image/png",
          byte_size: 3,
          width: 1,
          height: 1,
          name: "screen.png",
          modified_at: null,
          source: "history",
        },
      ],
    },
  ],
  media: [{ asset_id: "a1", sha256: "hash", base64: btoa("png") }],
};

test("ArchivePreview revokes every object URL it created once it unmounts", () => {
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:archive-a1");
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  const { unmount } = render(<ArchivePreview archive={archive} onOpenUrl={vi.fn()} />);
  expect(create).toHaveBeenCalledTimes(1);
  expect(revoke).not.toHaveBeenCalled();
  unmount();
  expect(revoke).toHaveBeenCalledWith("blob:archive-a1");
});

const importable: ReadOnlyConversationArchive = {
  formatVersion: 1,
  kind: "viki-conversation-archive",
  readOnly: true,
  session: { sessionId: "11111111-1111-4111-8111-111111111111", title: "Imported" },
  turns: [
    {
      sessionId: "11111111-1111-4111-8111-111111111111",
      turnId: "t1",
      role: "user",
      text: "hi",
    },
  ],
};
const importableJson = exportConversationArchive(importable);
const dummyAssets: AssetsApi = { upload: vi.fn(), read: vi.fn() };

/** A fake ChatArchivesApi whose `list` answers "ok" or "fail" in the given call order. */
function fakeArchivesApi(listBehavior: readonly ("ok" | "fail")[]) {
  const saves: string[] = [];
  const gets: string[] = [];
  let listCalls = 0;
  const api: ChatArchivesApi = {
    async list(): Promise<readonly ArchiveSummary[]> {
      const behavior = listBehavior[listCalls] ?? "ok";
      listCalls++;
      if (behavior === "fail") throw new Error("list_failed");
      return [];
    },
    async get(id) {
      gets.push(id);
      return importable;
    },
    async save(id) {
      saves.push(id);
    },
    async remove() {},
  };
  return { api, saves, gets };
}

test("a failed list load shows its own copy and Reload refetches", async () => {
  const { api } = fakeArchivesApi(["fail", "ok"]);
  render(<ArchiveTools onImport={vi.fn()} api={api} assets={dummyAssets} />);
  await screen.findByText("Could not load the saved copies.");
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  await waitFor(() => expect(screen.queryByText("Could not load the saved copies.")).toBeNull());
});

test("importing the same file twice posts the same id", async () => {
  const { api, saves, gets } = fakeArchivesApi(["ok", "ok", "ok"]);
  const onImport = vi.fn();
  render(<ArchiveTools onImport={onImport} api={api} assets={dummyAssets} />);
  await waitFor(() => expect(saves).toHaveLength(0));
  const input = screen.getByLabelText("Import a read-only copy") as HTMLInputElement;
  const file = () => new File([importableJson], "archive.json", { type: "application/json" });
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(saves).toHaveLength(1));
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(saves).toHaveLength(2));
  expect(saves[0]).toBe(saves[1]);
  expect(gets).toEqual(saves);
  expect(onImport).toHaveBeenCalledTimes(2);
});

test("a rejected save still refetches the list, because the refresh runs in finally", async () => {
  let listCalls = 0;
  const api: ChatArchivesApi = {
    async list(): Promise<readonly ArchiveSummary[]> {
      listCalls++;
      return [];
    },
    async get() {
      return importable;
    },
    async save() {
      throw new Error("save_failed");
    },
    async remove() {},
  };
  render(<ArchiveTools onImport={vi.fn()} api={api} assets={dummyAssets} />);
  await waitFor(() => expect(listCalls).toBe(1));
  const input = screen.getByLabelText("Import a read-only copy") as HTMLInputElement;
  const file = new File([importableJson], "archive.json", { type: "application/json" });
  fireEvent.change(input, { target: { files: [file] } });
  await screen.findByText("Could not process the file. Check it and try again. Limit: 8 MiB.");
  expect(listCalls).toBe(2);
});

const cutArchive: ReadOnlyConversationArchive = { ...archive, promptCut: true };

test("a selection holding a cut prompt disables both exports and shows why", () => {
  const { api } = fakeArchivesApi(["ok"]);
  render(<ArchiveTools archive={cutArchive} onImport={vi.fn()} api={api} assets={dummyAssets} />);
  expect(
    (screen.getByRole("button", { name: "Export Markdown" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Export JSON with attachments" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  screen.getByText(
    "A selected turn's prompt is cut short in this window, so this selection cannot be exported. Unselect that turn, or open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.",
  );
});

test("with the record missing, the cut-selection line drops Open conversation record", () => {
  const { api } = fakeArchivesApi(["ok"]);
  render(
    <ArchiveTools
      archive={cutArchive}
      onImport={vi.fn()}
      api={api}
      assets={dummyAssets}
      recordMissing
    />,
  );
  screen.getByText(
    "A selected turn's prompt is cut short in this window, so this selection cannot be exported. Unselect that turn, or open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.",
  );
});

test("a selection without a cut prompt shows no line and leaves the buttons enabled", () => {
  const { api } = fakeArchivesApi(["ok"]);
  render(<ArchiveTools archive={archive} onImport={vi.fn()} api={api} assets={dummyAssets} />);
  expect(
    (screen.getByRole("button", { name: "Export Markdown" }) as HTMLButtonElement).disabled,
  ).toBe(false);
  expect(
    (screen.getByRole("button", { name: "Export JSON with attachments" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
  expect(
    screen.queryByText(
      "A selected turn's prompt is cut short in this window, so this selection cannot be exported. Unselect that turn, or open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.",
    ),
  ).toBeNull();
});

test("a retry after a failed refresh posts the same id", async () => {
  const { api, saves } = fakeArchivesApi(["ok", "fail", "ok"]);
  render(<ArchiveTools onImport={vi.fn()} api={api} assets={dummyAssets} />);
  await waitFor(() => expect(saves).toHaveLength(0));
  const input = screen.getByLabelText("Import a read-only copy") as HTMLInputElement;
  const file = () => new File([importableJson], "archive.json", { type: "application/json" });
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(saves).toHaveLength(1));
  await screen.findByText("Could not load the saved copies.");
  fireEvent.change(input, { target: { files: [file()] } });
  await waitFor(() => expect(saves).toHaveLength(2));
  expect(saves[0]).toBe(saves[1]);
});

test("the section is named Export / Import and the import control reads Import a read-only copy", () => {
  const { api } = fakeArchivesApi(["ok"]);
  render(<ArchiveTools onImport={vi.fn()} api={api} assets={dummyAssets} />);
  expect(screen.getByRole("region", { name: "Export / Import" })).toBeTruthy();
  expect(screen.getByLabelText("Import a read-only copy")).toBeTruthy();
});

test("the saved list reads Saved copies and Remove from the app, and holds no archive wording", async () => {
  const populated: ChatArchivesApi = {
    async list() {
      return [{ archive_id: "x1", created_at: "2026-10-01T00:00:00.000Z", byte_size: 2048 }];
    },
    async get() {
      return importable;
    },
    async save() {},
    async remove() {},
  };
  const { container } = render(
    <ArchiveTools onImport={vi.fn()} api={populated} assets={dummyAssets} />,
  );
  await screen.findByText("Remove from the app");
  expect(container.textContent).toContain("Saved copies");
  expect(container.textContent).not.toMatch(/archive/i);
});

test("ArchivePreview reads Read-only copy · no session is started", () => {
  render(<ArchivePreview archive={archive} onOpenUrl={vi.fn()} />);
  expect(screen.getByText("Read-only copy · no session is started")).toBeTruthy();
});
