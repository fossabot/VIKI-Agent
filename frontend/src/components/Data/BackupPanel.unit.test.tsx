/**
 * `BackupPanel`: the preview states what a backup is before it is written,
 * the run sits behind a confirm, refusals render the app's own sentence for the typed code, and a
 * restore opens a new folder read-only. The knowledge client is an injected fake.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, type Mock, test, vi } from "vitest";
import { createShellStore, type ShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import {
  type BackupPreview,
  KnowledgeError,
  type RestorePreview,
  type VaultKnowledgeApi,
} from "../../state/vaultKnowledgeApi";
import { fakeShellApi, listingFixture, workspaceFixture } from "../../testShell";
import { BackupPanel } from "./BackupPanel";
import type { PathFieldProps } from "./ExportPanel";

afterEach(cleanup);

const VAULT = "11111111-1111-4111-8111-111111111111";
const PROTECTED = "22222222-2222-4222-8222-222222222222";
const SERVER_MESSAGE = "server words that must not render";

function stubPathField(props: PathFieldProps) {
  return (
    <div>
      <label htmlFor={props.id}>{props.label}</label>
      <input id={props.id} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      {props.error && <span role="alert">{props.error}</span>}
    </div>
  );
}

const previewOf = (over: Partial<BackupPreview> = {}): BackupPreview => ({
  files: 12,
  bytes: 2048,
  instruction_files: ["CLAUDE.md"],
  instruction_files_total: 1,
  skipped: [{ ref: "notes/odd.md", reason: "unreadable" }],
  skipped_total: 1,
  local_recovery: false,
  ...over,
});

const restorePreviewOf = (over: Partial<RestorePreview> = {}): RestorePreview => ({
  source_name: "Old notes",
  source_policy: "ordinary",
  protected: true,
  files: 7,
  bytes: 1024,
  instruction_files: [],
  instruction_files_total: 0,
  problems: [],
  problems_total: 0,
  backup_skipped: 2,
  ...over,
});

function fakeKnowledge(over: Record<string, Mock> = {}) {
  const api = {
    backupPreview: vi.fn().mockResolvedValue(previewOf()),
    backup: vi.fn().mockResolvedValue({
      folder: "D:\\b\\notes",
      files: 12,
      bytes: 2048,
      skipped: [],
      skipped_total: 0,
      replayed: false,
    }),
    backupRestorePreview: vi.fn().mockResolvedValue(restorePreviewOf()),
    backupRestore: vi.fn().mockResolvedValue({
      workspace: { workspace_id: VAULT, name: "Restored", policy: "read_only" },
      files: 7,
      bytes: 1024,
      verified: true,
      problems: [],
      problems_total: 0,
      replayed: false,
    }),
    ...over,
  };
  return api as typeof api & VaultKnowledgeApi;
}

async function mount(knowledge: VaultKnowledgeApi): Promise<ShellStore> {
  const store = createShellStore(
    fakeShellApi({
      workspaces: listingFixture([
        workspaceFixture({ workspace_id: VAULT, name: "My notes" }),
        workspaceFixture({ workspace_id: PROTECTED, name: "Guarded", policy: "protected" }),
      ]),
    }),
    new AppStore(),
  );
  await store.refreshWorkspaces();
  render(
    <BackupPanel
      knowledgeApi={knowledge}
      store={store}
      renderPathField={stubPathField}
      headingRef={createRef<HTMLHeadingElement>()}
    />,
  );
  return store;
}

async function settle(action: () => void): Promise<void> {
  await act(async () => {
    action();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const click = (name: string) => settle(() => screen.getByRole("button", { name }).click());

async function previewBackup(): Promise<void> {
  type("Backup destination folder", "D:\\backups");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[0]?.click());
}

test("a vault of any policy is offered, including a protected one", async () => {
  await mount(fakeKnowledge());
  const names = screen.getAllByRole("option").map((o) => o.textContent);
  expect(names).toEqual(["My notes", "Guarded"]);
});

test("the preview shows counts, instruction files and skipped files, and says it is not encrypted", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewBackup();
  expect(api.backupPreview).toHaveBeenCalledWith(VAULT, "D:\\backups");
  expect(screen.getByText("12")).toBeTruthy();
  expect(screen.getByText("2.0 KB")).toBeTruthy();
  expect(screen.getByText("CLAUDE.md")).toBeTruthy();
  expect(screen.getByText("notes/odd.md: unreadable")).toBeTruthy();
  expect(screen.getByText(/A backup is not encrypted/)).toBeTruthy();
  expect(api.backup).not.toHaveBeenCalled();
});

test("a destination on the vault's own disk is warned about; another disk is not", async () => {
  await mount(
    fakeKnowledge({
      backupPreview: vi.fn().mockResolvedValue(previewOf({ local_recovery: true })),
    }),
  );
  await previewBackup();
  expect(screen.getByText(/same disk as the vault/)).toBeTruthy();
  cleanup();
  await mount(fakeKnowledge());
  await previewBackup();
  expect(screen.queryByText(/same disk as the vault/)).toBeNull();
});

test("the run waits for a confirm, then says again that the backup is not encrypted", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewBackup();
  await click("Back up");
  expect(api.backup).not.toHaveBeenCalled();
  await click("Confirm backup");
  expect(api.backup).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Backup written.")).toBeTruthy();
  expect(screen.getByText("D:\\b\\notes")).toBeTruthy();
  expect(screen.getByText(/This backup is not encrypted/)).toBeTruthy();
});

test("cancelling the confirm writes nothing", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewBackup();
  await click("Back up");
  await click("Cancel");
  expect(api.backup).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Confirm backup" })).toBeNull();
});

test.each([
  ["denied_destination", /cannot go inside a vault/],
  ["backup_too_large", /too large to back up/],
  ["invalid_path", /full path of an existing folder/],
  ["something_new", /Could not complete: something_new/],
])(
  "a %s refusal renders the app's own sentence, never the server's message",
  async (code, sentence) => {
    const error = Object.assign(new KnowledgeError(code), { message: SERVER_MESSAGE });
    const api = fakeKnowledge({ backupPreview: vi.fn().mockRejectedValue(error) });
    await mount(api);
    await previewBackup();
    expect(screen.getByRole("alert").textContent).toMatch(sentence);
    expect(screen.queryByText(SERVER_MESSAGE, { exact: false })).toBeNull();
  },
);

test("a refused run keeps its command id for a retry and takes a new one when the request changes", async () => {
  const api = fakeKnowledge({
    backup: vi.fn().mockRejectedValueOnce(new KnowledgeError("unavailable")).mockResolvedValue({
      folder: "D:\\b",
      files: 1,
      bytes: 1,
      skipped: [],
      skipped_total: 0,
      replayed: true,
    }),
  });
  await mount(api);
  await previewBackup();
  await click("Back up");
  await click("Confirm backup");
  await click("Back up");
  await click("Confirm backup");
  const [first, second] = api.backup.mock.calls.map((call) => call[2]);
  expect(first).toBe(second);
  expect(screen.getByText("This is the earlier result; nothing was written again.")).toBeTruthy();

  type("Backup destination folder", "D:\\elsewhere");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[0]?.click());
  await click("Back up");
  await click("Confirm backup");
  expect(api.backup.mock.calls[2]?.[2]).not.toBe(first);
});

async function previewRestore(): Promise<void> {
  type("Backup folder to restore", "D:\\b\\notes");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[1]?.click());
}

test("the restore preview names the source, policy, counts and whether the copy is protected", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewRestore();
  expect(api.backupRestorePreview).toHaveBeenCalledWith("D:\\b\\notes");
  expect(screen.getByText("Old notes")).toBeTruthy();
  expect(screen.getByText("ordinary")).toBeTruthy();
  expect(screen.getByText("The copy will be registered as protected.")).toBeTruthy();
});

test("a restore runs behind a confirm into a new folder and says the copy opened read-only", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewRestore();
  type("Parent folder for the copy", "D:\\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  expect(api.backupRestore).not.toHaveBeenCalled();
  await click("Confirm restore");
  expect(api.backupRestore).toHaveBeenCalledWith(expect.any(String), {
    backup: "D:\\b\\notes",
    parent: "D:\\restores",
    folder: "copy",
  });
  expect(screen.getByText("Restored. The copy opened read-only.")).toBeTruthy();
  expect(screen.getByText("Restored")).toBeTruthy();
});

test("a successful restore refreshes the shell store once; a refused one does not", async () => {
  const api = fakeKnowledge({
    backupRestore: vi
      .fn()
      .mockRejectedValueOnce(new KnowledgeError("denied_destination"))
      .mockResolvedValue({
        workspace: { workspace_id: VAULT, name: "Restored", policy: "read_only" },
        files: 7,
        bytes: 1024,
        verified: true,
        problems: [],
        problems_total: 0,
        replayed: false,
      }),
  });
  const store = await mount(api);
  const spy = vi.spyOn(store, "afterMutation");
  await previewRestore();
  type("Parent folder for the copy", "D:\\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  await click("Confirm restore");
  expect(spy).not.toHaveBeenCalled();
  await click("Restore into a new folder");
  await click("Confirm restore");
  expect(spy).toHaveBeenCalledTimes(1);
  expect(spy).toHaveBeenCalledWith(["capabilities", "workspaces"]);
});

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

test("a backup preview answered after the destination changed is dropped", async () => {
  const late = deferred<BackupPreview>();
  const api = fakeKnowledge({ backupPreview: vi.fn().mockReturnValue(late.promise) });
  await mount(api);
  type("Backup destination folder", "D:\\x");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[0]?.click());
  type("Backup destination folder", "D:\\y");
  await settle(() => late.resolve(previewOf({ local_recovery: true })));
  expect(screen.queryByText("12")).toBeNull();
  expect(screen.queryByText(/same disk as the vault/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Back up" })).toBeNull();
});

test("a restore preview answered after the source changed is dropped", async () => {
  const late = deferred<RestorePreview>();
  const api = fakeKnowledge({ backupRestorePreview: vi.fn().mockReturnValue(late.promise) });
  await mount(api);
  type("Backup folder to restore", "D:\\x");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[1]?.click());
  type("Backup folder to restore", "D:\\y");
  await settle(() => late.resolve(restorePreviewOf()));
  expect(screen.queryByText("Old notes")).toBeNull();
  expect(screen.queryByText("The copy will be registered as protected.")).toBeNull();
  expect(screen.queryByRole("button", { name: "Restore into a new folder" })).toBeNull();
});

test("a restore refusal renders the app's own sentence and a typed refusal takes a new command id", async () => {
  const api = fakeKnowledge({
    backupRestore: vi
      .fn()
      .mockRejectedValueOnce(new KnowledgeError("denied_destination"))
      .mockRejectedValue(new KnowledgeError("exists")),
  });
  await mount(api);
  await previewRestore();
  type("Parent folder for the copy", "D:\\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  await click("Confirm restore");
  expect(screen.getByRole("alert").textContent).toMatch(/cannot go inside a vault/);
  await click("Restore into a new folder");
  await click("Confirm restore");
  const [first, second] = api.backupRestore.mock.calls.map((call) => call[0]);
  expect(first).not.toBe(second);
});

test("a backup to the same destination after a success sends a new command id", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewBackup();
  await click("Back up");
  await click("Confirm backup");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[0]?.click());
  await click("Back up");
  await click("Confirm backup");
  const [first, second] = api.backup.mock.calls.map((call) => call[2]);
  expect(first).not.toBe(second);
});

test("a backup retried after a network failure reuses its command id", async () => {
  const api = fakeKnowledge({ backup: vi.fn().mockRejectedValueOnce(new TypeError("offline")) });
  api.backup.mockResolvedValue({
    folder: "D:",
    files: 1,
    bytes: 1,
    skipped: [],
    skipped_total: 0,
    replayed: true,
  });
  await mount(api);
  await previewBackup();
  await click("Back up");
  await click("Confirm backup");
  await click("Back up");
  await click("Confirm backup");
  const [first, second] = api.backup.mock.calls.map((call) => call[2]);
  expect(first).toBe(second);
});

test("a restore to the same folder after a success sends a new command id", async () => {
  const api = fakeKnowledge();
  await mount(api);
  await previewRestore();
  type("Parent folder for the copy", "D:\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  await click("Confirm restore");
  await settle(() => screen.getAllByRole("button", { name: "Preview" })[1]?.click());
  await click("Restore into a new folder");
  await click("Confirm restore");
  const [first, second] = api.backupRestore.mock.calls.map((call) => call[0]);
  expect(first).not.toBe(second);
});

test("a restore retried after a network failure reuses its command id", async () => {
  const api = fakeKnowledge({
    backupRestore: vi.fn().mockRejectedValueOnce(new TypeError("offline")),
  });
  api.backupRestore.mockResolvedValue({
    workspace: { workspace_id: VAULT, name: "Restored", policy: "read_only" },
    files: 7,
    bytes: 1024,
    verified: true,
    problems: [],
    problems_total: 0,
    replayed: true,
  });
  await mount(api);
  await previewRestore();
  type("Parent folder for the copy", "D:\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  await click("Confirm restore");
  await click("Restore into a new folder");
  await click("Confirm restore");
  const [first, second] = api.backupRestore.mock.calls.map((call) => call[0]);
  expect(first).toBe(second);
});

test("a restore refused with recovery_required shows the unfinished-operation sentence", async () => {
  const api = fakeKnowledge({
    backupRestore: vi.fn().mockRejectedValue(new KnowledgeError("recovery_required")),
  });
  await mount(api);
  await previewRestore();
  type("Parent folder for the copy", "D:\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  await click("Confirm restore");
  expect(screen.getByRole("alert").textContent).toMatch(/unfinished save or note operation/);
});

test("backup coverage reports omitted instruction and skipped rows separately, including zero shown", async () => {
  const api = fakeKnowledge({
    backupPreview: vi.fn().mockResolvedValue(
      previewOf({
        instruction_files: [],
        instruction_files_total: 30,
        skipped: [],
        skipped_total: 2001,
      }),
    ),
    backup: vi.fn().mockResolvedValue({
      folder: "Vault backup",
      files: 12,
      bytes: 2048,
      skipped: [{ ref: "one.md", reason: "too_large" }],
      skipped_total: 2001,
      replayed: false,
    }),
  });
  await mount(api);
  await previewBackup();
  expect(screen.getByText("Showing 0 of 30; 30 omitted.")).toBeTruthy();
  expect(screen.getByText("Showing 0 of 2001; 2001 omitted.")).toBeTruthy();
  expect(screen.queryByText("None")).toBeNull();
  await click("Back up");
  await click("Confirm backup");
  expect(screen.getByText("Backup written.")).toBeTruthy();
  expect(screen.getByText("Showing 1 of 2001; 2000 omitted.")).toBeTruthy();
});

test("omitted restore problems preserve confirmation and the unverified outcome", async () => {
  const api = fakeKnowledge({
    backupRestorePreview: vi.fn().mockResolvedValue(
      restorePreviewOf({
        instruction_files: [],
        instruction_files_total: 30,
        problems: [],
        problems_total: 2000,
      }),
    ),
    backupRestore: vi.fn().mockResolvedValue({
      workspace: { workspace_id: VAULT, name: "Restored", policy: "existing_readonly" },
      files: 7,
      bytes: 1024,
      verified: false,
      problems: [],
      problems_total: 1999,
      replayed: false,
    }),
  });
  await mount(api);
  await previewRestore();
  expect(screen.getByText("Showing 0 of 30; 30 omitted.")).toBeTruthy();
  expect(screen.getByText("Showing 0 of 2000; 2000 omitted.")).toBeTruthy();
  expect(screen.queryByText("None")).toBeNull();
  type("Parent folder for the copy", "D:\\restores");
  type("New folder name", "copy");
  await click("Restore into a new folder");
  expect(api.backupRestore).not.toHaveBeenCalled();
  await click("Confirm restore");
  expect(screen.getByText("The copy could not be fully verified.")).toBeTruthy();
  expect(screen.queryByText("Every file was verified against the backup's manifest.")).toBeNull();
  expect(screen.getByText("Showing 0 of 1999; 1999 omitted.")).toBeTruthy();
  expect(screen.getByText("Restored. The copy opened read-only.")).toBeTruthy();
});
