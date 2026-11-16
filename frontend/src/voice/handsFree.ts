/**
 * Hands-free conversation: the loop's pure state machine. `useHandsFree` feeds it
 * what the window sees (the visible session's last turn and cards, the controller's playback, the
 * window's visibility) and the Composer feeds it what its capture and Send did; this module only
 * decides the next phase.
 *
 * Half-duplex: a capture is open only in `listening`. A card moves the loop to `waiting`, where
 * voice answers nothing (hard rule 6). Every event the current phase does not name is a no-op:
 * the state comes back unchanged.
 */

/** The pause before the mic re-arms after speech ends or a turn has nothing to speak. */
export const REARM_DELAY_MS = 400;

export type HandsFreePhase =
  | "off"
  | "listening"
  | "sending"
  | "thinking"
  | "waiting"
  | "speaking"
  | "rearming";

/** Why the mode ended or did not start; shown once, until dismissed or the next Enter. */
export type HandsFreeNotice =
  | "dictation_unavailable"
  | "no_voice"
  | "nothing_heard"
  | "send_blocked"
  | "turn_failed"
  | "turn_stopped"
  | "speech_failed"
  | "window_hidden"
  | "session_switched";

/** `read`: a playback the loop does not own started; `view`: the chat left the screen. */
export type HandsFreeExit = "esc" | "toggle" | "hidden" | "session" | "pagehide" | "read" | "view";

/** How a turn ended, as `useHandsFree` reads it: `silent` is tool-only, unreadable or voiceless. */
export type TurnOutcome = "speakable" | "silent" | "failed" | "stopped";

export interface HandsFreeState {
  phase: HandsFreePhase;
  /** The read-aloud notice is open because Enter came before it was acknowledged. */
  noticeOpen: boolean;
  notice: HandsFreeNotice | null;
  /** The turn whose reply is being spoken (`speaking` only). */
  turnId: string | null;
}

export type HandsFreeEvent =
  | { type: "enter"; dictationReady: boolean; localVoice: boolean; noticeSeen: boolean }
  | { type: "noticeConfirmed" }
  | { type: "noticeCancelled" }
  | { type: "captureDone" }
  | { type: "captureEmpty" }
  | { type: "sendAccepted" }
  | { type: "sendRefused" }
  | { type: "card"; open: boolean }
  | { type: "turnFinished"; turnId: string; outcome: TurnOutcome }
  | { type: "speechEnded"; turnId: string }
  | { type: "speechFailed"; turnId: string }
  | { type: "micPressed" }
  | { type: "rearm" }
  | { type: "exit"; reason: HandsFreeExit }
  | { type: "dismiss" };

/** What the Composer reports about the capture and the automatic Send it runs, and its mic pressed
 * while the loop is not listening. */
export type HandsFreeComposerEvent = Extract<
  HandsFreeEvent,
  {
    type: "captureDone" | "captureEmpty" | "sendAccepted" | "sendRefused" | "micPressed" | "exit";
  }
>;

/** The Composer's side of the loop. Absent when the window offers no hands-free mode. */
export interface HandsFreeBridge {
  /** The mode is on: captures end at their first non-empty final and send themselves. */
  active: boolean;
  /** The loop is in `listening`: each rising edge starts one toggled capture. */
  listening: boolean;
  dispatch(event: HandsFreeComposerEvent): void;
  /** What the automatic Send carries for the words heard: `useHandsFree` adds the spoken-answer
   * request. Absent, they are sent as heard. A typed Send never uses it. */
  message?: (heard: string) => string;
}

export const HANDS_FREE_OFF: HandsFreeState = {
  phase: "off",
  noticeOpen: false,
  notice: null,
  turnId: null,
};

/** The read-aloud controller's owner for a hands-free reply. */
export function speechOwner(turnId: string): string {
  return `handsfree:${turnId}`;
}

function withExit(reason: HandsFreeExit): HandsFreeState {
  const notice: HandsFreeNotice | null =
    reason === "hidden" ? "window_hidden" : reason === "session" ? "session_switched" : null;
  return { phase: "off", noticeOpen: false, notice, turnId: null };
}

function turnFinishedResult(outcome: TurnOutcome, turnId: string): HandsFreeState {
  switch (outcome) {
    case "speakable":
      return { phase: "speaking", noticeOpen: false, notice: null, turnId };
    case "silent":
      return { phase: "rearming", noticeOpen: false, notice: null, turnId: null };
    case "failed":
      return { phase: "off", noticeOpen: false, notice: "turn_failed", turnId: null };
    case "stopped":
      return { phase: "off", noticeOpen: false, notice: "turn_stopped", turnId: null };
  }
}

export function handsFreeReducer(state: HandsFreeState, event: HandsFreeEvent): HandsFreeState {
  // Every way out, from any phase, lands here first. With the mode already
  // off there is nothing to leave: an open notice closes, and nothing is said.
  if (event.type === "exit") {
    if (state.phase !== "off") return withExit(event.reason);
    return state.noticeOpen ? { ...state, noticeOpen: false } : state;
  }

  switch (state.phase) {
    case "off":
      switch (event.type) {
        case "enter":
          if (!event.dictationReady) {
            return {
              phase: "off",
              noticeOpen: false,
              notice: "dictation_unavailable",
              turnId: null,
            };
          }
          if (!event.localVoice) {
            return { phase: "off", noticeOpen: false, notice: "no_voice", turnId: null };
          }
          if (!event.noticeSeen) {
            return { phase: "off", noticeOpen: true, notice: null, turnId: null };
          }
          return { phase: "listening", noticeOpen: false, notice: null, turnId: null };
        case "noticeConfirmed":
          return state.noticeOpen
            ? { phase: "listening", noticeOpen: false, notice: null, turnId: null }
            : state;
        case "noticeCancelled":
          return state.noticeOpen ? { ...state, noticeOpen: false } : state;
        case "dismiss":
          return state.notice !== null || state.noticeOpen
            ? { phase: "off", noticeOpen: false, notice: null, turnId: null }
            : state;
        default:
          return state;
      }
    case "listening":
      switch (event.type) {
        case "captureDone":
          return { phase: "sending", noticeOpen: false, notice: null, turnId: null };
        case "captureEmpty":
          return { phase: "off", noticeOpen: false, notice: "nothing_heard", turnId: null };
        default:
          return state;
      }
    case "sending":
      switch (event.type) {
        case "sendAccepted":
          return { phase: "thinking", noticeOpen: false, notice: null, turnId: null };
        case "sendRefused":
          return { phase: "off", noticeOpen: false, notice: "send_blocked", turnId: null };
        // Both can come before the Send resolves (it awaits the draft's round trip): read as in
        // thinking, and the late sendAccepted is then a no-op.
        case "card":
          return event.open
            ? { phase: "waiting", noticeOpen: false, notice: null, turnId: null }
            : state;
        case "turnFinished":
          return turnFinishedResult(event.outcome, event.turnId);
        default:
          return state;
      }
    case "thinking":
      switch (event.type) {
        case "card":
          return event.open
            ? { phase: "waiting", noticeOpen: false, notice: null, turnId: null }
            : state;
        case "turnFinished":
          return turnFinishedResult(event.outcome, event.turnId);
        default:
          return state;
      }
    case "waiting":
      switch (event.type) {
        case "card":
          return event.open
            ? state
            : { phase: "thinking", noticeOpen: false, notice: null, turnId: null };
        case "turnFinished":
          return turnFinishedResult(event.outcome, event.turnId);
        default:
          return state;
      }
    case "speaking":
      switch (event.type) {
        case "speechEnded":
          return event.turnId === state.turnId
            ? { phase: "rearming", noticeOpen: false, notice: null, turnId: null }
            : state;
        case "speechFailed":
          return event.turnId === state.turnId
            ? { phase: "off", noticeOpen: false, notice: "speech_failed", turnId: null }
            : state;
        case "micPressed":
          return { phase: "listening", noticeOpen: false, notice: null, turnId: null };
        default:
          return state;
      }
    case "rearming":
      switch (event.type) {
        case "rearm":
          return { phase: "listening", noticeOpen: false, notice: null, turnId: null };
        default:
          return state;
      }
    default:
      return state;
  }
}
