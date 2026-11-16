import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type ChangeEntry,
  type CodeApi,
  CodeApiError,
  type CodeChanges,
  type CodeTree,
} from "../../clients/code/codeApi";
import { ChangesList } from "./ChangesList";
import { FileTree } from "./FileTree";
import { QuickOpen } from "./QuickOpen";

const WS = "00000000-0000-4000-8000-000000000001";
const XSS = "<script>alert(1)</script>";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function fakeApi(over: Partial<CodeApi> = {}): CodeApi {
  return {
    tree: vi.fn(),
    file: vi.fn(),
    search: vi.fn(),
    changes: vi.fn(),
    diff: vi.fn(),
    save: vi.fn(),
    ...over,
  };
}
const tree = (entries: CodeTree["entries"], truncated = false): CodeTree => ({
  path: "",
  entries,
  truncated,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}
const settle = () => act(async () => {});

test("tree lists lazily, expands, collapses and refs children", async () => {
  const api = fakeApi({
    tree: vi.fn(async (_id, path) =>
      path === ""
        ? tree([
            { name: "src", type: "dir", size: null },
            { name: XSS, type: "file", size: 1 },
            { name: "ln", type: "link", size: null },
            { name: "dev", type: "other", size: null },
          ])
        : tree([{ name: "a.ts", type: "file", size: 3 }]),
    ),
  });
  const onSelectFile = vi.fn();
  render(<FileTree api={api} workspaceId={WS} selected="src/a.ts" onSelectFile={onSelectFile} />);
  await settle();
  expect(api.tree).toHaveBeenCalledTimes(1);
  expect(screen.getByText(XSS).tagName).toBe("BUTTON");
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText("ln").tagName).toBe("LI");
  expect(screen.getByText("dev").tagName).toBe("LI");
  const dir = screen.getByRole("button", { name: "src" });
  expect(dir.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(dir);
  await settle();
  expect(api.tree).toHaveBeenLastCalledWith(WS, "src", expect.any(AbortSignal));
  expect(dir.getAttribute("aria-expanded")).toBe("true");
  const file = screen.getByRole("button", { name: "a.ts" });
  expect(file.getAttribute("aria-current")).toBe("true");
  fireEvent.click(file);
  expect(onSelectFile).toHaveBeenCalledWith("src/a.ts");
  fireEvent.click(dir);
  expect(screen.queryByText("a.ts")).toBeNull();
  fireEvent.click(dir);
  await settle();
  expect(screen.getByText("a.ts")).toBeTruthy();
  expect(api.tree).toHaveBeenCalledTimes(2);
});

test("tree icons distinguish expansion and files without changing button names", async () => {
  const api = fakeApi({
    tree: vi.fn(async (_id, path) =>
      path === ""
        ? tree([{ name: "src", type: "dir", size: null }])
        : tree([{ name: "a.ts", type: "file", size: 3 }]),
    ),
  });
  const onSelectFile = vi.fn();
  render(<FileTree api={api} workspaceId={WS} selected="src/a.ts" onSelectFile={onSelectFile} />);
  await settle();
  const dir = screen.getByRole("button", { name: "src" });
  const folderIcon = dir.querySelector("svg");
  expect(folderIcon).not.toBeNull();
  const closedPath = folderIcon?.querySelector("path")?.getAttribute("d");
  expect(closedPath).toBeTruthy();
  expect(dir.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(dir);
  await settle();
  expect(dir.getAttribute("aria-expanded")).toBe("true");
  expect(folderIcon?.querySelector("path")?.getAttribute("d")).not.toBe(closedPath);
  const file = screen.getByRole("button", { name: "a.ts" });
  const fileIcon = file.querySelector("svg");
  expect(fileIcon).not.toBeNull();
  expect(fileIcon?.querySelector("path")?.getAttribute("d")).not.toBe(closedPath);
  for (const icon of [folderIcon, fileIcon]) {
    expect(icon?.getAttribute("aria-hidden")).toBe("true");
    expect(icon?.getAttribute("focusable")).toBe("false");
    expect(icon?.getAttribute("width")).toBe("16");
    expect(icon?.getAttribute("height")).toBe("16");
  }
  expect(file.getAttribute("aria-current")).toBe("true");
  fireEvent.click(file);
  expect(onSelectFile).toHaveBeenCalledExactlyOnceWith("src/a.ts");
  fireEvent.click(dir);
  expect(folderIcon?.querySelector("path")?.getAttribute("d")).toBe(closedPath);
  expect(api.tree).toHaveBeenCalledTimes(2);
});

test("tree shows truncated and empty words", async () => {
  const api = fakeApi({ tree: vi.fn(async () => tree([], true)) });
  render(<FileTree api={api} workspaceId={WS} selected={null} onSelectFile={vi.fn()} />);
  await settle();
  expect(screen.getByText("Empty folder")).toBeTruthy();
  expect(screen.getByText("Showing the first 2,000 entries.")).toBeTruthy();
});

test("tree failure words", async () => {
  for (const [code, text] of [
    ["root_unavailable", "The folder is not where it was registered."],
    ["overloaded", "This folder could not be listed."],
  ] as [string, string][]) {
    const api = fakeApi({
      tree: vi.fn(async () => {
        throw new CodeApiError(code);
      }),
    });
    const view = render(
      <FileTree api={api} workspaceId={WS} selected={null} onSelectFile={vi.fn()} />,
    );
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(text);
    view.unmount();
  }
});

test("tree reloads from the root on a new workspace and drops the stale answer", async () => {
  const first = deferred<CodeTree>();
  const api = fakeApi({
    tree: vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(tree([{ name: "fresh.ts", type: "file", size: 1 }])),
  });
  const view = render(
    <FileTree api={api} workspaceId={WS} selected={null} onSelectFile={vi.fn()} />,
  );
  const signal = (api.tree as ReturnType<typeof vi.fn>).mock.calls[0]?.[2] as AbortSignal;
  view.rerender(
    <FileTree
      api={api}
      workspaceId="00000000-0000-4000-8000-000000000002"
      selected={null}
      onSelectFile={vi.fn()}
    />,
  );
  await settle();
  expect(signal.aborted).toBe(true);
  await act(async () => first.resolve(tree([{ name: "stale.ts", type: "file", size: 1 }])));
  expect(screen.queryByText("stale.ts")).toBeNull();
  expect(screen.getByText("fresh.ts")).toBeTruthy();
});

test("quick open debounces, aborts stale requests and renders hits as text", async () => {
  vi.useFakeTimers();
  const slow = deferred<{ hits: string[]; truncated: boolean }>();
  const api = fakeApi({
    search: vi
      .fn()
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce({ hits: [XSS, "b.ts"], truncated: true }),
  });
  const onSelectFile = vi.fn();
  render(<QuickOpen api={api} workspaceId={WS} enabled onSelectFile={onSelectFile} />);
  const input = screen.getByLabelText("Find a file");
  fireEvent.change(input, { target: { value: "  " } });
  act(() => vi.advanceTimersByTime(500));
  expect(api.search).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: "a" } });
  act(() => vi.advanceTimersByTime(249));
  expect(api.search).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(1));
  expect(api.search).toHaveBeenCalledWith(WS, "a", expect.any(AbortSignal));
  const signal = (api.search as ReturnType<typeof vi.fn>).mock.calls[0]?.[2] as AbortSignal;
  fireEvent.change(input, { target: { value: "ab" } });
  expect(signal.aborted).toBe(true);
  await act(async () => slow.resolve({ hits: ["stale.ts"], truncated: false }));
  expect(screen.queryByText("stale.ts")).toBeNull();
  await act(async () => vi.advanceTimersByTime(250));
  expect(api.search).toHaveBeenLastCalledWith(WS, "ab", expect.any(AbortSignal));
  expect(screen.getByText(XSS).tagName).toBe("BUTTON");
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText("Showing the first 50 matches.")).toBeTruthy();
  fireEvent.click(screen.getByText("b.ts"));
  expect(onSelectFile).toHaveBeenCalledWith("b.ts");
});

test("quick open words: not enabled, no hits, too_large, failure", async () => {
  vi.useFakeTimers();
  const search = vi.fn();
  const api = fakeApi({ search });
  const view = render(
    <QuickOpen api={api} workspaceId={WS} enabled={false} onSelectFile={vi.fn()} />,
  );
  expect(screen.getByText("Trust this folder to search its tracked files.")).toBeTruthy();
  view.unmount();

  const run = async (impl: () => Promise<unknown>, expected: string) => {
    search.mockImplementation(impl);
    const v = render(<QuickOpen api={api} workspaceId={WS} enabled onSelectFile={vi.fn()} />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "x" } });
    await act(async () => vi.advanceTimersByTime(250));
    expect(screen.getByText(expected)).toBeTruthy();
    v.unmount();
  };
  await run(async () => ({ hits: [], truncated: false }), "No matching tracked files.");
  await run(async () => {
    throw new CodeApiError("too_large");
  }, "This repository's index is too large to search.");
  await run(async () => {
    throw new CodeApiError("unavailable");
  }, "Search failed.");
  await run(async () => {
    throw new CodeApiError("not_repository");
  }, "This folder is not a Git repository.");
  await run(async () => {
    throw new CodeApiError("not_repository_root");
  }, "Git features need the repository's top folder.");
});

const entry = (over: Partial<ChangeEntry> = {}): ChangeEntry => ({
  path: "a.ts",
  orig_path: null,
  index: ".",
  worktree: "M",
  kind: "tracked",
  private: false,
  ...over,
});
const changes = (over: Partial<CodeChanges> = {}): CodeChanges => ({
  state: "ready",
  branch: "main",
  head: "0123456789ab",
  detached: false,
  upstream: "origin/main",
  ahead: 2,
  behind: 1,
  entries: [],
  truncated: false,
  ...over,
});
const renderChanges = (
  api: CodeApi,
  refreshKey = 0,
  onSelect = vi.fn(),
  selected: string | null = null,
) =>
  render(
    <ChangesList
      api={api}
      workspaceId={WS}
      refreshKey={refreshKey}
      selected={selected}
      onSelect={onSelect}
    />,
  );

test("changes shows branch line, rows, rename, private chip and selection", async () => {
  const rows = [
    entry(),
    entry({ path: "new.ts", orig_path: "old.ts", index: "R", worktree: "." }),
    entry({ path: XSS, private: true }),
  ];
  const api = fakeApi({ changes: vi.fn(async () => changes({ entries: rows, truncated: true })) });
  const onSelect = vi.fn();
  renderChanges(api, 0, onSelect, "a.ts");
  await settle();
  const line = screen.getByText("0123456789ab").closest("p") as HTMLElement;
  expect(line.textContent).toBe("main 0123456789ab ↑2 ↓1");
  expect(line.querySelector(".mono")?.textContent).toBe("0123456789ab");
  const first = screen.getByRole("button", { name: ".M a.ts" });
  expect(first.getAttribute("aria-current")).toBe("true");
  fireEvent.click(first);
  expect(onSelect).toHaveBeenCalledWith(rows[0]);
  expect(screen.getByRole("button", { name: "R. old.ts → new.ts" })).toBeTruthy();
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(screen.getByText("Private")).toBeTruthy();
  expect(screen.getByText(/alert\(1\)/)).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText("Showing the first 2,000 changes.")).toBeTruthy();
});

test("changes detached, no ahead/behind and empty list", async () => {
  const api = fakeApi({
    changes: vi.fn(async () =>
      changes({ detached: true, branch: null, ahead: null, behind: null }),
    ),
  });
  renderChanges(api);
  await settle();
  expect(screen.getByText("Detached at", { exact: false }).textContent).toBe(
    "Detached at 0123456789ab",
  );
  expect(screen.getByText("No changes.")).toBeTruthy();
});

test("changes words for each failure and for unknown state", async () => {
  const cases: [string, string][] = [
    ["trust_required", "Trust this folder in Settings → Workspaces to see its Git changes."],
    ["not_repository", "This folder is not a Git repository."],
    ["not_repository_root", "Git features need the repository's top folder."],
    ["git_unavailable", "Git is not available."],
    ["unavailable", "Git is not available."],
  ];
  for (const [code, text] of cases) {
    const api = fakeApi({
      changes: vi.fn(async () => {
        throw new CodeApiError(code);
      }),
    });
    const view = renderChanges(api);
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(text);
    view.unmount();
  }
  renderChanges(fakeApi({ changes: vi.fn(async () => changes({ state: "unknown" })) }));
  await settle();
  expect(screen.getByRole("alert").textContent).toBe("Git's status could not be read.");
});

test("changes reloads on refreshKey and drops the stale answer", async () => {
  const first = deferred<CodeChanges>();
  const api = fakeApi({
    changes: vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(changes({ entries: [entry({ path: "fresh.ts" })] })),
  });
  const view = renderChanges(api, 0);
  view.rerender(
    <ChangesList api={api} workspaceId={WS} refreshKey={1} selected={null} onSelect={vi.fn()} />,
  );
  await settle();
  await act(async () => first.resolve(changes({ entries: [entry({ path: "stale.ts" })] })));
  expect(screen.queryByText("stale.ts", { exact: false })).toBeNull();
  expect(screen.getByText("fresh.ts", { exact: false })).toBeTruthy();
  expect(api.changes).toHaveBeenCalledTimes(2);
});
