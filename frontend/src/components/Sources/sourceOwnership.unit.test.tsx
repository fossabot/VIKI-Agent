import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { SessionRootJson } from "../../clients/code/rootsApi";
import type {
  ConversationSearchApi,
  ToolSourcesResult,
} from "../../clients/conversations/conversationSearchApi";
import type { RootsSnapshot } from "../../state/sessionRoots";
import type { VaultReadApi } from "../../state/vaultApi";
import { emptyView, type Turn } from "../../state/view";
import { ChatSources } from "./ChatSources";
import { type PublishSourcesInput, usePublishSources } from "./publishSources";
import { OPEN_DELAY_MS } from "./SourceChip";
import { SourcesStrip } from "./SourcesStrip";
import { resetSourcesEnv, useSourcesEnv } from "./sourcesEnv";

const SID = "chat-a";
const ROOT: SessionRootJson = { session_id: SID, workspace_id: "vault-a", worktree_id: null };
const reply: Turn = {
  id: "reply",
  user: null,
  status: "success",
  history: false,
  usage: null,
  durationMs: 0,
  blocks: [
    { kind: "text", id: "text", text: "Alpha reply", done: true },
    {
      kind: "tools",
      id: "web",
      rows: [
        {
          id: "web",
          tool: "WebFetch",
          row: { head: "WebFetch", rest: "https://example.com", tail: "" },
          done: true,
          isError: false,
          summary: "",
          preview: "",
        },
      ],
    },
  ],
};

function read(ref: string): Turn {
  return {
    ...reply,
    blocks: [
      {
        kind: "tools",
        id: "read",
        rows: [
          {
            id: "read",
            tool: "Read",
            row: { head: "Read", rest: ref, tail: "" },
            done: true,
            isError: false,
            summary: "",
            preview: "",
            artifactStart: {
              artifact_id: "read",
              kind: "file",
              operation: "read",
              path_state: "workspace_relative",
              path: ref,
            },
          },
        ],
      },
    ],
  };
}

function roots(root?: SessionRootJson, complete = true): RootsSnapshot {
  return {
    roots: new Map(root ? [[root.session_id, root]] : []),
    worktrees: [],
    loaded: true,
    rootsComplete: complete,
  };
}

const unused = (): Promise<never> => Promise.reject(new Error("not used in this test"));
const vault = (note: VaultReadApi["note"]): VaultReadApi => ({
  entries: unused,
  note,
  backlinks: unused,
  history: unused,
});
const search = (toolSources: NonNullable<ConversationSearchApi["toolSources"]>) => ({
  search: unused,
  anchor: unused,
  toolSources,
});

function input(overrides: Partial<PublishSourcesInput> = {}): PublishSourcesInput {
  return {
    workspaceId: "vault-a",
    kind: "vault",
    chatOnly: false,
    roots: roots(),
    rootsTracked: true,
    save: "editor",
    openNote: vi.fn(),
    openFolder: vi.fn(),
    openUrl: vi.fn(),
    openDraft: vi.fn(),
    handoff: vi.fn(() => true),
    ...overrides,
  };
}

type Mode = "save" | "chat" | "strip";
function Harness({
  data,
  mode = "save",
  sessionId = SID,
  noteRef = "same.md",
}: {
  data: PublishSourcesInput;
  mode?: Mode;
  sessionId?: string;
  noteRef?: string;
}) {
  usePublishSources(data);
  if (mode === "chat") {
    return <ChatSources view={{ ...emptyView(sessionId), turns: [read(noteRef)] }} />;
  }
  return (
    <SourcesStrip
      key={mode === "save" ? sessionId : undefined}
      turn={mode === "save" ? reply : read(noteRef)}
      sessionId={sessionId}
    />
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept;
    reject = refuse;
  });
  return { promise, resolve, reject };
}

function startSave(
  save: "editor" | "handoff" = "editor",
  overrides: Partial<PublishSourcesInput> = {},
) {
  const pending = deferred<ToolSourcesResult>();
  const toolSources = vi.fn<NonNullable<ConversationSearchApi["toolSources"]>>(
    () => pending.promise,
  );
  const openDraft = vi.fn<PublishSourcesInput["openDraft"]>();
  const handoff = vi.fn<PublishSourcesInput["handoff"]>(() => true);
  const data = input({
    save,
    search: search(toolSources),
    vault: vault(unused),
    openDraft,
    handoff,
    ...overrides,
  });
  const mounted = render(<Harness data={data} />);
  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  expect(toolSources).toHaveBeenCalledTimes(1);
  expect(toolSources.mock.calls[0]?.[0]).toBe(SID);
  return { mounted, data, pending, toolSources, openDraft, handoff };
}

async function finish(pending: ReturnType<typeof deferred<ToolSourcesResult>>) {
  await act(async () => {
    pending.resolve({ request_id: "request", state: "ready", reason: "", items: [] });
  });
}

async function hover(ref = "same.md") {
  fireEvent.mouseEnter(screen.getByRole("button", { name: `Read note ${ref}` }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(OPEN_DELAY_MS);
  });
}

beforeEach(resetSourcesEnv);
afterEach(() => {
  cleanup();
  resetSourcesEnv();
  vi.useRealTimers();
});

test.each(["editor", "handoff"] as const)(
  "pending %s save cannot reach a replacement workspace after the old strip unmounts",
  async (save) => {
    const { mounted, data, pending, openDraft, handoff } = startSave(save);
    const replacementDraft = vi.fn();
    const replacementHandoff = vi.fn(() => true);
    mounted.rerender(
      <Harness
        data={{
          ...data,
          workspaceId: "vault-b",
          openDraft: replacementDraft,
          handoff: replacementHandoff,
        }}
        sessionId="chat-b"
      />,
    );
    await finish(pending);
    expect(openDraft).not.toHaveBeenCalled();
    expect(handoff).not.toHaveBeenCalled();
    expect(replacementDraft).not.toHaveBeenCalled();
    expect(replacementHandoff).not.toHaveBeenCalled();
  },
);

test.each(["resolve", "reject"] as const)(
  "revoked save remains unavailable when the source lookup %ss",
  async (settlement) => {
    const { mounted, data, pending, openDraft, handoff } = startSave();
    mounted.rerender(<Harness data={{ ...data, save: null }} />);
    if (settlement === "resolve") await finish(pending);
    else await act(async () => pending.reject(new Error("unavailable")));
    expect(openDraft).not.toHaveBeenCalled();
    expect(handoff).not.toHaveBeenCalled();
  },
);

test("returning from B to A does not restore the old save generation", async () => {
  const { mounted, data, pending, openDraft } = startSave();
  mounted.rerender(<Harness data={{ ...data, workspaceId: "vault-b" }} sessionId="chat-b" />);
  mounted.rerender(<Harness data={data} />);
  await finish(pending);
  expect(openDraft).not.toHaveBeenCalled();
});

test("unmount abandons a pending handoff", async () => {
  const { mounted, pending, handoff } = startSave("handoff");
  mounted.unmount();
  await finish(pending);
  expect(handoff).not.toHaveBeenCalled();
});

test.each(["search", "vault", "toolSources"] as const)(
  "replacing the %s client identity abandons a pending save",
  async (replacement) => {
    const { mounted, data, pending, openDraft } = startSave();
    const changed = { ...data };
    if (replacement === "search")
      changed.search = { ...data.search, search: unused, anchor: unused };
    else if (replacement === "vault") changed.vault = vault(unused);
    else if (data.search) data.search.toolSources = vi.fn(unused);
    mounted.rerender(<Harness data={changed} />);
    await finish(pending);
    expect(openDraft).not.toHaveBeenCalled();
  },
);

test("changing the same source session's root abandons its pending save", async () => {
  const { mounted, data, pending, openDraft } = startSave("editor", {
    roots: roots({ ...ROOT, workspace_id: "code-a" }),
  });
  // Both source roots are outside the destination vault; a boolean-only check would miss it.
  mounted.rerender(
    <Harness data={{ ...data, roots: roots({ ...ROOT, workspace_id: "code-b" }) }} />,
  );
  await finish(pending);
  expect(openDraft).not.toHaveBeenCalled();
});

test("revoked legacy source admission abandons a pending save", async () => {
  const { mounted, data, pending, openDraft } = startSave();
  mounted.rerender(<Harness data={{ ...data, roots: roots(undefined, false) }} />);
  await finish(pending);
  expect(openDraft).not.toHaveBeenCalled();
});

test("equal root refresh keeps the pending save and uses the latest callback", async () => {
  const { mounted, data, pending, openDraft, toolSources } = startSave("editor", {
    roots: roots(ROOT),
  });
  const latestDraft = vi.fn();
  mounted.rerender(
    <Harness data={{ ...data, roots: roots({ ...ROOT }), openDraft: latestDraft }} />,
  );
  await finish(pending);
  expect(openDraft).not.toHaveBeenCalled();
  expect(latestDraft).toHaveBeenCalledTimes(1);
  expect(latestDraft.mock.calls[0]?.[0].ref).toBe("Alpha reply.md");
  expect(toolSources).toHaveBeenCalledTimes(1);
});

test("a retained obsolete environment refuses a new save before requesting sources", async () => {
  const toolSources = vi.fn(
    async (): Promise<ToolSourcesResult> => ({
      request_id: "request",
      state: "ready",
      reason: "",
      items: [],
    }),
  );
  const data = input({ search: search(toolSources) });
  const mounted = render(<Harness data={data} />);
  const observed = renderHook(useSourcesEnv);
  const oldEnv = observed.result.current;
  mounted.rerender(<Harness data={{ ...data, workspaceId: "vault-b" }} />);
  await act(async () => {
    expect(await oldEnv?.saveAsNote?.(SID, reply)).toBe("unavailable");
  });
  expect(toolSources).not.toHaveBeenCalled();
});

test.each(["strip"] as const)(
  "%s rows replace a ready same-ref peek when the workspace changes",
  async (mode) => {
    vi.useFakeTimers();
    const alpha = vi.fn(async () => ({
      note: { text: "## Summary\nAlpha summary", frontmatter: {} },
    }));
    const beta = vi.fn(async () => ({
      note: { text: "## Summary\nBeta summary", frontmatter: {} },
    }));
    const data = input({ vault: vault(alpha) });
    const mounted = render(<Harness data={data} mode={mode} />);
    await hover();
    expect(screen.getByText("Alpha summary")).toBeTruthy();
    fireEvent.mouseLeave(screen.getByRole("button", { name: "Read note same.md" }));
    mounted.rerender(
      <Harness data={{ ...data, workspaceId: "vault-b", vault: vault(beta) }} mode={mode} />,
    );
    await hover();
    expect(beta).toHaveBeenCalledTimes(1);
    expect(beta.mock.calls[0]?.slice(0, 2)).toEqual(["vault-b", "same.md"]);
    expect(screen.queryByText("Alpha summary")).toBeNull();
    expect(screen.getByText("Beta summary")).toBeTruthy();
  },
);

test.each(["strip"] as const)(
  "%s rows abort an old peek and ignore its late completion after a workspace switch",
  async (mode) => {
    vi.useFakeTimers();
    const pending = deferred<Record<string, unknown>>();
    const alpha = vi.fn<VaultReadApi["note"]>(() => pending.promise);
    const beta = vi.fn(async () => ({
      note: { text: "## Summary\nBeta summary", frontmatter: {} },
    }));
    const data = input({ vault: vault(alpha) });
    const mounted = render(<Harness data={data} mode={mode} />);
    await hover();
    const signal = alpha.mock.calls[0]?.[2];
    expect(signal?.aborted).toBe(false);
    mounted.rerender(
      <Harness data={{ ...data, workspaceId: "vault-b", vault: vault(beta) }} mode={mode} />,
    );
    await hover();
    await act(async () => {
      pending.resolve({ note: { text: "## Summary\nAlpha summary", frontmatter: {} } });
    });
    expect(signal?.aborted).toBe(true);
    expect(beta).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Alpha summary")).toBeNull();
    expect(screen.getByText("Beta summary")).toBeTruthy();
  },
);

test.each(["strip"] as const)(
  "%s rows reuse a same-workspace peek through an equal root refresh",
  async (mode) => {
    vi.useFakeTimers();
    const note = vi.fn(async () => ({
      note: { text: "## Summary\nSame summary", frontmatter: {} },
    }));
    const data = input({ roots: roots(ROOT), vault: vault(note) });
    const mounted = render(<Harness data={data} mode={mode} />);
    await hover();
    const button = screen.getByRole("button", { name: "Read note same.md" });
    fireEvent.mouseLeave(button);
    mounted.rerender(<Harness data={{ ...data, roots: roots({ ...ROOT }) }} mode={mode} />);
    expect(screen.getByRole("button", { name: "Read note same.md" })).toBe(button);
    await hover();
    expect(screen.getByText("Same summary")).toBeTruthy();
    expect(note).toHaveBeenCalledTimes(1);
  },
);

test("a different exact ref in the same workspace gets its own peek", async () => {
  vi.useFakeTimers();
  const note = vi.fn<VaultReadApi["note"]>(async (_workspaceId, ref) => ({
    note: { text: `## Summary\nSummary for ${ref}`, frontmatter: {} },
  }));
  const data = input({ vault: vault(note) });
  const mounted = render(<Harness data={data} mode="strip" />);
  await hover();
  mounted.rerender(<Harness data={data} mode="strip" noteRef="other/same.md" />);
  await hover("other/same.md");
  expect(screen.queryByText("Summary for same.md")).toBeNull();
  expect(screen.getByText("Summary for other/same.md")).toBeTruthy();
  expect(note).toHaveBeenCalledTimes(2);
});
