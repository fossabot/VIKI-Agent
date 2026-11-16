import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  KnowledgeError,
  type MemoryItem,
  type MemoryListing,
  type VaultKnowledgeApi,
} from "../../state/vaultKnowledgeApi";
import { MemoryPanel, STATE_EXPLANATION } from "./MemoryPanel";

const W = "00000000-0000-4000-8000-000000000001";
const ID = "00000000-0000-4000-8000-0000000000aa";
const H1 = "a".repeat(64);
const H2 = "b".repeat(64);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function item(
  overrides: Partial<Omit<MemoryItem, "fields">> & { fields?: Partial<MemoryItem["fields"]> } = {},
) {
  const { fields, ...rest } = overrides;
  return {
    ref: "memory/a.md",
    sha256: H1,
    state: "candidate",
    claim: "<b>Favorite color is blue</b>",
    superseded_by: null,
    ...rest,
    fields: {
      id: ID,
      kind: "owner",
      scope: "vault",
      status: "candidate",
      created: "2026-10-01",
      reviewed: null,
      creator: null,
      sources: [],
      supersedes: null,
      retired_by: null,
      ...fields,
    },
  } satisfies MemoryItem;
}
const listing = (items: MemoryItem[], extra: Partial<MemoryListing> = {}): MemoryListing => ({
  items,
  invalid: [],
  partial: false,
  ...extra,
});
const WRITTEN = {
  outcome: "written" as const,
  memory_id: ID,
  ref: "memory/a.md",
  sha256: H2,
  operation_id: "op",
  created_folders: [],
};

function fakeApi(overrides: Partial<VaultKnowledgeApi> = {}) {
  const api = {
    memories: vi.fn(async () => listing([item()])),
    createMemory: vi.fn(async () => ({
      outcome: "written" as const,
      memory_id: ID,
      ref: "memory/a.md",
      sha256: H2,
      operation_id: "op",
      created_folders: [],
      replayed: false,
    })),
    composeMemory: vi.fn(async () => ({ text: "viki_memory: 1\nbody text" })),
    patchMemory: vi.fn(async () => WRITTEN),
    ...overrides,
  } as unknown as VaultKnowledgeApi;
  return api;
}

function mount(api: VaultKnowledgeApi, isProtected = false, onDone = vi.fn()) {
  render(
    <MemoryPanel
      api={api}
      workspaceId={W}
      revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
      protected={isProtected}
      onDone={onDone}
    />,
  );
  return onDone;
}
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

afterEach(cleanup);

describe("MemoryPanel listing", () => {
  it("shows kind, scope, status, effective state and its explanation; claim stays text", async () => {
    mount(fakeApi());
    expect(await screen.findByText("<b>Favorite color is blue</b>")).toBeTruthy();
    expect(document.querySelector(".memory-panel-claim b")).toBeNull();
    const meta = document.querySelector(".memory-panel-meta")?.textContent ?? "";
    for (const part of ["memory/a.md", "Owner", "Vault", "Candidate"]) expect(meta).toContain(part);
    expect(screen.getByText(STATE_EXPLANATION.candidate)).toBeTruthy();
  });

  it("shows pending, not active, when this device has not approved the exact bytes, after a byte change", async () => {
    const approved = item({
      state: "active",
      fields: { status: "approved", reviewed: "2026-10-02" },
    });
    // The file changed by one byte: its status still says approved, the device's hash no longer matches.
    const changed = item({
      sha256: H2,
      state: "pending",
      fields: { status: "approved", reviewed: "2026-10-02" },
    });
    const memories = vi.fn(async () => listing([approved]));
    mount(fakeApi({ memories }));
    expect(await screen.findByText(STATE_EXPLANATION.active)).toBeTruthy();
    memories.mockResolvedValueOnce(listing([changed]));
    // A write elsewhere re-lists: approve is not offered on active, so retire then re-list.
    click("Retire");
    click("Apply");
    await waitFor(() => expect(screen.getByText(STATE_EXPLANATION.pending)).toBeTruthy());
    expect(screen.queryByText(STATE_EXPLANATION.active)).toBeNull();
    expect(STATE_EXPLANATION.pending).toContain("hasn't approved");
  });

  it("says a partial listing is incomplete", async () => {
    mount(fakeApi({ memories: vi.fn(async () => listing([], { partial: true })) }));
    expect(await screen.findByText(/is partial/)).toBeTruthy();
  });

  it("shows an invalid record's raw text and reason with no action", async () => {
    const invalid = [
      {
        ref: "memory/bad.md",
        reason: "bad_grammar",
        text: "<i>raw</i>",
        state: "invalid" as const,
      },
      { ref: "memory/blocked.md", reason: "unreadable", text: null, state: "invalid" as const },
    ];
    mount(fakeApi({ memories: vi.fn(async () => listing([], { invalid })) }));
    expect(await screen.findByText("<i>raw</i>")).toBeTruthy();
    expect(screen.getByText(/bad_grammar/)).toBeTruthy();
    expect(screen.getByText(/reading it was refused/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retire" })).toBeNull();
  });

  it("offers no action on a retired record", async () => {
    mount(
      fakeApi({
        memories: vi.fn(async () =>
          listing([item({ state: "retired", fields: { status: "retired" } })]),
        ),
      }),
    );
    await screen.findByText(STATE_EXPLANATION.retired);
    expect(screen.queryByRole("button", { name: "Retire" })).toBeNull();
  });
});

describe("MemoryPanel actions in an ordinary vault", () => {
  it("approve of a candidate says it writes, then patches with the record's sha256", async () => {
    const api = fakeApi();
    const onDone = mount(api);
    await screen.findByText(STATE_EXPLANATION.candidate);
    click("Approve");
    expect(screen.getByText(/Writes approved status and the review date/)).toBeTruthy();
    expect(api.patchMemory).not.toHaveBeenCalled();
    click("Apply");
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.patchMemory).toHaveBeenCalledWith(W, ID, {
      policyRevision: 3,
      rootPhysicalId: "1:2",
      sha256: H1,
      action: "approve",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("approve of an already approved record says it writes nothing and records a hash only", async () => {
    mount(
      fakeApi({
        memories: vi.fn(async () =>
          listing([item({ state: "pending", fields: { status: "approved" } })]),
        ),
      }),
    );
    await screen.findByText(STATE_EXPLANATION.pending);
    click("Approve");
    expect(screen.getByText(/Nothing is written to the vault/)).toBeTruthy();
  });

  it("retire says provider context cannot be recalled", async () => {
    const api = fakeApi();
    mount(api);
    await screen.findByText(STATE_EXPLANATION.candidate);
    click("Retire");
    expect(screen.getByText(/Context already sent to the provider can't be undone/)).toBeTruthy();
    click("Apply");
    await waitFor(() =>
      expect(api.patchMemory).toHaveBeenCalledWith(
        W,
        ID,
        expect.objectContaining({ action: "retire", retiredBy: null, sha256: H1 }),
      ),
    );
  });

  it("a half-finished correction says so and finishing retires it with the replacement's id", async () => {
    const NEW_ID = "00000000-0000-4000-8000-0000000000bb";
    const api = fakeApi({
      memories: vi.fn(async () =>
        listing([item({ state: "active", superseded_by: NEW_ID, fields: { status: "approved" } })]),
      ),
    });
    mount(api);
    expect(
      await screen.findByText(
        /correction stopped partway through. The new record is already saved/,
      ),
    ).toBeTruthy();
    click("Finish correction");
    expect(screen.getByText(new RegExp(NEW_ID))).toBeTruthy();
    click("Apply");
    await waitFor(() =>
      expect(api.patchMemory).toHaveBeenCalledWith(
        W,
        ID,
        expect.objectContaining({ action: "retire", retiredBy: NEW_ID, sha256: H1 }),
      ),
    );
  });

  it("a record that is not half-corrected shows no such sentence or action", async () => {
    mount(fakeApi());
    await screen.findByText(STATE_EXPLANATION.candidate);
    expect(screen.queryByText(/correction stopped partway through/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Finish correction" })).toBeNull();
  });

  it("correct sends the edited claim; narrow offered only for a personal record", async () => {
    const api = fakeApi({
      memories: vi.fn(async () => listing([item({ fields: { scope: "personal" } })])),
    });
    mount(api);
    await screen.findByText(STATE_EXPLANATION.candidate);
    click("Correct");
    type("New content", "edited claim text");
    click("Apply");
    await waitFor(() =>
      expect(api.patchMemory).toHaveBeenCalledWith(
        W,
        ID,
        expect.objectContaining({ action: "correct", claim: "edited claim text" }),
      ),
    );
    click("Narrow scope");
    click("Apply");
    await waitFor(() =>
      expect(api.patchMemory).toHaveBeenLastCalledWith(
        W,
        ID,
        expect.objectContaining({ action: "narrow_scope" }),
      ),
    );
  });

  it("no narrow action for a vault-scoped record", async () => {
    mount(fakeApi());
    await screen.findByText(STATE_EXPLANATION.candidate);
    expect(screen.queryByRole("button", { name: "Narrow scope" })).toBeNull();
  });

  it("stale_revision re-lists and says the record changed", async () => {
    const memories = vi.fn(async () => listing([item()]));
    const patchMemory = vi.fn(async () => {
      throw new KnowledgeError("stale_revision");
    });
    mount(fakeApi({ memories, patchMemory }));
    await screen.findByText(STATE_EXPLANATION.candidate);
    click("Approve");
    click("Apply");
    expect(await screen.findByText(/has changed/)).toBeTruthy();
    await waitFor(() => expect(memories).toHaveBeenCalledTimes(2));
  });

  it("an unknown code gets the generic sentence, never the code or the message", async () => {
    const patchMemory = vi.fn(async () => {
      throw new KnowledgeError("weird_code_x");
    });
    mount(fakeApi({ patchMemory }));
    await screen.findByText(STATE_EXPLANATION.candidate);
    click("Approve");
    click("Apply");
    expect(await screen.findByText(/Couldn't complete the action/)).toBeTruthy();
    expect(screen.queryByText(/weird_code_x/)).toBeNull();
  });
});

describe("MemoryPanel in a protected vault", () => {
  it("offers no Approve, Retire, Correct or Narrow scope, only Propose after the preview", async () => {
    const createMemory = vi.fn(async () => ({
      outcome: "proposed" as const,
      memory_id: ID,
      ref: "memory/a.md",
      proposal_id: "p1",
      proposed_sha256: H2,
      replayed: false,
    }));
    const api = fakeApi({
      memories: vi.fn(async () => listing([item({ fields: { scope: "personal" } })])),
      createMemory,
    });
    mount(api, true);
    await screen.findByText(STATE_EXPLANATION.candidate);
    for (const name of ["Approve", "Retire", "Correct", "Narrow scope", "Create"])
      expect(screen.queryByRole("button", { name })).toBeNull();
    type("Content", "content to propose");
    click("Review");
    await screen.findByText(/Nothing is written to the vault/);
    click("Propose");
    expect(
      await screen.findByText(/Nothing is written to the vault until you apply the proposal/),
    ).toBeTruthy();
    expect(api.createMemory).toHaveBeenCalledTimes(1);
  });
});

describe("MemoryPanel new memory", () => {
  it("shows the composed text, then creates it with a command id", async () => {
    const api = fakeApi();
    const onDone = mount(api);
    await screen.findByText(STATE_EXPLANATION.candidate);
    type("Content", "body text");
    click("Review");
    expect(await screen.findByText(/viki_memory: 1/)).toBeTruthy();
    expect(api.createMemory).not.toHaveBeenCalled();
    click("Create");
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [, request, commandId] =
      (api.createMemory as ReturnType<typeof vi.fn>).mock.calls[0] ?? [];
    expect(commandId).toMatch(UUID);
    expect(request).toMatchObject({
      policyRevision: 3,
      rootPhysicalId: "1:2",
      kind: "owner",
      scope: "vault",
      claim: "body text",
      sources: [],
    });
  });

  it("reuses the command id on a retry and uses a new one after the draft changes", async () => {
    const createMemory = vi
      .fn()
      .mockRejectedValueOnce(new KnowledgeError("unavailable"))
      .mockRejectedValueOnce(new KnowledgeError("unavailable"))
      .mockResolvedValue({ ...WRITTEN, replayed: false });
    mount(fakeApi({ createMemory }));
    await screen.findByText(STATE_EXPLANATION.candidate);
    type("Content", "body text");
    click("Review");
    await screen.findByText(/viki_memory: 1/);
    click("Create");
    await screen.findByRole("alert");
    click("Create");
    await waitFor(() => expect(createMemory).toHaveBeenCalledTimes(2));
    await screen.findByRole("alert");
    expect(createMemory.mock.calls[1]?.[2]).toBe(createMemory.mock.calls[0]?.[2]);
    type("Content", "body text 2");
    click("Review");
    await screen.findByText(/viki_memory: 1/);
    click("Create");
    await waitFor(() => expect(createMemory).toHaveBeenCalledTimes(3));
    expect(createMemory.mock.calls[2]?.[2]).not.toBe(createMemory.mock.calls[0]?.[2]);
  });

  it("treats replayed: true as a success, not an error", async () => {
    const createMemory = vi.fn(async () => ({ ...WRITTEN, operation_id: null, replayed: true }));
    const onDone = mount(fakeApi({ createMemory }));
    await screen.findByText(STATE_EXPLANATION.candidate);
    type("Content", "body text");
    click("Review");
    await screen.findByText(/viki_memory: 1/);
    click("Create");
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Written to the vault");
  });
});

function holdMemory<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
type PendingMemoryKind =
  | "approve"
  | "retire"
  | "correct"
  | "narrow_scope"
  | "finish"
  | "preview"
  | "create"
  | "propose";
const MEMORY_PENDING_KINDS: PendingMemoryKind[] = [
  "approve",
  "retire",
  "correct",
  "narrow_scope",
  "finish",
  "create",
  "propose",
];
function memoryPendingApi(
  kind: PendingMemoryKind,
  dispatched: (method: "patch" | "preview" | "create") => void = () => {},
) {
  const heldPatch = holdMemory<Awaited<ReturnType<VaultKnowledgeApi["patchMemory"]>>>();
  const heldPreview = holdMemory<Awaited<ReturnType<VaultKnowledgeApi["composeMemory"]>>>();
  const heldCreate = holdMemory<Awaited<ReturnType<VaultKnowledgeApi["createMemory"]>>>();
  const record = item({
    fields: { scope: "personal" },
    superseded_by: kind === "finish" ? "00000000-0000-4000-8000-0000000000bb" : null,
  });
  const api = fakeApi({
    memories: vi.fn(async () => listing([record])),
    patchMemory: vi.fn(() => {
      dispatched("patch");
      return heldPatch.promise;
    }),
    composeMemory: vi.fn(() => {
      dispatched("preview");
      return kind === "preview"
        ? heldPreview.promise
        : Promise.resolve({ text: "literal preview" });
    }),
    createMemory: vi.fn(() => {
      dispatched("create");
      return heldCreate.promise;
    }),
  });
  return { api, heldPatch, heldPreview, heldCreate };
}
function pendingMemoryView(
  api: VaultKnowledgeApi,
  onPending: () => () => void,
  onDone = vi.fn(),
  workspaceId = W,
  isProtected = false,
) {
  return (
    <MemoryPanel
      api={api}
      workspaceId={workspaceId}
      revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
      protected={isProtected}
      onDone={onDone}
      onPending={onPending}
    />
  );
}
async function beginMemory(kind: PendingMemoryKind) {
  if (kind === "preview" || kind === "create" || kind === "propose") {
    type("Content", "exact authored claim");
    await act(async () => click("Review"));
    if (kind !== "preview") await act(async () => click(kind === "propose" ? "Propose" : "Create"));
  } else {
    const labels = {
      approve: "Approve",
      retire: "Retire",
      correct: "Correct",
      narrow_scope: "Narrow scope",
      finish: "Finish correction",
    };
    click(labels[kind]);
    if (kind === "correct") type("New content", "exact corrected claim");
    await act(async () => click("Apply"));
  }
}
async function settleMemory(
  kind: PendingMemoryKind,
  held: ReturnType<typeof memoryPendingApi>,
  rejection = false,
) {
  await act(async () => {
    if (rejection) {
      const target =
        kind === "preview"
          ? held.heldPreview
          : kind === "create" || kind === "propose"
            ? held.heldCreate
            : held.heldPatch;
      target.reject(new KnowledgeError("unavailable"));
      await target.promise.catch(() => {});
    } else if (kind === "preview") held.heldPreview.resolve({ text: "literal preview" });
    else if (kind === "propose")
      held.heldCreate.resolve({
        outcome: "proposed",
        memory_id: ID,
        ref: "memory/a.md",
        proposal_id: "proposal",
        proposed_sha256: H2,
        replayed: false,
      });
    else if (kind === "create") held.heldCreate.resolve({ ...WRITTEN, replayed: false });
    else held.heldPatch.resolve(WRITTEN);
  });
}
function memoryAdmissionArgs(kind: PendingMemoryKind, api: VaultKnowledgeApi, workspace = W) {
  if (kind === "preview")
    expect(api.composeMemory).toHaveBeenCalledExactlyOnceWith(workspace, {
      kind: "owner",
      scope: "vault",
      claim: "exact authored claim",
      sources: [],
    });
  else if (kind === "create" || kind === "propose")
    expect(api.createMemory).toHaveBeenCalledExactlyOnceWith(
      workspace,
      {
        policyRevision: 3,
        rootPhysicalId: "1:2",
        kind: "owner",
        scope: "vault",
        claim: "exact authored claim",
        sources: [],
      },
      expect.stringMatching(UUID),
    );
  else
    expect(api.patchMemory).toHaveBeenCalledExactlyOnceWith(workspace, ID, {
      policyRevision: 3,
      rootPhysicalId: "1:2",
      sha256: H1,
      action: kind === "finish" ? "retire" : kind,
      ...(kind === "retire" || kind === "finish"
        ? { retiredBy: kind === "finish" ? "00000000-0000-4000-8000-0000000000bb" : null }
        : {}),
      ...(kind === "correct" ? { claim: "exact corrected claim" } : {}),
    });
}
it.each(MEMORY_PENDING_KINDS)(
  "captures a pending release through held %s and preserves its admitted service arguments",
  async (kind) => {
    const releases: ReturnType<typeof vi.fn>[] = [];
    const held = memoryPendingApi(kind, (method) => {
      if (method !== "preview") expect(releases.at(-1)).toBeTypeOf("function");
    });
    const events: string[] = [];
    const onPending = vi.fn(() => {
      events.push("start");
      const release = vi.fn(() => events.push("release"));
      releases.push(release);
      return release;
    });
    const done = vi.fn(() => events.push("done"));
    render(pendingMemoryView(held.api, onPending, done, W, kind === "propose"));
    await act(async () => {});
    await beginMemory(kind);
    expect(onPending).toHaveBeenCalledTimes(1);
    expect(releases.at(-1)).not.toHaveBeenCalled();
    expect(done).not.toHaveBeenCalled();
    memoryAdmissionArgs(kind, held.api);
    await settleMemory(kind, held);
    expect(releases.at(-1)).toHaveBeenCalledTimes(1);
    expect(done).toHaveBeenCalledTimes(1);
    expect(events).toEqual(["start", "done", "release"]);
    memoryAdmissionArgs(kind, held.api);
  },
);

it("pure compose preview starts no mutation lease through success or rejection", async () => {
  for (const rejection of [false, true]) {
    const held = memoryPendingApi("preview");
    const begin = vi.fn(() => vi.fn());
    const done = vi.fn();
    const view = render(pendingMemoryView(held.api, begin, done));
    await act(async () => {});
    await beginMemory("preview");
    expect(begin).not.toHaveBeenCalled();
    memoryAdmissionArgs("preview", held.api);
    await settleMemory("preview", held, rejection);
    expect(begin).not.toHaveBeenCalled();
    expect(done).not.toHaveBeenCalled();
    expect(held.api.patchMemory).not.toHaveBeenCalled();
    expect(held.api.createMemory).not.toHaveBeenCalled();
    view.unmount();
  }
});

it.each(MEMORY_PENDING_KINDS)(
  "releases rejected %s once without replay or changing its exact arguments",
  async (kind) => {
    const held = memoryPendingApi(kind);
    const releases: ReturnType<typeof vi.fn>[] = [];
    const begin = vi.fn(() => {
      const release = vi.fn();
      releases.push(release);
      return release;
    });
    const done = vi.fn();
    render(pendingMemoryView(held.api, begin, done, W, kind === "propose"));
    await act(async () => {});
    await beginMemory(kind);
    expect(releases.at(-1)).not.toHaveBeenCalled();
    await settleMemory(kind, held, true);
    expect(releases.at(-1)).toHaveBeenCalledTimes(1);
    expect(done).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeTruthy();
    memoryAdmissionArgs(kind, held.api);
    expect(begin).toHaveBeenCalledTimes(1);
  },
);

it.each(["approve", "create"] as const)(
  "uses the captured %s callback after prop replacement",
  async (kind) => {
    const held = memoryPendingApi(kind);
    const firstReleases: ReturnType<typeof vi.fn>[] = [];
    const first = vi.fn(() => {
      const release = vi.fn();
      firstReleases.push(release);
      return release;
    });
    const replacementRelease = vi.fn();
    const replacement = vi.fn(() => replacementRelease);
    const done = vi.fn();
    const view = render(pendingMemoryView(held.api, first, done));
    await act(async () => {});
    await beginMemory(kind);
    view.rerender(pendingMemoryView(held.api, replacement, done));
    expect(firstReleases.at(-1)).not.toHaveBeenCalled();
    await settleMemory(kind, held);
    expect(firstReleases.at(-1)).toHaveBeenCalledTimes(1);
    expect(replacement).not.toHaveBeenCalled();
    expect(replacementRelease).not.toHaveBeenCalled();
    memoryAdmissionArgs(kind, held.api);
  },
);

function memoryTokens() {
  const tokens = new Set<symbol>();
  const releases: ReturnType<typeof vi.fn>[] = [];
  const begin = vi.fn(() => {
    const token = Symbol();
    tokens.add(token);
    const release = vi.fn(() => tokens.delete(token));
    releases.push(release);
    return release;
  });
  return { tokens, releases, begin };
}
it.each(["approve", "create"] as const)(
  "keeps a new API/workspace %s lease through both old unmounted outcomes",
  async (kind) => {
    for (const rejection of [false, true]) {
      const leases = memoryTokens();
      const old = memoryPendingApi(kind);
      const current = memoryPendingApi(kind);
      const oldDone = vi.fn();
      const currentDone = vi.fn();
      const oldView = render(pendingMemoryView(old.api, leases.begin, oldDone));
      await act(async () => {});
      await beginMemory(kind);
      const oldRelease = leases.releases.at(-1);
      oldView.unmount();
      expect(oldRelease).not.toHaveBeenCalled();
      const other = "00000000-0000-4000-8000-000000000002";
      const currentView = render(pendingMemoryView(current.api, leases.begin, currentDone, other));
      await act(async () => {});
      await beginMemory(kind);
      const currentRelease = leases.releases.at(-1);
      expect(leases.tokens.size).toBe(2);
      await settleMemory(kind, old, rejection);
      expect(oldRelease).toHaveBeenCalledTimes(1);
      expect(currentRelease).not.toHaveBeenCalled();
      expect(leases.tokens.size).toBe(1);
      expect(currentDone).not.toHaveBeenCalled();
      expect(oldDone).not.toHaveBeenCalled();
      memoryAdmissionArgs(kind, old.api);
      memoryAdmissionArgs(kind, current.api, other);
      await settleMemory(kind, current);
      expect(currentRelease).toHaveBeenCalledTimes(1);
      expect(leases.tokens.size).toBe(0);
      currentView.unmount();
    }
  },
);

it("Cancel before action admission starts no request or pending lease", async () => {
  const held = memoryPendingApi("approve");
  const begin = vi.fn(() => vi.fn());
  render(pendingMemoryView(held.api, begin));
  await act(async () => {});
  click("Approve");
  click("Cancel");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(begin).not.toHaveBeenCalled();
  expect(held.api.patchMemory).not.toHaveBeenCalled();
  expect(held.api.createMemory).not.toHaveBeenCalled();
  expect(held.api.composeMemory).not.toHaveBeenCalled();
});

it.each([false, true])(
  "a canceled patch retains its token until settlement without releasing the later request (%s)",
  async (rejection) => {
    const leases = memoryTokens();
    const held = memoryPendingApi("approve");
    const later = holdMemory<Awaited<ReturnType<VaultKnowledgeApi["patchMemory"]>>>();
    const done = vi.fn();
    render(pendingMemoryView(held.api, leases.begin, done));
    await act(async () => {});
    await beginMemory("approve");
    const firstRelease = leases.releases.at(-1);
    click("Cancel");
    expect(leases.tokens.size).toBe(1);
    expect(firstRelease).not.toHaveBeenCalled();
    vi.mocked(held.api.patchMemory).mockImplementationOnce(() => later.promise);
    await beginMemory("approve");
    const laterRelease = leases.releases.at(-1);
    expect(leases.tokens.size).toBe(2);
    await settleMemory("approve", held, rejection);
    expect(firstRelease).toHaveBeenCalledTimes(1);
    expect(laterRelease).not.toHaveBeenCalled();
    expect(leases.tokens.size).toBe(1);
    expect(done).not.toHaveBeenCalled();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Apply" }).disabled).toBe(true);
    await act(async () => later.resolve(WRITTEN));
    expect(laterRelease).toHaveBeenCalledTimes(1);
    expect(leases.tokens.size).toBe(0);
    expect(done).toHaveBeenCalledTimes(1);
    expect(held.api.patchMemory).toHaveBeenCalledTimes(2);
    expect(vi.mocked(held.api.patchMemory).mock.calls[0]).toEqual(
      vi.mocked(held.api.patchMemory).mock.calls[1],
    );
  },
);
