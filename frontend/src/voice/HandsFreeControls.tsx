/**
 * Hands-free conversation's own controls: the round voice toggle beside the
 * composer's Send button and its menu (VoiceControl), the first-use notice (the read-aloud
 * notice plus one sentence about the mic) and the status strip — which also carries the one-line
 * notice the loop leaves behind when Enter is refused or the mode exits mid-loop (without it
 * the toggle looked dead). Each draws only `UseHandsFree`'s `state` and calls back
 * into its actions; the loop itself lives in `useHandsFree`.
 */
import { type ReactElement, useContext, useEffect, useId, useRef, useState } from "react";
import { t } from "../i18n/t";
import type { HandsFreePhase, HandsFreeNotice as NoticeReason } from "./handsFree";
import type { HandsFreeNote, UseHandsFree } from "./useHandsFree";
import { VoiceMenu } from "./VoiceMenu";
import type { VoiceboxApi, VoiceboxStatus } from "./voiceboxApi";
import {
  installedState,
  type SpeechEngine,
  speechEngine,
  VoicePackContext,
  type VoicePackStatus,
} from "./voicePackApi";
import "./HandsFree.css";

const PHASE_KEY: Record<Exclude<HandsFreePhase, "off">, Parameters<typeof t>[0]> = {
  listening: "handsfree.phase.listening",
  sending: "handsfree.phase.sending",
  thinking: "handsfree.phase.thinking",
  waiting: "handsfree.phase.waiting",
  speaking: "handsfree.phase.speaking",
  rearming: "handsfree.phase.rearming",
};

const NOTICE_KEY: Record<NoticeReason, Parameters<typeof t>[0]> = {
  dictation_unavailable: "handsfree.notice.dictation_unavailable",
  no_voice: "handsfree.notice.no_voice",
  nothing_heard: "handsfree.notice.nothing_heard",
  send_blocked: "handsfree.notice.send_blocked",
  turn_failed: "handsfree.notice.turn_failed",
  turn_stopped: "handsfree.notice.turn_stopped",
  speech_failed: "handsfree.notice.speech_failed",
  window_hidden: "handsfree.notice.window_hidden",
  session_switched: "handsfree.notice.session_switched",
};

const NOTE_KEY: Record<HandsFreeNote, Parameters<typeof t>[0]> = {
  voice_fallback: "handsfree.note.voice_fallback",
  voice_slow: "handsfree.note.voice_slow",
  nothing_read: "handsfree.note.nothing_read",
  no_voice_for: "handsfree.note.no_voice_for",
};

export function HandsFreeToggle({
  handsFree,
  primary = false,
  engine,
  shortcut,
}: {
  handsFree: UseHandsFree;
  /** Set by Composer: true when this button stands in Send's own place. */
  primary?: boolean;
  /** What pressing the toggle would talk to, named in the tip (" — {engine}"). Omitted, the tip
   * names no engine. */
  engine?: string;
  /** The toggle's own keyboard shortcut (VIKI's launcher panel): adds `aria-keyshortcuts` and
   * names the key in the tip, ahead of the engine suffix. Omitted, neither is added. */
  shortcut?: { aria: string; label: string };
}): ReactElement {
  const pressed = handsFree.state.phase !== "off";
  const tipId = useId();
  const base = shortcut
    ? pressed
      ? `End voice conversation (Esc or ${shortcut.label})`
      : `Start voice conversation (${shortcut.label})`
    : pressed
      ? "End voice conversation (Esc)"
      : "Start voice conversation";
  const tip = `${base}${engine ? ` — ${engine}` : ""}`;
  return (
    <>
      <button
        type="button"
        className={primary ? "hands-free-toggle composer-submit" : "hands-free-toggle"}
        aria-label={t("handsfree.toggle")}
        aria-pressed={pressed}
        aria-describedby={tipId}
        aria-keyshortcuts={shortcut?.aria}
        onClick={handsFree.toggle}
      >
        <svg className="ico" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M1 8Q4 2 8 8T15 8" />
        </svg>
      </button>
      <span role="tooltip" id={tipId} className="hands-free-tip">
        {tip}
      </span>
    </>
  );
}

/** The tip's suffix after " — ". `pack` is this window's own read of the installed voice pack
 * (read-only, never starts it); `voiceboxStatus` is the user's own Voicebox app, named only once
 * a `VoiceMenu` open has checked it (never on its own). */
function tipSuffix(
  engine: SpeechEngine,
  pack: VoicePackStatus | null,
  voiceboxStatus: VoiceboxStatus | null,
  profile: string | null,
): string {
  if (engine === "system") return "system voice";
  if (engine === "pack") {
    if (pack !== null && pack.state === "failed")
      return "system voice (natural voices need repair)";
    if (pack === null || installedState(pack.state)) return "natural voice";
    return "system voice (natural voices aren't installed)";
  }
  if (voiceboxStatus === null) return "Voicebox app, if it's running";
  if (voiceboxStatus.state !== "ready") return "system voice (Voicebox isn't running)";
  const named =
    profile !== null
      ? voiceboxStatus.profiles.find((candidate) => candidate.id === profile)
      : undefined;
  return named ? `Voicebox · ${named.name}` : "system voice (the chosen Voicebox voice is gone)";
}

/** Beside Send: the round toggle plus its menu. `settings` carries the voice keys the same
 * way `useReadAloudSettings` reads them, plus `voice.speech_engine`; `primary` is whatever
 * Composer computed for this render. The tip names whichever engine is chosen, refined by the
 * pack's own read-only status (fetched here on mount and on every menu open) and, only once the
 * engine is Voicebox, the own-app check `VoiceMenu` runs on open. */
export function VoiceControl({
  handsFree,
  primary,
  settings,
  patch,
  openSettings,
  api,
}: {
  handsFree: UseHandsFree;
  primary: boolean;
  settings: Readonly<Record<string, unknown>>;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  openSettings: () => void;
  api?: VoiceboxApi | null;
}): ReactElement {
  const profileValue = settings["voice.voicebox_profile"];
  const profile = typeof profileValue === "string" ? profileValue : null;
  const urlValue = settings["voice.voicebox_url"];
  const url = typeof urlValue === "string" ? urlValue : "http://127.0.0.1:17493";
  const speakWhileWriting = settings["voice.speak_while_writing"] === true;
  const engine = speechEngine(settings);
  const packServices = useContext(VoicePackContext);
  const [voiceboxStatus, setVoiceboxStatus] = useState<VoiceboxStatus | null>(null);
  const [pack, setPack] = useState<VoicePackStatus | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true; // StrictMode's development remount runs the cleanup below once first
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function loadPack(): void {
    packServices?.api.status().then(
      (next) => {
        if (mountedRef.current) setPack(next);
      },
      () => {},
    );
  }
  // Mount-only read, keyed on services identity; a failure leaves the pack unknown (this call
  // only ever GETs the pack's own status, never the user's Voicebox app).
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above.
  useEffect(() => {
    loadPack();
  }, [packServices]);

  const tip = tipSuffix(engine, pack, voiceboxStatus, profile);
  return (
    <span className="hands-free-voice" data-primary={primary}>
      <HandsFreeToggle handsFree={handsFree} primary={primary} engine={tip} />
      <VoiceMenu
        engine={engine}
        pack={pack}
        profile={profile}
        speakWhileWriting={speakWhileWriting}
        url={url}
        patch={patch}
        openSettings={openSettings}
        onStatus={setVoiceboxStatus}
        onOpenChange={(open) => {
          if (open) loadPack();
        }}
        {...(packServices ? { openUrl: packServices.openUrl } : {})}
        {...(api !== undefined ? { api } : {})}
      />
    </span>
  );
}

export function HandsFreeNotice({ handsFree }: { handsFree: UseHandsFree }): ReactElement | null {
  const { noticeOpen } = handsFree.state;
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  // The notice is a step to take, not a quiet line: its confirm takes focus on open, as
  // ReadAloud.tsx's own first-use notice does.
  useEffect(() => {
    if (noticeOpen) confirmRef.current?.focus();
  }, [noticeOpen]);
  if (!noticeOpen) return null;
  const noticeId = "hands-free-notice-text";
  return (
    // biome-ignore lint/a11y/useSemanticElements: a fieldset brings a border, padding and legend this note does not want (ReadAloud.tsx's own notice makes the same choice).
    <div className="hands-free-notice" role="group" aria-labelledby={noticeId}>
      <span id={noticeId}>
        {t("readaloud.notice")} {t("handsfree.notice.mic")}
      </span>
      <button
        type="button"
        ref={confirmRef}
        className="hands-free-notice-confirm"
        onClick={handsFree.confirmNotice}
      >
        {t("handsfree.notice.confirm")}
      </button>
      <button type="button" className="hands-free-notice-cancel" onClick={handsFree.cancelNotice}>
        {t("common.button.cancel")}
      </button>
    </div>
  );
}

export function HandsFreeStrip({ handsFree }: { handsFree: UseHandsFree }): ReactElement | null {
  const { state } = handsFree;
  if (state.phase === "off") {
    // Enter refused (dictation_unavailable, no_voice) or an exit mid-loop (every other
    // reason) left nothing on screen — the toggle looked dead. Shown where the strip would be.
    if (state.notice === null) return null;
    return (
      <div className="hands-free-strip" role="status">
        <span>{t(NOTICE_KEY[state.notice])}</span>
        <button type="button" className="hands-free-end" onClick={handsFree.dismissNotice}>
          {t("handsfree.notice.dismiss")}
        </button>
      </div>
    );
  }
  const preparingVoice = state.phase === "thinking" && handsFree.preparing === true;
  return (
    <div className="hands-free-strip" role="status">
      <span>{t(preparingVoice ? "handsfree.phase.preparing" : PHASE_KEY[state.phase])}</span>
      <span className="chip dim">AI voice</span>
      {handsFree.note ? (
        <span className="hands-free-note">{t(NOTE_KEY[handsFree.note])}</span>
      ) : null}
      {state.phase === "speaking" || preparingVoice ? (
        <button type="button" className="hands-free-interrupt" onClick={handsFree.interrupt}>
          {t("handsfree.hint.interrupt")}
        </button>
      ) : null}
      <button type="button" className="hands-free-end" onClick={handsFree.toggle}>
        {t("handsfree.end")}
        <span className="kbd">Esc</span>
      </button>
    </div>
  );
}
