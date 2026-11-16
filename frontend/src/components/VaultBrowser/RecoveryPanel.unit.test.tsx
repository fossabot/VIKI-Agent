import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { catalog as vaultCatalog } from "../../i18n/vault";
import { createVaultApi, type VaultRecoveryStatus } from "../../state/vaultApi";
import {
  type RecoveryEntry,
  type StuckOperation,
  type VaultOpsApi,
  VaultOpsError,
} from "../../state/vaultOpsApi";
import { STOPPED } from "./NoteActions";
import { RecoveryPanel } from "./RecoveryPanel";

const W = "00000000-0000-4000-8000-000000000001";
const RID = "00000000-0000-4000-8000-0000000000aa";
const OP = "00000000-0000-4000-8000-0000000000bb";
const entry: RecoveryEntry = {
  recovery_id: RID,
  kind: "deleted",
  ref: "notes/old.md",
  byte_size: 2048,
  created_at: "2026-10-01T00:00:00.000Z",
  expires_at: "2026-10-31T00:00:00.000Z",
};
const row: VaultRecoveryStatus = {
  operation_id: OP,
  workspace_id: W,
  ref: "notes/half.md",
  state: "published",
  reason: "recovery_required",
  resolvable: ["forget", "drop_previous"],
};

const SID = "00000000-0000-4000-8000-0000000000cc";
const stuckRow: StuckOperation = {
  operation_id: SID,
  kind: "move",
  state: "recovery_required",
  paths: ["a/<b>.md", "c/d.md"],
};
const list = (entries: RecoveryEntry[] = [], stuck: StuckOperation[] = []) => ({ entries, stuck });

function fakeApi(over: Partial<VaultOpsApi> = {}): VaultOpsApi {
  return {
    preview: vi.fn(),
    apply: vi.fn(async () => ({
      operation_id: OP,
      status: "done" as const,
      replayed: false,
      steps_applied: 1,
      reason: null,
      created_folders: [],
    })),
    recovery: vi.fn(async () => list([entry])),
    forgetOperation: vi.fn(async () => ({
      operation_id: SID,
      recovery_ids: [],
      stage_retained: false,
    })),
    resolve: vi.fn(async () => ({
      operation_id: OP,
      choice: "forget" as const,
      stage_retained: false,
      backup_retained: false,
    })),
    ...over,
  };
}
function panel(
  api: VaultOpsApi,
  publications: VaultRecoveryStatus[],
  onDone: () => void,
  refreshKey = 0,
) {
  return (
    <RecoveryPanel
      api={api}
      workspaceId={W}
      policyRevision={3}
      rootPhysicalId="1:2"
      publications={publications}
      refreshKey={refreshKey}
      onDone={onDone}
    />
  );
}
function mount(api: VaultOpsApi, publications: VaultRecoveryStatus[] = [], onDone = vi.fn()) {
  render(panel(api, publications, onDone));
  return onDone;
}
const outcome = (
  status: "done" | "failed" | "conflict" | "recovery_required",
  reason: string | null = null,
) => ({
  operation_id: OP,
  status,
  replayed: false,
  steps_applied: 0,
  reason,
  created_folders: [],
});
afterEach(cleanup);

describe("RecoveryPanel", () => {
  it("lists a deleted note with size and expiry", async () => {
    mount(fakeApi());
    expect(await screen.findByText("notes/old.md")).toBeTruthy();
    expect(screen.getByText(/2\.0 KB · Expires 2026-10-31/)).toBeTruthy();
  });

  it("restores by recovery_id, not ref, to the original ref", async () => {
    const api = fakeApi();
    const onDone = mount(api);
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [id, request, commandId] = (api.apply as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      Record<string, unknown>,
      string,
    ];
    expect(id).toBe(W);
    expect(request).toEqual({
      policyRevision: 3,
      rootPhysicalId: "1:2",
      kind: "restore",
      recoveryId: RID,
      to: "notes/old.md",
    });
    expect(request).not.toHaveProperty("ref");
    expect(commandId).toMatch(/^[0-9a-f-]{36}$/);
    expect(screen.queryByText("notes/old.md")).toBeNull();
  });

  it.each([
    "bot-memory/00000000-0000-4000-8000-0000000000dd/MEMORY.md",
    "Bot-Memory/00000000-0000-4000-8000-0000000000dd/MEMORY.md",
  ])("offers no Restore for %s, says why, and still forgets", async (ref) => {
    const stuckApi = fakeApi({
      recovery: vi.fn(async () => list([{ ...entry, ref }], [stuckRow])),
    });
    mount(stuckApi);
    expect(await screen.findByText(ref)).toBeTruthy();
    expect(screen.getByText(vaultCatalog.en["vault.recovery.bot_memory_read_only"])).toBeTruthy();
    expect(screen.getByText(/Expires 2026-10-31/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(stuckApi.apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard record" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(stuckApi.forgetOperation).toHaveBeenCalledWith(SID));
  });

  it("still restores a nested bot-memory folder and an ordinary note", async () => {
    const nested = { ...entry, recovery_id: `${RID.slice(0, -1)}e`, ref: "notes/bot-memory/x.md" };
    const api = fakeApi({ recovery: vi.fn(async () => list([entry, nested])) });
    mount(api);
    await screen.findByText("notes/bot-memory/x.md");
    expect(screen.getAllByRole("button", { name: "Restore" })).toHaveLength(2);
    expect(screen.queryByText(vaultCatalog.en["vault.recovery.bot_memory_read_only"])).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "Restore" })[1] as HTMLElement);
    await waitFor(() => expect(api.apply).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.apply).mock.calls[0]?.[1]).toMatchObject({ to: "notes/bot-memory/x.md" });
  });

  it("says when both lists are empty", async () => {
    mount(fakeApi({ recovery: vi.fn(async () => list()) }));
    expect(await screen.findByText("There are no unfinished operations.")).toBeTruthy();
    expect(await screen.findByText("There are no deleted notes to restore.")).toBeTruthy();
    expect(screen.getByText("There are no unfinished save records.")).toBeTruthy();
  });

  it("shows a typed sentence for a refusal and a generic line for an unknown code", async () => {
    const apply = vi.fn().mockRejectedValueOnce(new VaultOpsError("recovery_full"));
    mount(fakeApi({ apply }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/storage for deleted notes is full/)).toBeTruthy();
    apply.mockRejectedValueOnce(new VaultOpsError("zzz_secret_code"));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/Couldn't complete the operation/)).toBeTruthy();
    expect(screen.queryByText(/zzz_secret_code/)).toBeNull();
  });

  it("puts each resolve choice behind a confirm", async () => {
    const api = fakeApi({ recovery: vi.fn(async () => list()) });
    const onDone = mount(api, [row]);
    fireEvent.click(await screen.findByRole("button", { name: "Delete previous version" }));
    expect(api.resolve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.resolve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard record" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.resolve).toHaveBeenCalledWith(OP, "forget");
    expect(screen.queryByText("notes/half.md")).toBeNull();
  });

  it("offers only the choices the row lists, and renders refs as text", async () => {
    const hostile = { ...row, ref: "<img src=x>.md", resolvable: [] };
    mount(fakeApi({ recovery: vi.fn(async () => list()) }), [hostile]);
    expect(await screen.findByText(hostile.ref)).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard record" })).toBeNull();
  });

  it("shows the reason as a sentence, not the code", async () => {
    mount(fakeApi(), [row, { ...row, operation_id: `${OP.slice(0, -1)}c`, reason: "conflict" }]);
    expect(
      await screen.findByText("Recovery is needed because the previous save didn't finish."),
    ).toBeTruthy();
    expect(
      screen.getByText("This save is stopped because the note changed before it was saved."),
    ).toBeTruthy();
    expect(screen.queryByText(/recovery_required/)).toBeNull();
  });

  it("renders resolve buttons from rows parsed by the real recovery() parser", async () => {
    const wire = (id: string, resolvable: string[]) => ({ ...row, operation_id: id, resolvable });
    const body = {
      publications: [
        wire(OP, []),
        { ...wire(`${OP.slice(0, -1)}c`, ["drop_previous"]), ref: "notes/one.md" },
      ],
    };
    const parsed = await createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 })),
    ).recovery();
    mount(fakeApi({ recovery: vi.fn(async () => list()) }), parsed);
    expect(await screen.findByText("notes/one.md")).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: /Discard record|Delete previous version/ }),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Delete previous version" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Discard record" })).toBeNull();
  });

  it("lists stuck operations as text and forgets one behind a confirm", async () => {
    const recovery = vi
      .fn()
      .mockResolvedValueOnce(list([], [stuckRow]))
      .mockResolvedValue(list());
    const api = fakeApi({ recovery });
    const onDone = mount(api);
    expect(await screen.findByText("a/<b>.md → c/d.md")).toBeTruthy();
    expect(screen.getByText("Move")).toBeTruthy();
    expect(document.querySelector("b")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Discard record" }));
    expect(api.forgetOperation).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        /This discards only the app's record\..*copy for recovery stays until it expires/,
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.forgetOperation).toHaveBeenCalledWith(SID);
    expect(api.resolve).not.toHaveBeenCalled();
    expect(recovery).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("a/<b>.md → c/d.md")).toBeNull();
    expect(screen.queryByText(/hidden temporary file/)).toBeNull();
  });

  it("says a hidden temporary file may remain when stage_retained is true", async () => {
    const forgetOperation = vi.fn(async () => ({
      operation_id: SID,
      recovery_ids: [],
      stage_retained: true,
    }));
    mount(fakeApi({ recovery: vi.fn(async () => list([], [stuckRow])), forgetOperation }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard record" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(/A hidden temporary file may remain in the vault/)).toBeTruthy();
  });

  it("reports what a resolve may have left behind", async () => {
    const resolve = vi.fn(async () => ({
      operation_id: OP,
      choice: "forget" as const,
      stage_retained: true,
      backup_retained: true,
    }));
    mount(fakeApi({ resolve }), [row]);
    fireEvent.click(await screen.findByRole("button", { name: "Discard record" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(/hidden temporary file/)).toBeTruthy();
    expect(screen.getByText(/backup of the previous version/)).toBeTruthy();
  });

  it("refreshes the list when the record is already gone", async () => {
    const recovery = vi
      .fn()
      .mockResolvedValueOnce(list([], [stuckRow]))
      .mockResolvedValue(list());
    const forgetOperation = vi.fn().mockRejectedValue(new VaultOpsError("unknown_operation"));
    mount(fakeApi({ recovery, forgetOperation }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard record" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(/already been handled/)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("a/<b>.md → c/d.md")).toBeNull());
    expect(recovery).toHaveBeenCalledTimes(2);
  });

  it("uses the app's word vault, not a synonym, in refusals", async () => {
    const apply = vi.fn().mockRejectedValue(new VaultOpsError("overloaded"));
    mount(fakeApi({ apply }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/^The vault is busy/)).toBeTruthy();
  });

  it.each([
    [
      "conflict",
      outcome("conflict"),
      "Can't restore because a note already exists at that location.",
    ],
    ["recovery_required", outcome("recovery_required"), STOPPED],
    [
      "failed with a reason",
      outcome("failed", "target_busy"),
      "Another app has this note open. Close it, then try again.",
    ],
    [
      "failed with an unknown reason",
      outcome("failed", "no_such_code"),
      "Couldn't complete the operation. Try again in a moment.",
    ],
    [
      "failed without a reason",
      outcome("failed"),
      "Couldn't complete the operation. Try again in a moment.",
    ],
  ])("does not call a %s restore done", async (_name, result, sentence) => {
    mount(fakeApi({ apply: vi.fn(async () => result) }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect((await screen.findByRole("status")).textContent).toBe(sentence);
    expect(screen.queryByText(/Restored/)).toBeNull();
    expect(screen.getByText("notes/old.md")).toBeTruthy();
  });

  it("says the copy has expired for recovery_missing, and reloads the list", async () => {
    const recovery = vi
      .fn()
      .mockResolvedValueOnce(list([entry]))
      .mockResolvedValue(list());
    const apply = vi.fn().mockRejectedValue(new VaultOpsError("recovery_missing"));
    mount(fakeApi({ apply, recovery }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "The copy of the deleted note has expired or has already been used. The list has been refreshed.",
    );
    await waitFor(() => expect(screen.queryByText("notes/old.md")).toBeNull());
    expect(recovery).toHaveBeenCalledTimes(2);
  });

  it("loads the lists again when the refresh signal changes", async () => {
    const api = fakeApi();
    const view = render(panel(api, [], vi.fn(), 0));
    await screen.findByText("notes/old.md");
    expect(api.recovery).toHaveBeenCalledTimes(1);
    view.rerender(panel(api, [], vi.fn(), 1));
    await waitFor(() => expect(api.recovery).toHaveBeenCalledTimes(2));
  });

  it("reports pending-plus-stuck count through onAttention, and again after it changes", async () => {
    const onAttention = vi.fn();
    const api = fakeApi({ recovery: vi.fn(async () => list([], [stuckRow])) });
    const view = render(
      <RecoveryPanel
        api={api}
        workspaceId={W}
        policyRevision={3}
        rootPhysicalId="1:2"
        publications={[row]}
        refreshKey={0}
        onDone={vi.fn()}
        onAttention={onAttention}
      />,
    );
    await screen.findByText("a/<b>.md → c/d.md");
    await act(async () => {});
    expect(onAttention).toHaveBeenLastCalledWith(2);
    vi.mocked(api.recovery).mockResolvedValue(list());
    view.rerender(
      <RecoveryPanel
        api={api}
        workspaceId={W}
        policyRevision={3}
        rootPhysicalId="1:2"
        publications={[]}
        refreshKey={1}
        onDone={vi.fn()}
        onAttention={onAttention}
      />,
    );
    await waitFor(() => expect(onAttention).toHaveBeenLastCalledWith(0));
  });

  async function restoreTwice(first: unknown) {
    const apply = vi
      .fn()
      .mockImplementationOnce(async () => {
        if (first instanceof Error) throw first;
        return first;
      })
      .mockResolvedValue(outcome("failed"));
    mount(fakeApi({ apply }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
    await screen.findByRole("status");
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
    return [apply.mock.calls[0]?.[2], apply.mock.calls[1]?.[2]];
  }

  it.each([
    ["a typed refusal", new VaultOpsError("recovery_full")],
    ["a read_only_target answer", new VaultOpsError("read_only_target")],
    ["a conflict outcome", outcome("conflict")],
  ])("mints a new command id for the next restore after %s", async (_name, first) => {
    const [one, two] = await restoreTwice(first);
    expect(two).toMatch(/^[0-9a-f-]{36}$/);
    expect(two).not.toBe(one);
  });

  it.each([
    ["a network failure", new TypeError("Failed to fetch")],
    ["an unavailable answer", new VaultOpsError("unavailable")],
    ["an op_in_progress answer", new VaultOpsError("op_in_progress")],
  ])("keeps the command id for the next restore after %s", async (_name, first) => {
    const [one, two] = await restoreTwice(first);
    expect(one).toMatch(/^[0-9a-f-]{36}$/);
    expect(two).toBe(one);
  });
});

// A vault whose folder is gone: the app's own records can still be cleared, nothing else.
describe("RecoveryPanel, forget only", () => {
  function mountForgetOnly(api: VaultOpsApi, publications: VaultRecoveryStatus[] = []) {
    const onDone = vi.fn();
    render(
      <RecoveryPanel
        forgetOnly
        api={api}
        workspaceId={W}
        publications={publications}
        refreshKey={0}
        onDone={onDone}
      />,
    );
    return onDone;
  }
  const stuckApi = () => fakeApi({ recovery: vi.fn(async () => list([entry], [stuckRow])) });

  it("offers only Discard record and says why", async () => {
    mountForgetOnly(stuckApi(), [row]);
    expect(await screen.findByText("a/<b>.md → c/d.md")).toBeTruthy();
    expect(
      screen.getByText(/vault's folder can't be opened.*only clear the app's records/),
    ).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Discard record" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete previous version" })).toBeNull();
    expect(screen.queryByText("Deleted notes")).toBeNull();
    expect(screen.queryByText("notes/old.md")).toBeNull();
  });

  it("forgets a stuck operation row through forgetOperation", async () => {
    const api = stuckApi();
    const onDone = mountForgetOnly(api);
    fireEvent.click(await screen.findByRole("button", { name: "Discard record" }));
    expect(screen.getByText(/This discards only the app's record/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.forgetOperation).toHaveBeenCalledWith(SID);
    expect(api.resolve).not.toHaveBeenCalled();
    expect(api.apply).not.toHaveBeenCalled();
  });

  it("forgets a publication row through resolve(id, forget), and never drops the previous version", async () => {
    const api = fakeApi({ recovery: vi.fn(async () => list()) });
    const onDone = mountForgetOnly(api, [row]);
    fireEvent.click(await screen.findByRole("button", { name: "Discard record" }));
    expect(screen.getByText(/This discards only the record of this save/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.resolve).toHaveBeenCalledTimes(1);
    expect(api.resolve).toHaveBeenCalledWith(OP, "forget");
    expect(api.apply).not.toHaveBeenCalled();
  });

  it("offers nothing for a row that lists only drop_previous", async () => {
    const dropOnly = { ...row, resolvable: ["drop_previous" as const] };
    mountForgetOnly(fakeApi({ recovery: vi.fn(async () => list()) }), [dropOnly]);
    expect(await screen.findByText("notes/half.md")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /Discard record|Delete previous version/ }),
    ).toBeNull();
  });
});

function holdRecovery() {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function pendingRecovery(wait: Promise<void>): VaultOpsApi {
  const base = fakeApi({ recovery: vi.fn(async () => list([entry], [stuckRow])) });
  return {
    ...base,
    apply: vi.fn(async (...args: Parameters<VaultOpsApi["apply"]>) => {
      await wait;
      return base.apply(...args);
    }),
    resolve: vi.fn(async (...args: Parameters<VaultOpsApi["resolve"]>) => {
      await wait;
      return base.resolve(...args);
    }),
    forgetOperation: vi.fn(async (...args: Parameters<VaultOpsApi["forgetOperation"]>) => {
      await wait;
      return base.forgetOperation(...args);
    }),
  };
}
function pendingPanel(
  api: VaultOpsApi,
  onPending: () => () => void,
  onDone: () => void,
  workspaceId = W,
) {
  return (
    <RecoveryPanel
      api={api}
      workspaceId={workspaceId}
      policyRevision={3}
      rootPhysicalId="1:2"
      publications={[row]}
      refreshKey={0}
      onDone={onDone}
      onPending={onPending}
    />
  );
}
type PendingKind = "restore" | "resolve" | "forget";
function startRecovery(kind: PendingKind): void {
  if (kind === "restore") fireEvent.click(screen.getByRole("button", { name: "Restore" }));
  else if (kind === "resolve") {
    fireEvent.click(screen.getByRole("button", { name: "Delete previous version" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  } else {
    const item = screen.getByText("a/<b>.md → c/d.md").closest("li");
    if (!item) throw new Error("Missing stuck operation row");
    fireEvent.click(within(item).getByRole("button", { name: "Discard record" }));
    fireEvent.click(within(item).getByRole("button", { name: "Apply" }));
  }
}

it.each(["restore", "resolve", "forget"] as const)(
  "RecoveryPanel holds its %s lease before service dispatch and releases after completion",
  async (kind) => {
    const held = holdRecovery();
    const api = pendingRecovery(held.promise);
    const release = vi.fn();
    const begin = vi.fn(() => release);
    const onDone = vi.fn();
    render(pendingPanel(api, begin, onDone));
    await act(async () => {});
    expect(begin).not.toHaveBeenCalled();
    startRecovery(kind);
    const method =
      kind === "restore" ? "apply" : kind === "resolve" ? "resolve" : "forgetOperation";
    expect(begin).toHaveBeenCalledTimes(1);
    expect(begin.mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api[method]).mock.invocationCallOrder[0] ?? 0,
    );
    expect(release).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
    if (kind === "restore")
      expect(api.apply).toHaveBeenCalledExactlyOnceWith(
        W,
        {
          policyRevision: 3,
          rootPhysicalId: "1:2",
          kind: "restore",
          recoveryId: RID,
          to: entry.ref,
        },
        expect.any(String),
      );
    else if (kind === "resolve")
      expect(api.resolve).toHaveBeenCalledExactlyOnceWith(OP, "drop_previous");
    else expect(api.forgetOperation).toHaveBeenCalledExactlyOnceWith(SID);
    await act(async () => held.resolve());
    expect(release).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onDone.mock.invocationCallOrder[0]).toBeLessThan(
      release.mock.invocationCallOrder[0] ?? 0,
    );
    expect(api[method]).toHaveBeenCalledTimes(1);
  },
);

it.each(["restore", "resolve", "forget"] as const)(
  "RecoveryPanel releases rejected %s once without replay or onDone",
  async (kind) => {
    const held = holdRecovery();
    const api = pendingRecovery(held.promise);
    const release = vi.fn();
    const begin = vi.fn(() => release);
    const onDone = vi.fn();
    render(pendingPanel(api, begin, onDone));
    await act(async () => {});
    startRecovery(kind);
    expect(begin).toHaveBeenCalledTimes(1);
    expect(release).not.toHaveBeenCalled();
    await act(async () => held.reject(new VaultOpsError("target_busy")));
    expect(release).toHaveBeenCalledTimes(1);
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toContain("Another app has this note open");
    const method =
      kind === "restore" ? "apply" : kind === "resolve" ? "resolve" : "forgetOperation";
    expect(api[method]).toHaveBeenCalledTimes(1);
    expect(api.recovery).toHaveBeenCalledTimes(1);
  },
);

it("RecoveryPanel keeps a gone-outcome lease until its existing reload settles", async () => {
  const reloading = holdRecovery();
  const api = fakeApi({
    apply: vi.fn(async () => {
      throw new VaultOpsError("recovery_missing");
    }),
    recovery: vi
      .fn<VaultOpsApi["recovery"]>()
      .mockResolvedValueOnce(list([entry]))
      .mockImplementation(async () => {
        await reloading.promise;
        return list();
      }),
  });
  const release = vi.fn();
  const begin = vi.fn(() => release);
  const onDone = vi.fn();
  render(pendingPanel(api, begin, onDone));
  await act(async () => {});
  await act(async () => startRecovery("restore"));
  expect(api.recovery).toHaveBeenCalledTimes(2);
  expect(release).not.toHaveBeenCalled();
  expect(onDone).not.toHaveBeenCalled();
  await act(async () => reloading.resolve());
  expect(release).toHaveBeenCalledTimes(1);
  expect(begin).toHaveBeenCalledTimes(1);
  expect(onDone).not.toHaveBeenCalled();
  expect(api.apply).toHaveBeenCalledTimes(1);
});

it("RecoveryPanel confirmation and Cancel do not admit a pending service run", async () => {
  const api = fakeApi({ recovery: vi.fn(async () => list([entry], [stuckRow])) });
  const begin = vi.fn(() => vi.fn());
  render(pendingPanel(api, begin, vi.fn()));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Delete previous version" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(begin).not.toHaveBeenCalled();
  expect(api.apply).not.toHaveBeenCalled();
  expect(api.resolve).not.toHaveBeenCalled();
  expect(api.forgetOperation).not.toHaveBeenCalled();
});

it("RecoveryPanel releases its captured callback after a replacement", async () => {
  const held = holdRecovery();
  const api = pendingRecovery(held.promise);
  const release = vi.fn();
  const nextRelease = vi.fn();
  const begin = vi.fn(() => release);
  const nextBegin = vi.fn(() => nextRelease);
  const onDone = vi.fn();
  const view = render(pendingPanel(api, begin, onDone));
  await act(async () => {});
  startRecovery("restore");
  view.rerender(pendingPanel(api, nextBegin, onDone));
  expect(begin).toHaveBeenCalledTimes(1);
  expect(nextBegin).not.toHaveBeenCalled();
  await act(async () => held.resolve());
  expect(release).toHaveBeenCalledTimes(1);
  expect(nextRelease).not.toHaveBeenCalled();
  expect(api.apply).toHaveBeenCalledTimes(1);
  expect(onDone).toHaveBeenCalledTimes(1);
});

it.each(["success", "rejection"] as const)(
  "RecoveryPanel retains an unmounted old scope's lease until %s without releasing its successor",
  async (finish) => {
    const old = holdRecovery();
    const current = holdRecovery();
    const oldApi = pendingRecovery(old.promise);
    const currentApi = pendingRecovery(current.promise);
    const tokens = new Set<symbol>();
    const releases: ReturnType<typeof vi.fn>[] = [];
    const begin = vi.fn(() => {
      const token = Symbol();
      tokens.add(token);
      const release = vi.fn(() => tokens.delete(token));
      releases.push(release);
      return release;
    });
    const oldDone = vi.fn();
    const currentDone = vi.fn();
    const previous = render(pendingPanel(oldApi, begin, oldDone));
    await act(async () => {});
    startRecovery("restore");
    previous.unmount();
    const currentWorkspace = "00000000-0000-4000-8000-000000000002";
    render(pendingPanel(currentApi, begin, currentDone, currentWorkspace));
    await act(async () => {});
    startRecovery("restore");
    const [oldRelease, currentRelease] = releases;
    if (!oldRelease || !currentRelease) throw new Error("Missing actual pending owners");
    expect(begin).toHaveBeenCalledTimes(2);
    expect(tokens.size).toBe(2);
    expect(oldRelease).not.toHaveBeenCalled();
    expect(currentRelease).not.toHaveBeenCalled();
    await act(async () => {
      if (finish === "success") old.resolve();
      else old.reject(new VaultOpsError("target_busy"));
    });
    expect(oldRelease).toHaveBeenCalledTimes(1);
    expect(currentRelease).not.toHaveBeenCalled();
    expect(tokens.size).toBe(1);
    expect(currentDone).not.toHaveBeenCalled();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Restore" }).disabled).toBe(true);
    expect(currentApi.apply).toHaveBeenCalledExactlyOnceWith(
      currentWorkspace,
      { policyRevision: 3, rootPhysicalId: "1:2", kind: "restore", recoveryId: RID, to: entry.ref },
      expect.any(String),
    );
    await act(async () => current.resolve());
    expect(currentRelease).toHaveBeenCalledTimes(1);
    expect(oldRelease).toHaveBeenCalledTimes(1);
    expect(tokens.size).toBe(0);
    expect(currentDone).toHaveBeenCalledTimes(1);
    expect(oldApi.apply).toHaveBeenCalledTimes(1);
    expect(currentApi.apply).toHaveBeenCalledTimes(1);
  },
);
