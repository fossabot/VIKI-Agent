/** Durable, independent editor buffers. Every operation for one draft key shares one queue. */
import {
  type CommandInput,
  type CommandReceipt,
  type Draft,
  type DurableApi,
  DurableApiError,
} from "../clients/conversations/durableApi";
import type { Attachment } from "../turnInput";
import type { VaultExcerptPart } from "./excerptApi";

export type EditorStatus = "loading" | "saving" | "saved" | "conflict" | "unavailable";

/** Why a durable send was refused while the draft itself is safe: the actor would not
 * admit it. Never set together with `status: "unavailable"`, which means the save itself failed. */
export type DispatchRefusalCode =
  | "turn_running"
  | "stale_generation"
  | "closed"
  | "unsupported_attachment"
  | "attachment_unavailable";

export interface EditorEntry {
  key: string;
  text: string;
  attachments: readonly Attachment[];
  selectionStart: number;
  selectionEnd: number;
  revision: number;
  savedAt: string | null;
  status: EditorStatus;
  conflictRevision: number | null;
  conflictDraft: Draft | null;
  error: string | null;
  pendingCommand: CommandInput | null;
  lastReceipt: CommandReceipt | null;
  acceptedUncleared: boolean;
  backupKey: string | null;
  submitting: boolean;
  dispatchRefusal: DispatchRefusalCode | null;
}

export interface EditorSnapshot {
  selectedKey: string | null;
  editors: Readonly<Record<string, EditorEntry>>;
}

export interface EditorStore {
  getSnapshot(): EditorSnapshot;
  subscribe(listener: () => void): () => void;
  select(key: string): void;
  ensure(key: string): Promise<void>;
  edit(key: string, text: string, selectionStart?: number, selectionEnd?: number): void;
  attach(key: string, attachments: readonly Attachment[]): void;
  setSelection(key: string, start: number, end: number): void;
  flush(key: string): Promise<Draft | null>;
  /** The key is the target session UUID; the caller must already hold that session open.
   * `parts` are the vault excerpts of this turn: they reach the service on the submitted
   * draft revision, which is the only place a command reads its turn from. With `keepReceipt`,
   * a send that `admit` stops or the service definitely refuses puts back the `lastReceipt`
   * the record held before it; an ambiguous failure still leaves it null beside its pending
   * command. */
  submit(
    key: string,
    generation: string,
    submittedText?: string,
    attachments?: readonly Attachment[],
    parts?: readonly VaultExcerptPart[],
    admit?: () => boolean,
    options?: { keepReceipt?: boolean },
  ): Promise<CommandReceipt | null>;
  /** GET only: never creates another native attempt, even when the receipt is missing. */
  recoverPending(key: string): Promise<CommandReceipt | null>;
  /** An explicit retry reuses the original command ID and immutable input. */
  retryPending(key: string): Promise<CommandReceipt | null>;
  /** An explicit review decision retains the draft text and unlocks a fresh turn. */
  releaseAccepted(key: string): void;
  /** Release a dedicated source's settled empty editor after its matching native turn closes. */
  releaseCompleted(key: string, expectedCommandId: string): boolean;
  capacityAvailable(key: string): boolean;
  reviewConflict(key: string): Promise<Draft | null>;
  /** Resolve a same-key/window revision conflict after reviewing the other draft. */
  reconcile(key: string, choice: "keep-local" | "use-server"): Promise<void>;
}

export interface EditorStoreOptions {
  saveDelayMs?: number;
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
}

interface RecordState extends EditorEntry {
  loaded: boolean;
  serial: number;
  savedText: string;
  savedAttachments: readonly Attachment[];
  /** The excerpts of the next send, as `submit` captured them. The chips themselves live in the
   * view, so `parts` is not restored from a loaded draft. */
  parts: readonly VaultExcerptPart[];
  /** What the stored revision holds, so a loaded or reconciled draft's excerpts read as a change
   * the next save clears: a window that shows no chip never submits a revision carrying one. */
  savedParts: readonly VaultExcerptPart[];
  pendingSerial: number | null;
  reviewedSerial: number | null;
}

const MAX_EDITORS = 64;
const PENDING_PREFIX = "viki.editor.pending.";

function sameAttachments(a: readonly Attachment[], b: readonly Attachment[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function sameParts(a: readonly VaultExcerptPart[], b: readonly VaultExcerptPart[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** The durable API refuses a draft whose text is larger than this (`MAX_TEXT_BYTES` there), but
 * only when the autosave runs. Code that adds text to a draft checks it first, so the refusal
 * reaches the place the text came from. */
export const MAX_DRAFT_BYTES = 1 << 20;

export function draftFits(text: string): boolean {
  return new TextEncoder().encode(text).byteLength <= MAX_DRAFT_BYTES;
}

/** Drop trailing `\n` characters, linearly (no backtracking regex). Exported so the Composer's
 * own pre-send trim, which this store's comparisons must match, uses this one function. */
export function trimTrailingNewlines(text: string): string {
  let end = text.length;
  while (end > 0 && text.charCodeAt(end - 1) === 10) end -= 1;
  return text.slice(0, end);
}

/** The accepted clear's own failure is worth one retry: a lost response after a committed
 * write, or a transient service refusal. Never a conflict or any other 4xx. */
function retryableClearFailure(error: unknown): boolean {
  return (
    error instanceof DurableApiError &&
    (error.status === 503 ||
      (error.status === 0 && (error.code === "timeout" || error.code === "network_error")))
  );
}

function unchanged(
  record: RecordState,
  text = record.text,
  attachments = record.attachments,
  parts = record.parts,
): boolean {
  return (
    record.savedText === text &&
    sameAttachments(record.savedAttachments, attachments) &&
    sameParts(record.savedParts, parts)
  );
}

function copyAttachments(attachments: readonly Attachment[]): readonly Attachment[] {
  return attachments.map((attachment) => ({ ...attachment }));
}

function copyParts(parts: readonly VaultExcerptPart[]): readonly VaultExcerptPart[] {
  return parts.map((part) => ({ ...part }));
}

function codeOf(error: unknown): string {
  return error instanceof DurableApiError ? error.code : "unavailable";
}

/** A refusal to dispatch, never a failure to save: the actor would not admit the send. */
function dispatchRefusalCode(error: unknown): DispatchRefusalCode | null {
  if (!(error instanceof DurableApiError)) return null;
  const { code } = error;
  if (
    code === "turn_running" ||
    code === "stale_generation" ||
    code === "closed" ||
    code === "unsupported_attachment" ||
    code === "attachment_unavailable"
  )
    return code;
  return null;
}

function ambiguous(error: unknown): boolean {
  return (
    !(error instanceof DurableApiError) ||
    error.status === 0 ||
    error.status >= 500 ||
    error.status === 408
  );
}

function savedPending(storage: EditorStoreOptions["storage"], key: string): CommandInput | null {
  try {
    const raw = storage?.getItem(`${PENDING_PREFIX}${key}`);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object") return null;
    const input = value as Partial<CommandInput>;
    if (
      typeof input.command_id !== "string" ||
      typeof input.session_id !== "string" ||
      typeof input.generation !== "string" ||
      !Number.isSafeInteger(input.draft_revision) ||
      input.draft_revision === undefined ||
      input.draft_revision < 1 ||
      input.session_id !== key
    )
      return null;
    return input as CommandInput;
  } catch {
    return null;
  }
}

/** No prompt text enters browser storage; only an uncertain command's immutable identifiers do. */
function rememberPending(
  storage: EditorStoreOptions["storage"],
  key: string,
  input: CommandInput | null,
): void {
  try {
    if (input) storage?.setItem(`${PENDING_PREFIX}${key}`, JSON.stringify(input));
    else storage?.removeItem(`${PENDING_PREFIX}${key}`);
  } catch {
    // The live buffer and command ID remain in memory. Receipt listing can recover after reload.
  }
}

export function createEditorStore(api: DurableApi, options: EditorStoreOptions = {}): EditorStore {
  let defaultStorage: EditorStoreOptions["storage"] = null;
  try {
    if (typeof window !== "undefined") defaultStorage = window.sessionStorage;
  } catch {
    // Storage refusal must not prevent an in-memory editor from opening.
  }
  const storage = options.storage === undefined ? defaultStorage : options.storage;
  const delay = options.saveDelayMs ?? 400;
  const records = new Map<string, RecordState>();
  const tails = new Map<string, Promise<void>>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const loading = new Map<string, Promise<void>>();
  const flushing = new Map<string, Promise<Draft | null>>();
  const reviewing = new Map<string, Promise<Draft | null>>();
  const reconciling = new Map<string, Promise<void>>();
  const listeners = new Set<() => void>();
  let snapshot: EditorSnapshot = { selectedKey: null, editors: {} };

  function publish(record?: RecordState, selectedKey = snapshot.selectedKey): void {
    snapshot = {
      selectedKey,
      editors: record ? { ...snapshot.editors, [record.key]: { ...record } } : snapshot.editors,
    };
    for (const listener of [...listeners]) listener();
  }

  function entry(key: string): RecordState {
    const found = records.get(key);
    if (found) return found;
    if (records.size >= MAX_EDITORS) throw new Error("editor_capacity");
    const pendingCommand = savedPending(storage, key);
    const made: RecordState = {
      key,
      text: "",
      attachments: [],
      selectionStart: 0,
      selectionEnd: 0,
      revision: 0,
      savedAt: null,
      status: "loading",
      conflictRevision: null,
      conflictDraft: null,
      error: null,
      pendingCommand,
      lastReceipt: null,
      acceptedUncleared: false,
      backupKey: null,
      submitting: false,
      dispatchRefusal: null,
      loaded: false,
      serial: 0,
      savedText: "",
      savedAttachments: [],
      parts: [],
      savedParts: [],
      pendingSerial: null,
      reviewedSerial: null,
    };
    records.set(key, made);
    publish(made);
    return made;
  }

  function queue<T>(key: string, task: () => Promise<T>): Promise<T> {
    const previous = tails.get(key) ?? Promise.resolve();
    const result = previous.then(task, task);
    const tail = result.then(
      () => {},
      () => {},
    );
    tails.set(key, tail);
    void tail.then(() => {
      if (tails.get(key) === tail) tails.delete(key);
    });
    return result;
  }

  function cancelTimer(key: string): void {
    const timer = timers.get(key);
    if (timer !== undefined) clearTimeout(timer);
    timers.delete(key);
  }

  function schedule(record: RecordState): void {
    if (!record.loaded || record.conflictRevision !== null || record.pendingCommand) return;
    cancelTimer(record.key);
    timers.set(
      record.key,
      setTimeout(() => {
        timers.delete(record.key);
        void flush(record.key);
      }, delay),
    );
  }

  async function loadNow(record: RecordState): Promise<boolean> {
    if (record.loaded) return true;
    const startSerial = record.serial;
    record.status = "loading";
    publish(record);
    try {
      const draft = await api.getDraft(record.key);
      record.loaded = true;
      record.revision = draft.revision;
      record.savedAt = draft.saved_at;
      record.savedText = draft.text;
      record.savedAttachments = copyAttachments(draft.attachments ?? []);
      record.savedParts = copyParts(draft.parts ?? []);
      record.error = null;
      if (record.serial === 0 && startSerial === 0) {
        record.text = draft.text;
        record.attachments = copyAttachments(draft.attachments ?? []);
        record.selectionStart = draft.text.length;
        record.selectionEnd = draft.text.length;
        record.status = "saved";
      } else if (draft.revision > 0 && !unchanged(record)) {
        // A response arriving after typing cannot replace it or silently overwrite another window.
        record.status = "conflict";
        record.conflictRevision = draft.revision;
      } else {
        record.status = "saving";
        schedule(record);
      }
      publish(record);
      return true;
    } catch (error) {
      record.status = "unavailable";
      record.error = codeOf(error);
      publish(record);
      return false;
    }
  }

  async function saveNow(
    record: RecordState,
    text: string,
    attachments = copyAttachments(record.attachments),
    parts = copyParts(record.parts),
  ): Promise<Draft | null> {
    if (!(await loadNow(record))) return null;
    if (record.conflictRevision !== null) return null;
    if (unchanged(record, text, attachments, parts)) {
      record.status = unchanged(record) ? "saved" : "saving";
      publish(record);
      return {
        key: record.key,
        revision: record.revision,
        text,
        saved_at: record.savedAt,
        attachments,
        parts,
      };
    }
    record.status = "saving";
    record.error = null;
    publish(record);
    try {
      // Each save names only the lists it carries: the service reads an omitted `parts` as the
      // legacy save, which is also how a draft is told it has no excerpts any more.
      let draft: Draft;
      if (parts.length)
        draft = await api.saveDraft(record.key, record.revision, text, attachments, parts);
      else if (attachments.length)
        draft = await api.saveDraft(record.key, record.revision, text, attachments);
      else draft = await api.saveDraft(record.key, record.revision, text);
      record.revision = draft.revision;
      record.savedAt = draft.saved_at;
      record.savedText = text;
      record.savedAttachments = copyAttachments(attachments);
      record.savedParts = copyParts(parts);
      record.status = unchanged(record) ? "saved" : "saving";
      record.conflictRevision = null;
      record.conflictDraft = null;
      publish(record);
      if (!unchanged(record)) schedule(record);
      return draft;
    } catch (error) {
      record.error = codeOf(error);
      // Only the draft route's own "conflict" code means another window won the CAS; any other
      // 409 read as one would blame a competing edit for a failure that was something else
      // (finalize()'s catch below makes the same check for the same reason).
      if (error instanceof DurableApiError && error.status === 409 && error.code === "conflict") {
        record.status = "conflict";
        record.conflictRevision = error.currentRevision ?? null;
        record.conflictDraft = null;
        record.reviewedSerial = null;
      } else record.status = "unavailable";
      publish(record);
      return null;
    }
  }

  function completePending(record: RecordState): void {
    record.pendingCommand = null;
    record.pendingSerial = null;
    record.acceptedUncleared = false;
    rememberPending(storage, record.key, null);
  }

  async function finalize(record: RecordState, receipt: CommandReceipt): Promise<void> {
    const acceptedSerial = record.pendingSerial;
    const input = record.pendingCommand;
    record.lastReceipt = receipt;
    record.submitting = false;
    if (!input) return;
    // After reload the old change serial is gone. A newer empty tombstone proves a prior clear;
    // otherwise keep the ID until the user reviews the retained submission.
    if (acceptedSerial === null) {
      if (record.backupKey && !unchanged(record)) {
        const saved = await saveNow(record, record.text);
        if (saved) completePending(record);
        else record.acceptedUncleared = true;
      } else if (
        record.loaded &&
        record.revision > input.draft_revision &&
        record.text === "" &&
        record.attachments.length === 0
      ) {
        completePending(record);
      } else {
        record.acceptedUncleared = true;
        record.status = "unavailable";
        record.error = "accepted_review";
      }
      publish(record);
      return;
    }
    if (record.serial !== acceptedSerial) {
      const saved = await saveNow(record, record.text);
      if (saved) completePending(record);
      else record.acceptedUncleared = true;
      publish(record);
      return;
    }
    record.status = "saving";
    record.error = null;
    publish(record);
    try {
      // Keep the visible buffer until the clear tombstone commits. Its CAS revision is exactly
      // the submitted revision, and the queue excludes any earlier autosave completing later.
      let cleared: Draft;
      try {
        cleared = await api.clearDraft(record.key, record.revision);
      } catch (error) {
        if (!retryableClearFailure(error)) throw error;
        // Replay-safe: the draft route echoes an `expected + 1` save with the same (empty) text.
        cleared = await api.clearDraft(record.key, record.revision);
      }
      record.revision = cleared.revision;
      record.savedAt = cleared.saved_at;
      record.savedText = "";
      record.savedAttachments = [];
      record.savedParts = [];
      record.parts = [];
      record.conflictRevision = null;
      record.conflictDraft = null;
      if (record.serial === acceptedSerial) {
        cancelTimer(record.key);
        record.serial += 1;
        record.text = "";
        record.attachments = [];
        record.selectionStart = 0;
        record.selectionEnd = 0;
        record.status = "saved";
      } else {
        record.status = "saving";
      }
      if (record.text !== "" || record.attachments.length > 0) {
        const saved = await saveNow(record, record.text);
        if (saved) completePending(record);
        else record.acceptedUncleared = true;
      } else completePending(record);
      publish(record);
    } catch (error) {
      record.acceptedUncleared = true;
      record.error = codeOf(error);
      // Same reason as saveNow's catch above: only "conflict" means a lost CAS, so any other
      // 409 here must not be read as another window editing the draft.
      if (error instanceof DurableApiError && error.status === 409 && error.code === "conflict") {
        record.status = "conflict";
        record.conflictRevision = error.currentRevision ?? null;
        record.conflictDraft = null;
      } else record.status = "unavailable";
      publish(record);
    }
  }

  async function recoverNow(record: RecordState): Promise<CommandReceipt | null> {
    const input = record.pendingCommand;
    if (!input) return null;
    try {
      const receipt = await api.getCommand(input.command_id);
      if (
        receipt.session_id !== input.session_id ||
        receipt.generation !== input.generation ||
        receipt.draft_revision !== input.draft_revision
      ) {
        record.status = "unavailable";
        record.error = "invalid_receipt";
        publish(record);
        return null;
      }
      if (record.lastReceipt) {
        record.lastReceipt = receipt;
        publish(record);
      } else await finalize(record, receipt);
      return receipt;
    } catch (error) {
      record.status = "unavailable";
      record.error =
        error instanceof DurableApiError && error.status === 404
          ? "receipt_pending"
          : codeOf(error);
      publish(record);
      return null;
    }
  }

  function bindPrior(record: RecordState, prior: CommandReceipt): void {
    record.lastReceipt = prior;
    record.pendingCommand = {
      command_id: prior.command_id,
      session_id: prior.session_id,
      generation: prior.generation,
      draft_revision: prior.draft_revision,
    };
    record.pendingSerial = null;
    record.acceptedUncleared = true;
    record.submitting = false;
    record.status = "unavailable";
    record.error = "accepted_review";
    rememberPending(storage, record.key, record.pendingCommand);
    publish(record);
  }

  async function postNow(
    record: RecordState,
    input: CommandInput,
    keptReceipt: CommandReceipt | null = null,
  ): Promise<CommandReceipt | null> {
    try {
      const receipt = await api.submitCommand(input);
      await finalize(record, receipt);
      return receipt;
    } catch (error) {
      // Even a failed response may have followed a commit. Inspect this exact ID before deciding.
      const recovered = await recoverNow(record);
      if (recovered) return recovered;
      if (error instanceof DurableApiError && error.code === "draft_already_submitted") {
        try {
          const prior = await api.getDraftCommand(record.key, input.draft_revision);
          if (prior) {
            bindPrior(record, prior);
            return null;
          }
        } catch (lookupError) {
          record.error = codeOf(lookupError);
        }
      }
      if (!ambiguous(error)) {
        record.pendingCommand = null;
        record.pendingSerial = null;
        rememberPending(storage, record.key, null);
        if (keptReceipt) record.lastReceipt = keptReceipt;
      }
      record.submitting = false;
      // A refusal to dispatch is not a failure to save -- but only when the store still holds
      // exactly what is on screen. retryPending() re-sends an old draft revision with no save of
      // its own, so a refusal there must never claim today's buffer was saved.
      const refusal = unchanged(record) ? dispatchRefusalCode(error) : null;
      record.status = refusal ? "saved" : "unavailable";
      record.dispatchRefusal = refusal;
      record.error = codeOf(error);
      publish(record);
      return null;
    }
  }

  function flush(key: string): Promise<Draft | null> {
    const record = entry(key);
    cancelTimer(key);
    const existing = flushing.get(key);
    if (existing) return existing;
    const result = queue(key, () =>
      record.pendingCommand ? Promise.resolve(null) : saveNow(record, record.text),
    );
    flushing.set(key, result);
    void result.finally(() => {
      if (flushing.get(key) === result) flushing.delete(key);
    });
    return result;
  }

  async function backUpNewerText(
    record: RecordState,
    submittedText: string,
    attachments: readonly Attachment[],
  ): Promise<boolean> {
    if (
      (record.text === submittedText || trimTrailingNewlines(record.text) === submittedText) &&
      sameAttachments(record.attachments, attachments)
    )
      return true;
    const backupKey = crypto.randomUUID();
    let revision = 0;
    // Captured text can be old by the time the browser finishes opening the target session.
    // Preserve the newer editor in another encrypted draft before writing the captured
    // submission over this key.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const current = record.text;
      const currentAttachments = copyAttachments(record.attachments);
      try {
        const backup = currentAttachments.length
          ? await api.saveDraft(backupKey, revision, current, currentAttachments)
          : await api.saveDraft(backupKey, revision, current);
        revision = backup.revision;
        record.backupKey = backupKey;
        publish(record);
      } catch (error) {
        record.status = "unavailable";
        record.error = codeOf(error);
        publish(record);
        return false;
      }
      if (record.text === current && sameAttachments(record.attachments, currentAttachments))
        return true;
    }
    record.status = "unavailable";
    record.error = "editing_during_submit";
    publish(record);
    return false;
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    select(key) {
      entry(key);
      if (snapshot.selectedKey !== key) publish(undefined, key);
      void this.ensure(key);
    },
    ensure(key) {
      const record = entry(key);
      if (record.loaded) return Promise.resolve();
      const existing = loading.get(key);
      if (existing) return existing;
      const promise = queue(key, async () => {
        await loadNow(record);
      });
      loading.set(key, promise);
      void promise.finally(() => {
        if (loading.get(key) === promise) loading.delete(key);
      });
      return promise;
    },
    edit(key, text, selectionStart = text.length, selectionEnd = selectionStart) {
      const record = entry(key);
      if (
        record.text === text &&
        record.selectionStart === selectionStart &&
        record.selectionEnd === selectionEnd
      )
        return;
      if (record.text !== text) {
        record.serial += 1;
        // The refusal note describes the text it was raised for; newer typing makes it stale
        // before status even changes (e.g. while pendingCommand blocks autosave below).
        record.dispatchRefusal = null;
      }
      record.text = text;
      record.selectionStart = selectionStart;
      record.selectionEnd = selectionEnd;
      if (record.loaded && record.conflictRevision === null && !record.pendingCommand) {
        record.status = unchanged(record) ? "saved" : "saving";
        if (!unchanged(record)) schedule(record);
      }
      publish(record);
      if (!record.loaded) void this.ensure(key);
    },
    attach(key, attachments) {
      const record = entry(key);
      if (sameAttachments(record.attachments, attachments)) return;
      record.serial += 1;
      record.attachments = copyAttachments(attachments);
      record.dispatchRefusal = null;
      if (record.loaded && record.conflictRevision === null && !record.pendingCommand) {
        record.status = unchanged(record) ? "saved" : "saving";
        if (!unchanged(record)) schedule(record);
      }
      publish(record);
      if (!record.loaded) void this.ensure(key);
    },
    setSelection(key, start, end) {
      const record = entry(key);
      if (record.selectionStart === start && record.selectionEnd === end) return;
      record.selectionStart = start;
      record.selectionEnd = end;
      publish(record);
    },
    flush,
    submit(
      key,
      generation,
      submittedText,
      submittedAttachments,
      submittedParts,
      admit,
      sendOptions,
    ) {
      const record = entry(key);
      if (record.submitting || record.pendingCommand || record.conflictRevision !== null)
        return Promise.resolve(null);
      const text = submittedText ?? record.text;
      const attachments = copyAttachments(submittedAttachments ?? record.attachments);
      const parts = copyParts(submittedParts ?? record.parts);
      // An excerpt alone is not a turn, exactly as the service reads sendability.
      if (!text.trim() && attachments.length === 0) return Promise.resolve(null);
      // The store holds what it is about to save: the draft save below reads the excerpts from
      // here, and the autosave that follows the send does not see them as a change and re-save
      // the draft without them.
      record.parts = parts;
      const serial = record.serial;
      record.submitting = true;
      cancelTimer(key);
      publish(record);
      return queue(key, async () => {
        if (!(await loadNow(record))) {
          record.submitting = false;
          publish(record);
          return null;
        }
        if (record.conflictRevision !== null) {
          record.submitting = false;
          publish(record);
          return null;
        }
        if (!(await backUpNewerText(record, text, attachments))) {
          record.submitting = false;
          publish(record);
          return null;
        }
        const draft = await saveNow(record, text, attachments);
        if (!draft || draft.revision < 1) {
          record.submitting = false;
          publish(record);
          return null;
        }
        try {
          const prior = await api.getDraftCommand(key, draft.revision);
          if (prior) {
            bindPrior(record, prior);
            return null;
          }
        } catch (error) {
          record.submitting = false;
          record.status = "unavailable";
          record.error = codeOf(error);
          publish(record);
          return null;
        }
        const input: CommandInput = {
          command_id: crypto.randomUUID(),
          session_id: key,
          generation,
          draft_revision: draft.revision,
        };
        const keptReceipt = sendOptions?.keepReceipt ? record.lastReceipt : null;
        record.lastReceipt = null;
        record.acceptedUncleared = false;
        record.dispatchRefusal = null;
        record.pendingCommand = input;
        // Programmatic sends (for example wrap-up) must never clear different text the user typed.
        record.pendingSerial =
          (record.text === text || trimTrailingNewlines(record.text) === text) &&
          sameAttachments(record.attachments, attachments)
            ? serial
            : null;
        rememberPending(storage, key, input);
        publish(record);
        // Publication listeners can synchronously change workspace, membership or Stop. This
        // check follows every preparation await and publication, with no await before POST.
        if (admit && !admit()) {
          completePending(record);
          record.submitting = false;
          if (keptReceipt) record.lastReceipt = keptReceipt;
          record.status = unchanged(record) ? "saved" : "saving";
          publish(record);
          if (!unchanged(record)) schedule(record);
          return null;
        }
        return postNow(record, input, keptReceipt);
      });
    },
    recoverPending(key) {
      const record = entry(key);
      return queue(key, () => recoverNow(record));
    },
    retryPending(key) {
      const record = entry(key);
      if (!record.pendingCommand || record.submitting || record.lastReceipt)
        return Promise.resolve(null);
      const input = record.pendingCommand;
      record.submitting = true;
      publish(record);
      return queue(key, () => postNow(record, input));
    },
    releaseAccepted(key) {
      const record = entry(key);
      if (!record.lastReceipt || !record.acceptedUncleared || record.submitting) return;
      completePending(record);
      record.error = null;
      record.status = unchanged(record) ? "saved" : "saving";
      publish(record);
      if (!unchanged(record)) schedule(record);
    },
    releaseCompleted(key, expectedCommandId) {
      const record = records.get(key);
      if (
        !record?.loaded ||
        record.lastReceipt?.command_id !== expectedCommandId ||
        record.status !== "saved" ||
        record.text !== "" ||
        record.attachments.length ||
        record.parts.length ||
        !unchanged(record) ||
        record.submitting ||
        record.pendingCommand ||
        record.acceptedUncleared ||
        record.conflictRevision !== null ||
        record.conflictDraft ||
        record.backupKey ||
        record.error ||
        tails.has(key) ||
        loading.has(key) ||
        flushing.has(key) ||
        reviewing.has(key) ||
        reconciling.has(key) ||
        timers.has(key)
      )
        return false;
      records.delete(key);
      const editors = { ...snapshot.editors };
      delete editors[key];
      snapshot = {
        selectedKey: snapshot.selectedKey === key ? null : snapshot.selectedKey,
        editors,
      };
      publish();
      return true;
    },
    capacityAvailable(key) {
      return records.has(key) || records.size < MAX_EDITORS;
    },
    reviewConflict(key) {
      const record = entry(key);
      const existing = reviewing.get(key);
      if (existing) return existing;
      const result = queue(key, async () => {
        try {
          const server = await api.getDraft(key);
          record.conflictDraft = server;
          record.conflictRevision = server.revision;
          record.reviewedSerial = record.serial;
          record.status = "conflict";
          record.error = null;
          publish(record);
          return server;
        } catch (error) {
          record.conflictDraft = null;
          record.reviewedSerial = null;
          record.status = "unavailable";
          record.error = codeOf(error);
          publish(record);
          return null;
        }
      });
      reviewing.set(key, result);
      void result.finally(() => {
        if (reviewing.get(key) === result) reviewing.delete(key);
      });
      return result;
    },
    reconcile(key, choice) {
      const record = entry(key);
      const existing = reconciling.get(key);
      if (existing) return existing;
      const result = queue(key, async () => {
        const server = record.conflictDraft;
        if (!server || record.reviewedSerial !== record.serial || record.pendingCommand) return;
        record.loaded = true;
        record.revision = server.revision;
        record.savedText = server.text;
        record.savedAttachments = copyAttachments(server.attachments ?? []);
        record.savedParts = copyParts(server.parts ?? []);
        record.savedAt = server.saved_at;
        record.error = null;
        record.conflictRevision = null;
        record.conflictDraft = null;
        record.reviewedSerial = null;
        if (choice === "use-server") {
          record.text = server.text;
          record.attachments = copyAttachments(server.attachments ?? []);
          record.serial += 1;
          record.selectionStart = server.text.length;
          record.selectionEnd = server.text.length;
          record.status = "saved";
          publish(record);
        } else {
          record.status = "saving";
          publish(record);
          await saveNow(record, record.text);
        }
      });
      reconciling.set(key, result);
      void result.finally(() => {
        if (reconciling.get(key) === result) reconciling.delete(key);
      });
      return result;
    },
  };
}
