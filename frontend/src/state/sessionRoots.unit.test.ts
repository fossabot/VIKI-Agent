import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../clients/app/shellApi";
import type { RootsApi, SessionRootJson, WorktreeJson } from "../clients/code/rootsApi";
import { workspaceFixture } from "../testShell";
import {
  codeFolderOf,
  folderLabel,
  noteRoot,
  noteWorktree,
  refreshRoots,
  resetRoots,
  useRoots,
} from "./sessionRoots";

const S = "11111111-1111-4111-8111-111111111111";
const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";
const U = "44444444-4444-4444-8444-444444444444";
const root = (over: Partial<SessionRootJson> = {}): SessionRootJson => ({
  session_id: S,
  workspace_id: W,
  worktree_id: null,
  ...over,
});
const tree = (over: Partial<WorktreeJson> = {}): WorktreeJson => ({
  worktree_id: T,
  workspace_id: W,
  branch: "viki/x",
  base_oid: "a".repeat(40),
  path: "C:\\wt",
  state: "ready",
  created_at: "t",
  updated_at: "t",
  ...over,
});
const api = (roots: SessionRootJson[], worktrees: WorktreeJson[], truncated = false) =>
  ({
    roots: vi.fn(async () => ({ roots, truncated })),
    worktrees: vi.fn(async () => ({ worktrees, truncated: false })),
  }) as unknown as RootsApi;
const folders = [{ workspace_id: W, name: "code" }];
const codeFolder = (over: Partial<WorkspaceJson> = {}): WorkspaceJson =>
  workspaceFixture({ kind: "code", workspace_id: W, ...over });

function deferredRoots() {
  let resolve!: (roots: SessionRootJson[]) => void;
  const promise = new Promise<{ roots: SessionRootJson[]; truncated: boolean }>((yes) => {
    resolve = (roots) => yes({ roots, truncated: false });
  });
  return { resolve, api: { ...api([], []), roots: () => promise } };
}

function cappedRoots(): SessionRootJson[] {
  return Array.from({ length: 4096 }, (_, i) =>
    root({ session_id: `55555555-5555-4555-8555-${i.toString(16).padStart(12, "0")}` }),
  );
}

beforeEach(resetRoots);

test("refreshRoots retains immutable roots and replaces the worktree snapshot", async () => {
  const { result } = renderHook(() => useRoots());
  expect(result.current.loaded).toBe(false);
  expect(result.current.rootsComplete).toBe(false);
  await act(() => refreshRoots(api([root()], [tree()])));
  expect(result.current.loaded).toBe(true);
  expect(result.current.rootsComplete).toBe(true);
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.worktrees).toEqual([tree()]);
  await act(() => refreshRoots(api([], [])));
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.worktrees).toEqual([]);
});

test("a refresh started before noteRoot retains the acknowledged identity", async () => {
  const { result } = renderHook(() => useRoots());
  const pending = deferredRoots();
  const refreshing = refreshRoots(pending.api);
  act(() => noteRoot(root()));
  await act(async () => {
    pending.resolve([]);
    await refreshing;
  });
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.rootsComplete).toBe(true);
});

test("only the latest refresh owns lists and coverage", async () => {
  const { result } = renderHook(() => useRoots());
  const pending = deferredRoots();
  const older = refreshRoots(pending.api);
  await act(() => refreshRoots(api([root()], [tree({ state: "removed" })], true)));
  const current = result.current;
  expect(current.rootsComplete).toBe(false);
  await act(async () => {
    pending.resolve([]);
    await older;
  });
  expect(result.current).toBe(current);
  expect(result.current.worktrees).toEqual([tree({ state: "removed" })]);
});

test("an aborted refresh cannot publish even when its client ignores the signal", async () => {
  const { result } = renderHook(() => useRoots());
  await act(() => refreshRoots(api([root()], [tree()])));
  const current = result.current;
  const pending = deferredRoots();
  const controller = new AbortController();
  const refreshing = refreshRoots(pending.api, controller.signal);
  controller.abort();
  await act(async () => {
    pending.resolve([root({ session_id: U })]);
    await refreshing;
  });
  expect(result.current).toBe(current);
});

test("reset clears this lifetime and fences its pending refresh", async () => {
  const { result } = renderHook(() => useRoots());
  act(() => noteRoot(root()));
  const pending = deferredRoots();
  const refreshing = refreshRoots(pending.api);
  act(resetRoots);
  const current = result.current;
  await act(async () => {
    pending.resolve([root()]);
    await refreshing;
  });
  expect(result.current).toBe(current);
  expect(result.current.roots.size).toBe(0);
  expect(result.current.loaded).toBe(false);
  expect(result.current.rootsComplete).toBe(false);
});

test("capped replies retain known identities within 4096 rows and keep overflow incomplete", async () => {
  const { result } = renderHook(() => useRoots());
  act(() => noteRoot(root()));
  const rows = cappedRoots();
  await act(() => refreshRoots(api(rows, [], true)));
  expect(result.current.roots.size).toBe(4096);
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.roots.has(rows[4095]?.session_id ?? "")).toBe(false);
  expect(result.current.rootsComplete).toBe(false);
  await act(() => refreshRoots(api(rows, [], true)));
  await act(() => refreshRoots(api([], [])));
  expect(result.current.roots.size).toBe(4096);
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.rootsComplete).toBe(false);
});

test("noteRoot at capacity cannot expand the map or grant missing-root fallback", async () => {
  const { result } = renderHook(() => useRoots());
  const rows = cappedRoots();
  await act(() => refreshRoots(api(rows, [])));
  expect(result.current.rootsComplete).toBe(true);
  act(() => noteRoot(root()));
  expect(result.current.roots.size).toBe(4096);
  expect(result.current.roots.has(S)).toBe(false);
  expect(result.current.rootsComplete).toBe(false);
  await act(() => refreshRoots(api(rows, [])));
  expect(result.current.rootsComplete).toBe(false);
  act(resetRoots);
  await act(() => refreshRoots(api([root()], [])));
  expect(result.current.roots.size).toBe(1);
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.rootsComplete).toBe(true);
});

test("a failed refresh keeps the snapshot and rethrows", async () => {
  const { result } = renderHook(() => useRoots());
  await act(() => refreshRoots(api([root()], [tree()])));
  const failing = {
    roots: vi.fn(async () => ({ roots: [], truncated: false })),
    worktrees: vi.fn(async () => {
      throw new Error("boom");
    }),
  } as unknown as RootsApi;
  await expect(refreshRoots(failing)).rejects.toThrow("boom");
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.worktrees).toEqual([tree()]);
});

test("noteRoot and noteWorktree insert or replace", () => {
  const { result } = renderHook(() => useRoots());
  act(() => noteRoot(root()));
  act(() => noteWorktree(tree()));
  act(() => noteWorktree(tree({ state: "removed" })));
  expect(result.current.roots.get(S)).toEqual(root());
  expect(result.current.worktrees).toEqual([tree({ state: "removed" })]);
  act(() => noteWorktree(tree({ worktree_id: W })));
  expect(result.current.worktrees).toHaveLength(2);
});

test("folderLabel cases", () => {
  const snap = (r: SessionRootJson | null, w: WorktreeJson[] = []) => ({
    roots: new Map(r ? [[r.session_id, r]] : []),
    worktrees: w,
    loaded: true,
    rootsComplete: true,
  });
  const label = (s: ReturnType<typeof snap>, active: string | null = null) =>
    folderLabel(S, s, folders, active);
  expect(label(snap(null))).toBeNull();
  expect(label(snap(root()), W)).toBeNull();
  expect(label(snap(root()))).toBe("code");
  expect(label(snap(root({ workspace_id: T })))).toBe("Unknown folder");
  expect(label(snap(root({ worktree_id: T }), [tree()]), W)).toBe("code · viki/x");
  expect(label(snap(root({ worktree_id: T }), [tree({ state: "removed" })]))).toBe(
    "code · viki/x (removed)",
  );
  expect(label(snap(root({ worktree_id: T })))).toBe("code (removed)");
});

test("multiple folder labels retain selected ID order even when primary is active", async () => {
  const selected = root({ additional_workspace_ids: Object.freeze([U, T]) });
  const { result } = renderHook(() => useRoots());
  await act(() => refreshRoots(api([selected], [])));
  const named = [
    { workspace_id: T, name: "library" },
    { workspace_id: W, name: "code" },
    { workspace_id: U, name: "app" },
  ];
  for (const active of [null, W, U, T]) {
    expect(folderLabel(S, result.current, named, active)).toBe("code · app · library");
    expect(folderLabel(S, result.current, [...named].reverse(), active)).toBe(
      "code · app · library",
    );
  }
  expect(result.current.roots.get(S)).toEqual(selected);
  expect(result.current.roots.get(S)?.additional_workspace_ids).toEqual([U, T]);
});

test("multiple labels resolve exact IDs, preserve duplicate names and expose unknown folders", () => {
  const { result } = renderHook(() => useRoots());
  act(() => noteRoot(root({ additional_workspace_ids: [U, T] })));
  expect(folderLabel(S, result.current, [...folders, { workspace_id: U, name: "code" }], W)).toBe(
    "code · code · Unknown folder",
  );
  expect(folderLabel(S, result.current, [{ workspace_id: T, name: "code" }], W)).toBe(
    "Unknown folder · Unknown folder · code",
  );
  expect(result.current.roots.get(S)?.additional_workspace_ids).toEqual([U, T]);
});

test("empty additional lists retain single-folder and worktree labels", () => {
  const { result } = renderHook(() => useRoots());
  act(() => noteRoot(root({ additional_workspace_ids: [] })));
  expect(folderLabel(S, result.current, folders, W)).toBeNull();
  expect(folderLabel(S, result.current, folders, null)).toBe("code");
  act(() => noteRoot(root({ additional_workspace_ids: [], worktree_id: T })));
  act(() => noteWorktree(tree()));
  expect(folderLabel(S, result.current, folders, W)).toBe("code · viki/x");
});

test("codeFolderOf cases", () => {
  const code = codeFolder({ workspace_id: W });
  const otherKind = workspaceFixture({ kind: "vault", workspace_id: T, name: "vault" });
  const unavailable = codeFolder({ workspace_id: U, root_available: false });
  const allFolders = [code, otherKind, unavailable];
  const notLoaded = { roots: new Map(), worktrees: [], loaded: false, rootsComplete: false };
  const noRoot = { roots: new Map(), worktrees: [], loaded: true, rootsComplete: true };
  const withRoot = {
    roots: new Map([[S, root()]]),
    worktrees: [],
    loaded: true,
    rootsComplete: true,
  };
  const worktreeRoot = {
    roots: new Map([[S, root({ worktree_id: T })]]),
    worktrees: [],
    loaded: true,
    rootsComplete: true,
  };

  // tracked and not loaded gives null, whatever the other inputs say
  expect(codeFolderOf(S, notLoaded, true, allFolders, W)).toBeNull();
  // a root outside any worktree resolves through its own workspace
  expect(codeFolderOf(S, withRoot, false, allFolders, null)).toEqual(code);
  // a root inside a worktree never resolves a code folder
  expect(codeFolderOf(S, worktreeRoot, false, allFolders, W)).toBeNull();
  // no recorded root falls back to the active workspace
  expect(codeFolderOf(S, noRoot, false, allFolders, W)).toEqual(code);
  // a resolved id whose folder is not a code folder does not resolve
  expect(codeFolderOf(S, noRoot, false, allFolders, T)).toBeNull();
  // a resolved code folder whose root is unavailable does not resolve
  expect(codeFolderOf(S, noRoot, false, allFolders, U)).toBeNull();
});
