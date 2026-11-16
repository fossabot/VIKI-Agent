import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";
import {
  KnowledgeError,
  type MemoryItem,
  type MemoryKind,
  type MemoryListing,
  type MemoryPatch,
  type MemoryScope,
  type MemoryState,
  type VaultKnowledgeApi,
} from "../../state/vaultKnowledgeApi";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./MemoryPanel.css";

// Inline copy, like `NoteActions`. A code not listed gets `GENERIC`, never the code itself.
export const MEMORY_COPY: Record<string, string> = {
  stale_revision: "This memory has changed. The list has been reloaded. Review the content.",
  invalid_transition: "This action isn't available for this memory.",
  conflict: "Another memory already exists at that location. Check the list.",
  vault_protected: "This vault can't be written to. You can only propose.",
  invalid_record: "This memory isn't correctly formatted, so it can't be acted on.",
  invalid_input: "The memory's content isn't valid.",
  invalid_request: "The memory's content isn't valid.",
  unknown_workspace: "This vault can't be found.",
  workspace_changed: "This workspace has changed. Reopen it and try again.",
  vault_locked: "Another save or operation is in progress. Try again in a moment.",
  vault_recovery_required:
    "This vault needs recovery. Memory actions aren't available until recovery finishes.",
  unsupported_filesystem: "Memory actions aren't available on this vault's drive.",
};
const GENERIC = "Couldn't complete the action. Check the vault's status.";

const STATE_LABEL: Record<MemoryState | "invalid", string> = {
  active: "Active",
  pending: "Awaiting approval",
  candidate: "Candidate",
  retired: "Retired",
  invalid: "Invalid",
};
export const STATE_EXPLANATION: Record<MemoryState, string> = {
  active: "This device has approved this content.",
  pending:
    "This device hasn't approved this content (this exact byte sequence) yet. It won't take effect until approved.",
  candidate: "This is a candidate. It won't take effect until approved.",
  retired: "This has been retired. It's no longer used.",
};
const KIND_LABEL: Record<MemoryKind, string> = {
  owner: "Owner",
  assistant: "Assistant",
  inference: "Inferred",
  summary: "Summary",
};
const SCOPE_LABEL: Record<MemoryScope, string> = { vault: "Vault", personal: "Personal" };
const STATUS_LABEL = { candidate: "Candidate", approved: "Approved", retired: "Retired" };
const KINDS = Object.keys(KIND_LABEL) as MemoryKind[];
const SCOPES = Object.keys(SCOPE_LABEL) as MemoryScope[];

type Action = "approve" | "retire" | "correct" | "narrow_scope" | "finish_correction";
const ACTION_LABEL: Record<Action, string> = {
  approve: "Approve",
  retire: "Retire",
  correct: "Correct",
  narrow_scope: "Narrow scope",
  finish_correction: "Finish correction",
};

const refusalText = (failure: unknown): string =>
  failure instanceof KnowledgeError ? (MEMORY_COPY[failure.code] ?? GENERIC) : GENERIC;
const isStale = (failure: unknown): boolean =>
  failure instanceof KnowledgeError && failure.code === "stale_revision";

// An approved record that is not an inference only has its hash recorded (`src/viki_agent/vaults/memory.py` approve).
const recordsHashOnly = (item: MemoryItem): boolean =>
  item.fields.status === "approved" && item.fields.kind !== "inference";

function actionsFor(item: MemoryItem): Action[] {
  if (item.state === "retired") return [];
  const actions: Action[] = [];
  if (item.superseded_by !== null) actions.push("finish_correction");
  if (item.state !== "active") actions.push("approve");
  actions.push("retire", "correct");
  if (item.fields.scope === "personal") actions.push("narrow_scope");
  return actions;
}

function whatItWrites(action: Action, item: MemoryItem): string {
  switch (action) {
    case "approve":
      return recordsHashOnly(item)
        ? "Nothing is written to the vault. This only records that this device approved this content."
        : `Writes approved status and the review date to "${item.ref}" in the vault, and records this device's approval.${
            item.fields.kind === "inference"
              ? ' The kind changes from "Inferred" to "Assistant".'
              : ""
          }`;
    case "retire":
      return `Writes "${item.ref}" in the vault as retired. Context already sent to the provider can't be undone.`;
    case "correct":
      return `Writes a new approved memory to the vault, then writes the original "${item.ref}" as retired (two writes).`;
    case "finish_correction":
      return `Writes "${item.ref}" in the vault as retired, and records the new record that replaced it (${item.superseded_by}). The new record is already saved.`;
    case "narrow_scope":
      return `Writes "${item.ref}" in the vault, changing its scope from personal to vault.`;
  }
}

interface Revision {
  policyRevision: number;
  rootPhysicalId: string;
}

function ConfirmDialog({
  api,
  workspaceId,
  revision,
  item,
  action,
  onDone,
  onPending,
  onClose,
  onStale,
  restoreFocus,
}: {
  api: VaultKnowledgeApi;
  workspaceId: string;
  revision: Revision;
  item: MemoryItem;
  action: Action;
  onDone: () => void;
  onPending?: () => () => void;
  onClose: () => void;
  onStale: () => void;
  restoreFocus: () => HTMLElement | null;
}): ReactElement {
  const [claim, setClaim] = useState(item.claim);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: cancelRef,
    onDismiss: onClose,
    restoreFocus,
  });

  const patch = (): MemoryPatch => {
    const base = { ...revision, sha256: item.sha256 };
    if (action === "retire") return { ...base, action, retiredBy: null };
    if (action === "finish_correction") {
      return { ...base, action: "retire", retiredBy: item.superseded_by };
    }
    if (action === "correct") return { ...base, action, claim };
    return { ...base, action };
  };
  async function apply() {
    const release = onPending?.();
    setBusy(true);
    setError("");
    try {
      await api.patchMemory(workspaceId, item.fields.id, patch());
      if (!live.current) return;
      onDone();
      onClose();
    } catch (failure) {
      if (isStale(failure)) onStale();
      if (live.current) setError(refusalText(failure));
    } finally {
      release?.();
      if (live.current) setBusy(false);
    }
  }

  const label = ACTION_LABEL[action];
  return (
    <div className="memory-panel-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className="memory-panel-dialog"
      >
        <h2>{label}</h2>
        <p className="memory-panel-ref">{item.ref}</p>
        {action === "correct" ? (
          <label>
            New content{" "}
            <textarea
              value={claim}
              disabled={busy}
              rows={4}
              onChange={(event) => setClaim(event.target.value)}
            />
          </label>
        ) : null}
        <p>{whatItWrites(action, item)}</p>
        {error ? <p role="alert">{error}</p> : null}
        <div className="memory-panel-buttons">
          <button type="button" ref={cancelRef} className="btn txt sm" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn go sm" disabled={busy} onClick={apply}>
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}

function Compose({
  api,
  workspaceId,
  revision,
  isProtected,
  onDone,
  onPending,
}: {
  api: VaultKnowledgeApi;
  workspaceId: string;
  revision: Revision;
  isProtected: boolean;
  onDone: () => void;
  onPending?: () => () => void;
}): ReactElement {
  const [kind, setKind] = useState<MemoryKind>("owner");
  const [scope, setScope] = useState<MemoryScope>("vault");
  const [claim, setClaim] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // One id per confirmed create: a retry of the same request reuses it, an edit clears it.
  const commandId = useRef<string | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const edited = () => {
    setPreview(null);
    setMessage("");
    setError("");
    commandId.current = null;
  };
  const draft = () => ({ kind, scope, claim, sources: [] });
  async function compose() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api.composeMemory(workspaceId, draft());
      if (live.current) setPreview(result.text);
    } catch (failure) {
      if (live.current) setError(refusalText(failure));
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function create() {
    commandId.current ??= crypto.randomUUID();
    const release = onPending?.();
    setBusy(true);
    setError("");
    try {
      // A replayed answer is the first try's result, so it is a success too.
      const result = await api.createMemory(
        workspaceId,
        { ...revision, ...draft() },
        commandId.current,
      );
      if (!live.current) return;
      setMessage(
        result.outcome === "proposed"
          ? "Proposed. Nothing is written to the vault until you apply the proposal."
          : "Written to the vault as a candidate.",
      );
      setPreview(null);
      setClaim("");
      commandId.current = null;
      onDone();
    } catch (failure) {
      if (live.current) setError(refusalText(failure));
    } finally {
      release?.();
      if (live.current) setBusy(false);
    }
  }

  return (
    <section className="memory-panel-compose" aria-label="New memory">
      <h3>New memory</h3>
      {isProtected ? (
        <p>
          This vault can't be written to. You can only propose. Nothing is written to the vault.
        </p>
      ) : null}
      <label>
        Kind{" "}
        <select
          value={kind}
          disabled={busy}
          onChange={(event) => {
            setKind(event.target.value as MemoryKind);
            edited();
          }}
        >
          {KINDS.map((item) => (
            <option key={item} value={item}>
              {KIND_LABEL[item]}
            </option>
          ))}
        </select>
      </label>{" "}
      <label>
        Scope{" "}
        <select
          value={scope}
          disabled={busy}
          onChange={(event) => {
            setScope(event.target.value as MemoryScope);
            edited();
          }}
        >
          {SCOPES.map((item) => (
            <option key={item} value={item}>
              {SCOPE_LABEL[item]}
            </option>
          ))}
        </select>
      </label>
      <label>
        Content{" "}
        <textarea
          value={claim}
          disabled={busy}
          rows={3}
          onChange={(event) => {
            setClaim(event.target.value);
            edited();
          }}
        />
      </label>
      {preview !== null ? (
        <div>
          <p>
            {isProtected
              ? "Proposes the following memory. Nothing is written to the vault."
              : "Writes the following content to the vault as a candidate."}
          </p>
          <pre className="memory-panel-text">{preview}</pre>
        </div>
      ) : null}
      {message ? <p role="status">{message}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <div className="memory-panel-buttons">
        <button type="button" className="btn alt sm" disabled={busy || !claim} onClick={compose}>
          Review
        </button>
        {preview !== null ? (
          <button type="button" className="btn go sm" disabled={busy} onClick={create}>
            {isProtected ? "Propose" : "Create"}
          </button>
        ) : null}
      </div>
    </section>
  );
}

export interface MemoryPanelProps {
  api: VaultKnowledgeApi;
  workspaceId: string;
  revision: Revision;
  protected: boolean;
  onDone: () => void;
  onPending?: () => () => void;
}

export function MemoryPanel({
  api,
  workspaceId,
  revision,
  protected: isProtected,
  onDone,
  onPending,
}: MemoryPanelProps): ReactElement {
  const [listing, setListing] = useState<MemoryListing | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<{ item: MemoryItem; action: Action } | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const result = await api.memories(workspaceId);
      if (!live.current) return;
      setListing(result);
      setError("");
    } catch (failure) {
      if (live.current) setError(refusalText(failure));
    }
  }, [api, workspaceId]);
  useEffect(() => {
    void load();
  }, [load]);

  const done = () => {
    void load();
    onDone();
  };

  return (
    <section className="memory-panel" aria-label="Memory">
      <h2>Memory</h2>
      {error ? <p role="alert">{error}</p> : null}
      {listing?.partial ? (
        <p role="status">The list is partial. Not everything has been loaded yet.</p>
      ) : null}
      {listing && listing.items.length === 0 && listing.invalid.length === 0 ? (
        <p>There are no memories yet.</p>
      ) : null}
      <ul className="memory-panel-list">
        {listing?.items.map((item) => (
          <li key={item.fields.id} className="memory-panel-item">
            <p className="memory-panel-claim">{item.claim}</p>
            <p className="memory-panel-meta">
              {item.ref} · {KIND_LABEL[item.fields.kind]} · {SCOPE_LABEL[item.fields.scope]} ·{" "}
              {STATUS_LABEL[item.fields.status]} ·{" "}
              <strong data-state={item.state}>{STATE_LABEL[item.state]}</strong>
            </p>
            <p className="memory-panel-why">{STATE_EXPLANATION[item.state]}</p>
            {item.superseded_by !== null && item.state !== "retired" ? (
              <p className="memory-panel-why">
                This record's correction stopped partway through. The new record is already saved.
              </p>
            ) : null}
            {isProtected
              ? null
              : actionsFor(item).map((action) => (
                  <button
                    key={action}
                    type="button"
                    className="btn txt sm"
                    onClick={(event) => {
                      trigger.current = event.currentTarget;
                      setPending({ item, action });
                    }}
                  >
                    {ACTION_LABEL[action]}
                  </button>
                ))}
          </li>
        ))}
        {listing?.invalid.map((item) => (
          <li key={item.ref} className="memory-panel-item memory-panel-invalid">
            <p className="memory-panel-meta">
              {item.ref} · <strong data-state="invalid">{STATE_LABEL.invalid}</strong>
            </p>
            <p>Reason: {item.reason}</p>
            {item.text === null ? (
              <p>The content can't be shown because reading it was refused.</p>
            ) : (
              <pre className="memory-panel-text">{item.text}</pre>
            )}
          </li>
        ))}
      </ul>
      <Compose
        api={api}
        workspaceId={workspaceId}
        revision={revision}
        isProtected={isProtected}
        onDone={done}
        {...(onPending ? { onPending } : {})}
      />
      {pending ? (
        <ConfirmDialog
          api={api}
          workspaceId={workspaceId}
          revision={revision}
          item={pending.item}
          action={pending.action}
          onDone={done}
          {...(onPending ? { onPending } : {})}
          onClose={() => setPending(null)}
          onStale={() => void load()}
          restoreFocus={() => trigger.current}
        />
      ) : null}
    </section>
  );
}
