/**
 * The browser half of the durable approval ledger.
 *
 * Every user choice on a permission, plan or question card is recorded through the
 * authenticated REST route before the server resolves the driver's pending request, so one
 * request can only ever carry one recorded choice — whatever this page does, and whatever any
 * other window does at the same time. This store holds, per native request, what this page
 * knows about that record: nothing yet (`checking`), no record (`idle`), one it created or
 * adopted (`recorded`), or a POST whose result it never saw (`ambiguous`), whose choice ID is
 * retained and inspected by GET — never re-posted under a new ID, and never on a timer.
 *
 * Only identifiers are ever written to `sessionStorage`; the choice itself lives in the ledger.
 */
import {
  type ApprovalChoice,
  type ApprovalInput,
  type ApprovalReceipt,
  type DurableApi,
  DurableApiError,
} from "../clients/conversations/durableApi";
import type { NativeRequest } from "../protocol";
import type { Card } from "./view";

export type ApprovalPhase = "checking" | "idle" | "posting" | "recorded" | "ambiguous" | "refused";

/** Why a definite refusal happened: the request was already over, nothing could be saved, or the
 * scope's own choice quota (no eviction) is full — a retry can never succeed for that
 * last one, unlike `save`, so the card must say so and stop offering a retry. */
export type ApprovalRefusal = "over" | "save" | "capacity";

/** Exactly Cards.tsx's own `AnswerOutcome`. Written out here rather than imported so the store
 * stays independent of the component tree — the same boundary rule Cards.tsx follows when it
 * copies Composer's `isComposingEvent`. */
export type AnswerOutcome = "applied" | "rejected" | "unknown";

export interface ApprovalView {
  phase: ApprovalPhase;
  /** The ledger's metadata for the recorded choice: its state and reason are what the card says. */
  receipt: ApprovalReceipt | null;
  /** Read back from the ledger, never from browser storage. */
  choice: ApprovalChoice | null;
  refusal: ApprovalRefusal | null;
}

export interface ApprovalSnapshot {
  /** Keyed by the native request ID the card is bound to. */
  approvals: Readonly<Record<string, ApprovalView>>;
}

export interface ApprovalStore {
  getSnapshot(): ApprovalSnapshot;
  subscribe(listener: () => void): () => void;
  /** Look this card's request up once, so a reload or a second window shows the recorded choice. */
  observe(card: Card, sessionId: string, generation: string): void;
  decide(
    card: Card,
    sessionId: string,
    generation: string,
    choice: ApprovalChoice,
  ): Promise<AnswerOutcome>;
  /** The card left the view: drop what this page remembers about it. */
  forget(requestId: string): void;
  /** A fresh subscription for this session: inspect every choice ID still in doubt. */
  resynced(sessionId: string): void;
}

export interface ApprovalStoreOptions {
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
}

/** What `sessionStorage` may hold: identifiers, never the user's answer. */
interface Pending {
  choice_id: string;
  session_id: string;
  generation: string;
  tool_use_id: string;
  native_request: NativeRequest;
}

interface Entry extends ApprovalView {
  requestId: string;
  sessionId: string;
  binding: NativeRequest;
  pending: Pending | null;
  /** A lookup or inspection in flight. A click waits for it rather than racing it. */
  busy: Promise<void> | null;
}

const PENDING_PREFIX = "viki.approval.pending.";

/** A response that reached us with one of these codes proves nothing was recorded under this ID.
 * `stale_request`, `stale_generation`, `closed` and the two 404s mean the request itself is over. */
const REQUEST_OVER = new Set([
  "stale_request",
  "stale_generation",
  "closed",
  "not_found",
  "no_target",
]);

/** The result is unknown: either no answer reached us, or one arrived that we cannot read. The
 * service answers with a receipt once a record commits, so every recognised refusal code below
 * is a definite "not recorded" instead — classified by code, never by status class. */
const UNCERTAIN = new Set([
  "network_error",
  "timeout",
  "intake_timeout",
  "http_error",
  "invalid_response",
  "internal",
]);

/** The phase an awaited step left behind. Read through a call because a direct read inside the
 * branch that tested it would still carry that branch's narrowing. */
function phaseOf(entry: Entry): ApprovalPhase {
  return entry.phase;
}

function savedPending(storage: ApprovalStoreOptions["storage"], requestId: string): Pending | null {
  try {
    const raw = storage?.getItem(`${PENDING_PREFIX}${requestId}`);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object") return null;
    const saved = value as Partial<Pending>;
    const binding = saved.native_request;
    if (
      typeof saved.choice_id !== "string" ||
      typeof saved.session_id !== "string" ||
      typeof saved.generation !== "string" ||
      typeof saved.tool_use_id !== "string" ||
      !binding ||
      typeof binding.runtime_id !== "string" ||
      typeof binding.turn_id !== "string" ||
      binding.request_id !== requestId ||
      "choice" in saved
    )
      return null;
    return saved as Pending;
  } catch {
    return null;
  }
}

/** No choice body ever enters browser storage: only the identifiers needed to ask the ledger
 * what became of one uncertain POST. */
function rememberPending(
  storage: ApprovalStoreOptions["storage"],
  requestId: string,
  pending: Pending | null,
): void {
  try {
    if (pending) storage?.setItem(`${PENDING_PREFIX}${requestId}`, JSON.stringify(pending));
    else storage?.removeItem(`${PENDING_PREFIX}${requestId}`);
  } catch {
    // Storage refused. The ID stays in memory for this page, which is where recovery starts.
  }
}

export function createApprovalStore(
  api: DurableApi,
  options: ApprovalStoreOptions = {},
): ApprovalStore {
  let defaultStorage: ApprovalStoreOptions["storage"] = null;
  try {
    if (typeof window !== "undefined") defaultStorage = window.sessionStorage;
  } catch {
    // A refused storage must not stop a card from being answered.
  }
  const storage = options.storage === undefined ? defaultStorage : options.storage;
  const entries = new Map<string, Entry>();
  const listeners = new Set<() => void>();
  let snapshot: ApprovalSnapshot = { approvals: {} };

  function publish(entry?: Entry): void {
    if (entry) {
      // A request answered elsewhere closes its card (and `forget`s it) while this page's own
      // POST is still in flight. Publishing the finished attempt then would put the entry back
      // where nothing would remove it again, and a native request ID the CLI reuses would open
      // its next card holding the previous choice.
      if (entries.get(entry.requestId) !== entry) return;
      const view: ApprovalView = {
        phase: entry.phase,
        receipt: entry.receipt,
        choice: entry.choice,
        refusal: entry.refusal,
      };
      snapshot = { approvals: { ...snapshot.approvals, [entry.requestId]: view } };
    } else {
      snapshot = {
        approvals: Object.fromEntries(
          [...entries.values()].map((each) => [
            each.requestId,
            {
              phase: each.phase,
              receipt: each.receipt,
              choice: each.choice,
              refusal: each.refusal,
            },
          ]),
        ),
      };
    }
    for (const listener of [...listeners]) listener();
  }

  function track(entry: Entry, task: () => Promise<void>): Promise<void> {
    const run = task().finally(() => {
      if (entry.busy === run) entry.busy = null;
    });
    entry.busy = run;
    return run;
  }

  function release(entry: Entry): void {
    entry.pending = null;
    rememberPending(storage, entry.requestId, null);
  }

  /** The record is this page's now, whether it created it or found it. Its choice is read back
   * from the ledger rather than assumed from the click, so two windows say the same thing. */
  async function adopt(entry: Entry, receipt: ApprovalReceipt): Promise<void> {
    release(entry);
    entry.phase = "recorded";
    entry.receipt = receipt;
    entry.refusal = null;
    publish(entry);
    try {
      const record = await api.getApprovalChoice(receipt.choice_id);
      if (entries.get(entry.requestId) !== entry || entry.phase !== "recorded") return;
      // Only the choice is taken from this read: the decrypted body stays out of the metadata
      // the view carries, and the delivery state keeps the receipt it was adopted with.
      entry.choice = record.choice;
      publish(entry);
    } catch {
      // The delivery state still renders; the choice line waits for a later read.
    }
  }

  function refuse(entry: Entry, refusal: ApprovalRefusal): void {
    release(entry);
    entry.phase = "refused";
    entry.refusal = refusal;
    publish(entry);
  }

  /** Recovery for a retained ID: one GET, never a second POST. A confirmed 404 releases the ID
   * and the card becomes answerable again; anything else leaves it in doubt. */
  function inspect(entry: Entry): Promise<void> {
    if (entry.busy) return entry.busy;
    const pending = entry.pending;
    if (!pending) return Promise.resolve();
    return track(entry, async () => {
      try {
        const receipt = await api.getApproval(pending.choice_id);
        if (
          receipt.session_id !== pending.session_id ||
          receipt.tool_use_id !== pending.tool_use_id ||
          receipt.runtime_id !== pending.native_request.runtime_id ||
          receipt.request_id !== pending.native_request.request_id
        )
          return; // never adopt a record that is not this card's; stay in doubt instead
        await adopt(entry, receipt);
      } catch (error) {
        if (
          error instanceof DurableApiError &&
          error.status === 404 &&
          error.code === "not_found"
        ) {
          release(entry);
          entry.phase = "idle";
          publish(entry);
        }
      }
    });
  }

  function ensure(card: Card, sessionId: string): Entry | null {
    const binding = card.nativeRequest;
    if (!binding) return null;
    const existing = entries.get(binding.request_id);
    if (existing) return existing;
    const pending = savedPending(storage, binding.request_id);
    const entry: Entry = {
      requestId: binding.request_id,
      sessionId,
      binding,
      phase: pending ? "ambiguous" : "checking",
      receipt: null,
      choice: null,
      refusal: null,
      pending,
      busy: null,
    };
    entries.set(entry.requestId, entry);
    publish(entry);
    if (pending) void inspect(entry);
    else
      void track(entry, async () => {
        try {
          const found = await api.findApproval(sessionId, binding.runtime_id, binding.request_id);
          if (entry.phase !== "checking") return; // a click or a forget overtook the lookup
          if (found) await adopt(entry, found);
          else {
            entry.phase = "idle";
            publish(entry);
          }
        } catch {
          // A failed lookup is not evidence of anything: leave the card answerable. The POST
          // itself reports `already_decided` if a record does exist.
          if (entry.phase === "checking") {
            entry.phase = "idle";
            publish(entry);
          }
        }
      });
    return entry;
  }

  async function settle(entry: Entry, error: unknown): Promise<AnswerOutcome> {
    const code = error instanceof DurableApiError ? error.code : "network_error";
    if (!(error instanceof DurableApiError) || UNCERTAIN.has(code)) {
      entry.phase = "ambiguous";
      publish(entry);
      return "unknown";
    }
    // `already_decided`: another window recorded this request first. `conflict` cannot happen
    // with a freshly allocated ID, and if it ever does a record exists under it — both are
    // answered by reading the one record this request has.
    if (code === "already_decided" || code === "conflict") {
      release(entry);
      try {
        const found = await api.findApproval(
          entry.sessionId,
          entry.binding.runtime_id,
          entry.binding.request_id,
        );
        if (found) {
          await adopt(entry, found);
          return "applied";
        }
      } catch {
        // fall through to the honest refusal below
      }
      refuse(entry, "over");
      return "rejected";
    }
    refuse(entry, REQUEST_OVER.has(code) ? "over" : code === "capacity" ? "capacity" : "save");
    return "rejected";
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    observe(card, sessionId) {
      // The generation is the click's business (it is bound into the record), not the lookup's.
      ensure(card, sessionId);
    },
    async decide(card, sessionId, generation, choice) {
      const entry = ensure(card, sessionId);
      if (!entry) return "rejected"; // no binding: there is no request this could answer
      if (entry.busy) await entry.busy;
      if (entry.phase === "recorded") return "applied";
      if (entry.phase === "posting") return "unknown";
      if (entry.phase === "ambiguous") {
        // This click inspects; the next one decides. Recovery is never a second POST.
        await inspect(entry);
        const inspected = phaseOf(entry);
        if (inspected === "recorded") return "applied";
        // A confirmed 404 released the ID: the card is answerable again, with a fresh one.
        return inspected === "idle" ? "rejected" : "unknown";
      }
      const input: ApprovalInput = {
        choice_id: crypto.randomUUID(),
        session_id: sessionId,
        generation,
        tool_use_id: card.data.tool_use_id,
        native_request: entry.binding,
        choice,
      };
      const { choice: _choice, ...pending } = input;
      entry.pending = pending;
      rememberPending(storage, entry.requestId, pending);
      entry.phase = "posting";
      entry.refusal = null;
      publish(entry);
      // Only the POST's own failure is classified: anything after it already has a receipt.
      let receipt: ApprovalReceipt;
      try {
        receipt = await api.recordApproval(input);
      } catch (error) {
        return settle(entry, error);
      }
      await adopt(entry, receipt);
      return "applied";
    },
    forget(requestId) {
      if (!entries.delete(requestId)) return;
      rememberPending(storage, requestId, null);
      publish();
    },
    resynced(sessionId) {
      for (const entry of entries.values())
        if (entry.sessionId === sessionId && entry.phase === "ambiguous") void inspect(entry);
    },
  };
}
