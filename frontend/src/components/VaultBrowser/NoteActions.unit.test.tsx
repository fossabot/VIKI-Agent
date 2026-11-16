import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type OperationOutcome,
  type OperationPlan,
  type OperationRequest,
  type VaultOpsApi,
  VaultOpsError,
} from "../../state/vaultOpsApi";
import { NoteActions, OPERATION_COPY } from "./NoteActions";

const W = "00000000-0000-4000-8000-000000000001";
const OUTCOME: OperationOutcome = {
  operation_id: "00000000-0000-4000-8000-0000000000aa",
  status: "done",
  replayed: false,
  steps_applied: 1,
  reason: null,
  created_folders: [],
};
const SHA = "a".repeat(64);
const OTHER_SHA = "b".repeat(64);
const PLAN: OperationPlan = {
  steps: [{ n: 1, action: "move", ref: "a/n.md", to: "b/n.md" }],
  creates_folders: ["b"],
  recovery_days: 30,
};
const GENERIC_LINE = "The operation failed. Check the vault's status.";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Off Windows, read_only_target names a POSIX remedy; on Windows it names the Properties
// dialog (its OPERATION_COPY line).
const READ_ONLY_TARGET_CASES: [platform: string, copy: string][] = [
  ["Win32", OPERATION_COPY.read_only_target as string],
  [
    "MacIntel",
    "This note is read-only or locked. Make it writable in your file manager, then try again.",
  ],
  [
    "Linux x86_64",
    "This note is read-only or locked. Make it writable in your file manager, then try again.",
  ],
];

/** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property is
 * removed afterwards rather than restored — there is no own descriptor to put back. */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

function fakeApi(overrides: Partial<VaultOpsApi> = {}) {
  const calls: string[] = [];
  const api: VaultOpsApi = {
    preview: vi.fn(async (_id: string, _request: OperationRequest) => {
      calls.push("preview");
      return PLAN;
    }),
    apply: vi.fn(async () => {
      calls.push("apply");
      return OUTCOME;
    }),
    recovery: vi.fn(async () => ({ entries: [], stuck: [] })),
    forgetOperation: vi.fn(async () => ({
      operation_id: OUTCOME.operation_id,
      recovery_ids: [],
      stage_retained: false,
    })),
    resolve: vi.fn(async () => ({
      operation_id: OUTCOME.operation_id,
      choice: "forget" as const,
      stage_retained: false,
      backup_retained: false,
    })),
    ...overrides,
  };
  return { api, calls };
}

function element(
  api: VaultOpsApi | undefined,
  onDone: () => void,
  noteSha256 = SHA,
  onRefresh?: () => void,
) {
  return (
    <NoteActions
      workspaceId={W}
      noteRef="a/n.md"
      noteSha256={noteSha256}
      revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
      api={api}
      onDone={onDone}
      onRefresh={onRefresh}
    />
  );
}

function mount(api: VaultOpsApi | undefined, onDone = vi.fn(), onRefresh = vi.fn()) {
  const view = render(element(api, onDone, SHA, onRefresh));
  return Object.assign(onDone, {
    reload: (sha: string) => view.rerender(element(api, onDone, sha, onRefresh)),
    onRefresh,
  });
}

function open(item: string) {
  fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: item }));
}
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

afterEach(cleanup);

describe("NoteActions", () => {
  it("renders nothing without an api", () => {
    mount(undefined);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers the four actions in one menu", () => {
    mount(fakeApi().api);
    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Rename",
      "Move",
      "Archive",
      "Delete",
    ]);
  });

  it("renders an extra-only menu: no api, button still shows, click closes it and calls onSelect", () => {
    const onSelect = vi.fn();
    render(
      <NoteActions
        workspaceId={W}
        noteRef="a/n.md"
        noteSha256={SHA}
        revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
        onDone={vi.fn()}
        extra={[{ label: "Pin", onSelect }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Pin"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("lists extra items before the four api actions", () => {
    render(
      <NoteActions
        workspaceId={W}
        noteRef="a/n.md"
        noteSha256={SHA}
        revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
        api={fakeApi().api}
        onDone={vi.fn()}
        extra={[{ label: "Pin", onSelect: vi.fn() }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Note actions" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Pin",
      "Rename",
      "Move",
      "Archive",
      "Delete",
    ]);
  });

  it("previews before it applies, and apply stays off until a preview", async () => {
    const { api, calls } = fakeApi();
    const onDone = mount(api);
    open("Move");
    expect((screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    click("Review");
    await screen.findByText("Folders to create: b");
    expect(api.apply).not.toHaveBeenCalled();
    click("Apply");
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(calls).toEqual(["preview", "apply"]);
    expect(api.preview).toHaveBeenCalledWith(
      W,
      expect.objectContaining({ kind: "move", ref: "a/n.md", to: "a/n.md", policyRevision: 3 }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows posix folder prefixes and builds a rename inside the note's folder", async () => {
    const { api } = fakeApi({
      preview: vi.fn(async () => ({ ...PLAN, creates_folders: ["a", "a/b"] })),
    });
    mount(api);
    open("Rename");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "m.md" } });
    click("Review");
    await screen.findByText("Folders to create: a, a/b");
    expect(api.preview).toHaveBeenCalledWith(W, expect.objectContaining({ to: "a/m.md" }));
  });

  it("states the recovery window for a delete and sends no destination", async () => {
    const { api } = fakeApi();
    mount(api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    expect(api.preview).toHaveBeenCalledWith(W, expect.objectContaining({ to: undefined }));
  });

  it("reuses the command id on a retry of an unknown outcome, never retries by itself, and mints a new id after an edit", async () => {
    const apply = vi
      .fn()
      .mockRejectedValueOnce(new VaultOpsError("unavailable"))
      .mockRejectedValueOnce(new VaultOpsError("unavailable"))
      .mockResolvedValue(OUTCOME);
    mount(fakeApi({ apply }).api);
    open("Move");
    click("Review");
    await screen.findByText("Folders to create: b");
    click("Apply");
    await screen.findByText(GENERIC_LINE);
    expect(apply).toHaveBeenCalledTimes(1);
    click("Apply");
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
    const first = apply.mock.calls[0]?.[2];
    expect(first).toMatch(UUID);
    expect(apply.mock.calls[1]?.[2]).toBe(first);
    await screen.findByText(GENERIC_LINE);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "c/n.md" } });
    click("Review");
    await screen.findByText("Folders to create: b");
    click("Apply");
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(3));
    expect(apply.mock.calls[2]?.[2]).toMatch(UUID);
    expect(apply.mock.calls[2]?.[2]).not.toBe(first);
  });

  it.each([
    "conflict",
    "vault_locked",
    "vault_recovery_required",
    "target_busy",
    "recovery_full",
    "recovery_unavailable",
    "unsupported_filesystem",
    "unsafe_target",
    "read_only_target",
    "overloaded",
    "folder_create_failed",
    "invalid_operation",
  ])("shows its own sentence for %s", async (code) => {
    const sentence = OPERATION_COPY[code];
    expect(sentence).toBeTruthy();
    mount(
      fakeApi({
        preview: vi.fn(async () => {
          throw new VaultOpsError(code);
        }),
      }).api,
    );
    open("Delete");
    click("Review");
    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
  });

  it.each(READ_ONLY_TARGET_CASES)(
    "names the platform's remedy for a read-only or locked note on %s",
    async (platform, copy) => {
      const restore = setPlatform(platform);
      try {
        mount(
          fakeApi({
            preview: vi.fn(async () => {
              throw new VaultOpsError("read_only_target");
            }),
          }).api,
        );
        open("Delete");
        click("Review");
        expect((await screen.findByRole("alert")).textContent).toBe(copy);
      } finally {
        restore();
      }
    },
  );

  it("has a distinct sentence for every listed code", () => {
    const sentences = Object.values(OPERATION_COPY);
    expect(new Set(sentences).size).toBe(sentences.length);
  });

  it("falls back to one generic line for an unknown code, without echoing it", async () => {
    mount(
      fakeApi({
        preview: vi.fn(async () => {
          throw new VaultOpsError("C:\\secret\\who_knows");
        }),
      }).api,
    );
    open("Delete");
    click("Review");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toContain("secret");
    expect(alert.textContent).toBe("The operation failed. Check the vault's status.");
  });

  it("shows a preview refusal as its sentence and keeps apply off", async () => {
    const { api } = fakeApi({
      preview: vi.fn(async () => ({ ...PLAN, refusal: "read_only_target" })),
    });
    mount(api);
    open("Delete");
    click("Review");
    await screen.findByText(OPERATION_COPY.read_only_target as string);
    expect((screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("renders step text as text, never as markup", async () => {
    const { api } = fakeApi({
      preview: vi.fn(async () => ({
        ...PLAN,
        steps: [{ n: 1, action: "remove" as const, ref: "<img src=x>.md", to: null }],
      })),
    });
    mount(api);
    open("Delete");
    click("Review");
    await screen.findByText(/<img src=x>\.md/);
    expect(document.querySelector("img")).toBeNull();
  });

  it("returns focus to the trigger when the dialog closes", async () => {
    mount(fakeApi().api);
    open("Delete");
    click("Cancel");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Note actions" }));
  });

  it("sends the loaded note's hash in the preview and the apply", async () => {
    const { api } = fakeApi();
    mount(api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    await waitFor(() => expect(api.apply).toHaveBeenCalledOnce());
    expect(api.preview).toHaveBeenCalledWith(W, expect.objectContaining({ expectedSha256: SHA }));
    expect(api.apply).toHaveBeenCalledWith(
      W,
      expect.objectContaining({ expectedSha256: SHA }),
      expect.stringMatching(UUID),
    );
  });

  it("mints a new command id when the note's hash changes", async () => {
    const apply = vi
      .fn()
      .mockRejectedValueOnce(new VaultOpsError("target_busy"))
      .mockResolvedValue(OUTCOME);
    const view = mount(fakeApi({ apply }).api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    await screen.findByText(OPERATION_COPY.target_busy as string);
    view.reload(OTHER_SHA);
    click("Apply");
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
    expect(apply.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ expectedSha256: OTHER_SHA }),
    );
    expect(apply.mock.calls[1]?.[2]).not.toBe(apply.mock.calls[0]?.[2]);
  });

  it.each([
    [{ status: "conflict" as const, reason: null }, OPERATION_COPY.conflict],
    [{ status: "failed" as const, reason: "target_busy" }, OPERATION_COPY.target_busy],
    [
      { status: "failed" as const, reason: "no_such_code" },
      "The operation failed. Check the vault's status.",
    ],
    [
      { status: "recovery_required" as const, reason: null },
      "The operation stopped partway through. The note isn't lost. It's shown under \"Unfinished operations\".",
    ],
  ])("shows the sentence for an apply outcome %j without closing", async (outcome, sentence) => {
    const onDone = mount(fakeApi({ apply: vi.fn(async () => ({ ...OUTCOME, ...outcome })) }).api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it.each([
    ["conflict", { status: "conflict" as const, reason: null }],
    ["failed", { status: "failed" as const, reason: "target_busy" }],
    ["recovery_required", { status: "recovery_required" as const, reason: null }],
  ])(
    "asks the shell to refresh after a %s outcome, and keeps the dialog",
    async (_name, outcome) => {
      const onDone = mount(fakeApi({ apply: vi.fn(async () => ({ ...OUTCOME, ...outcome })) }).api);
      open("Delete");
      click("Review");
      await screen.findByText("Can be restored for 30 days.");
      click("Apply");
      await screen.findByRole("alert");
      expect(onDone.onRefresh).toHaveBeenCalledOnce();
      expect(onDone).not.toHaveBeenCalled();
      expect(screen.getByRole("dialog")).toBeTruthy();
    },
  );

  it("closes through onDone, not onRefresh, on a done outcome", async () => {
    const onDone = mount(fakeApi().api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(onDone.onRefresh).not.toHaveBeenCalled();
  });

  // A refusal or an outcome is the server's definitive answer, which it remembers per command id,
  // so the next Apply needs a new id; only an unknown outcome keeps the id for a faithful replay.
  async function applyTwice(first: unknown) {
    const apply = vi
      .fn()
      .mockImplementationOnce(async () => {
        if (first instanceof Error) throw first;
        return first;
      })
      .mockResolvedValue({ ...OUTCOME, status: "failed", reason: "target_busy" });
    mount(fakeApi({ apply }).api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
    await screen.findByRole("alert");
    click("Apply");
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
    return [apply.mock.calls[0]?.[2], apply.mock.calls[1]?.[2]];
  }

  it.each([
    ["a typed refusal", new VaultOpsError("read_only_target")],
    ["a failed outcome", { ...OUTCOME, status: "failed", reason: "target_busy" }],
    ["a conflict outcome", { ...OUTCOME, status: "conflict" }],
  ])("mints a new command id after %s", async (_name, first) => {
    const [one, two] = await applyTwice(first);
    expect(one).toMatch(UUID);
    expect(two).toMatch(UUID);
    expect(two).not.toBe(one);
  });

  it.each([
    ["a network failure", new TypeError("Failed to fetch")],
    ["an unavailable answer", new VaultOpsError("unavailable")],
    ["an op_in_progress answer", new VaultOpsError("op_in_progress")],
  ])("keeps the command id after %s", async (_name, first) => {
    const [one, two] = await applyTwice(first);
    expect(two).toBe(one);
  });

  it("says a missing note is gone and refreshes the lists", async () => {
    const apply = vi.fn().mockRejectedValue(new VaultOpsError("missing_target"));
    const view = mount(fakeApi({ apply }).api);
    open("Delete");
    click("Review");
    await screen.findByText("Can be restored for 30 days.");
    click("Apply");
    await screen.findByText(/This note can't be found/);
    expect(view.onRefresh).toHaveBeenCalled();
  });
});

describe("NoteActions pending navigation ownership", () => {
  function hold<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    return { promise, resolve, reject };
  }
  function owned(api: VaultOpsApi, onPending: () => () => void) {
    const onDone = vi.fn();
    const onRefresh = vi.fn();
    render(
      <NoteActions
        workspaceId={W}
        noteRef="a/n.md"
        noteSha256={SHA}
        revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
        api={api}
        onDone={onDone}
        onRefresh={onRefresh}
        onPending={onPending}
      />,
    );
    return { onDone, onRefresh };
  }
  it.each(["preview", "apply"])(
    "holds %s after Cancel until its actual promise settles",
    async (kind) => {
      const preview = hold<OperationPlan>();
      const apply = hold<OperationOutcome>();
      const api = fakeApi(
        kind === "preview"
          ? { preview: vi.fn(() => preview.promise) }
          : { apply: vi.fn(() => apply.promise) },
      ).api;
      const releases: ReturnType<typeof vi.fn>[] = [];
      const begin = vi.fn(() => {
        const release = vi.fn();
        releases.push(release);
        return release;
      });
      const { onDone, onRefresh } = owned(api, begin);
      open("Delete");
      await act(async () => click("Review"));
      if (kind === "apply") await act(async () => click("Apply"));
      const release = releases.at(-1);
      if (!release) throw new Error("Missing actual request lease");
      expect(begin).toHaveBeenCalledTimes(kind === "preview" ? 1 : 2);
      expect(release).not.toHaveBeenCalled();
      await act(async () => click("Cancel"));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(release).not.toHaveBeenCalled();
      await act(async () => {
        if (kind === "preview") preview.resolve(PLAN);
        else apply.resolve(OUTCOME);
      });
      expect(release).toHaveBeenCalledTimes(1);
      expect(onDone).not.toHaveBeenCalled();
      expect(onRefresh).not.toHaveBeenCalled();
      expect(api.preview).toHaveBeenCalledTimes(1);
      expect(api.apply).toHaveBeenCalledTimes(kind === "preview" ? 0 : 1);
    },
  );
  it.each(["preview", "apply"])(
    "releases rejected %s exactly once without replay",
    async (kind) => {
      const held = hold<never>();
      const api = fakeApi(
        kind === "preview"
          ? { preview: vi.fn(() => held.promise) }
          : { apply: vi.fn(() => held.promise) },
      ).api;
      const release = vi.fn();
      const begin = vi.fn(() => release);
      const { onDone } = owned(api, begin);
      open("Delete");
      await act(async () => click("Review"));
      if (kind === "apply") {
        release.mockClear();
        await act(async () => click("Apply"));
      }
      expect(release).not.toHaveBeenCalled();
      await act(async () => held.reject(new VaultOpsError("target_busy")));
      expect(release).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("alert").textContent).toBe(OPERATION_COPY.target_busy);
      expect(onDone).not.toHaveBeenCalled();
      expect(api.preview).toHaveBeenCalledTimes(1);
      expect(api.apply).toHaveBeenCalledTimes(kind === "preview" ? 0 : 1);
    },
  );
});

it("NoteActions releases the captured owner after its pending callback is replaced", async () => {
  let finish!: (value: OperationPlan) => void;
  const api = fakeApi({
    preview: vi.fn(
      () =>
        new Promise<OperationPlan>((resolve) => {
          finish = resolve;
        }),
    ),
  }).api;
  const release = vi.fn();
  const nextRelease = vi.fn();
  const begin = vi.fn(() => release);
  const nextBegin = vi.fn(() => nextRelease);
  const onDone = vi.fn();
  const note = (onPending: () => () => void) => (
    <NoteActions
      workspaceId={W}
      noteRef="a/n.md"
      noteSha256={SHA}
      revision={{ policyRevision: 3, rootPhysicalId: "1:2" }}
      api={api}
      onDone={onDone}
      onPending={onPending}
    />
  );
  const view = render(note(begin));
  open("Delete");
  await act(async () => click("Review"));
  view.rerender(note(nextBegin));
  expect(begin).toHaveBeenCalledTimes(1);
  expect(nextBegin).not.toHaveBeenCalled();
  await act(async () => finish(PLAN));
  expect(release).toHaveBeenCalledTimes(1);
  expect(nextRelease).not.toHaveBeenCalled();
  expect(api.preview).toHaveBeenCalledTimes(1);
  expect(onDone).not.toHaveBeenCalled();
});
