/** Tracks model and effort requests separately from the values reported by the active session. A request is shown as applied only after that session reports the new value. */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ErrorData } from "../../protocol";
import type { ControlOutcome } from "../../state/viki";

/** How long an "applied" note stays up before it clears itself. */
export const APPLIED_NOTE_MS = 4000;

export type ChipNoteKind = "pending" | "applied" | "failed";
export interface ChipNote {
  kind: ChipNoteKind;
  text: string;
}

/** Runs `callback` after `ms` and hands back a canceller, so a test can inject a fake instead of
 * reaching for real timers. */
export type Schedule = (callback: () => void, ms: number) => () => void;
const windowSchedule: Schedule = (callback, ms) => {
  const handle = window.setTimeout(callback, ms);
  return () => window.clearTimeout(handle);
};

export interface UseModelChoiceOptions {
  sessionId: string;
  model: string;
  effort: string;
  /** Before initialization, choosing the displayed default explicitly assigns the chat's route. */
  allowCurrentModel?: boolean;
  /** The session's own standing error (`SnapshotState.error`). A *different* object landing
   * after a model pick is a fresh refusal; the same one standing from before the pick is not. */
  error: ErrorData | null;
  setModel: (sessionId: string, model: string) => Promise<ControlOutcome>;
  setEffort: (sessionId: string, effort: string) => Promise<ControlOutcome>;
  /** The CLI's own display label for a model value (`info.models`), as the chip itself shows it. */
  labelOf: (value: string) => string;
  schedule?: Schedule;
}

export interface UseModelChoiceResult {
  note: ChipNote | null;
  pickModel: (value: string) => void;
  pickEffort: (value: string) => void;
}

interface Pick {
  kind: "model" | "effort";
  value: string;
  errorAtPick: ErrorData | null;
  confirmOnReceipt?: boolean;
}

export function useModelChoice({
  sessionId,
  model,
  effort,
  allowCurrentModel = false,
  error,
  setModel,
  setEffort,
  labelOf,
  schedule = windowSchedule,
}: UseModelChoiceOptions): UseModelChoiceResult {
  const [note, setNote] = useState<ChipNote | null>(null);
  // The pick this hook is still waiting on, or null once it resolved (or nothing is in flight).
  // Object identity, not the session or value alone, tells a stale resolution apart from the
  // active one: a later pick, or a session switch, replaces or clears this before an older
  // promise's `.then` ever gets to check it.
  const pickRef = useRef<Pick | null>(null);
  const cancelHideRef = useRef<(() => void) | null>(null);

  // The caller's latest `labelOf`/`schedule`, read through the ref everywhere below instead of
  // closing over the prop directly. App.tsx passes a new `labelOf` arrow every render (it reads
  // `info.models` inline); keeping the confirm effect's and showApplied's own dependency lists
  // off that unstable identity is this hook's job, not every caller's.
  const labelOfRef = useRef(labelOf);
  labelOfRef.current = labelOf;
  const scheduleRef = useRef(schedule);
  scheduleRef.current = schedule;

  const clearHide = useCallback(() => {
    cancelHideRef.current?.();
    cancelHideRef.current = null;
  }, []);
  const showApplied = useCallback(
    (text: string) => {
      clearHide();
      setNote({ kind: "applied", text });
      cancelHideRef.current = scheduleRef.current(() => setNote(null), APPLIED_NOTE_MS);
    },
    [clearHide],
  );
  // Cancels a still-running hide timer on unmount; nothing else here needs tearing down.
  useEffect(() => clearHide, [clearHide]);

  // The session's own confirmation: `model`/`effort` catching up, or a fresh `error` after the
  // pick. A session switch abandons whatever was in flight for the one just left, note included.
  const lastSessionRef = useRef(sessionId);
  useEffect(() => {
    if (lastSessionRef.current !== sessionId) {
      lastSessionRef.current = sessionId;
      pickRef.current = null;
      clearHide();
      setNote(null);
      return;
    }
    const pick = pickRef.current;
    if (!pick) return;
    if (pick.kind === "model") {
      if (model === pick.value && !pick.confirmOnReceipt) {
        pickRef.current = null;
        showApplied(`Now using ${labelOfRef.current(pick.value)}`);
      } else if (error !== null && error !== pick.errorAtPick) {
        pickRef.current = null;
        setNote({ kind: "failed", text: `Couldn't switch to ${labelOfRef.current(pick.value)}` });
      }
    } else if (effort === pick.value) {
      pickRef.current = null;
      showApplied(`Now at ${pick.value} effort`);
    }
  }, [sessionId, model, effort, error, clearHide, showApplied]);

  function pickModel(value: string): void {
    const pending = pickRef.current;
    if (pending?.kind === "model") {
      if (value === pending.value) return; // already the pending pick
    } else if (value === model && !allowCurrentModel) {
      return; // already running it, nothing pending to undo
    }
    clearHide();
    const pick: Pick = {
      kind: "model",
      value,
      errorAtPick: error,
      confirmOnReceipt: allowCurrentModel && value === model,
    };
    pickRef.current = pick;
    setNote({ kind: "pending", text: `Switching to ${labelOfRef.current(value)}…` });
    void setModel(sessionId, value).then((outcome) => {
      if (pickRef.current !== pick) return; // superseded: a later pick, a session switch, or resolved already
      if (outcome === "rejected") {
        pickRef.current = null;
        setNote({ kind: "failed", text: `Couldn't switch to ${labelOfRef.current(value)}` });
      } else if (outcome === "applied" && pick.confirmOnReceipt) {
        pickRef.current = null;
        showApplied(`Now using ${labelOfRef.current(value)}`);
      }
      // A live switch still needs `model` itself to catch up; only an explicit assignment of
      // the current prestart value can be confirmed by its applied receipt alone.
    });
  }

  function pickEffort(value: string): void {
    const pending = pickRef.current;
    if (pending?.kind === "effort") {
      if (value === pending.value) return; // already the pending pick
    } else if (value === effort) {
      return; // already running it, nothing pending to undo
    }
    clearHide();
    const pick: Pick = { kind: "effort", value, errorAtPick: error };
    pickRef.current = pick;
    setNote(null); // nothing to show until the save itself settles
    void setEffort(sessionId, value).then((outcome) => {
      if (pickRef.current !== pick) return;
      if (outcome === "applied") {
        if (value === effort) {
          // a revert: the save now matches what's already running, nothing left to apply
          pickRef.current = null;
          setNote(null);
        } else {
          setNote({ kind: "pending", text: `${value} from your next message` });
        }
      } else {
        pickRef.current = null;
        setNote({ kind: "failed", text: `Couldn't switch to ${value} effort` });
      }
    });
  }

  return { note, pickModel, pickEffort };
}
