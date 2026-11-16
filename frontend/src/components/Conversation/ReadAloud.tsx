/**
 * Read aloud, the reply's side: the hook `Reply` calls for its controls and notes, and the
 * context that carries the settings. The speaking itself is `voice/readAloud.ts`; what is read and
 * in which language is `voice/readAloudText.ts`.
 *
 * Read prepares before it speaks: every text block is parsed by the same parser the column uses
 * (a cache hit for a reply already on screen), the installed voices are awaited, and only then is
 * a voice chosen. Each await can be overtaken by a stop, another reply's Read or an unmount, so
 * the controller's ticket and a mounted flag are checked before anything is shown or spoken.
 *
 * Notes, the notice and voice names are text nodes. The notice appears before the first Read
 * (`voice.read_aloud_notice_seen`) and says what the feature does with the text.
 */
import {
  createContext,
  type ReactElement,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { Key } from "../../i18n/index";
import { t, tf } from "../../i18n/t";
import { type PlatformKind, platformKind } from "../../platform";
import type { TextBlock, Turn } from "../../state/view";
import { speechOwner } from "../../voice/handsFree";
import { type ReadAloud, readAloud } from "../../voice/readAloud";
import { playVoicebox, prepareReadAloud, readAloudParse } from "../../voice/readAloudPrepare";
import { speakable } from "../../voice/readAloudText";
import { type VoiceboxSettings, voiceboxProblem } from "../../voice/voiceboxApi";
import type { VoiceboxFeed } from "../../voice/voiceboxFeed";
import { installedState, VoicePackContext } from "../../voice/voicePackApi";
import { Spinner } from "../Loading/Loading";
import { MarkdownParserContext } from "./markdownParser";
import "./ReadAloud.css";

export interface ReadAloudSettings {
  controller: ReadAloud;
  rate: number;
  voices: Readonly<Record<string, string>>;
  noticeSeen: boolean;
  voicebox?: VoiceboxSettings | null;
  /** True the first time this code is seen this window (and should be noted); false for a repeat.
   * Absent, every fallback is noted. */
  noteFallback?(code: string): boolean;
  /** Opens Settings › Chat, where a voice pack or Voicebox profile is chosen and installed. */
  openVoiceSettings?(): void;
  acknowledge(): void;
}

/** Without a provider a reply reads aloud with the window's own controller at the defaults. */
export const ReadAloudContext = createContext<ReadAloudSettings>({
  controller: readAloud,
  rate: 1,
  voices: {},
  noticeSeen: false,
  acknowledge: () => {},
});

const STEPS: Readonly<Record<PlatformKind, Key>> = {
  windows: "readaloud.how.windows",
  mac: "readaloud.how.mac",
  linux: "readaloud.how.linux",
};

interface Message {
  text: string;
  steps: string | null;
  /** True for the "no system voice for this language" note, which alone may offer the pack or
   * Windows's own voice settings. */
  isNoVoice?: boolean;
  /** True for the "the system voice read this reply instead" fallback note, which alone offers
   * Voice settings…. */
  isFallback?: boolean;
}

export interface ReadAloudReply {
  controls: ReactElement | null;
  note: ReactElement | null;
}

export function useReadAloudReply(turn: Turn): ReadAloudReply {
  const {
    controller,
    rate,
    voices: chosen,
    noticeSeen,
    voicebox = null,
    noteFallback,
    openVoiceSettings,
    acknowledge,
  } = useContext(ReadAloudContext);
  const parser = useContext(MarkdownParserContext);
  const packServices = useContext(VoicePackContext);
  const owner = useId();
  // Hands-free speaking this reply shows and is steered here as the reply's own Read is.
  const handsFreeOwner = speechOwner(turn.id);
  const status = useSyncExternalStore(controller.subscribe, () => {
    const own = controller.statusOf(owner);
    return own === "idle" ? controller.statusOf(handsFreeOwner) : own;
  });
  const [message, setMessage] = useState<Message | null>(null);
  const [noticeOpen, setNoticeOpen] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [voiceboxPending, setVoiceboxPending] = useState(false);
  // Whether the pack looked cold (stopped/starting) when a pack-engine Read began, for the
  // pending note's wording; unused for the voicebox engine.
  const [packCold, setPackCold] = useState(false);
  // Whether the no-voice note may offer "Install natural voices…" (fetched lazily, see `noVoice`).
  const [packOffer, setPackOffer] = useState(false);
  const mounted = useRef(false);
  const parseOwners = useRef(new Set<object>());
  // This component's own attempt counter: `offerPackInstall`'s fetch outlives the "no_voice" case
  // that starts it, which calls `controller.stopIf` and so bumps the controller's own generation
  // right away — checking `controller.isCurrent` there would always read stale, even with no newer
  // Read. Counting attempts here instead stays accurate regardless of the controller's own state.
  const requestId = useRef(0);
  // The Voicebox feed now playing (or last played) under this owner, so a later "failed" status
  // from it — surfacing only through `status`, not through anything `start` awaits — can be told
  // apart from a system-voice failure and worded with its own problem code.
  const voiceboxFeedRef = useRef<{
    feed: VoiceboxFeed;
    url: string;
    engine: "pack" | "voicebox" | undefined;
  } | null>(null);

  useEffect(() => {
    mounted.current = true;
    const owners = parseOwners.current;
    return () => {
      mounted.current = false;
      controller.stopIf(owner);
      for (const parseOwner of owners) parser?.cancel(parseOwner);
      owners.clear();
    };
  }, [controller, owner, parser]);

  // The notice takes focus when it opens: a first Read that only showed a quiet line read as
  // "nothing happened".
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (noticeOpen) confirmRef.current?.focus();
  }, [noticeOpen]);

  const textBlocks = turn.blocks.filter((block): block is TextBlock => block.kind === "text");
  if (!controller.supported || turn.status === "running" || textBlocks.length === 0) {
    return { controls: null, note: null };
  }

  const parse = readAloudParse(parser, parseOwners.current);

  function noVoice(lang: string, installed: number): Message {
    const language = new Intl.DisplayNames(["en"], { type: "language" }).of(lang);
    return {
      text:
        installed === 0
          ? t("readaloud.no_voices")
          : tf("readaloud.no_voice", { language: language ?? lang }),
      steps: t(STEPS[platformKind()]),
      isNoVoice: true,
    };
  }

  // Whether the pack is worth offering from a no-voice note: not for the pack engine itself (it
  // already covers what it can), not without a pack to ask, and not when installing is pointless
  // (already installed in some form, or ruled out for this platform). Read lazily, right when a
  // no-voice note appears, so an idle reply never probes the pack; a failed read just leaves the
  // button off. Guarded by `requestId`, so a slow reply from a Read the user already left never
  // overwrites a newer one's note.
  function offerPackInstall(request: number): void {
    if (voicebox?.engine === "pack" || packServices === null) return;
    packServices.api
      .status()
      .then((packStatus) => {
        if (!mounted.current || requestId.current !== request) return;
        setPackOffer(
          !installedState(packStatus.state) &&
            !(packStatus.state === "unsupported" && packStatus.reason === "platform"),
        );
      })
      .catch(() => undefined);
  }

  async function start(): Promise<void> {
    const ticket = controller.begin(owner);
    requestId.current += 1;
    const request = requestId.current;
    setMessage(null);
    setPreparing(true);
    setPackOffer(false);
    voiceboxFeedRef.current = null;
    try {
      const outcome = await prepareReadAloud(
        textBlocks,
        parse,
        controller,
        chosen,
        speakable,
        voicebox !== null,
        voicebox?.languages,
      );
      // Stopped, overtaken by another reply's Read, or unmounted while preparing: say nothing.
      if (!mounted.current || !controller.isCurrent(ticket)) return;
      switch (outcome.kind) {
        case "unreadable":
          controller.stopIf(owner);
          setMessage({ text: t("readaloud.unreadable"), steps: null });
          return;
        case "nothing":
          controller.stopIf(owner);
          setMessage({ text: t("readaloud.nothing"), steps: null });
          return;
        case "no_voice":
          controller.stopIf(owner);
          setMessage(noVoice(outcome.lang, outcome.installed));
          offerPackInstall(request);
          return;
        case "speak": {
          const { speech } = outcome;
          if (!controller.play(ticket, owner, speech.chunks, speech.voice, rate)) {
            controller.stopIf(owner);
            setMessage(noVoice(speech.lang, speech.installed));
            offerPackInstall(request);
          }
          return;
        }
        case "voicebox": {
          if (voicebox === null) return;
          // A cold pack (not yet warmed up) takes much longer for its first reading, so the
          // pending note below says so instead of reading like a stall. Ignoring failure here
          // just keeps the ordinary "Preparing speech…" wording.
          let cold = false;
          if (voicebox.engine === "pack" && packServices !== null) {
            cold = await packServices.api
              .status()
              .then(
                (packStatus) => packStatus.state === "stopped" || packStatus.state === "starting",
              )
              .catch(() => false);
          }
          if (!mounted.current || !controller.isCurrent(ticket)) return;
          setPackCold(cold);
          setVoiceboxPending(true);
          try {
            const result = await playVoicebox(
              controller,
              ticket,
              owner,
              outcome.speech,
              voicebox.api,
              rate,
            );
            // Stopped (surfaces as "stale" here, never a fallback) or unmounted meanwhile: nothing.
            if (!mounted.current || result.result === "stale") return;
            if (result.result === "playing") {
              voiceboxFeedRef.current = {
                feed: result.feed,
                url: voicebox.url,
                engine: voicebox.engine,
              };
              return;
            }
            const code = result.code ?? "unavailable";
            if (result.result === "fallback") {
              if (noteFallback === undefined || noteFallback(code)) {
                setMessage({
                  text: `${voiceboxProblem(code, voicebox.url, voicebox.engine)} The system voice read this reply instead.`,
                  steps: null,
                  isFallback: true,
                });
              }
              return;
            }
            controller.stopIf(owner);
            setMessage({ text: voiceboxProblem(code, voicebox.url, voicebox.engine), steps: null });
          } finally {
            if (mounted.current) setVoiceboxPending(false);
          }
        }
      }
    } finally {
      if (mounted.current) setPreparing(false);
    }
  }

  function read(): void {
    if (!noticeSeen) {
      setNoticeOpen(true);
      return;
    }
    void start();
  }

  function confirmNotice(): void {
    acknowledge();
    setNoticeOpen(false);
    void start();
  }

  const labelledButton = (key: "read" | "pause" | "resume" | "stop", onClick: () => void) => (
    <button
      type="button"
      className="read-aloud-btn"
      aria-label={t(`readaloud.${key}.label`)}
      aria-busy={key === "read" && preparing ? true : undefined}
      onClick={onClick}
    >
      {t(`readaloud.${key}`)}
    </button>
  );
  const stop = () => {
    controller.stopIf(owner);
    controller.stopIf(handsFreeOwner);
  };
  const controls = (
    <span className="read-aloud">
      {status === "speaking" || status === "paused" ? (
        <span className="chip dim">AI voice</span>
      ) : null}
      {status === "speaking" ? labelledButton("pause", () => controller.pause()) : null}
      {status === "paused" ? labelledButton("resume", () => controller.resume()) : null}
      {status === "speaking" || status === "paused" || voiceboxPending
        ? labelledButton("stop", stop)
        : labelledButton("read", read)}
    </span>
  );

  const closeButton = (onClick: () => void) => (
    <button type="button" className="read-aloud-btn" onClick={onClick}>
      {t("common.button.close")}
    </button>
  );
  let note: ReactElement | null = null;
  if (noticeOpen) {
    const noticeId = `${owner}-notice`;
    const noticeText =
      platformKind() === "linux"
        ? `${t("readaloud.notice")} ${t("readaloud.notice.linux")}`
        : t("readaloud.notice");
    // English literals: the speech engines have no notice copy of their own yet.
    const voiceboxNotice =
      voicebox === null
        ? ""
        : voicebox.engine === "pack"
          ? " With natural voices chosen, replies in the languages they speak go to VIKI's natural-voice server on this computer instead."
          : " With a Voicebox voice chosen, replies in the languages Voicebox speaks go to the Voicebox app on this computer instead.";
    note = (
      // biome-ignore lint/a11y/useSemanticElements: a fieldset brings a border, padding and legend this note does not want
      <div className="read-aloud-note" role="group" aria-labelledby={noticeId}>
        <span id={noticeId}>
          {noticeText}
          {voiceboxNotice}
        </span>
        <button
          type="button"
          ref={confirmRef}
          className="read-aloud-btn read-aloud-confirm"
          onClick={confirmNotice}
        >
          {t("readaloud.notice.confirm")}
        </button>
        <button type="button" className="read-aloud-btn" onClick={() => setNoticeOpen(false)}>
          {t("common.button.cancel")}
        </button>
      </div>
    );
  } else if (voiceboxPending) {
    // English literal: pack and Voicebox each get their own pending wording.
    const pendingText =
      voicebox?.engine === "pack"
        ? packCold
          ? "Starting the natural voice… The first reading takes longer."
          : "Preparing speech…"
        : "Preparing speech in Voicebox…";
    note = (
      <div className="read-aloud-note" role="status">
        <Spinner className="spinner-late" />
        <span>{pendingText}</span>
      </div>
    );
  } else if (status === "failed") {
    const activeFeed = voiceboxFeedRef.current;
    const failedText =
      activeFeed !== null
        ? voiceboxProblem(
            activeFeed.feed.failure() ?? "unavailable",
            activeFeed.url,
            activeFeed.engine,
          )
        : t("readaloud.failed");
    note = (
      <div className="read-aloud-note" role="status">
        <span>{failedText}</span>
        {closeButton(stop)}
      </div>
    );
  } else if (message !== null) {
    // English literals: both are hand-offs to Settings/the OS, not installs themselves.
    const installPack = message.isNoVoice && packOffer;
    const windowsVoiceSettings =
      message.isNoVoice && packServices !== null && platformKind() === "windows";
    note = (
      <div className="read-aloud-note" role="status">
        <span>{message.text}</span>
        {message.steps === null ? null : <span>{` ${message.steps}`}</span>}
        {installPack ? (
          <button type="button" className="read-aloud-btn" onClick={() => openVoiceSettings?.()}>
            Install natural voices…
          </button>
        ) : null}
        {windowsVoiceSettings ? (
          <button
            type="button"
            className="read-aloud-btn"
            onClick={() => {
              void packServices?.api.openSpeechSettings().catch(() => undefined);
            }}
          >
            Open Windows voice settings
          </button>
        ) : null}
        {message.isFallback ? (
          <button type="button" className="read-aloud-btn" onClick={() => openVoiceSettings?.()}>
            Voice settings…
          </button>
        ) : null}
        {closeButton(() => setMessage(null))}
      </div>
    );
  }
  return { controls, note };
}
