import { type ReactElement, useEffect, useRef, useState } from "react";
import { platformKind } from "../../platform";
import {
  type OperationKind,
  type OperationPlan,
  type OperationRequest,
  type VaultOpsApi,
  VaultOpsError,
} from "../../state/vaultOpsApi";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./NoteActions.css";

// This surface keeps its strings here, like VaultBrowser's `CODE_COPY`, not in `i18n/`.
// Every typed refusal gets one sentence; a code not listed gets `GENERIC`, never the code itself.
export const OPERATION_COPY: Record<string, string> = {
  conflict:
    "This note changed, or the destination already has an item with that name. Reopen it and check again.",
  command_conflict: "This operation already ran with different details. Close it and start over.",
  vault_locked: "Another save or operation is in progress. Try again in a moment.",
  vault_recovery_required:
    "This vault needs recovery. Note actions aren't available until recovery finishes.",
  workspace_changed: "This workspace has changed. Reopen it and try again.",
  ordinary_workspace_required: "This vault doesn't support note actions.",
  target_busy: "Another app has this note open. Close it and try again.",
  overloaded: "The vault is busy. Wait a moment and try again.",
  recovery_full: "Recovery storage is full. This note can't be deleted.",
  recovery_unavailable: "Recovery storage isn't available. This note can't be deleted.",
  unsupported_filesystem: "This vault's drive doesn't support note actions.",
  unsafe_target: "The app can't safely act on this note or its destination.",
  read_only_target: "This note is read-only. Clear Read-only in its Properties, then try again.",
  folder_create_failed: "Couldn't create the destination folder.",
  invalid_operation: "This name can't be used. Enter a different name.",
  invalid_ref: "This note reference is invalid.",
  excluded_path: "The app can't act on this file.",
  missing_target:
    "This note can't be found. It may have been moved or deleted elsewhere. The list has been refreshed.",
};
const GENERIC = "The operation failed. Check the vault's status.";
export const STOPPED =
  "The operation stopped partway through. The note isn't lost. It's shown under \"Unfinished operations\".";

// An outcome the client cannot know: the retry keeps its command id to get the real answer.
const UNKNOWN_OUTCOME = ["unavailable", "op_in_progress"];

type NoteKind = Exclude<OperationKind, "restore">;
const KIND_LABEL: Record<NoteKind, string> = {
  rename: "Rename",
  move: "Move",
  archive: "Archive",
  delete: "Delete",
};
const KINDS = Object.keys(KIND_LABEL) as NoteKind[];
const ACTION_LABEL = { create: "Create", move: "Move", remove: "Delete", mkdir: "Create folder" };

// POSIX has no Properties dialog with a Read-only checkbox, so off Windows this names the
// remedy without claiming a Windows-only mechanism; chosen over OPERATION_COPY.read_only_target
// when platformKind() is not "windows".
const POSIX_READ_ONLY_TARGET =
  "This note is read-only or locked. Make it writable in your file manager, then try again.";
const refusalText = (code: string): string =>
  code === "read_only_target" && platformKind() !== "windows"
    ? POSIX_READ_ONLY_TARGET
    : (OPERATION_COPY[code] ?? GENERIC);
const slash = (ref: string): number => ref.lastIndexOf("/");
const initialTarget = (kind: NoteKind, ref: string): string =>
  kind === "rename" ? ref.slice(slash(ref) + 1) : kind === "archive" ? `archive/${ref}` : ref;

export interface NoteActionsProps {
  workspaceId: string;
  noteRef: string;
  /** The note's sha256 as last loaded; the server refuses the operation if the note changed. */
  noteSha256: string;
  revision: { policyRevision: number; rootPhysicalId: string };
  api?: VaultOpsApi | undefined;
  /** Shown first in the menu, above Rename/Move/Archive/Delete; the menu closes before onSelect runs. */
  extra?: readonly { label: string; onSelect: () => void }[];
  /** The operation finished: the note no longer exists at this ref, so the caller closes it. */
  onDone: () => void;
  /** The server answered but the operation did not finish: lists may have changed, the note stays. */
  onRefresh?: (() => void) | undefined;
  /** Holds navigation until the actual request settles, including after dialog dismissal. */
  onPending?: () => () => void;
}

function OperationDialog({
  workspaceId,
  noteRef,
  noteSha256,
  revision,
  api,
  kind,
  onDone,
  onRefresh,
  onPending,
  onClose,
  restoreFocus,
}: Omit<NoteActionsProps, "api"> & {
  api: VaultOpsApi;
  kind: NoteKind;
  onClose: () => void;
  restoreFocus: () => HTMLElement | null;
}): ReactElement {
  const [target, setTarget] = useState(() => initialTarget(kind, noteRef));
  const [plan, setPlan] = useState<OperationPlan | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  // One id per request: a retry of the same request reuses it, any change to the request (the
  // target or the note's hash) mints a new one, so the server never sees one id with two requests.
  // It is also dropped after a definitive answer, since the server replays a remembered refusal
  // for the same id; only an unknown outcome keeps it, so the retry gets the server's real answer.
  const command = useRef<{ key: string; id: string } | null>(null);
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

  const request = (): OperationRequest => {
    const to =
      kind === "delete"
        ? undefined
        : kind === "rename"
          ? noteRef.slice(0, slash(noteRef) + 1) + target
          : target;
    return { ...revision, kind, ref: noteRef, to, expectedSha256: noteSha256 };
  };
  const fail = (failure: unknown) => {
    if (live.current)
      setError(failure instanceof VaultOpsError ? refusalText(failure.code) : GENERIC);
  };

  async function preview() {
    const release = onPending?.();
    setBusy(true);
    setError("");
    try {
      const result = await api.preview(workspaceId, request());
      if (!live.current) return;
      setPlan(result);
      if (result.refusal !== undefined) setError(refusalText(result.refusal));
    } catch (failure) {
      fail(failure);
    } finally {
      release?.();
      if (live.current) setBusy(false);
    }
  }

  async function apply() {
    const body = request();
    const key = JSON.stringify(body);
    if (command.current?.key !== key) command.current = { key, id: crypto.randomUUID() };
    const release = onPending?.();
    setBusy(true);
    setError("");
    try {
      const outcome = await api.apply(workspaceId, body, command.current.id);
      command.current = null;
      if (!live.current) return;
      if (outcome.status === "done") {
        onDone();
        onClose();
        return;
      }
      onRefresh?.();
      if (outcome.status === "conflict") setError(refusalText("conflict"));
      else if (outcome.status === "recovery_required") setError(STOPPED);
      else setError(refusalText(outcome.reason ?? ""));
    } catch (failure) {
      if (failure instanceof VaultOpsError && !UNKNOWN_OUTCOME.includes(failure.code))
        command.current = null;
      if (failure instanceof VaultOpsError && failure.code === "missing_target") onRefresh?.();
      fail(failure);
    } finally {
      release?.();
      if (live.current) setBusy(false);
    }
  }

  const label = KIND_LABEL[kind];
  const ready = plan !== null && plan.refusal === undefined;
  return (
    <div className="note-actions-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className="note-actions-dialog"
      >
        <h2>{label}</h2>
        <p className="note-actions-ref">{noteRef}</p>
        {kind === "delete" ? null : (
          <label>
            {kind === "rename" ? "New name" : "Destination"}{" "}
            <input
              type="text"
              value={target}
              disabled={busy}
              onChange={(event) => {
                setTarget(event.target.value);
                setPlan(null);
                setError("");
              }}
            />
          </label>
        )}
        {plan ? (
          <div className="note-actions-plan">
            <ol>
              {plan.steps.map((step) => (
                <li key={step.n}>
                  {ACTION_LABEL[step.action]}: {step.ref}
                  {step.to === null ? "" : ` → ${step.to}`}
                </li>
              ))}
            </ol>
            {plan.creates_folders.length > 0 ? (
              <p>Folders to create: {plan.creates_folders.join(", ")}</p>
            ) : null}
            {kind === "delete" && ready ? (
              <p>Can be restored for {plan.recovery_days} days.</p>
            ) : null}
          </div>
        ) : null}
        {error ? <p role="alert">{error}</p> : null}
        <div className="note-actions-buttons">
          <button type="button" ref={cancelRef} className="btn txt sm" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn alt sm" disabled={busy} onClick={preview}>
            Review
          </button>
          <button type="button" className="btn go sm" disabled={busy || !ready} onClick={apply}>
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}

export function NoteActions(props: NoteActionsProps): ReactElement | null {
  const { api, extra, ...rest } = props;
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<NoteKind | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  if (!api && !extra?.length) return null;
  return (
    <span className="note-actions">
      <button
        type="button"
        ref={trigger}
        className="btn txt sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        Note actions
      </button>
      {open ? (
        <div
          className="note-actions-menu menu-panel"
          role="menu"
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            setOpen(false);
            trigger.current?.focus();
          }}
        >
          {extra?.map((item) => (
            <button
              key={item.label}
              type="button"
              className="mi"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
          {api
            ? KINDS.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="mi"
                  role="menuitem"
                  onClick={() => {
                    setOpen(false);
                    setKind(item);
                  }}
                >
                  {KIND_LABEL[item]}
                </button>
              ))
            : null}
        </div>
      ) : null}
      {kind && api ? (
        <OperationDialog
          {...rest}
          api={api}
          kind={kind}
          restoreFocus={() => trigger.current}
          onClose={() => setKind(null)}
        />
      ) : null}
    </span>
  );
}
