import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { type CodeApi, CodeApiError } from "../../clients/code/codeApi";
import { dirtyRefs, dropBuffer, getBuffer, putBuffer } from "../../state/codeBuffers";
import {
  type CodeOpenRequest,
  CodeSidebar,
  type CodeSidebarProps,
  codeFolderKey,
} from "./CodeSidebar";

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
afterEach(() => {
  cleanup();
  for (const id of [A, B]) for (const ref of dirtyRefs(id)) dropBuffer(id, ref);
});
const folder = (id: string, name: string, over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id: id,
    kind: "code",
    root: `registered-${name}`,
    root_physical_id: `physical-${name}`,
    root_available: true,
    name,
    policy: "ordinary",
    policy_revision: 3,
    persistent: true,
    ...over,
  }) as WorkspaceJson;

function api(over: Partial<CodeApi> = {}): CodeApi {
  return {
    tree: vi.fn().mockResolvedValue({
      path: "",
      entries: [
        { name: "src", type: "dir", size: null },
        { name: "a.ts", type: "file", size: 3 },
      ],
      truncated: false,
    }),
    file: vi.fn().mockImplementation(async (_id: string, path: string) => ({
      path,
      size: 3,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      newline: "lf",
      text: "one",
      reason: null,
    })),
    diff: vi.fn().mockImplementation(async (_id: string, path: string) => ({
      path,
      text: "+current diff",
      reason: null,
    })),
    search: vi.fn(),
    save: vi.fn(),
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
      entries: [
        {
          path: "a.ts",
          orig_path: null,
          index: ".",
          worktree: "M",
          kind: "tracked",
          private: false,
        },
      ],
    }),
    ...over,
  };
}
function setup(over: Partial<CodeSidebarProps> = {}) {
  const props: CodeSidebarProps = {
    api: api(),
    folders: [folder(A, "Alpha"), folder(B, "Beta")],
    activeId: A,
    selected: null,
    onOpen: vi.fn(),
    onOpenWorkbench: vi.fn(),
    ...over,
  };
  return { props, ...render(<CodeSidebar {...props} />) };
}
const settle = () => act(async () => {});

const retained = (workspaceId: string, ref: string) => ({
  workspaceId,
  ref,
  base: { text: "base", sha256: "a".repeat(64), encoding: "utf-8", newline: "lf" } as const,
  text: "private draft text",
});

test("unsaved refs stay reachable without a tree entry or automatic editing", async () => {
  const { props, rerender, unmount } = setup({
    api: api({ tree: vi.fn().mockResolvedValue({ path: "", entries: [], truncated: false }) }),
  });
  await settle();
  const upper = retained(A, "Src/missing.ts");
  const lower = retained(A, "src/missing.ts");
  const inactive = retained(B, "inactive/deleted.ts");
  act(() => {
    putBuffer(upper);
    putBuffer(lower);
    putBuffer(inactive);
  });
  const unsaved = screen.getByRole("group", { name: "Unsaved changes" });
  expect(
    within(unsaved)
      .getAllByRole("button")
      .map((row) => row.textContent),
  ).toEqual(["Src/missing.ts", "src/missing.ts"]);
  expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual([
    "Alpha (unsaved)",
    "Beta (unsaved)",
  ]);
  expect(screen.queryByText("private draft text")).toBeNull();
  expect(props.onOpen).not.toHaveBeenCalled();
  fireEvent.click(within(unsaved).getByRole("button", { name: lower.ref }));
  const request: CodeOpenRequest = {
    api: props.api,
    folder: props.folders[0] ?? folder(A, "Alpha"),
    folderKey: codeFolderKey(props.folders[0] ?? null),
    path: lower.ref,
    withDiff: false,
  };
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith(request);
  rerender(<CodeSidebar {...props} selected={request} />);
  expect(
    within(screen.getByRole("group", { name: "Unsaved changes" }))
      .getByRole("button", { name: lower.ref })
      .getAttribute("aria-current"),
  ).toBe("true");
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  expect(
    within(screen.getByRole("group", { name: "Unsaved changes" })).getByRole("button", {
      name: upper.ref,
    }),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: B } });
  await settle();
  const inactiveRows = within(screen.getByRole("group", { name: "Unsaved changes" }));
  expect(inactiveRows.queryByRole("button", { name: lower.ref })).toBeNull();
  fireEvent.click(inactiveRows.getByRole("button", { name: inactive.ref }));
  expect(props.onOpen).toHaveBeenLastCalledWith({
    api: props.api,
    folder: props.folders[1],
    folderKey: codeFolderKey(props.folders[1] ?? null),
    path: inactive.ref,
    withDiff: false,
  });
  for (const method of [props.api.file, props.api.diff, props.api.search, props.api.save])
    expect(method).not.toHaveBeenCalled();
  act(() => dropBuffer(B, inactive.ref));
  expect(screen.queryByRole("group", { name: "Unsaved changes" })).toBeNull();
  expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual([
    "Alpha (unsaved)",
    "Beta",
  ]);
  unmount();
  expect(getBuffer(A, upper.ref)).toBe(upper);
  expect(getBuffer(A, lower.ref)).toBe(lower);
});

test("unsaved refs retain the registered folder gate without reading draft text", async () => {
  const draft = retained(A, "missing.ts");
  putBuffer(draft);
  const { props, rerender } = setup({
    folders: [folder(A, "Alpha", { root_available: false })],
  });
  const row = within(screen.getByRole("group", { name: "Unsaved changes" })).getByRole("button", {
    name: draft.ref,
  });
  fireEvent.click(row);
  expect(props.onOpen).not.toHaveBeenCalled();
  expect(props.api.tree).not.toHaveBeenCalled();
  expect(screen.queryByText(draft.text)).toBeNull();
  const replacement = folder(A, "Alpha", {
    root: "new-registration",
    root_physical_id: "new-physical-id",
    policy_revision: 4,
    policy: "existing_readonly",
  });
  const replacementApi = api();
  rerender(<CodeSidebar {...props} api={replacementApi} folders={[replacement]} />);
  await settle();
  expect(props.onOpen).not.toHaveBeenCalled();
  fireEvent.click(
    within(screen.getByRole("group", { name: "Unsaved changes" })).getByRole("button", {
      name: draft.ref,
    }),
  );
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith({
    api: replacementApi,
    folder: replacement,
    folderKey: codeFolderKey(replacement),
    path: draft.ref,
    withDiff: false,
  });
  expect(replacementApi.file).not.toHaveBeenCalled();
  expect(replacementApi.save).not.toHaveBeenCalled();
  expect(getBuffer(A, draft.ref)).toBe(draft);
});

test("the explorer emits exact selections without reading or rendering file content", async () => {
  const { props, container } = setup({
    activeId: B,
    folders: [folder(A, "Alpha"), folder(B, "Beta"), folder("vault", "Vault", { kind: "vault" })],
  });
  await settle();
  expect((screen.getByLabelText("Code folder") as HTMLSelectElement).value).toBe(B);
  expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual(["Alpha", "Beta"]);
  expect(vi.mocked(props.api.tree).mock.calls[0]?.slice(0, 2)).toEqual([B, ""]);
  expect(document.activeElement).toBe(document.body);
  fireEvent.click(screen.getByRole("button", { name: "a.ts" }));
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith({
    api: props.api,
    folder: props.folders[1],
    folderKey: codeFolderKey(props.folders[1] ?? null),
    path: "a.ts",
    withDiff: false,
  });
  for (const method of [
    props.api.file,
    props.api.diff,
    props.api.changes,
    props.api.search,
    props.api.save,
  ])
    expect(method).not.toHaveBeenCalled();
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  expect(container.querySelector(".code-file")).toBeNull();
});

test("nested refs and changes select the current registered folder without eager reads", async () => {
  const tree = vi.fn<CodeApi["tree"]>(async (_id, path) => ({
    path,
    entries:
      path === ""
        ? [{ name: "src", type: "dir", size: null }]
        : [{ name: "a b.ts", type: "file", size: 3 }],
    truncated: false,
  }));
  const { props } = setup({ api: api({ tree }) });
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "src" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "a b.ts" }));
  expect(props.onOpen).toHaveBeenLastCalledWith({
    api: props.api,
    folder: props.folders[0],
    folderKey: codeFolderKey(props.folders[0] ?? null),
    path: "src/a b.ts",
    withDiff: false,
  });
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: ".M a.ts" }));
  expect(props.onOpen).toHaveBeenLastCalledWith({
    api: props.api,
    folder: props.folders[0],
    folderKey: codeFolderKey(props.folders[0] ?? null),
    path: "a.ts",
    withDiff: true,
  });
  expect(props.api.file).not.toHaveBeenCalled();
  expect(props.api.diff).not.toHaveBeenCalled();
});

test("folder changes clear selection but unmounting a drawer does not clear it", async () => {
  const { props, unmount } = setup();
  await settle();
  fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: B } });
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith(null);
  unmount();
  expect(props.onOpen).toHaveBeenCalledTimes(1);
});

test("refresh reloads the list and emits a fresh current request once", async () => {
  const client = api(),
    alpha = folder(A, "Alpha");
  const request: CodeOpenRequest = {
    api: client,
    folder: alpha,
    folderKey: codeFolderKey(alpha),
    path: "a.ts",
    withDiff: true,
  };
  const { props, rerender } = setup({ api: client, folders: [alpha], selected: request });
  fireEvent.click(screen.getByRole("tab", { name: "Files" }));
  await settle();
  expect(screen.getByRole("button", { name: "a.ts" }).getAttribute("aria-current")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await settle();
  expect(props.api.tree).toHaveBeenCalledTimes(2);
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith(request);
  expect(vi.mocked(props.onOpen).mock.calls[0]?.[0]).not.toBe(request);
  rerender(<CodeSidebar {...props} folders={[folder(A, "Alpha", { policy_revision: 4 })]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await settle();
  expect(props.onOpen).toHaveBeenCalledTimes(1);
  expect(props.api.file).not.toHaveBeenCalled();
});

test("private changes and unavailable roots cannot emit file requests", async () => {
  const client = api();
  const changes = await client.changes(A);
  const entry = changes.entries[0];
  if (!entry) throw Error("missing synthetic change");
  entry.private = true;
  const { props, rerender } = setup({ api: api({ changes: vi.fn().mockResolvedValue(changes) }) });
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  expect(screen.getByText("Private")).toBeTruthy();
  expect(screen.queryByRole("button", { name: ".M a.ts" })).toBeNull();
  rerender(<CodeSidebar {...props} folders={[folder(A, "Alpha", { root_available: false })]} />);
  expect(screen.getByRole("alert").textContent).toBe("The folder is not where it was registered.");
  expect(props.onOpen).not.toHaveBeenCalled();
});

test("empty and untrusted explorers retain guidance and explicit workbench access", async () => {
  const { props, rerender } = setup({ folders: [] });
  expect(screen.getByText("No code folders yet. Open one in Settings → Workspaces.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open workbench" }));
  expect(props.onOpenWorkbench).toHaveBeenCalledExactlyOnceWith();
  expect(props.api.tree).not.toHaveBeenCalled();
  rerender(
    <CodeSidebar
      {...props}
      api={api({ changes: vi.fn().mockRejectedValue(new CodeApiError("trust_required")) })}
      folders={[folder(A, "Alpha", { policy: "existing_readonly" })]}
    />,
  );
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  expect(
    screen.getByText("Trust this folder in Settings → Workspaces to see its Git changes."),
  ).toBeTruthy();
});

test("the Files and Changes tabs retain their roving keyboard behavior", async () => {
  setup();
  const files = screen.getByRole("tab", { name: "Files" });
  files.focus();
  fireEvent.keyDown(files, { key: "ArrowRight" });
  const changes = screen.getByRole("tab", { name: "Changes" });
  expect(document.activeElement).toBe(changes);
  expect(changes.getAttribute("aria-selected")).toBe("true");
  expect(files.tabIndex).toBe(-1);
  fireEvent.keyDown(changes, { key: "Home" });
  expect(document.activeElement).toBe(files);
  fireEvent.keyDown(files, { key: "End" });
  expect(document.activeElement).toBe(changes);
  await settle();
});

test.each([false, true])(
  "reopening the explorer retains the inactive preview folder and selection (diff=%s)",
  async (withDiff) => {
    const { props, unmount } = setup();
    await settle();
    fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: B } });
    if (withDiff) fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
    await settle();
    const label = withDiff ? ".M a.ts" : "a.ts";
    fireEvent.click(screen.getByRole("button", { name: label }));
    const selected = vi.mocked(props.onOpen).mock.calls.at(-1)?.[0];
    if (!selected) throw new Error("missing selected request");
    expect(selected.folder.workspace_id).toBe(B);
    unmount();
    render(<CodeSidebar {...props} selected={selected} />);
    await settle();
    expect((screen.getByLabelText("Code folder") as HTMLSelectElement).value).toBe(B);
    expect(
      screen
        .getByRole("tab", { name: withDiff ? "Changes" : "Files" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(screen.getByRole("button", { name: label }).getAttribute("aria-current")).toBe("true");
    expect(props.api.file).not.toHaveBeenCalled();
    expect(props.api.diff).not.toHaveBeenCalled();
  },
);
