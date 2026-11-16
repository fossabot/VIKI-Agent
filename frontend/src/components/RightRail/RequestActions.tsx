/**
 * RequestActions: inline Allow once / Deny controls for one open permission request, meant to
 * sit directly in a run row (AgentRuns.tsx) rather than opening the full Cards.tsx treatment.
 * `onDecide` is the caller's own send; this component only tracks what became of that one call:
 * pending while it is in flight, then the outcome it resolved with, or — if the promise itself
 * rejected, meaning delivery was never even confirmed as attempted — an error that lets the
 * buttons answer again for a retry. A resolved outcome (applied, rejected or unknown) never does:
 * each one means the request is already decided or already sent, so a second click could only
 * double-send or repeat a refusal that will not change.
 *
 * What an answer is for is shown whole above the buttons: the tool and its summary with their
 * own line breaks and every hidden character written as its code (Cards/DecisionText.tsx), a line
 * that says so when there was one, and a line that says so when the text runs past its box.
 *
 * One click answers, so three things keep an answer with the request it was aimed at:
 * - a pointer press is ignored for a moment after the buttons appear or move (useAnswerHold.ts);
 * - buttons that have answered are marked `aria-disabled`, never `disabled`: the browser takes
 *   focus away from a disabled button and leaves the keyboard at the top of the page. The
 *   `sending` latch, not the attribute, is what stops a second answer;
 * - when the caller drops this component (its request was answered, here or elsewhere, or
 *   withdrawn) while focus is inside it, `handOff` gives focus to something that cannot answer:
 *   the list the request sat in when that takes focus, else the nearest control outside every
 *   request. Never another request's Allow once or Deny, where a key already on its way down
 *   would answer a request the user was not looking at.
 */
import { type ReactElement, useId, useLayoutEffect, useRef, useState } from "react";
import type { PermissionRequestedData } from "../../protocol";
import { runElapsed } from "../../state/runGroups";
import { DecisionNotes, DecisionText, useOverflowing } from "../Cards/DecisionText";
import { visibleText } from "../Cards/visibleText";
import { Spinner } from "../Loading/Loading";
import { useAnswerHold } from "./useAnswerHold";
import "./RequestActions.css";

const FOCUSABLE = "button, a[href], input, select, textarea, [tabindex]";

/** Whether `element` could take focus now: not disabled, hidden or inert, by itself or through
 * something it sits in. */
function usable(element: HTMLElement): boolean {
  if (element.matches(":disabled")) return false;
  if (element.closest('[hidden], [inert], [aria-disabled="true"]')) return false;
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
  }
  return true;
}

/** Gives focus a place once the request it was in has gone. `ancestors` is what that request sat
 * in, nearest first, read while it was still attached. Of those still in the page, the nearest
 * that takes focus itself (a list given a `tabindex`) wins, else its first usable control that is
 * outside every request. A no-op when focus has gone somewhere of its own accord, or when nothing
 * is left to take it (never throws). */
function handOff(ancestors: readonly HTMLElement[]): void {
  const active = document.activeElement;
  if (active !== null && active !== document.body) return;
  for (const ancestor of ancestors) {
    if (!ancestor.isConnected) continue;
    const candidates = [
      ...(ancestor.matches(FOCUSABLE) ? [ancestor] : []),
      ...ancestor.querySelectorAll<HTMLElement>(FOCUSABLE),
    ];
    for (const candidate of candidates) {
      if (candidate.closest(".request-actions") !== null || !usable(candidate)) continue;
      candidate.focus();
      if (document.activeElement === candidate) return;
    }
  }
}

/** Exactly Cards.tsx's own `AnswerOutcome` (and state/approvals.ts's copy of it), written out
 * again here rather than imported so this RightRail component stays independent of the
 * Conversation card tree — the same boundary Cards.tsx itself draws around Composer's own
 * `isComposingEvent`, copied rather than imported. */
export type AnswerOutcome = "applied" | "rejected" | "unknown";

export interface RequestActionsProps {
  request: PermissionRequestedData;
  /** The earliest moment anything made this run wait for a user decision, or null while that
   * is not yet known — runGroups.ts's own `RunRow.waitingSince`/`SessionNeed.waitingSince`. */
  waitingSince: number | null;
  nowMs: number;
  onDecide: (allow: boolean) => Promise<AnswerOutcome>;
}

type Phase = "idle" | "pending" | "applied" | "rejected" | "unknown" | "error";

const NOTE_TEXT: Record<"pending" | "rejected" | "unknown" | "error", string> = {
  pending: "Sending…",
  rejected: "Not recorded: this request has already ended.",
  unknown: "Sent; Claude Code has not confirmed it yet.",
  error: "Could not send your answer. Try again.",
};

export function RequestActions({
  request,
  waitingSince,
  nowMs,
  onDecide,
}: RequestActionsProps): ReactElement {
  const [phase, setPhase] = useState<Phase>("idle");
  // A synchronous latch, and the only thing that stops a second answer: the buttons stay
  // clickable (see the module doc), and a second click fired before React commits the "pending"
  // render must still only send once (Cards.tsx's PermissionCard guards its own respond() the
  // same way, for the same reason).
  const sending = useRef(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const overflowing = useOverflowing(headRef);
  const hold = useAnswerHold(request.tool_use_id);
  const notesId = useId();
  // A layout effect, not a passive one: its cleanup must run in the same synchronous mutation
  // that detaches this node, while `document.activeElement` can still be read as inside it — a
  // passive effect's cleanup is deferred past that point, too late to tell. Where focus goes is
  // decided only after that mutation: the same one can take the list, or the row, with it.
  useLayoutEffect(() => {
    const root = rootRef.current;
    return () => {
      if (!root?.contains(document.activeElement)) return;
      const ancestors: HTMLElement[] = [];
      for (let node = root.parentElement; node !== null; node = node.parentElement) {
        ancestors.push(node);
      }
      queueMicrotask(() => handOff(ancestors));
    };
  }, []);

  // Not trimmed: a hidden character at either end is part of what would run.
  const detail = request.summary === "" ? request.tool : `${request.tool} ${request.summary}`;
  const shown = visibleText(detail);
  const latched = phase !== "idle" && phase !== "error";
  const described = shown.hidden || overflowing ? notesId : undefined;

  async function decide(allow: boolean): Promise<void> {
    if (sending.current) return;
    sending.current = true;
    setPhase("pending");
    let outcome: AnswerOutcome;
    try {
      outcome = await onDecide(allow);
    } catch {
      sending.current = false;
      setPhase("error");
      return;
    }
    setPhase(outcome);
  }

  return (
    <div className="request-actions" ref={rootRef}>
      <div className="request-actions-head" ref={headRef}>
        {"Wants to run: "}
        <DecisionText text={detail} />
      </div>
      <DecisionNotes id={notesId} hidden={shown.hidden} overflowing={overflowing} />
      {waitingSince !== null ? (
        <div className="request-actions-wait">
          {`waiting ${runElapsed(Math.max(0, nowMs - waitingSince))}`}
        </div>
      ) : null}
      <div
        className="request-actions-buttons"
        ref={hold.ref}
        data-settling={hold.settling ? "" : undefined}
        onMouseDownCapture={hold.onPointer}
        onClickCapture={hold.onPointer}
      >
        <button
          type="button"
          className="btn go sm"
          aria-disabled={latched || undefined}
          aria-label={`Allow once: ${shown.text}`}
          aria-describedby={described}
          onClick={() => void decide(true)}
        >
          Allow once
        </button>
        <button
          type="button"
          className="btn alt sm"
          aria-disabled={latched || undefined}
          aria-label={`Deny: ${shown.text}`}
          aria-describedby={described}
          onClick={() => void decide(false)}
        >
          Deny
        </button>
      </div>
      {phase !== "idle" && phase !== "applied" ? (
        <div className="request-actions-note" role={phase === "error" ? "alert" : "status"}>
          {phase === "pending" ? <Spinner className="spinner-late" /> : null}
          {NOTE_TEXT[phase]}
        </div>
      ) : null}
    </div>
  );
}
