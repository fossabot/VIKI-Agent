import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { RootsApi, SessionRootJson } from "../../clients/code/rootsApi";
import { noteRoot, refreshRoots, resetRoots, useRoots } from "../../state/sessionRoots";
import type { Turn } from "../../state/view";
import { usePublishSources } from "./publishSources";
import { SourcesStrip } from "./SourcesStrip";
import { resetSourcesEnv } from "./sourcesEnv";

const SID = "11111111-1111-4111-8111-111111111111";
const VAULT = "22222222-2222-4222-8222-222222222222";
const CODE = "33333333-3333-4333-8333-333333333333";
const codeRoot: SessionRootJson = { session_id: SID, workspace_id: CODE, worktree_id: null };
const read: Turn = {
  id: "reply",
  user: null,
  status: "success",
  history: false,
  usage: null,
  durationMs: 0,
  blocks: [
    {
      kind: "tools",
      id: "read",
      rows: [
        {
          id: "read",
          tool: "Read",
          row: { head: "Read", rest: "README.md", tail: "" },
          done: true,
          isError: false,
          summary: "",
          preview: "",
          artifactStart: {
            artifact_id: "artifact",
            kind: "file",
            operation: "read",
            path_state: "workspace_relative",
            path: "README.md",
          },
        },
      ],
    },
  ],
};

const api = (roots: SessionRootJson[], truncated = false) =>
  ({
    roots: async () => ({ roots, truncated }),
    worktrees: async () => ({ worktrees: [], truncated: false }),
  }) as unknown as RootsApi;

const otherRoots = () =>
  Array.from({ length: 4096 }, (_, i) => ({
    ...codeRoot,
    session_id: `44444444-4444-4444-8444-${i.toString(16).padStart(12, "0")}`,
  }));

function Sources({ openNote }: { openNote: (ref: string) => void }) {
  const roots = useRoots();
  usePublishSources({
    workspaceId: VAULT,
    kind: "vault",
    chatOnly: false,
    roots,
    rootsTracked: true,
    save: null,
    openNote,
    openFolder: () => {},
    openUrl: () => {},
    openDraft: () => {},
    handoff: () => false,
  });
  return <SourcesStrip turn={read} sessionId={SID} />;
}

const chip = () => screen.queryByRole("button", { name: "Read note README.md" });

beforeEach(() => {
  resetRoots();
  resetSourcesEnv();
});
afterEach(() => {
  cleanup();
  resetRoots();
  resetSourcesEnv();
});

test("an older complete list cannot turn an acknowledged code read into an active-vault note", async () => {
  await refreshRoots(api([]));
  let resolve!: (value: { roots: SessionRootJson[]; truncated: boolean }) => void;
  const pending = new Promise<{ roots: SessionRootJson[]; truncated: boolean }>((yes) => {
    resolve = yes;
  });
  const refreshing = refreshRoots({ ...api([]), roots: () => pending });
  noteRoot(codeRoot);
  const openNote = vi.fn();
  render(<Sources openNote={openNote} />);
  expect(chip()).toBeNull();
  await act(async () => {
    resolve({ roots: [], truncated: false });
    await refreshing;
  });
  expect(chip()).toBeNull();
  expect(openNote).not.toHaveBeenCalled();
});

test("a current truncated list cannot retarget a known code chat into the active vault", async () => {
  await refreshRoots(api([codeRoot]));
  const openNote = vi.fn();
  render(<Sources openNote={openNote} />);
  expect(chip()).toBeNull();
  await act(() => refreshRoots(api(otherRoots(), true)));
  expect(chip()).toBeNull();
  expect(openNote).not.toHaveBeenCalled();
});

test("an unlisted chat under incomplete root coverage gets no active-vault note action", async () => {
  await refreshRoots(api(otherRoots(), true));
  const openNote = vi.fn();
  render(<Sources openNote={openNote} />);
  expect(chip()).toBeNull();
  expect(openNote).not.toHaveBeenCalled();
});

test("a current complete list retaining the code row keeps the vault action unavailable", async () => {
  await refreshRoots(api([codeRoot]));
  const openNote = vi.fn();
  render(<Sources openNote={openNote} />);
  expect(chip()).toBeNull();
  expect(openNote).not.toHaveBeenCalled();
});

test("a complete unrecorded legacy chat retains its exact active-vault note action", async () => {
  await refreshRoots(api([]));
  const opened = vi.fn();
  render(<Sources openNote={(ref) => opened({ workspace_id: VAULT, ref })} />);
  fireEvent.click(screen.getByRole("button", { name: "Read note README.md" }));
  expect(opened).toHaveBeenCalledExactlyOnceWith({ workspace_id: VAULT, ref: "README.md" });
});
