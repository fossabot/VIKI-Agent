/**
 * One Decision item, shown in the launcher panel: a kind chip, the `what`/`why` text, evidence
 * chips, the item's own actions (primary first), and the feedback controls that are always
 * there — "Not now", "Useful" (reads "Marked useful", pressed, once this click's own
 * `onFeedback("useful")` succeeds, until the card shows a different item), "Not useful" and a
 * "⋯"
 * menu for "Stop these" and the four reasons. Every piece of server or item text (`what`, `why`,
 * an evidence label, an action's `label`) is interpolated as plain JSX, so it always lands as a
 * text node; nothing here uses `dangerouslySetInnerHTML`.
 *
 * `onAction` and `onFeedback` are already bound to this one item by the caller (the panel):
 * neither takes an item key. `ItemEvidence` carries no action `type` of its own, so a click on an
 * evidence chip that names a note (`ref`) or a chat (`session_id`) builds the matching
 * `open_note`/`open_chat` action here and hands it to `onAction`, same as any of the item's own
 * actions. An `onAction` that answers a promise (the launcher's Start on a Bot, Run checks and
 * Suggest destinations) leaves that button disabled until the promise settles, so a second click
 * cannot send the same request again while the first is in flight.
 *
 * The "⋯" menu copies the app's existing small-menu pattern (TopBar/HelpMenu.tsx: a toggle with
 * `aria-haspopup`/`aria-expanded`, a `role="menu"` of `role="menuitem"` buttons, a window-level
 * Escape/Arrow key handler scoped to this menu's own wrapper, outside-click close) rather than
 * importing a library or extracting a shared component — the same choice RowMenu.tsx and
 * NoteActions.tsx each made for their own small menu.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import {
  FEEDBACK_REASONS,
  type FeedbackReason,
  type FeedbackValue,
  type ItemAction,
  type ItemEvidence,
  type ItemView,
} from "../../clients/decisionLoop/types";
import { ACTION_LABELS, KIND_LABELS, REASON_LABELS } from "./wording";
import "./ItemCard.css";

export interface ItemCardProps {
  item: ItemView;
  /** Answers nothing, or a promise for an action whose request is in flight. */
  onAction: (action: ItemAction) => unknown;
  onFeedback: (value: FeedbackValue, reason?: FeedbackReason) => void;
}

/** The action an evidence chip stands for: a note when it carries both `ref` and `workspace_id`
 * (the wire's note shape always sends them together), a chat when it carries `session_id`, or
 * none for the wire's third shape, a plain label with neither. Checking both fields together,
 * rather than `ref` alone, is also what lets TypeScript narrow `workspace_id` to a definite
 * `string` below — `exactOptionalPropertyTypes` refuses an explicit `undefined` for it. */
function evidenceAction(evidence: ItemEvidence): ItemAction | null {
  if (evidence.ref !== undefined && evidence.workspace_id !== undefined) {
    return {
      type: "open_note",
      label: evidence.label,
      primary: false,
      workspace_id: evidence.workspace_id,
      ref: evidence.ref,
    };
  }
  if (evidence.session_id !== undefined) {
    return {
      type: "open_chat",
      label: evidence.label,
      primary: false,
      session_id: evidence.session_id,
    };
  }
  return null;
}

/** An action's React key: its type and what it acts on, since an item may carry two actions of
 * one type (two chats to open). */
function actionKey(action: ItemAction): string {
  const target = [action.session_id, action.mission_id, action.routine_id, action.workspace_id];
  return [action.type, ...target, action.ref, action.call_id].map((part) => part ?? "").join(" ");
}

/** The item's actions, stably partitioned so the primary one renders first even if the server
 * ever sends it out of order; the rest keep their given order. */
function orderedActions(actions: readonly ItemAction[]): ItemAction[] {
  return [
    ...actions.filter((action) => action.primary),
    ...actions.filter((action) => !action.primary),
  ];
}

export function ItemCard({ item, onAction, onFeedback }: ItemCardProps): ReactElement {
  const [menuOpen, setMenuOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  // The keys of the actions whose request is still in flight.
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  // The item_key whose own Useful click already succeeded; compared against the current item so
  // a different item never shows as pressed.
  const [pressedKey, setPressedKey] = useState<string | null>(null);
  const pressed = pressedKey === item.item_key;

  // Copied from TopBar/HelpMenu.tsx's own effect: Escape closes and returns focus to the toggle,
  // Up/Down cycle the menu's own items, an outside click or focus move closes it, and the first
  // item takes focus as soon as the menu opens.
  useEffect(() => {
    if (!menuOpen) return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        setMenuOpen(false);
        toggleRef.current?.focus();
        return;
      }
      if (!wrapRef.current?.contains(event.target as Node)) return;
      if (event.key === "Tab") {
        setMenuOpen(false);
        return;
      }
      const items = itemRefs.current.filter(
        (el): el is HTMLButtonElement => el?.isConnected === true,
      );
      if (items.length === 0) return;
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        items[current < 0 ? 0 : (current + 1) % items.length]?.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        items[
          current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
        ]?.focus();
      }
    }
    function onOutside(event: MouseEvent | FocusEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setMenuOpen(false);
    }
    window.addEventListener("keydown", onWindowKeyDown);
    window.addEventListener("mousedown", onOutside);
    window.addEventListener("focusin", onOutside);
    itemRefs.current[0]?.focus();
    return () => {
      window.removeEventListener("keydown", onWindowKeyDown);
      window.removeEventListener("mousedown", onOutside);
      window.removeEventListener("focusin", onOutside);
    };
  }, [menuOpen]);

  function closeMenu(): void {
    setMenuOpen(false);
    toggleRef.current?.focus();
  }

  const actions = orderedActions(item.actions);

  function act(action: ItemAction): void {
    const answer = onAction(action);
    if (!(answer instanceof Promise)) return;
    const key = actionKey(action);
    setPending((now) => new Set(now).add(key));
    const settle = () =>
      setPending((now) => {
        const next = new Set(now);
        next.delete(key);
        return next;
      });
    answer.then(settle, settle);
  }

  function markUseful(): void {
    const answer = onFeedback("useful") as unknown;
    if (!(answer instanceof Promise)) {
      setPressedKey(item.item_key);
      return;
    }
    answer.then(
      () => setPressedKey(item.item_key),
      () => {},
    );
  }

  return (
    <article className="dloop-item">
      <span className="chip dloop-item-kind">{KIND_LABELS[item.kind]}</span>
      <p className="dloop-item-what">{item.what}</p>
      <p className="dloop-item-why">Why now: {item.why}</p>
      {item.evidence.length > 0 ? (
        <ul className="dloop-item-evidence">
          {item.evidence.map((evidence) => {
            const action = evidenceAction(evidence);
            const key = evidence.ref ?? evidence.session_id ?? evidence.label;
            return (
              <li key={key}>
                {action ? (
                  <button
                    type="button"
                    className="chip dloop-item-evidence-chip"
                    onClick={() => onAction(action)}
                  >
                    {evidence.label}
                  </button>
                ) : (
                  <span className="chip dloop-item-evidence-chip">{evidence.label}</span>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
      {actions.length > 0 ? (
        <div className="dloop-item-actions">
          {actions.map((action) => (
            <button
              key={actionKey(action)}
              type="button"
              className={
                action.primary ? "btn go sm dloop-item-action" : "btn alt sm dloop-item-action"
              }
              disabled={pending.has(actionKey(action))}
              onClick={() => act(action)}
            >
              {action.label || ACTION_LABELS[action.type]}
            </button>
          ))}
        </div>
      ) : null}
      <div className="dloop-item-feedback">
        <button
          type="button"
          className="btn txt sm dloop-item-feedback-btn"
          onClick={() => onFeedback("later")}
        >
          Not now
        </button>
        <button
          type="button"
          className="btn txt sm dloop-item-feedback-btn"
          aria-pressed={pressed}
          onClick={markUseful}
        >
          {pressed ? "Marked useful" : "Useful"}
        </button>
        <button
          type="button"
          className="btn txt sm dloop-item-feedback-btn"
          onClick={() => onFeedback("wrong")}
        >
          Not useful
        </button>
        <div className="dloop-item-menu-wrap" ref={wrapRef}>
          <button
            type="button"
            ref={toggleRef}
            className="btn txt sm dloop-item-menu-toggle"
            aria-label="More"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((shown) => !shown)}
          >
            ⋯
          </button>
          {menuOpen ? (
            <div className="dloop-item-menu menu-panel" role="menu" aria-label="More">
              <button
                type="button"
                role="menuitem"
                className="mi"
                ref={(el) => {
                  itemRefs.current[0] = el;
                }}
                onClick={() => {
                  closeMenu();
                  onFeedback("mute");
                }}
              >
                Stop these
              </button>
              {FEEDBACK_REASONS.map((reason, index) => (
                <button
                  key={reason}
                  type="button"
                  role="menuitem"
                  className="mi"
                  ref={(el) => {
                    itemRefs.current[index + 1] = el;
                  }}
                  onClick={() => {
                    closeMenu();
                    onFeedback("wrong", reason);
                  }}
                >
                  {REASON_LABELS[reason]}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}
