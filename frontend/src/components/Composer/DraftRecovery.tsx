/** Explicit reads of saved drafts and submitted turn copies. No action here sends a turn. */
import { type ReactElement, useRef, useState } from "react";
import type {
  CommandPage,
  CommandReceipt,
  DraftPage,
  DurableApi,
} from "../../clients/conversations/durableApi";
import type { Attachment } from "../../turnInput";
import { Loading } from "../Loading/Loading";
import "./DraftRecovery.css";

interface Props {
  api: DurableApi;
  /** The user chose a copy; App opens it in a new editor and keeps the current one. */
  onRestore(text: string, attachments?: readonly Attachment[]): void;
  /** Every command record this panel reads, listed or refreshed. The submission POST answers
   * before the dispatch runs, so these reads are where a dispatch outcome — an excerpt refusal
   * among them — first reaches the app. */
  onCommandState?(receipt: CommandReceipt): void;
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-US", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}

/** The service's five excerpt refusals (`commands.REASONS`): the quoted lines no longer match the
 * note, name another vault, could not be read, grew past the turn's room, or rode a command. */
const EXCERPT_REASONS: ReadonlySet<string> = new Set([
  "excerpt_with_command",
  "excerpt_wrong_vault",
  "excerpt_stale",
  "excerpt_unavailable",
  "excerpts_too_large",
]);

/** A refusal the user answers on the chips themselves: the excerpts never left the app, so the
 * composer takes them back for a Refresh or a Remove rather than dropping the quotation. */
export function excerptRefusal(receipt: CommandReceipt): boolean {
  return receipt.state === "not_dispatched" && EXCERPT_REASONS.has(receipt.reason);
}

export function commandState(receipt: CommandReceipt): string {
  switch (receipt.state) {
    case "accepted":
      return "Accepted. The text is saved.";
    case "dispatching":
      return "Dispatch started. Arrival at Claude Code is unconfirmed.";
    case "not_dispatched":
      // Acceptance already cleared the draft: for an attachment or excerpt refusal, this
      // submission's own copy (Send history → Restore text) is the only place the turn's text
      // remains.
      if (excerptRefusal(receipt))
        return "An excerpt could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text.";
      return receipt.reason === "unsupported_attachment" ||
        receipt.reason === "attachment_unavailable"
        ? "An attached image could not be used, so this was not sent to Claude Code. Restore the text from Drafts and send history → Send history → Restore text."
        : "Not sent to Claude Code.";
    case "unknown":
      return "Arrival at Claude Code and the execution result are unconfirmed.";
  }
}

/** Lists are ordered by key/ID, not by time. A page change never erases its prior view on error. */
export function DraftRecovery({ api, onRestore, onCommandState }: Props): ReactElement {
  const [draftPage, setDraftPage] = useState<DraftPage | null>(null);
  const [commandPage, setCommandPage] = useState<CommandPage | null>(null);
  const [draftCursor, setDraftCursor] = useState<string | null>(null);
  const [commandCursor, setCommandCursor] = useState<string | null>(null);
  const [draftError, setDraftError] = useState(false);
  const [commandError, setCommandError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [draftLoading, setDraftLoading] = useState(false);
  const [commandLoading, setCommandLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const draftRequest = useRef(0);
  const commandRequest = useRef(0);

  async function loadDrafts(after?: string): Promise<void> {
    const request = ++draftRequest.current;
    setDraftLoading(true);
    setDraftError(false);
    try {
      const page = await api.listDrafts(after ? { after } : undefined);
      if (request !== draftRequest.current) return;
      setDraftPage(page);
      setDraftCursor(after ?? null);
    } catch {
      if (request === draftRequest.current) setDraftError(true);
    } finally {
      if (request === draftRequest.current) setDraftLoading(false);
    }
  }

  async function loadCommands(after?: string): Promise<void> {
    const request = ++commandRequest.current;
    setCommandLoading(true);
    setCommandError(false);
    try {
      const page = await api.listCommands(after ? { after } : undefined);
      if (request !== commandRequest.current) return;
      setCommandPage(page);
      setCommandCursor(after ?? null);
      for (const command of page.commands) onCommandState?.(command);
    } catch {
      if (request === commandRequest.current) setCommandError(true);
    } finally {
      if (request === commandRequest.current) setCommandLoading(false);
    }
  }

  async function restoreDraft(key: string, listedRevision: number): Promise<void> {
    if (actionBusy) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const draft = await api.getDraft(key);
      if (draft.revision !== listedRevision || (draft.text === "" && !draft.attachments?.length)) {
        setActionError("The saved draft changed. Refresh the list and select it again.");
        return;
      }
      if (draft.attachments?.length) onRestore(draft.text, draft.attachments);
      else onRestore(draft.text);
    } catch {
      setActionError("Couldn't read the draft text. Your current input is still available.");
    } finally {
      setActionBusy(false);
    }
  }

  async function restoreSubmission(commandId: string): Promise<void> {
    if (actionBusy) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const submission = await api.getSubmission(commandId);
      if (submission.attachments?.length) onRestore(submission.text, submission.attachments);
      else onRestore(submission.text);
    } catch {
      setActionError("Couldn't read the submitted text. Your current input is still available.");
    } finally {
      setActionBusy(false);
    }
  }

  async function refreshCommand(commandId: string): Promise<void> {
    if (actionBusy) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const receipt = await api.getCommand(commandId);
      setCommandPage(
        (page) =>
          page && {
            ...page,
            commands: page.commands.map((item) => (item.command_id === commandId ? receipt : item)),
          },
      );
      onCommandState?.(receipt);
    } catch {
      setActionError("Couldn't refresh the send status. The displayed record is unchanged.");
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <details
      className="draft-recovery jp"
      onToggle={(event) => {
        if (event.currentTarget.open) {
          void loadDrafts();
          void loadCommands();
        }
      }}
    >
      <summary>Drafts and send history</summary>
      <div className="draft-recovery-body">
        <p className="draft-recovery-help">
          Restored text opens in a new message box. Your current input stays available.
        </p>
        {actionError ? (
          <p role="alert" className="draft-recovery-error">
            {actionError}
          </p>
        ) : null}

        <section aria-label="Saved drafts">
          <div className="draft-recovery-heading">
            <h3>Saved drafts</h3>
            <button
              type="button"
              onClick={() => void loadDrafts(draftCursor ?? undefined)}
              disabled={draftLoading}
            >
              Refresh
            </button>
          </div>
          {draftError ? (
            <p role="alert" className="draft-recovery-error">
              Couldn't read the saved drafts.
            </p>
          ) : null}
          {draftLoading && !draftPage ? <Loading>Loading…</Loading> : null}
          {draftPage?.drafts.length === 0 ? <p>No saved drafts.</p> : null}
          {draftPage?.drafts.length ? (
            <ul className="draft-recovery-list">
              {draftPage.drafts.map((draft) => (
                <li key={draft.key}>
                  <span className="draft-recovery-id" title={draft.key}>
                    {draft.key === "new" ? "New conversation" : draft.key.slice(0, 8)}
                  </span>
                  <span className="draft-recovery-meta">
                    {time(draft.saved_at)} · {draft.bytes.toLocaleString("en-US")} B
                  </span>
                  <button
                    type="button"
                    disabled={actionBusy}
                    aria-label={`Restore text from draft ${draft.key}`}
                    onClick={() => void restoreDraft(draft.key, draft.revision)}
                  >
                    Restore text
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="draft-recovery-pages">
            {draftCursor ? (
              <button type="button" disabled={draftLoading} onClick={() => void loadDrafts()}>
                First page
              </button>
            ) : null}
            {draftPage?.next_after ? (
              <button
                type="button"
                disabled={draftLoading}
                onClick={() => void loadDrafts(draftPage.next_after ?? undefined)}
              >
                Next page
              </button>
            ) : null}
          </div>
        </section>

        <section aria-label="Send history">
          <div className="draft-recovery-heading">
            <h3>Send history</h3>
            <button
              type="button"
              onClick={() => void loadCommands(commandCursor ?? undefined)}
              disabled={commandLoading}
            >
              Refresh
            </button>
          </div>
          {commandError ? (
            <p role="alert" className="draft-recovery-error">
              Couldn't read the send history.
            </p>
          ) : null}
          {commandLoading && !commandPage ? <Loading>Loading…</Loading> : null}
          {commandPage?.commands.length === 0 ? <p>No send history.</p> : null}
          {commandPage?.commands.length ? (
            <ul className="draft-recovery-list">
              {commandPage.commands.map((command) => (
                <li key={command.command_id}>
                  <span className="draft-recovery-id" title={command.command_id}>
                    {command.command_id.slice(0, 8)}
                  </span>
                  <span className="draft-recovery-meta">
                    {time(command.created_at)} · {command.bytes.toLocaleString("en-US")} B
                  </span>
                  <span className="draft-recovery-state">{commandState(command)}</span>
                  <button
                    type="button"
                    disabled={actionBusy}
                    aria-label={`Refresh status for send ${command.command_id}`}
                    onClick={() => void refreshCommand(command.command_id)}
                  >
                    Refresh status
                  </button>
                  <button
                    type="button"
                    disabled={actionBusy}
                    aria-label={`Restore text from send ${command.command_id}`}
                    onClick={() => void restoreSubmission(command.command_id)}
                  >
                    Restore text
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="draft-recovery-pages">
            {commandCursor ? (
              <button type="button" disabled={commandLoading} onClick={() => void loadCommands()}>
                First page
              </button>
            ) : null}
            {commandPage?.next_after ? (
              <button
                type="button"
                disabled={commandLoading}
                onClick={() => void loadCommands(commandPage.next_after ?? undefined)}
              >
                Next page
              </button>
            ) : null}
          </div>
        </section>
      </div>
    </details>
  );
}
