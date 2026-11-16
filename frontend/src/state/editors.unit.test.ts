import { afterEach, expect, test, vi } from "vitest";
import {
  type CommandInput,
  type CommandReceipt,
  createDurableApi,
  type Draft,
  type DurableApi,
  DurableApiError,
} from "../clients/conversations/durableApi";
import type { Attachment } from "../turnInput";
import { createEditorStore, draftFits, MAX_DRAFT_BYTES, trimTrailingNewlines } from "./editors";
import type { VaultExcerptPart } from "./excerptApi";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const G = "33333333-3333-4333-8333-333333333333";
const AT = "2026-09-14T00:00:00.000Z";

const IMAGE: Attachment = {
  asset_id: "44444444-4444-4444-8444-444444444444",
  media_type: "image/png",
  byte_size: 100,
  width: 1,
  height: 1,
  name: "screen.png",
  modified_at: null,
  source: "paste",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function draft(key: string, revision: number, text: string): Draft {
  return { key, revision, text, saved_at: revision ? AT : null };
}

function receipt(input: CommandInput): CommandReceipt {
  return { ...input, state: "accepted", reason: "", created_at: AT, updated_at: AT, bytes: 5 };
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const data = new Map<string, string>();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

function fakeApi(overrides: Partial<DurableApi> = {}): DurableApi {
  return {
    getDraft: vi.fn(async (key) => draft(key, 0, "")),
    saveDraft: vi.fn(async (key, revision, text) => draft(key, revision + 1, text)),
    clearDraft: vi.fn(async (key, revision) => draft(key, revision + 1, "")),
    getTarget: vi.fn(),
    getDraftCommand: vi.fn(async () => null),
    submitCommand: vi.fn(async (input) => receipt(input)),
    getCommand: vi.fn(),
    listDrafts: vi.fn(),
    listCommands: vi.fn(),
    getSubmission: vi.fn(),
    findApproval: vi.fn(),
    recordApproval: vi.fn(),
    getApproval: vi.fn(),
    getApprovalChoice: vi.fn(),
    listApprovals: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

test("final admission cancels after draft preparation without inventing a pending command", async () => {
  vi.useFakeTimers();
  const lookup = deferred<CommandReceipt | null>();
  const api = fakeApi({ getDraftCommand: vi.fn(() => lookup.promise) });
  const storage = memoryStorage();
  const store = createEditorStore(api, { storage });
  await store.ensure(A);
  store.edit(A, "captured request");
  let allowed = true;
  const sending = store.submit(A, G, undefined, undefined, undefined, () => allowed);
  await vi.waitFor(() => expect(api.getDraftCommand).toHaveBeenCalled(), {
    timeout: 50,
    interval: 1,
  });
  store.edit(A, "newer words");
  allowed = false;
  lookup.resolve(null);
  expect(await sending).toBeNull();
  expect(api.submitCommand).not.toHaveBeenCalled();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "newer words",
    pendingCommand: null,
    submitting: false,
  });
  expect(storage.getItem(`viki.editor.pending.${A}`)).toBeNull();
  await store.flush(A);
  expect(api.saveDraft).toHaveBeenLastCalledWith(A, 1, "newer words");
});

test("final admission follows synchronous publication and keeps a newer-text backup", async () => {
  vi.useFakeTimers();
  const api = fakeApi();
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "newer words");
  let allowed = true;
  const unsubscribe = store.subscribe(() => {
    if (store.getSnapshot().editors[A]?.pendingCommand) allowed = false;
  });
  expect(await store.submit(A, G, "captured request", [], [], () => allowed)).toBeNull();
  unsubscribe();
  expect(api.submitCommand).not.toHaveBeenCalled();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "newer words",
    pendingCommand: null,
    submitting: false,
    backupKey: expect.any(String),
  });
});

test("final admission does not discard an already submitted revision binding", async () => {
  vi.useFakeTimers();
  const prior = receipt({ command_id: B, session_id: A, generation: G, draft_revision: 1 });
  const api = fakeApi({ getDraftCommand: vi.fn(async () => prior) });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "accepted elsewhere");
  const admit = vi.fn(() => false);
  expect(await store.submit(A, G, undefined, undefined, undefined, admit)).toBeNull();
  expect(admit).not.toHaveBeenCalled();
  expect(api.submitCommand).not.toHaveBeenCalled();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    acceptedUncleared: true,
    lastReceipt: prior,
    pendingCommand: { command_id: B },
  });
});

test("releases only the matching clean completed editor", async () => {
  vi.useFakeTimers();
  const store = createEditorStore(fakeApi(), { storage: null });
  await store.ensure(A);
  store.edit(A, "request");
  const sent = await store.submit(A, G);
  expect(sent).not.toBeNull();
  expect(store.releaseCompleted(A, B)).toBe(false);
  await Promise.resolve();
  expect(store.releaseCompleted(A, sent?.command_id ?? "")).toBe(true);
  expect(store.getSnapshot().editors[A]).toBeUndefined();
});

test.each(["dirty", "attachment", "backup", "pending", "accepted", "conflict", "queue"])(
  "retains a completed editor with %s work",
  async (kind) => {
    vi.useFakeTimers();
    const api = fakeApi();
    const store = createEditorStore(api, { storage: null });
    await store.ensure(A);
    store.edit(A, kind === "backup" ? "newer words" : "request");
    if (kind === "accepted")
      vi.mocked(api.clearDraft).mockRejectedValue(new DurableApiError(503, "unavailable"));
    const sent = await store.submit(A, G, "request");
    expect(sent).not.toBeNull();
    await Promise.resolve();
    const waiting = deferred<Draft>();
    let pending: Promise<unknown> | undefined;
    if (kind === "dirty" || kind === "queue") store.edit(A, "new work");
    if (kind === "attachment") store.attach(A, [IMAGE]);
    if (kind === "pending") {
      store.edit(A, "new request");
      vi.mocked(api.submitCommand).mockRejectedValue(new DurableApiError(0, "network_error"));
      vi.mocked(api.getCommand).mockRejectedValue(new DurableApiError(404, "not_found"));
      await store.submit(A, G);
    }
    if (kind === "conflict") {
      store.edit(A, "new work");
      vi.mocked(api.saveDraft).mockRejectedValue(new DurableApiError(409, "conflict", 9));
      await store.flush(A);
    }
    if (kind === "queue") {
      vi.mocked(api.saveDraft).mockImplementation(() => waiting.promise);
      pending = store.flush(A);
    }
    expect(store.releaseCompleted(A, sent?.command_id ?? "")).toBe(false);
    expect(store.getSnapshot().editors[A]).toBeDefined();
    waiting.resolve(draft(A, 3, "new work"));
    await pending;
  },
);

test("independent keys and late load never replace typing or active selection", async () => {
  vi.useFakeTimers();
  const first = deferred<Draft>();
  const api = fakeApi({
    getDraft: vi.fn((key) => (key === A ? first.promise : Promise.resolve(draft(key, 0, "")))),
  });
  const store = createEditorStore(api, { saveDelayMs: 5000, storage: null });
  store.select(A);
  store.edit(A, "local", 2, 2);
  store.select(B);
  store.edit(B, "second", 3, 3);
  first.resolve(draft(A, 1, "other window"));
  await store.ensure(A);
  await store.ensure(B);
  expect(store.getSnapshot().selectedKey).toBe(B);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "local",
    selectionStart: 2,
    status: "conflict",
    conflictRevision: 1,
  });
  expect(store.getSnapshot().editors[B]).toMatchObject({ text: "second", selectionStart: 3 });
  expect(api.saveDraft).not.toHaveBeenCalled();
});

test("save, command acceptance, and clear stay serial; intervening typing survives", async () => {
  vi.useFakeTimers();
  const posting = deferred<CommandReceipt>();
  const calls: string[] = [];
  const api = fakeApi({
    saveDraft: vi.fn(async (key, revision, text) => {
      calls.push(`save:${revision}:${text}`);
      return draft(key, revision + 1, text);
    }),
    submitCommand: vi.fn(async (input) => {
      calls.push("post");
      return posting.promise.then(() => receipt(input));
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 5000, storage: null });
  await store.ensure(A);
  store.edit(A, "first");
  const sent = store.submit(A, G);
  await vi.waitFor(() => {
    expect(calls).toEqual(["save:0:first", "post"]);
  });
  store.edit(A, "next words");
  posting.resolve(receipt({ command_id: A, session_id: A, generation: G, draft_revision: 1 }));
  expect(await sent).toMatchObject({ state: "accepted" });
  expect(calls).toEqual(["save:0:first", "post", "save:1:next words"]);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "next words",
    revision: 2,
    status: "saved",
  });
});

test("unchanged accepted text clears with a tombstone after POST", async () => {
  const saves: string[] = [];
  const api = fakeApi({
    saveDraft: vi.fn(async (key, revision, text) => {
      saves.push(`${revision}:${text}`);
      return draft(key, revision + 1, text);
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello\n");
  const result = await store.submit(A, G, "hello");
  expect(result?.state).toBe("accepted");
  expect(saves).toEqual(["0:hello"]);
  expect(api.clearDraft).toHaveBeenCalledWith(A, 1);
  expect(store.getSnapshot().editors[A]).toMatchObject({ text: "", revision: 2, status: "saved" });
});

test("failed accepted clear keeps the visible buffer and reports the failure", async () => {
  const api = fakeApi({
    clearDraft: vi.fn(async () => {
      throw new DurableApiError(503, "protection");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    revision: 1,
    status: "unavailable",
    error: "protection",
    acceptedUncleared: true,
    lastReceipt: { state: "accepted" },
  });
  expect(store.getSnapshot().editors[A]?.pendingCommand).not.toBeNull();
  expect(await store.submit(A, G)).toBeNull();
  expect(api.submitCommand).toHaveBeenCalledTimes(1);
});

test("a non-conflict 409 from the accepted clear never claims a revision conflict either", async () => {
  // finalize()'s clear-after-accept catch must not read "any 409 is a conflict" any more than
  // saveNow's does (they hit the same draft route). Latent, since
  // DraftConflict always hardcodes "conflict" server-side, but proven the same way regardless.
  const api = fakeApi({
    clearDraft: vi.fn(async () => {
      throw new DurableApiError(409, "turn_running");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    status: "unavailable",
    conflictRevision: null,
    acceptedUncleared: true,
  });
});

test("a 503 accepted-clear failure retries once with the same arguments, then completes", async () => {
  let calls = 0;
  const api = fakeApi({
    clearDraft: vi.fn(async (key, revision) => {
      calls += 1;
      if (calls === 1) throw new DurableApiError(503, "protection");
      return draft(key, revision + 1, "");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(api.clearDraft).toHaveBeenCalledTimes(2);
  expect(api.clearDraft).toHaveBeenNthCalledWith(1, A, 1);
  expect(api.clearDraft).toHaveBeenNthCalledWith(2, A, 1);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "",
    revision: 2,
    status: "saved",
    acceptedUncleared: false,
    pendingCommand: null,
  });
});

test("a network-error accepted-clear failure retries once, then completes", async () => {
  let calls = 0;
  const api = fakeApi({
    clearDraft: vi.fn(async (key, revision) => {
      calls += 1;
      if (calls === 1) throw new DurableApiError(0, "network_error");
      return draft(key, revision + 1, "");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(api.clearDraft).toHaveBeenCalledTimes(2);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "",
    status: "saved",
    acceptedUncleared: false,
    pendingCommand: null,
  });
});

test("two consecutive 503 accepted-clear failures retry once, then report as today", async () => {
  const api = fakeApi({
    clearDraft: vi.fn(async () => {
      throw new DurableApiError(503, "protection");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(api.clearDraft).toHaveBeenCalledTimes(2);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    status: "unavailable",
    error: "protection",
    acceptedUncleared: true,
  });
});

test("a conflict accepted-clear failure is never retried", async () => {
  const api = fakeApi({
    clearDraft: vi.fn(async () => {
      throw new DurableApiError(409, "conflict", 7);
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(api.clearDraft).toHaveBeenCalledTimes(1);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    status: "conflict",
    conflictRevision: 7,
    acceptedUncleared: true,
  });
});

test("the accepted-submit trim strips any number of trailing newlines linearly", async () => {
  const saves: string[] = [];
  const api = fakeApi({
    saveDraft: vi.fn(async (key, revision, text) => {
      saves.push(`${revision}:${text}`);
      return draft(key, revision + 1, text);
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, `hello${"\n".repeat(100000)}`);
  expect((await store.submit(A, G, "hello"))?.state).toBe("accepted");
  expect(saves).toEqual(["0:hello"]);
  expect(api.clearDraft).toHaveBeenCalledWith(A, 1);
});

test("the accepted-submit trim leaves an interior run of newlines unchanged", () => {
  const value = `a${"\n".repeat(100000)}b`;
  expect(trimTrailingNewlines(value)).toBe(value);
});

test("a turn_running refusal keeps the draft saved and records why the send did not happen", async () => {
  // The draft PUT submit() awaited already committed by the time submitCommand is refused,
  // so the record must not claim the save itself failed.
  const api = fakeApi({
    submitCommand: vi.fn(async () => {
      throw new DurableApiError(409, "turn_running");
    }),
    getCommand: vi.fn(async () => {
      throw new DurableApiError(404, "not_found");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect(await store.submit(A, G)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    status: "saved",
    dispatchRefusal: "turn_running",
    pendingCommand: null,
  });
});

test.each([
  ["refused", true, true],
  ["not admitted", true, true],
  ["ambiguous", true, false],
  ["refused", false, false],
  ["not admitted", false, false],
  ["ambiguous", false, false],
] as const)(
  "a %s send with keepReceipt %s gives back the earlier receipt: %s",
  async (kind, keep, back) => {
    const api = fakeApi({
      getCommand: vi.fn(async () => {
        throw new DurableApiError(404, "not_found");
      }),
    });
    const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
    await store.ensure(A);
    store.edit(A, "first");
    const earlier = await store.submit(A, G);
    expect(earlier).not.toBeNull();
    if (kind !== "not admitted")
      vi.mocked(api.submitCommand).mockRejectedValue(
        kind === "refused"
          ? new DurableApiError(409, "turn_running")
          : new DurableApiError(0, "timeout"),
      );
    store.edit(A, "second");
    const admit = () => kind !== "not admitted";
    const options = keep ? { keepReceipt: true } : undefined;
    expect(await store.submit(A, G, undefined, undefined, undefined, admit, options)).toBeNull();
    expect(store.getSnapshot().editors[A]).toMatchObject({
      text: "second",
      lastReceipt: back ? earlier : null,
      pendingCommand: kind === "ambiguous" ? expect.objectContaining({ session_id: A }) : null,
    });
  },
);

test("a keystroke after a refusal stops the stale claim before status even changes", async () => {
  // dispatchRefusal must not outlive the text it was raised for. pendingCommand
  // is null here (a 409 clears it), so this keystroke's own status recompute runs too -- the
  // regression this guards is dispatchRefusal surviving *any* later edit, not just an unwatched one.
  const api = fakeApi({
    submitCommand: vi.fn(async () => {
      throw new DurableApiError(409, "turn_running");
    }),
    getCommand: vi.fn(async () => {
      throw new DurableApiError(404, "not_found");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect(await store.submit(A, G)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    status: "saved",
    dispatchRefusal: "turn_running",
  });
  store.edit(A, "hello more");
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello more",
    status: "saving",
    dispatchRefusal: null,
  });
});

test("an oversized attachment refuses dispatch but keeps the draft and its attachments", async () => {
  // unsupported_attachment (oversized/unsupported image) is a dispatch refusal, same
  // category as turn_running -- the draft is safe, only the send did not happen.
  const api = fakeApi({
    submitCommand: vi.fn(async () => {
      throw new DurableApiError(400, "unsupported_attachment");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  store.attach(A, [IMAGE]);
  expect(await store.submit(A, G)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    attachments: [IMAGE],
    status: "saved",
    dispatchRefusal: "unsupported_attachment",
    pendingCommand: null,
  });
});

test("an attachment that expired between save and send is also a kept-draft refusal", async () => {
  const api = fakeApi({
    submitCommand: vi.fn(async () => {
      throw new DurableApiError(409, "attachment_unavailable");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  store.attach(A, [IMAGE]);
  expect(await store.submit(A, G)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    status: "saved",
    dispatchRefusal: "attachment_unavailable",
    pendingCommand: null,
  });
});

test("a draft save refused as attachment_unavailable keeps the buffer and names the code", async () => {
  // The draft PUT can answer 409 attachment_unavailable when the image expired.
  // Unlike the route's own "conflict" code, this must fall through to "unavailable" with the
  // image code preserved, so the composer can show removal/replacement copy for it specifically.
  const api = fakeApi({
    saveDraft: vi.fn(async () => {
      throw new DurableApiError(409, "attachment_unavailable");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  store.attach(A, [IMAGE]);
  expect(await store.flush(A)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    attachments: [IMAGE],
    status: "unavailable",
    error: "attachment_unavailable",
    conflictRevision: null,
  });
});

test("stalled save coalesces many autosave ticks into one latest follow-up", async () => {
  vi.useFakeTimers();
  const first = deferred<Draft>();
  const calls: string[] = [];
  const api = fakeApi({
    saveDraft: vi.fn((key, revision, text) => {
      calls.push(text);
      return calls.length === 1 ? first.promise : Promise.resolve(draft(key, revision + 1, text));
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 400, storage: null });
  await store.ensure(A);
  store.edit(A, "first");
  vi.advanceTimersByTime(400);
  await Promise.resolve();
  await Promise.resolve();
  expect(calls).toEqual(["first"]);
  for (let i = 0; i < 100; i += 1) {
    store.edit(A, `latest ${i}`);
    vi.advanceTimersByTime(400);
  }
  expect(calls).toEqual(["first"]);
  first.resolve(draft(A, 1, "first"));
  await store.flush(A);
  vi.advanceTimersByTime(400);
  await Promise.resolve();
  await Promise.resolve();
  expect(calls).toEqual(["first", "latest 99"]);
});

test("409 conflict requires reviewed content and one CAS choice", async () => {
  const api = fakeApi({
    getDraft: vi.fn(async (key) => draft(key, 1, "other")),
    saveDraft: vi.fn(async () => {
      throw new DurableApiError(409, "conflict", 2);
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "mine");
  await store.flush(A);
  expect(store.getSnapshot().editors[A]).toMatchObject({ text: "mine", status: "conflict" });
  await store.reconcile(A, "keep-local");
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  const viewed = await store.reviewConflict(A);
  expect(viewed?.text).toBe("other");
  await store.reconcile(A, "keep-local");
  expect(api.saveDraft).toHaveBeenCalledTimes(2);
  expect(api.saveDraft).toHaveBeenLastCalledWith(A, 1, "mine");
  expect(store.getSnapshot().editors[A]).toMatchObject({ text: "mine", status: "conflict" });
});

test("a 409 with a non-conflict code never claims a revision conflict", async () => {
  // Only the draft route's own "conflict" code means another window won the CAS. Any
  // other 409 (a store lock, a dispatch refusal reusing the status) must fall through to the
  // plain unavailable branch instead of inventing a competing revision.
  const api = fakeApi({
    saveDraft: vi.fn(async () => {
      throw new DurableApiError(409, "turn_running");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "mine");
  expect(await store.flush(A)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "mine",
    status: "unavailable",
    conflictRevision: null,
  });
});

test("failed conflict review keeps the write frozen", async () => {
  let reads = 0;
  const api = fakeApi({
    getDraft: vi.fn(async (key) => {
      reads += 1;
      if (reads > 1) throw new DurableApiError(503, "protection");
      return draft(key, 1, "other");
    }),
    saveDraft: vi.fn(async () => {
      throw new DurableApiError(409, "conflict", 2);
    }),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "mine");
  await store.flush(A);
  expect(await store.reviewConflict(A)).toBeNull();
  store.edit(A, "mine again");
  expect(await store.flush(A)).toBeNull();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "mine again",
    conflictRevision: 2,
    status: "unavailable",
  });
});

test("newer text is backed up before a captured older submission overwrites its key", async () => {
  const saves: Array<{ key: string; text: string }> = [];
  const api = fakeApi({
    saveDraft: vi.fn(async (key, revision, text) => {
      saves.push({ key, text });
      return draft(key, revision + 1, text);
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "old");
  await store.flush(A);
  store.edit(A, "newer");
  await store.flush(A);
  expect((await store.submit(A, G, "old"))?.state).toBe("accepted");
  const backupKey = store.getSnapshot().editors[A]?.backupKey;
  expect(backupKey).toBeTruthy();
  expect(backupKey).not.toBe(A);
  expect(saves).toEqual([
    { key: A, text: "old" },
    { key: A, text: "newer" },
    { key: backupKey, text: "newer" },
    { key: A, text: "old" },
    { key: A, text: "newer" },
  ]);
  expect(store.getSnapshot().editors[A]?.text).toBe("newer");
});

test("a previous command for the saved revision blocks a fresh ID", async () => {
  const prior = receipt({ command_id: B, session_id: A, generation: G, draft_revision: 1 });
  const api = fakeApi({
    getDraft: vi.fn(async (key) => draft(key, 1, "hello")),
    getDraftCommand: vi.fn(async () => prior),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  expect(await store.submit(A, G)).toBeNull();
  expect(api.submitCommand).not.toHaveBeenCalled();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    acceptedUncleared: true,
    lastReceipt: { command_id: B },
  });
});

test("atomic other-window 409 binds the winning receipt after both earlier lookups missed", async () => {
  const winner = receipt({ command_id: B, session_id: A, generation: G, draft_revision: 1 });
  let lookups = 0;
  const api = fakeApi({
    getDraftCommand: vi.fn(async () => {
      lookups += 1;
      return lookups === 1 ? null : winner;
    }),
    submitCommand: vi.fn(async () => {
      throw new DurableApiError(409, "draft_already_submitted");
    }),
    getCommand: vi.fn(async () => {
      throw new DurableApiError(404, "not_found");
    }),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect(await store.submit(A, G)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello",
    acceptedUncleared: true,
    pendingCommand: { command_id: B },
    lastReceipt: { command_id: B },
  });
  expect(api.submitCommand).toHaveBeenCalledTimes(1);
  expect(await store.submit(A, G)).toBeNull();
  expect(api.submitCommand).toHaveBeenCalledTimes(1);
});

test("ambiguous POST inspects only its ID and blocks a new ID through reload", async () => {
  const storage = memoryStorage();
  const inputs: CommandInput[] = [];
  const api = fakeApi({
    getDraft: vi.fn(async (key) => draft(key, 1, "hello")),
    submitCommand: vi.fn(async (input) => {
      inputs.push(input);
      throw new DurableApiError(0, "timeout");
    }),
    getCommand: vi.fn(async () => {
      throw new DurableApiError(404, "not_found");
    }),
  });
  const first = createEditorStore(api, { saveDelayMs: 100000, storage });
  await first.ensure(A);
  expect(await first.submit(A, G)).toBeNull();
  expect(inputs).toHaveLength(1);
  expect(api.getCommand).toHaveBeenCalledWith(inputs[0]?.command_id);
  expect(first.getSnapshot().editors[A]?.pendingCommand?.command_id).toBe(inputs[0]?.command_id);
  const reloaded = createEditorStore(api, { saveDelayMs: 100000, storage });
  await reloaded.ensure(A);
  expect(reloaded.getSnapshot().editors[A]?.pendingCommand?.command_id).toBe(inputs[0]?.command_id);
  expect(await reloaded.submit(A, G)).toBeNull();
  expect(inputs).toHaveLength(1);
  await reloaded.retryPending(A);
  expect(inputs).toHaveLength(2);
  expect(inputs[1]).toEqual(inputs[0]);
});

test("a stale_generation retry after the buffer moved on does not claim the draft is saved", async () => {
  // retryPending() re-sends the accepted text with no save of its own. Autosave
  // never arms while pendingCommand is set, so a refusal on retry must not say "saved" about text
  // the store never wrote.
  let attempt = 0;
  const api = fakeApi({
    submitCommand: vi.fn(async () => {
      attempt += 1;
      throw attempt === 1
        ? new DurableApiError(0, "network_error")
        : new DurableApiError(409, "stale_generation");
    }),
    getCommand: vi.fn(async () => {
      throw new DurableApiError(404, "not_found");
    }),
  });
  const store = createEditorStore(api, { saveDelayMs: 100000, storage: null });
  await store.ensure(A);
  store.edit(A, "hello");
  expect(await store.submit(A, G)).toBeNull(); // ambiguous: pendingCommand survives
  expect(store.getSnapshot().editors[A]?.pendingCommand).not.toBeNull();
  store.edit(A, "hello more"); // no autosave: schedule() refuses while pendingCommand is set
  expect(await store.retryPending(A)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "hello more",
    status: "unavailable",
    dispatchRefusal: null,
  });
  expect(api.saveDraft).toHaveBeenCalledTimes(1); // "hello more" was never saved
});

test("a second lost POST response recovers its own receipt after an earlier success", async () => {
  let count = 0;
  const secondInput: { current: CommandInput | null } = { current: null };
  const api = fakeApi({
    submitCommand: vi.fn(async (input) => {
      count += 1;
      if (count === 2) {
        secondInput.current = input;
        throw new DurableApiError(0, "timeout");
      }
      return receipt(input);
    }),
    getCommand: vi.fn(async () => {
      if (!secondInput.current) throw new Error("missing second command");
      return receipt(secondInput.current);
    }),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "first");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  store.edit(A, "second");
  expect((await store.submit(A, G))?.state).toBe("accepted");
  expect(api.getCommand).toHaveBeenCalledWith(secondInput.current?.command_id);
  expect(api.clearDraft).toHaveBeenCalledTimes(2);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "",
    pendingCommand: null,
    acceptedUncleared: false,
    lastReceipt: { command_id: secondInput.current?.command_id },
  });
});

test("load failure preserves local text and refuses a save without known revision", async () => {
  const api = fakeApi({
    getDraft: vi.fn(async () => {
      throw new DurableApiError(503, "protection");
    }),
  });
  const store = createEditorStore(api, { storage: null });
  store.edit(A, "keep me");
  await store.ensure(A);
  expect(await store.flush(A)).toBeNull();
  expect(store.getSnapshot().editors[A]).toMatchObject({ text: "keep me", status: "unavailable" });
  expect(api.saveDraft).not.toHaveBeenCalled();
});

test("image-only draft is saved, submitted once and cleared after acceptance", async () => {
  vi.useFakeTimers();
  const api = fakeApi();
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.attach(A, [IMAGE]);
  await store.submit(A, G);
  expect(api.saveDraft).toHaveBeenCalledWith(A, 0, "", [IMAGE]);
  expect(api.submitCommand).toHaveBeenCalledTimes(1);
  expect(store.getSnapshot().editors[A]?.attachments).toEqual([]);
});

test("attachments added during admission survive its delayed clear", async () => {
  vi.useFakeTimers();
  const posting = deferred<CommandReceipt>();
  const api = fakeApi({ submitCommand: vi.fn(() => posting.promise) });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "first");
  store.attach(A, [IMAGE]);
  const sending = store.submit(A, G);
  await vi.waitFor(() => expect(api.submitCommand).toHaveBeenCalledOnce());
  const next = { ...IMAGE, asset_id: B, name: "second.png" };
  store.attach(A, [next]);
  const input = vi.mocked(api.submitCommand).mock.calls[0]?.[0];
  if (!input) throw new Error("missing command");
  posting.resolve(receipt(input));
  await sending;
  expect(api.clearDraft).not.toHaveBeenCalled();
  expect(store.getSnapshot().editors[A]?.attachments).toEqual([next]);
  expect(api.saveDraft).toHaveBeenLastCalledWith(A, 1, "first", [next]);
});

test("attachment-only change racing a load becomes a reviewable conflict", async () => {
  const loading = deferred<Draft>();
  const api = fakeApi({ getDraft: vi.fn(() => loading.promise) });
  const store = createEditorStore(api, { storage: null });
  store.select(A);
  store.attach(A, [IMAGE]);
  loading.resolve({ ...draft(A, 2, ""), attachments: [] });
  await store.ensure(A);
  expect(store.getSnapshot().editors[A]).toMatchObject({
    status: "conflict",
    attachments: [IMAGE],
    conflictRevision: 2,
  });
  expect(api.saveDraft).not.toHaveBeenCalled();
});

test("reload restores media and conflict resolution replaces text and media together", async () => {
  const api = fakeApi({
    getDraft: vi.fn(async (key) => ({ ...draft(key, 3, ""), attachments: [IMAGE] })),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  expect(store.getSnapshot().editors[A]?.attachments).toEqual([IMAGE]);
  const replacement = { ...IMAGE, asset_id: B };
  vi.mocked(api.getDraft).mockResolvedValue({
    ...draft(A, 4, "newer"),
    attachments: [replacement],
  });
  await store.reviewConflict(A);
  await store.reconcile(A, "use-server");
  expect(store.getSnapshot().editors[A]).toMatchObject({
    text: "newer",
    attachments: [replacement],
  });
});

const EXCERPT: VaultExcerptPart = {
  kind: "vault_excerpt",
  vault_id: "55555555-5555-4555-8555-555555555555",
  ref: "30-knowledge/note.md",
  start_line: 2,
  end_line: 4,
  sha256: "e".repeat(64),
};

test("a send carries its excerpts on the submitted draft revision, then lets them go", async () => {
  const api = fakeApi({
    saveDraft: vi.fn(async (key, revision, text, _attachments, parts) => ({
      ...draft(key, revision + 1, text),
      ...(parts?.length ? { parts } : {}),
    })),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "quote this");
  expect(await store.submit(A, G, "quote this", [], [EXCERPT])).not.toBeNull();
  expect(api.saveDraft).toHaveBeenCalledWith(A, 0, "quote this", [], [EXCERPT]);
  expect(api.submitCommand).toHaveBeenCalledWith(
    expect.objectContaining({ session_id: A, draft_revision: 1 }),
  );
  // The excerpt went with its turn: the next save is text-only again.
  store.edit(A, "next message");
  await store.flush(A);
  expect(api.saveDraft).toHaveBeenLastCalledWith(A, 2, "next message");
});

test("an excerpt attached after the text was saved still reaches the draft", async () => {
  vi.useFakeTimers();
  const api = fakeApi();
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "saved words");
  await vi.advanceTimersByTimeAsync(401);
  expect(api.saveDraft).toHaveBeenCalledWith(A, 0, "saved words");
  // Only the excerpts differ from what is stored. A comparison that saw text and attachments
  // only would call this draft clean and send the turn without its quotation.
  expect(await store.submit(A, G, "saved words", [], [EXCERPT])).not.toBeNull();
  expect(api.saveDraft).toHaveBeenLastCalledWith(A, 1, "saved words", [], [EXCERPT]);
});

test("a text-only send saves the legacy body, with no parts argument at all", async () => {
  const api = fakeApi();
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  store.edit(A, "plain");
  expect(await store.submit(A, G, "plain", [], [])).not.toBeNull();
  expect(api.saveDraft).toHaveBeenCalledWith(A, 0, "plain");
});

test("a reloaded draft's stored excerpts are cleared before the same text is submitted", async () => {
  const api = fakeApi({
    getDraft: vi.fn(async (key) => ({ ...draft(key, 3, "quoted"), parts: [EXCERPT] })),
  });
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  // No chip is restored, so the user cannot see the stored excerpt. Sending the unchanged text
  // must still rewrite the draft: revision 3 carries the quotation and must never be submitted.
  expect(await store.submit(A, G, "quoted", [], [])).not.toBeNull();
  expect(api.saveDraft).toHaveBeenCalledWith(A, 3, "quoted");
  expect(api.submitCommand).toHaveBeenCalledWith(
    expect.objectContaining({ session_id: A, draft_revision: 4 }),
  );
});

test("a reconciled draft's stored excerpts are cleared before the same text is submitted", async () => {
  const api = fakeApi();
  const store = createEditorStore(api, { storage: null });
  await store.ensure(A);
  vi.mocked(api.getDraft).mockResolvedValue({ ...draft(A, 3, "quoted"), parts: [EXCERPT] });
  await store.reviewConflict(A);
  await store.reconcile(A, "use-server");
  expect(store.getSnapshot().editors[A]).toMatchObject({ text: "quoted", revision: 3 });
  expect(await store.submit(A, G, "quoted", [], [])).not.toBeNull();
  expect(api.saveDraft).toHaveBeenCalledWith(A, 3, "quoted");
  expect(api.submitCommand).toHaveBeenCalledWith(
    expect.objectContaining({ session_id: A, draft_revision: 4 }),
  );
});

test("the draft cap callers check is the one the durable API refuses at", async () => {
  const fetchImpl = vi.fn(async () => new Response("{}", { status: 503 }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  // A three-byte character, so a cap counted in UTF-16 units instead of bytes would differ.
  const atCap = "\u3042".repeat(Math.floor(MAX_DRAFT_BYTES / 3)) + "x".repeat(MAX_DRAFT_BYTES % 3);
  expect(draftFits(atCap)).toBe(true);
  expect(draftFits(`${atCap}x`)).toBe(false);
  await expect(api.saveDraft(A, 0, `${atCap}x`)).rejects.toMatchObject({ code: "too_large" });
  expect(fetchImpl).not.toHaveBeenCalled();
  await api.saveDraft(A, 0, atCap).catch(() => undefined);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});
