import { useEffect, useRef, useState } from "react";
import { t } from "../../i18n/t";
import { type VaultRecoveryStatus, validWriteRef } from "../../state/vaultApi";
import type {
  OperationKind,
  OperationOutcome,
  RecoveryEntry,
  ResolveChoice,
  StuckOperation,
  VaultOpsApi,
} from "../../state/vaultOpsApi";
import { VaultOpsError } from "../../state/vaultOpsApi";
import { formatBytes } from "../Data/formatBytes";
import { Loading } from "../Loading/Loading";
import { STOPPED } from "./NoteActions";
import "./RecoveryPanel.css";

// The two reasons a save row can be blocked (VaultRecoveryStatus.reason), in the vault page's voice.
const REASON: Record<VaultRecoveryStatus["reason"], string> = {
  conflict: "This save is stopped because the note changed before it was saved.",
  recovery_required: "Recovery is needed because the previous save didn't finish.",
};

// Both choices act on a half-finished save of one note, never on the restore of a deleted note.
const CHOICE_LABEL: Record<ResolveChoice, string> = {
  forget: "Discard record",
  drop_previous: "Delete previous version",
};
const CHOICE_CONFIRM: Record<ResolveChoice, string> = {
  forget: "This discards only the record of this save. The note's file doesn't change.",
  drop_previous:
    "This deletes the previous version (the backup) of this save. This can't be undone.",
};
const KIND_LABEL: Record<OperationKind, string> = {
  rename: "Rename",
  move: "Move",
  archive: "Archive",
  delete: "Delete",
  restore: "Restore",
};
const FORGET_STUCK_CONFIRM =
  "This discards only the app's record. The vault's files don't change, and the copy for recovery stays until it expires.";
// What a resolve or forget may leave in the vault, per the server's flags.
const STAGE_NOTE = "A hidden temporary file may remain in the vault.";
const BACKUP_NOTE = "A backup of the previous version may remain in the vault.";
const withNotes = (head: string, ...notes: string[]) => [head, ...notes].join(" ");
// The record is already gone, so the list is refreshed instead of retried.
const GONE = ["unknown_publication", "unknown_operation", "recovery_missing"];
// An outcome the client cannot know: the retry keeps its command id to get the real answer.
const UNKNOWN_OUTCOME = ["unavailable", "op_in_progress"];
const GENERIC = "Couldn't complete the operation. Try again in a moment.";
// A refusal shows its typed sentence; a code not listed here shows GENERIC, never the code.
const REFUSALS: Record<string, string> = {
  recovery_full: "The storage for deleted notes is full. Wait for one to expire.",
  target_busy: "Another app has this note open. Close it, then try again.",
  overloaded: "The vault is busy. Try again in a moment.",
  conflict: "Can't restore because a note already exists at that location.",
  stale_revision: "The vault's settings changed. Open it again.",
  workspace_changed: "The workspace changed. Open it again.",
  not_resolvable: "This record can't be handled this way right now.",
  recovery_missing:
    "The copy of the deleted note has expired or has already been used. The list has been refreshed.",
  invalid_ref: "The note's reference isn't valid.",
  unknown_publication: "This record has already been handled. The list has been refreshed.",
  unknown_operation: "This record has already been handled. The list has been refreshed.",
  unsupported_filesystem: "This vault's drive can't save here.",
};
// A 200 answer that is not "done": the restore did not happen, and the sentence says why.
const notDone = ({ status, reason }: OperationOutcome): string =>
  status === "conflict"
    ? (REFUSALS.conflict ?? GENERIC)
    : status === "recovery_required"
      ? STOPPED
      : (REFUSALS[reason ?? ""] ?? GENERIC);
const refusal = (error: unknown): string =>
  error instanceof VaultOpsError ? (REFUSALS[error.code] ?? GENERIC) : GENERIC;

export function RecoveryPanel({
  api,
  workspaceId,
  policyRevision,
  rootPhysicalId,
  publications,
  refreshKey,
  onDone,
  onPending,
  onAttention,
  forgetOnly = false,
}: {
  api: VaultOpsApi;
  workspaceId: string;
  /** A restore names the vault revision; a forget-only panel has no folder to restore into. */
  policyRevision?: number;
  rootPhysicalId?: string;
  publications: readonly VaultRecoveryStatus[];
  /** Changes whenever an operation elsewhere may have changed the lists. */
  refreshKey: number;
  onDone: () => void;
  /** Holds only this admitted request, including its existing recovery reload. */
  onPending?: () => () => void;
  /** The count of rows needing attention (pending saves plus stuck operations), on every change. */
  onAttention?: (count: number) => void;
  /**
   * For a vault whose folder is not available: only the app's records can be cleared, so there is
   * no restore and no Delete previous version, both of which read or write the folder.
   */
  forgetOnly?: boolean;
}) {
  const [entries, setEntries] = useState<RecoveryEntry[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<{ id: string; choice: ResolveChoice } | null>(null);
  const [busy, setBusy] = useState(false);
  const [stuck, setStuck] = useState<StuckOperation[]>([]);
  const [confirmingStuck, setConfirmingStuck] = useState<string | null>(null);
  const [handled, setHandled] = useState<readonly string[]>([]);
  // A restore's command id per entry, kept only while its outcome is unknown, so a retry replays
  // the server's real answer; a definitive answer drops it, since the server remembers it per id.
  const commands = useRef(new Map<string, string>());

  useEffect(() => {
    void refreshKey;
    const controller = new AbortController();
    setEntries(null);
    api
      .recovery(workspaceId, controller.signal)
      .then((list) => {
        setEntries(list.entries);
        setStuck(list.stuck);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setEntries([]);
        setMessage(refusal(error));
      });
    return () => controller.abort();
  }, [api, workspaceId, refreshKey]);

  async function reload() {
    const list = await api.recovery(workspaceId);
    setEntries(list.entries);
    setStuck(list.stuck);
  }

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setMessage(null);
    const release = onPending?.();
    try {
      await work();
      onDone();
    } catch (error) {
      setMessage(refusal(error));
      if (error instanceof VaultOpsError && GONE.includes(error.code))
        await reload().catch(() => {});
    } finally {
      setBusy(false);
      release?.();
    }
  }

  // The deleted note goes back to the ref it was deleted from; the server refuses a taken one.
  const restore = (entry: RecoveryEntry) =>
    run(async () => {
      if (policyRevision === undefined || rootPhysicalId === undefined) return;
      const held = commands.current.get(entry.recovery_id) ?? crypto.randomUUID();
      commands.current.set(entry.recovery_id, held);
      let outcome: OperationOutcome;
      try {
        outcome = await api.apply(
          workspaceId,
          {
            policyRevision,
            rootPhysicalId,
            kind: "restore",
            recoveryId: entry.recovery_id,
            to: entry.ref,
          },
          held,
        );
      } catch (error) {
        if (error instanceof VaultOpsError && !UNKNOWN_OUTCOME.includes(error.code))
          commands.current.delete(entry.recovery_id);
        throw error;
      }
      commands.current.delete(entry.recovery_id);
      if (outcome.status === "done") {
        setEntries((rows) => rows?.filter((row) => row.recovery_id !== entry.recovery_id) ?? null);
        setMessage("Restored the deleted note to its original location.");
      } else setMessage(notDone(outcome));
    });

  const resolve = (id: string, choice: ResolveChoice) =>
    run(async () => {
      try {
        const done = await api.resolve(id, choice);
        setConfirming(null);
        setHandled((ids) => [...ids, id]);
        setMessage(
          withNotes(
            `Done: ${CHOICE_LABEL[choice]}.`,
            ...(done.stage_retained ? [STAGE_NOTE] : []),
            ...(done.backup_retained ? [BACKUP_NOTE] : []),
          ),
        );
      } catch (error) {
        if (error instanceof VaultOpsError && GONE.includes(error.code)) {
          setConfirming(null);
          setHandled((ids) => [...ids, id]);
        }
        throw error;
      }
    });

  const forgetStuck = (id: string) =>
    run(async () => {
      try {
        const done = await api.forgetOperation(id);
        setMessage(
          withNotes("Done: Discard record.", ...(done.stage_retained ? [STAGE_NOTE] : [])),
        );
      } finally {
        setConfirmingStuck(null);
      }
      await reload();
    });

  const deleted = entries?.filter((entry) => entry.kind === "deleted") ?? [];
  const pending = publications.filter((row) => !handled.includes(row.operation_id));
  useEffect(() => {
    onAttention?.(pending.length + stuck.length);
  }, [pending.length, stuck.length, onAttention]);
  // Dropping the previous version reads the vault's folder, so a forget-only panel offers Discard record alone.
  const choicesOf = (row: VaultRecoveryStatus) =>
    forgetOnly ? row.resolvable.filter((choice) => choice === "forget") : row.resolvable;

  return (
    <section className="recovery-panel" aria-label="Recovery">
      {forgetOnly ? (
        <p className="recovery-meta">
          The vault's folder can't be opened, so this can only clear the app's records. The vault's
          files don't change.
        </p>
      ) : (
        <>
          <h3>Deleted notes</h3>
          {entries === null ? <Loading announce={false}>Loading…</Loading> : null}
          {entries !== null && deleted.length === 0 ? (
            <p>There are no deleted notes to restore.</p>
          ) : null}
          <ul>
            {deleted.map((entry) => (
              <li key={entry.recovery_id}>
                <span className="recovery-ref">{entry.ref}</span>
                <span className="recovery-meta">
                  {formatBytes(entry.byte_size)} · Expires {entry.expires_at.slice(0, 10)}
                </span>
                {validWriteRef(entry.ref) ? (
                  <button type="button" disabled={busy} onClick={() => void restore(entry)}>
                    Restore
                  </button>
                ) : (
                  <span className="recovery-meta">{t("vault.recovery.bot_memory_read_only")}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="recovery-meta">
            Restoring here brings one deleted note back from the app's storage location. It's
            different from restoring from a backup into another folder.
          </p>
        </>
      )}
      <h3>Saves stopped partway through</h3>
      {pending.length === 0 ? <p>There are no unfinished save records.</p> : null}
      <ul>
        {pending.map((row) => (
          <li key={row.operation_id}>
            <span className="recovery-ref">{row.ref}</span>
            <span className="recovery-meta">{REASON[row.reason]}</span>
            {choicesOf(row).map((choice) =>
              confirming?.id === row.operation_id && confirming.choice === choice ? (
                <span key={choice} className="recovery-confirm">
                  <span>{CHOICE_CONFIRM[choice]}</span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void resolve(row.operation_id, choice)}
                  >
                    Apply
                  </button>
                  <button type="button" disabled={busy} onClick={() => setConfirming(null)}>
                    Cancel
                  </button>
                </span>
              ) : (
                <button
                  key={choice}
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirming({ id: row.operation_id, choice })}
                >
                  {CHOICE_LABEL[choice]}
                </button>
              ),
            )}
          </li>
        ))}
      </ul>
      <h3>Unfinished operations</h3>
      {stuck.length === 0 ? <p>There are no unfinished operations.</p> : null}
      <ul>
        {stuck.map((op) => (
          <li key={op.operation_id}>
            <span>{KIND_LABEL[op.kind]}</span>
            <span className="recovery-ref">{op.paths.join(" → ")}</span>
            {confirmingStuck === op.operation_id ? (
              <span className="recovery-confirm">
                <span>{FORGET_STUCK_CONFIRM}</span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void forgetStuck(op.operation_id)}
                >
                  Apply
                </button>
                <button type="button" disabled={busy} onClick={() => setConfirmingStuck(null)}>
                  Cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirmingStuck(op.operation_id)}
              >
                Discard record
              </button>
            )}
          </li>
        ))}
      </ul>
      {message ? <p role="status">{message}</p> : null}
    </section>
  );
}
