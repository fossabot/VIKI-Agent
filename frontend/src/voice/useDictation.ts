/**
 * The composer's dictation lifecycle: press or toggle the mic, stream the microphone to
 * `/ws/voice`, show partials as provisional text, insert finals into the draft at the capture's
 * anchor, and stop the capture on every way out (release, Esc, unmount, a hidden window, a lost
 * or silent microphone, a closed socket, a server error).
 *
 * Voice only edits the draft: this hook calls `setText`, `focusBox` and the api, nothing else.
 * It cannot send a turn or answer a card.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  begin,
  type DictationState,
  edit,
  final,
  finish,
  IDLE,
  partial,
  provisionalText,
} from "./dictation";
import type {
  CaptureHandle,
  DictationApi,
  DictationControls,
  DictationView,
  VoiceServerMessage,
  VoiceSocket,
  VoiceStatus,
} from "./dictationTypes";
import { MAX_CAPTURE_BYTES, MICROPHONE_KEY, PRESTART_AUDIO_BYTES } from "./dictationTypes";
import { dictationNotice } from "./dictationWords";
import { stopReadAloud } from "./readAloud";

export const HOLD_MS = 300;
export const AUDIO_STALL_MS = 2_000;
export const TICK_MS = 250;
export const FINISH_BACKSTOP_MS = 30_000;
// Cancelling waits for the socket to close so a quick re-press cannot meet the old connection's
// 1013; this bounds the wait.
const CANCEL_BACKSTOP_MS = 5_000;

export interface DictationClock {
  now(): number;
  setTimeout(fn: () => void, ms: number): number;
  clearTimeout(id: number): void;
}

export interface DictationOptions {
  api: DictationApi | null;
  text: string;
  setText(next: string, start?: number, end?: number): void;
  caret(): number;
  composing(): boolean;
  focusBox(caret: number): void;
  clock?: DictationClock;
  /** Hands-free: the first non-empty final ends the capture, and a hidden window
   * cancels it instead of finishing it. */
  endOnFinal?: boolean;
}

const SYSTEM_CLOCK: DictationClock = {
  now: () => performance.now(),
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (id) => window.clearTimeout(id),
};

const INITIAL_VIEW: DictationView = {
  phase: "unavailable",
  status: null,
  provisional: "",
  elapsedMs: 0,
  notice: null,
  fallback: null,
  toggled: false,
};

// Errors whose fix is the setup or a setting: the status is read again so the mic shows it.
const SETUP_CODES = new Set([
  "setup_needed",
  "model_mismatch",
  "backend_missing",
  "backend_mismatch",
  "engine_unavailable",
  "worker_unavailable",
  "engine_mismatch",
  "model_unavailable",
  "backend_unavailable",
]);

const CLOSE_NOTICES: Record<number, string> = {
  1013: "busy",
  4401: "refused",
  4406: "refused",
  4408: "timed_out",
};

type FinalMessage = Extract<VoiceServerMessage, { type: "final" }>;

interface Session {
  id: string;
  socket: VoiceSocket;
  capture: Promise<CaptureHandle | null>;
  startedAt: number;
  pressedAt: number;
  lastAudioAt: number;
  toggled: boolean;
  acknowledged: boolean;
  finishing: boolean;
  cancelled: boolean;
  serverEnded: boolean;
  endSent: boolean;
  sent: number;
  backstop: number | null;
}

const field = (error: unknown, key: string): unknown =>
  typeof error === "object" && error !== null ? (error as Record<string, unknown>)[key] : undefined;

function micCode(error: unknown): string {
  const name = field(error, "name");
  if (name === "NotAllowedError" || name === "SecurityError") return "mic_denied";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "mic_missing";
  if (name === "NotReadableError" || name === "AbortError") return "mic_busy";
  return "mic_failed";
}

function codeOf(error: unknown): string {
  const code = field(error, "code");
  return typeof code === "string" ? code : "unavailable";
}

function createDictation(get: () => DictationOptions, setView: (view: DictationView) => void) {
  let view = INITIAL_VIEW;
  let state: DictationState = IDLE;
  let lastText = get().text;
  // Finals that arrived during an IME composition, applied at compositionend.
  let held: FinalMessage[] = [];
  let settlePending = false;
  let session: Session | null = null;
  let tickTimer: number | null = null;
  let pointerDown = false;
  let refetching = false;
  // A cancelled readiness read cannot start capture or change a newer explicit attempt.
  let startIntent = 0;
  let lifetime = new AbortController();

  const clock = () => get().clock ?? SYSTEM_CLOCK;
  const update = (patch: Partial<DictationView>) => {
    view = { ...view, ...patch };
    setView(view);
  };
  const noticeOf = (code: string | null | undefined) =>
    code ? { notice: dictationNotice(code) } : {};

  function applyStatus(status: VoiceStatus) {
    const resting = view.phase === "idle" || view.phase === "unavailable";
    const phase = status.state === "ready" ? "idle" : "unavailable";
    update({ status, fallback: status.fallback, ...(resting ? { phase } : {}) });
  }

  async function fetchStatus(intent?: number): Promise<VoiceStatus | null> {
    const api = get().api;
    const signal = lifetime.signal;
    if (!api) return null;
    const status = await api.status(signal);
    if (signal.aborted || (intent !== undefined && intent !== startIntent)) return null;
    applyStatus(status);
    return status;
  }

  function startWhenReady(toggled: boolean) {
    if (refetching || document.visibilityState === "hidden") return;
    refetching = true;
    const intent = ++startIntent;
    const signal = lifetime.signal;
    fetchStatus(intent)
      .then(
        (status) => {
          if (!status || signal.aborted || intent !== startIntent) return;
          if (status.state !== "ready") update(noticeOf(status.state));
          // A pointer released while the status was read made this a click: start toggled.
          else if (view.phase === "idle" && document.visibilityState !== "hidden") {
            startSession(toggled || !pointerDown);
          }
        },
        (error) => {
          if (!signal.aborted && intent === startIntent) update(noticeOf(codeOf(error)));
        },
      )
      .finally(() => {
        if (intent === startIntent) refetching = false;
      });
  }

  function startSession(toggled: boolean) {
    const api = get().api;
    if (!api || document.visibilityState === "hidden") return;
    applyHeld();
    const now = clock().now();
    const id = crypto.randomUUID();
    state = begin(id, get().caret());
    const s: Session = {
      id,
      socket: api.socket({
        message: (m) => onMessage(s, m),
        closed: (code) => onClosed(s, code),
      }),
      capture: Promise.resolve(null),
      startedAt: now,
      pressedAt: now,
      lastAudioAt: now,
      toggled,
      acknowledged: false,
      finishing: false,
      cancelled: false,
      serverEnded: false,
      endSent: false,
      sent: 0,
      backstop: null,
    };
    session = s;
    s.socket.begin(id);
    update({ phase: "starting", provisional: "", elapsedMs: 0, notice: null, toggled });
    tick();
    // Speech from the speakers would be heard by the microphone.
    stopReadAloud();
    s.capture = api
      .startCapture({
        deviceId: localStorage.getItem(MICROPHONE_KEY),
        workletUrl: api.workletUrl,
        onAudio: (chunk) => onAudio(s, chunk),
        onEnded: () => finishSession(s, "mic_lost"),
      })
      .catch((error: unknown) => {
        cancelSession(s, micCode(error));
        return null;
      });
  }

  function stopCapture(s: Session): Promise<void> {
    return s.capture.then((handle) => handle?.stop()).catch(() => undefined);
  }

  function stopTimers(s: Session) {
    const c = clock();
    if (tickTimer !== null) c.clearTimeout(tickTimer);
    if (s.backstop !== null) c.clearTimeout(s.backstop);
    tickTimer = null;
    s.backstop = null;
  }

  function tick() {
    const c = clock();
    tickTimer = c.setTimeout(() => {
      const s = session;
      if (!s) return;
      const now = c.now();
      update({ elapsedMs: now - s.startedAt });
      if (view.phase === "recording" && now - s.lastAudioAt >= AUDIO_STALL_MS) {
        finishSession(s, "mic_stalled");
      }
      tick();
    }, TICK_MS);
  }

  function onAudio(s: Session, chunk: ArrayBuffer) {
    // Nothing goes out after `end`, `cancel` or the server's `stopping`; the flush of a finish
    // still does, since `end` waits for it.
    if (s !== session || s.cancelled || s.endSent || s.serverEnded) return;
    s.lastAudioAt = clock().now();
    const total = s.sent + chunk.byteLength;
    if (total > MAX_CAPTURE_BYTES) {
      finishSession(s, "capture_cap");
    } else if (!s.acknowledged && total > PRESTART_AUDIO_BYTES) {
      cancelSession(s, "still_starting");
    } else {
      s.sent = total;
      s.socket.audio(chunk);
    }
  }

  function finishSession(s: Session, code?: string) {
    if (s !== session || s.cancelled) return;
    update(noticeOf(code));
    if (s.finishing) return;
    s.finishing = true;
    update({ phase: "finalizing" });
    s.backstop = clock().setTimeout(() => closeSession(s, "connection_lost"), FINISH_BACKSTOP_MS);
    void stopCapture(s).then(() => {
      if (s !== session || s.cancelled || s.serverEnded) return;
      s.endSent = true;
      s.socket.end(s.id);
    });
  }

  function cancelSession(s: Session, code?: string) {
    if (s !== session || s.cancelled) return;
    s.cancelled = true;
    s.socket.cancel(s.id);
    void stopCapture(s);
    state = finish(state);
    held = [];
    settlePending = false;
    stopTimers(s);
    update({ phase: "cancelling", provisional: "", toggled: false, ...noticeOf(code) });
    s.backstop = clock().setTimeout(() => closeSession(s, null), CANCEL_BACKSTOP_MS);
  }

  function closeSession(s: Session, code: string | null) {
    if (s !== session) return;
    session = null;
    stopTimers(s);
    void stopCapture(s);
    s.socket.close();
    if (held.length > 0) settlePending = true;
    else state = finish(state);
    update({ phase: "idle", provisional: "", toggled: false, ...noticeOf(code) });
  }

  function onClosed(s: Session, code: number) {
    if (s !== session) return;
    closeSession(s, s.cancelled ? null : (CLOSE_NOTICES[code] ?? "connection_lost"));
  }

  function onMessage(s: Session, m: VoiceServerMessage) {
    if (s !== session) return;
    if (m.capture_id !== s.id && !(m.type === "error" && m.capture_id === null)) return;
    // Results: the draft machine alone decides; after a cancel it holds no capture and drops them.
    if (m.type === "partial") {
      state = partial(state, m);
      update({ provisional: provisionalText(state) });
      return;
    }
    if (m.type === "final") {
      if (get().composing()) {
        held.push(m);
        return;
      }
      applyFinal(m);
      // Hands-free: the first non-empty final is the whole capture.
      if (get().endOnFinal && m.text.trim() !== "") finishSession(s);
      return;
    }
    // A cancelled capture waits for the socket to close, whatever the server still says.
    if (s.cancelled) return;
    switch (m.type) {
      case "capturing":
        s.acknowledged = true;
        s.lastAudioAt = clock().now();
        update({
          fallback: m.fallback,
          ...(view.phase === "starting" ? { phase: "recording" } : {}),
        });
        return;
      case "stopping":
        s.serverEnded = true;
        finishSession(s, m.reason);
        return;
      case "done": {
        const anchor = state.anchor;
        closeSession(s, null);
        if (!get().composing()) get().focusBox(anchor);
        return;
      }
      case "cancelled":
        closeSession(s, null);
        return;
      case "error":
        closeSession(s, m.code);
        if (SETUP_CODES.has(m.code)) fetchStatus().catch(() => undefined);
        return;
    }
  }

  function applyFinal(m: FinalMessage) {
    const result = final(state, m, lastText);
    if (!result) return;
    state = result.state;
    if (result.draft !== lastText) {
      lastText = result.draft;
      get().setText(result.draft, state.anchor, state.anchor);
    }
    update({ provisional: provisionalText(state) });
  }

  function applyHeld() {
    const pending = held;
    held = [];
    for (const m of pending) applyFinal(m);
    if (settlePending) {
      settlePending = false;
      state = finish(state);
      update({ provisional: "" });
    }
  }

  async function checkDevice(s: Session) {
    const api = get().api;
    const handle = await s.capture;
    if (!api || !handle || handle.deviceId === null) return;
    const present = (await api.microphones()).some((m) => m.deviceId === handle.deviceId);
    if (!present) finishSession(s, "mic_lost");
  }

  function edited(text: string) {
    if (text === lastText) return;
    state = edit(state, lastText, text);
    lastText = text;
  }

  function mount(api: DictationApi | null): () => void {
    lifetime = new AbortController();
    if (api) fetchStatus().catch(() => undefined);
    else update({ phase: "unavailable", status: null });
    const onVisibility = () => {
      if (document.visibilityState !== "hidden") return;
      startIntent += 1;
      refetching = false;
      pointerDown = false;
      if (!session) return;
      // Hands-free: a half sentence must never reach the draft, so a covered window
      // cancels the capture instead of finishing it.
      if (get().endOnFinal) cancelSession(session);
      else finishSession(session);
    };
    const onPageHide = () => {
      startIntent += 1;
      refetching = false;
      pointerDown = false;
      if (session) cancelSession(session);
    };
    const onDeviceChange = () => {
      if (session) checkDevice(session).catch(() => undefined);
    };
    const devices: MediaDevices | undefined = navigator.mediaDevices;
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    devices?.addEventListener("devicechange", onDeviceChange);
    return () => {
      lifetime.abort();
      startIntent += 1;
      refetching = false;
      pointerDown = false;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
      devices?.removeEventListener("devicechange", onDeviceChange);
      const s = session;
      if (s) {
        session = null;
        if (!s.cancelled) s.socket.cancel(s.id);
        stopTimers(s);
        void stopCapture(s);
        s.socket.close();
      }
      state = finish(state);
      held = [];
      settlePending = false;
      update({ phase: "unavailable", provisional: "", toggled: false });
    };
  }

  const controls: DictationControls = {
    press() {
      pointerDown = true;
      if (view.phase === "recording" && session) {
        // A press in toggle mode, or a hold whose pointerup was lost: either way, stop.
        finishSession(session);
      } else if (view.phase === "idle") {
        startSession(false);
      } else if (view.phase === "unavailable") {
        startWhenReady(false);
      }
      // starting, finalizing, cancelling: ignored, not queued.
    },
    release() {
      pointerDown = false;
      const s = session;
      if (!s || s.toggled || (view.phase !== "starting" && view.phase !== "recording")) return;
      if (clock().now() - s.pressedAt < HOLD_MS) {
        s.toggled = true;
        update({ toggled: true });
      } else {
        finishSession(s);
      }
    },
    toggle() {
      if (view.phase === "idle") startSession(true);
      else if (view.phase === "unavailable") startWhenReady(true);
      else if (session && (view.phase === "starting" || view.phase === "recording")) {
        finishSession(session);
      }
    },
    cancel() {
      startIntent += 1;
      refetching = false;
      pointerDown = false;
      if (session) cancelSession(session);
    },
    retryGpu() {
      const api = get().api;
      const signal = lifetime.signal;
      api?.clearFallback(signal).then(
        (status) => {
          if (!signal.aborted) applyStatus(status);
        },
        (error: unknown) => {
          if (!signal.aborted) update(noticeOf(codeOf(error)));
        },
      );
    },
    dismiss() {
      update({ notice: null });
    },
    compositionEnd: applyHeld,
  };

  return { controls, edited, mount };
}

export function useDictation(options: DictationOptions): [DictationView, DictationControls] {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const [view, setView] = useState(INITIAL_VIEW);
  const [dictation] = useState(() => createDictation(() => optionsRef.current, setView));
  const { api, text } = options;
  // Before any later message: a final must land against the draft the user sees now.
  useLayoutEffect(() => dictation.edited(text), [dictation, text]);
  useEffect(() => dictation.mount(api), [dictation, api]);
  return [view, dictation.controls];
}
