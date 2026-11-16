/**
 * The decision inbox: every open card across this window's tracked sessions, in one panel, with
 * a link back to each card's own conversation. It never answers a question or a plan itself —
 * only a tool permission's inline card renders here, through the same `renderAnswer` the caller
 * already uses elsewhere, so the answer path stays one path.
 *
 * Keyboard triage: j/k (or the arrows) move the roving focus across every
 * `[data-approval-row]` this dialog holds, Enter opens the focused row's own `[data-approval-open]`
 * control, and a/e/r/l act on the row. For a native row, a and r arm `decisionQueue.tsx`'s 5 s
 * undo window before calling `onDecide`, so a key press is never irreversible the
 * instant it lands. The guard (composing, Ctrl/Alt/Meta, a text-entry target, or a nested dialog
 * already owning the key) and the row navigation are generic over any `[data-approval-row]`, not
 * just this file's own — a future row kind that carries its own `[data-approval-action]` buttons
 * (DecisionSection's) rides the same dialog-root handler for free,
 * because it is found and clicked by that attribute rather than by this file knowing its shape.
 */
import {
  type ReactElement,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useId,
  useRef,
  useState,
} from "react";
import type { DecisionItem, NativeDecision } from "../../state/decisions";
import { visibleLine } from "../Cards/visibleText";
import { isComposingKeydown } from "../Composer/ime";
import { useModalFocus } from "../Dialog/useModalFocus";
import { approvalActions } from "./approvalKinds";
import {
  type DecisionLine,
  DecisionQueueProvider,
  focusNextRow,
  UndoBar,
  useDecisionQueue,
} from "./decisionQueue";
import "./DecisionInbox.css";

const isNative = (item: DecisionItem): item is NativeDecision =>
  item.kind === "permission" || item.kind === "question";

/** Text fields and selects own their keys, including the triage letters and arrows. */
function isTextEntryTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable
  );
}

const NOT_ACTIONABLE_NOTE = "Answer this one in its conversation: press Enter.";
const NO_EDIT_NOTE = "Edit is not available for this item.";
const KEYS_LINE = "Keys: j and k move · a approve · e edit · r reject · Enter opens";

export interface DecisionInboxProps {
  items: readonly DecisionItem[];
  sessionLabel: (sessionId: string) => string;
  /** The active workspace's name, not its id: every row shares this window's one active
   * workspace, so a raw UUID would tell rows apart by nothing. Null shows no workspace part. */
  workspaceName: string | null;
  renderAnswer: (item: DecisionItem) => ReactNode;
  onOpenConversation: (item: NativeDecision, origin: HTMLDivElement) => void;
  onClose: () => void;
  /** Arms the 5 s undo window for a native row's own a (allow) or r (deny) key.
   * Absent, or the row is not an inline permission (a plan or a question never is), the row
   * answers only "press Enter" — the inline card it already renders keeps its own immediate
   * Allow once / Deny (pinned by the frozen accept tests), unchanged by this prop either way. */
  onDecide?: (item: NativeDecision, allow: boolean) => Promise<void>;
  /** A queued decision that failed after this panel closed (the queue's `onLateFailure`). */
  onLateFailure?: (label: string, text: string) => void;
  /** The window's one decision line (App.tsx), passed straight to `DecisionQueueProvider`. Absent,
   * the provider keeps its own. */
  line?: DecisionLine;
  children?: ReactNode;
}

export function DecisionInbox(props: DecisionInboxProps): ReactElement {
  const { onLateFailure, line } = props;
  return (
    <DecisionQueueProvider
      {...(onLateFailure ? { onLateFailure } : {})}
      {...(line ? { line } : {})}
    >
      <DecisionInboxPanel {...props} />
    </DecisionQueueProvider>
  );
}

function DecisionInboxPanel({
  items,
  sessionLabel,
  workspaceName,
  renderAnswer,
  onOpenConversation,
  onClose,
  onDecide,
  children,
}: DecisionInboxProps): ReactElement {
  // Proposals live in the section children.
  const natives = items.filter(isNative);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const queue = useDecisionQueue();
  const [note, setNote] = useState<string | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const shortcutsId = useId();

  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: onClose });

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>): void {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (isComposingKeydown(event.nativeEvent)) return;
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (isTextEntryTarget(event.target)) return;
    if (!(event.target instanceof Element)) return;
    const target = event.target;
    if (target.closest('[role="dialog"]') !== dialog) return;

    const key = event.key.toLowerCase();
    const forward = key === "j" || event.key === "ArrowDown";
    const backward = key === "k" || event.key === "ArrowUp";
    const currentRow = target.closest<HTMLElement>("[data-approval-row]");

    if (forward || backward) {
      event.preventDefault();
      setNote(null);
      if (currentRow) {
        focusNextRow(currentRow, forward ? 1 : -1);
      } else {
        const rows = [...dialog.querySelectorAll<HTMLElement>("[data-approval-row]")];
        (forward ? rows[0] : rows[rows.length - 1])?.focus();
      }
      return;
    }

    if (event.key === "Enter") {
      if (currentRow && target === currentRow) {
        event.preventDefault();
        setNote(null);
        currentRow.querySelector<HTMLElement>("[data-approval-open]")?.click();
      }
      return;
    }

    if (!currentRow || (key !== "a" && key !== "e" && key !== "r" && key !== "l")) return;
    event.preventDefault();
    setNote(null);

    const actionAttr =
      key === "a" ? "approve" : key === "e" ? "edit" : key === "r" ? "reject" : "later";
    const item = natives.find((native) => native.id === currentRow.dataset.id);

    if (!item) {
      // A focused row that is not a native is a section row (DecisionSection's): it
      // carries its own `[data-approval-action]` buttons, found and clicked by that attribute
      // alone, with no need for this file to know the row's item shape. A missing Later just
      // moves on, like a native row's l; any other missing button, or one that is present but
      // disabled, shows the note instead; a present, enabled button is clicked (approve and later
      // also move focus to the next row, same as a native row's — edit and reject leave focus for
      // whatever they open, e.g. ReasonDialog).
      const button = currentRow.querySelector<HTMLButtonElement>(
        `[data-approval-action="${actionAttr}"]`,
      );
      if (!button && actionAttr === "later") {
        focusNextRow(currentRow);
        return;
      }
      if (button && !button.disabled) {
        button.click();
        if (actionAttr === "approve" || actionAttr === "later") focusNextRow(currentRow);
        return;
      }
      const label =
        actionAttr === "approve" ? "Approve" : actionAttr === "edit" ? "Edit" : "Reject";
      setNote(`${button ? button.textContent : label} is not available for this item.`);
      return;
    }
    if (key === "e") {
      setNote(NO_EDIT_NOTE);
      return;
    }
    if (key === "l") {
      focusNextRow(currentRow);
      return;
    }
    const allow = key === "a";
    const spec = approvalActions(item, { activeWorkspaceId: null }).find(
      (candidate) => candidate.action === (allow ? "approve" : "reject"),
    );
    if (spec && onDecide) {
      queue.enqueue(item.id, `${spec.label} · ${visibleLine(item.resource)}`, () =>
        onDecide(item, allow),
      );
      focusNextRow(currentRow);
    } else {
      setNote(NOT_ACTIONABLE_NOTE);
    }
  }

  return (
    <div className="decision-inbox-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Approvals"
        className="decision-inbox decision-inbox-panel"
        onKeyDown={handleKeyDown}
      >
        <div className="decision-inbox-head">
          <h2 className="decision-inbox-title jp">Approvals</h2>
          <button type="button" ref={closeRef} className="btn txt sm" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="decision-inbox-intro">
          What waits for you: tool requests, plans and questions from your chats, and the note and
          skill changes VIKI proposes.
        </p>
        <div className="decision-inbox-shortcuts">
          <button
            type="button"
            className="btn txt sm"
            aria-expanded={shortcutsOpen}
            aria-controls={shortcutsId}
            onClick={() => setShortcutsOpen((open) => !open)}
          >
            Keyboard shortcuts
          </button>
          <p id={shortcutsId} className="decision-inbox-keys" hidden={!shortcutsOpen}>
            {KEYS_LINE}
          </p>
        </div>
        <p role="status" aria-live="polite" className="decision-inbox-status">
          {note ?? ""}
        </p>
        <p className="decision-inbox-note">
          For conversation requests, allowing a tool permits an attempt. Check the conversation for
          the result.
        </p>
        <div className="decision-inbox-details">
          <h3 className="decision-inbox-section-title">Conversation requests</h3>
          {natives.length === 0 ? (
            <p className="decision-inbox-empty">No conversation requests are waiting.</p>
          ) : (
            <ul className="decision-inbox-rows">
              {natives.map((item) => (
                <li
                  key={item.id}
                  className="decision-inbox-row"
                  data-approval-row
                  data-id={item.id}
                  tabIndex={-1}
                >
                  <div className="decision-row-kind">
                    {item.kind === "question"
                      ? "Question"
                      : item.card.kind === "permission" && item.card.data.kind === "plan"
                        ? "Plan review"
                        : "Tool request"}
                  </div>
                  <div className="decision-row-session">{sessionLabel(item.origin.session_id)}</div>
                  <div className="decision-row-meta">
                    {workspaceName ? `${workspaceName} · ` : ""}
                    {item.provider} · {item.actor}
                  </div>
                  {/* The row's a and r keys answer from these two lines, so they read as what
                      was sent: hidden characters as their codes, line breaks drawn (visibleText.ts). */}
                  <div className="decision-row-action mono">{visibleLine(item.action)}</div>
                  <div className="decision-row-resource">{visibleLine(item.resource)}</div>
                  <div className="decision-row-revision mono">#{item.revision.slice(0, 8)}</div>
                  <p className="decision-row-consequence">
                    {item.inline
                      ? "Allow once lets the tool try this action. Deny refuses this request."
                      : "Review and answer in its conversation."}
                  </p>
                  {item.inline ? (
                    <div className="decision-row-answer">{renderAnswer(item)}</div>
                  ) : null}
                  <button
                    type="button"
                    className="btn alt sm decision-row-open"
                    data-approval-open
                    onClick={() => {
                      if (dialogRef.current) onOpenConversation(item, dialogRef.current);
                    }}
                  >
                    Open conversation
                  </button>
                </li>
              ))}
            </ul>
          )}
          {children}
        </div>
        <UndoBar />
      </div>
    </div>
  );
}
