/**
 * The Settings control for `voice.speech_engine`: a vertical list of three
 * options (system voices, natural voices, the user's own Voicebox), each one row of radio + label
 * text with a one-line description under it. The voice pack's own install/health card sits under
 * the Natural voices option, indented to line up with its text, inside a quiet bordered panel
 * (`.voice-pack-card`) driven by `VoicePackContext`. With no `services` (no provider and no
 * override), the pack card is replaced by a notice but the radios still render, so the user can
 * still pick system or Voicebox voices. Read-only follows `VoiceboxField`: the radios stay
 * focusable with `aria-disabled` and name the notice, and a pick is dropped rather than sent.
 * While the pack is `unsupported`, the Natural voices radio alone also gets `aria-disabled`,
 * describing the card's own reason sentence, and a pick of "pack" is dropped the same way — a
 * stored "pack" value still shows checked, since `checked` never depends on `aria-disabled`.
 * English literals only.
 *
 * The pack card never contacts anything on its own until the user opts in by reaching
 * "Install"; the mount probe only ever reads/triggers a local hardware+install check
 * (`status`/`check`), never a Voicebox call — that gate lives in `VoiceboxField` itself.
 */

import {
  type ReactElement,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { platformKind } from "../../platform";
import {
  type PackAction,
  type PackState,
  type SpeechEngine,
  VOICEBOX_RELEASES_URL,
  VoicePackContext,
  VoicePackError,
  type VoicePackProgress,
  type VoicePackServices,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { Loading } from "../Loading/Loading";
import { RollingNumber } from "../Loading/RollingNumber";
import "./VoiceEngineField.css";

export interface VoiceEngineFieldProps {
  value: SpeechEngine;
  labelId: string;
  readOnlyNoticeId: string | null;
  onChange: (next: SpeechEngine) => void;
  services?: VoicePackServices | null;
}

const POLL_STATES: ReadonlySet<PackState> = new Set([
  "checking",
  "installing",
  "removing",
  "starting",
]);

const VOICEBOX_LICENSE_URL = "https://github.com/jamiepine/voicebox/blob/v0.5.0/LICENSE";
const NVIDIA_EULA_URL = "https://docs.nvidia.com/cuda/eula/index.html";
const QWEN_MODEL_URL = "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice";
const KOKORO_MODEL_URL = "https://huggingface.co/hexgrad/Kokoro-82M";

/** Decimal GB (1e9 bytes) to one decimal, the unit every size in this card uses. */
function gb(bytes: number): string {
  return (bytes / 1e9).toFixed(1);
}

function errorCode(error: unknown): string {
  return error instanceof VoicePackError ? error.code : "unavailable";
}

/** The disk-use line installed states add: "Uses {n} GB on disk" plus the GPU name, when one was
 * probed (a GET never re-probes it, so it is whatever the last check or install saw). */
function diskLine(st: VoicePackStatus): string {
  const used = `Uses ${gb(st.diskBytes)} GB on disk`;
  return st.gpu === null ? used : `${used} · ${st.gpu.name}`;
}

/** 0-100, unrounded; 0 while total is not yet known. */
function percentOf(progress: VoicePackProgress): number {
  return progress.total > 0 ? (progress.done / progress.total) * 100 : 0;
}

/** The step's first observed point, for `timeLeftSuffix` below to extrapolate from. */
interface ProgressSample {
  step: VoicePackProgress["step"];
  at: number;
  pct: number;
}

const TIME_LEFT_DELAY_MS = 3000;
const TIME_LEFT_MIN_PCT = 1;

/** Pure: the sample to keep for this step's time-left math, given the live progress (or null
 * while unknown) and the previous one. A step change (or progress disappearing) starts a fresh
 * sample; otherwise the first one already seen for this step is kept, unchanged. */
function nextSample(
  progress: VoicePackProgress | null,
  prev: ProgressSample | null,
  now: number,
): ProgressSample | null {
  if (progress === null) return null;
  if (prev !== null && prev.step === progress.step) return prev;
  return { step: progress.step, at: now, pct: percentOf(progress) };
}

/** Pure: " · about N min left" / " · less than a minute left", extrapolated from `sample` (this
 * step's first point) to `progress` at `now` at a steady rate; "" until that gap is at least 3 s
 * and 1 percentage point. */
function timeLeftSuffix(progress: VoicePackProgress, sample: ProgressSample, now: number): string {
  const elapsed = now - sample.at;
  const pct = percentOf(progress);
  const delta = pct - sample.pct;
  if (elapsed < TIME_LEFT_DELAY_MS || delta < TIME_LEFT_MIN_PCT) return "";
  const minutes = Math.round(((100 - pct) * elapsed) / delta / 60_000);
  return minutes < 1 ? " · less than a minute left" : ` · about ${minutes} min left`;
}

/** `failed`'s plain-word sentence for the pack server's own codes (src/viki_agent/voice/voice_pack_server.py); any
 * other code keeps the raw-code fallback, which still gives the user something to search. */
function failedSentence(code: string | null): string {
  if (code === "start_timeout") return "Natural voices took too long to start.";
  if (code === "crashed") return "Natural voices stopped unexpectedly.";
  if (code === "missing_exe") return "Some natural-voice files are missing. Repair restores them.";
  if (code === "port_taken")
    return "Another program took the port natural voices were starting on.";
  return `Natural voices stopped working (${code}).`;
}

/** The action error line's plain words for the voice-pack facade's own refusals (src/viki_agent/voice/voice_api.py);
 * any other code keeps the raw-code fallback. */
function actionErrorSentence(code: string): string {
  if (code === "pack_busy")
    return "Natural voices are busy with another change. Try again in a moment.";
  if (code === "pack_unsupported") return "This PC can't run natural voices.";
  return `That didn't work (${code}). Try again.`;
}

export function VoiceEngineField({
  value,
  labelId,
  readOnlyNoticeId,
  onChange,
  services: servicesProp,
}: VoiceEngineFieldProps): ReactElement {
  const contextServices = useContext(VoicePackContext);
  const services = servicesProp ?? contextServices;
  const base = useId();
  const readOnly = readOnlyNoticeId !== null;
  const packReasonId = `${base}-pack-reason`;
  const [status, setStatus] = useState<VoicePackStatus | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const unsupported = status !== null && status.state === "unsupported";

  // runAction's own actions (install/cancel/resume/repair/remove/check) run outside any effect,
  // so a rejection's status-reread backstop (below) needs its own liveness flag rather than an
  // effect's local `live` closure — the same signal, just not scoped to one effect's cleanup.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Every fresh status from the server also closes a stale confirm prompt: once the pack has
  // actually moved (or re-confirmed where it is), whatever the user was about to confirm no
  // longer applies. A stable identity (empty deps: it only closes over the two setters, which
  // React guarantees never change) so the effects below can depend on it without re-running
  // on every render.
  const applyStatus = useCallback((next: VoicePackStatus) => {
    setStatus(next);
    setConfirmRemove(false);
  }, []);

  // installingCard's time-left sample: computed fresh every render (useHeld's own pattern) so a
  // step's first point is available to this same render rather than one render late, and pushed
  // into state only when it actually changes (a step switch), never on every progress poll.
  const [sampleState, setSampleState] = useState<ProgressSample | null>(null);
  const sample = nextSample(status?.progress ?? null, sampleState, Date.now());
  if (sample !== sampleState) setSampleState(sample);

  // Mount probe: read status once, then check once if nothing has ever run.
  useEffect(() => {
    if (!services) return;
    let live = true;
    const controller = new AbortController();
    services.api.status(controller.signal).then(
      (next) => {
        if (!live) return;
        applyStatus(next);
        if (next.state === "unchecked") {
          services.api.act("check").then(
            (after) => {
              if (live) applyStatus(after);
            },
            (error) => {
              if (live) setActionError(errorCode(error));
            },
          );
        }
      },
      () => {},
    );
    return () => {
      live = false;
      controller.abort();
    };
  }, [services, applyStatus]);

  // While the pack is mid-transition, re-read status every second; a fresh status restarts the
  // interval (a no-op when the state is unchanged), and unmount (or leaving the poll states)
  // aborts whatever call was still in flight.
  useEffect(() => {
    if (!services || status === null || !POLL_STATES.has(status.state)) return;
    let live = true;
    const controller = new AbortController();
    const id = setInterval(() => {
      services.api.status(controller.signal).then(
        (next) => {
          if (live) applyStatus(next);
        },
        () => {},
      );
    }, 1000);
    return () => {
      live = false;
      clearInterval(id);
      controller.abort();
    };
  }, [services, status, applyStatus]);

  // Remove can take ~120 s (deleting the install) and check up to 15 s (nvidia-smi); either
  // can outlive act()'s own client budget even though the server is still working. On a rejection
  // this re-reads status once as a backstop: a poll state (another window's own action, or the
  // server still finishing this one) means work is still running, so the stale error clears and
  // the poll effect above takes over; any other state, or a re-read that itself fails, leaves the
  // error line up.
  function runAction(action: PackAction): void {
    if (!services) return;
    setActionError(null);
    services.api.act(action).then(
      (next) => applyStatus(next),
      (error) => {
        if (!mounted.current) return;
        setActionError(errorCode(error));
        services.api.status().then(
          (after) => {
            if (!mounted.current) return;
            applyStatus(after);
            if (POLL_STATES.has(after.state)) setActionError(null);
          },
          () => {},
        );
      },
    );
  }

  function install(): void {
    if (!readOnly) onChange("pack");
    runAction("install");
  }

  function removeNow(): void {
    setConfirmRemove(false);
    runAction("remove");
  }

  function addWindowsVoice(): void {
    services?.api.openSpeechSettings().catch(() => {});
  }

  function pick(next: SpeechEngine): void {
    if (readOnly) return;
    if (next === "pack" && unsupported) return;
    onChange(next);
  }

  /** A licence name rendered as a real link, the way chat Markdown renders one
   * (components/Conversation/Markdown.tsx `markdownComponents.a`): a middle click opens it too,
   * since a plain click never fires onAuxClick. */
  function licenceLink(url: string, label: string): ReactElement {
    return (
      <a
        href={url}
        className="voice-pack-link"
        rel="noreferrer noopener"
        onClick={(event) => {
          event.preventDefault();
          services?.openUrl(url);
        }}
        onAuxClick={(event) => {
          if (event.button !== 1) return;
          event.preventDefault();
          services?.openUrl(url);
        }}
      >
        {label}
      </a>
    );
  }

  const removeButton = (
    <button type="button" className="btn alt sm" onClick={() => setConfirmRemove(true)}>
      Remove
    </button>
  );

  function unsupportedCard(st: VoicePackStatus): ReactElement {
    const addVoiceButton = platformKind() === "windows" && (
      <div className="voice-pack-actions">
        <button type="button" className="btn alt sm" onClick={addWindowsVoice}>
          Add a Windows voice…
        </button>
      </div>
    );
    const checkAgainButton = (
      <div className="voice-pack-actions">
        <button type="button" className="btn alt sm" onClick={() => runAction("check")}>
          Check again
        </button>
      </div>
    );
    if (st.reason === "platform")
      return (
        <p role="status" id={packReasonId} className="voice-pack-status">
          Natural voices need Windows on a PC with an NVIDIA graphics card.
        </p>
      );
    if (st.reason === "no_nvidia")
      return (
        <>
          <p role="status" id={packReasonId} className="voice-pack-status">
            VIKI found no NVIDIA graphics driver on this PC, so natural voices can't run here.
          </p>
          {addVoiceButton}
        </>
      );
    if (st.reason === "driver_old")
      return (
        <>
          <p role="status" id={packReasonId} className="voice-pack-status">
            The NVIDIA driver is too old for natural voices; they need driver 570.65 or later.
          </p>
          {checkAgainButton}
        </>
      );
    if (st.reason === "low_vram") {
      const memoryGb = ((st.gpu?.memoryMib ?? 0) / 1024).toFixed(1);
      return (
        <>
          <p role="status" id={packReasonId} className="voice-pack-status">
            {`This graphics card has ${memoryGb} GB of memory; natural voices need 4 GB.`}
          </p>
          {addVoiceButton}
        </>
      );
    }
    // probe_failed, or defensively a null reason.
    return (
      <>
        <p role="status" id={packReasonId} className="voice-pack-status">
          VIKI couldn't check the graphics card.
        </p>
        {checkAgainButton}
      </>
    );
  }

  function notInstalledCard(st: VoicePackStatus): ReactElement {
    const free = st.freeBytes === null ? null : gb(st.freeBytes);
    const sentence =
      free === null
        ? `Natural voices aren't installed. The download is ${gb(st.downloadBytes)} GB and needs ${gb(st.needBytes)} GB free.`
        : `Natural voices aren't installed. The download is ${gb(st.downloadBytes)} GB and needs ${gb(st.needBytes)} GB free (${free} GB free now).`;
    const tooSmall = st.freeBytes !== null && st.freeBytes < st.needBytes;
    return (
      <>
        <p role="status" className="voice-pack-status">
          {sentence}
        </p>
        <p className="voice-pack-sub">
          {"Includes the "}
          {licenceLink(VOICEBOX_LICENSE_URL, "Voicebox server 0.5.0")}
          {" (MIT), "}
          {licenceLink(NVIDIA_EULA_URL, "NVIDIA CUDA libraries")}
          {" (NVIDIA licence), the "}
          {licenceLink(QWEN_MODEL_URL, "Qwen3-TTS 0.6B CustomVoice model")}
          {" (Apache-2.0) and the "}
          {licenceLink(KOKORO_MODEL_URL, "Kokoro-82M model")}
          {" (Apache-2.0)."}
        </p>
        <div className="voice-pack-actions">
          <button type="button" className="btn go sm" onClick={install} disabled={tooSmall}>
            Install natural voices
          </button>
        </div>
        {tooSmall && <p className="voice-pack-sub">Free up space first.</p>}
      </>
    );
  }

  function installingCard(st: VoicePackStatus): ReactElement {
    const progress = st.progress;
    const textId = `${base}-progress-text`;
    const label =
      progress === null
        ? "Installing…"
        : progress.step === "unpack"
          ? `Unpacking… ${Math.round(percentOf(progress))}%`
          : `${progress.step === "server" ? "Downloading the voice server" : "Downloading the voice model"}… ${gb(progress.done)} of ${gb(progress.total)} GB`;
    const timeLeft =
      progress !== null && sample !== null ? timeLeftSuffix(progress, sample, Date.now()) : "";
    return (
      <>
        <div className="voice-pack-progress-head">
          <p role="status" id={textId} className="voice-pack-status">
            {label}
            {timeLeft}
          </p>
          {progress !== null && (
            <span className="voice-pack-pct mono roll-host" aria-hidden="true">
              <RollingNumber value={`${Math.round(percentOf(progress))}%`} />
            </span>
          )}
        </div>
        {progress !== null ? (
          <progress
            className="voice-pack-progress"
            value={progress.done}
            max={progress.total}
            aria-labelledby={textId}
          />
        ) : (
          <div className="voice-pack-progress-indet" role="progressbar" aria-labelledby={textId} />
        )}
        <div className="voice-pack-actions">
          <button type="button" className="btn alt sm" onClick={() => runAction("cancel")}>
            Cancel
          </button>
        </div>
      </>
    );
  }

  function installFailedCard(st: VoicePackStatus): ReactElement {
    const resumeButton = (
      <button type="button" className="btn go sm" onClick={() => runAction("install")}>
        Resume
      </button>
    );
    const tryAgainButton = (
      <button type="button" className="btn go sm" onClick={() => runAction("install")}>
        Try again
      </button>
    );
    if (st.error === "cancelled")
      return (
        <>
          <p role="status" className="voice-pack-status">
            The download is paused. It continues where it stopped.
          </p>
          <div className="voice-pack-actions">
            {resumeButton}
            {removeButton}
          </div>
        </>
      );
    if (st.error === "network")
      return (
        <>
          <p role="status" className="voice-pack-status">
            The download stopped because the network failed.
          </p>
          <div className="voice-pack-actions">{resumeButton}</div>
        </>
      );
    if (st.error === "disk_full")
      return (
        <>
          <p role="status" className="voice-pack-status">
            The disk is full. Free up space, then resume.
          </p>
          <div className="voice-pack-actions">{resumeButton}</div>
        </>
      );
    if (
      st.error === "hash_mismatch" ||
      st.error === "size_mismatch" ||
      st.error === "bad_range" ||
      st.error === "bad_archive"
    )
      return (
        <>
          <p role="status" className="voice-pack-status">
            A downloaded file didn't match VIKI's pinned copy, so it was deleted.
          </p>
          <div className="voice-pack-actions">{tryAgainButton}</div>
        </>
      );
    if (st.error === "remove_failed")
      return (
        <>
          <p role="status" className="voice-pack-status">
            {
              "VIKI couldn't remove every natural-voice file. Close any program using them, then select Remove again."
            }
          </p>
          <div className="voice-pack-actions">{removeButton}</div>
        </>
      );
    if (st.error === "write_failed" || st.error === "not_regular")
      return (
        <>
          <p role="status" className="voice-pack-status">
            VIKI couldn't write the voice files to disk.
          </p>
          <div className="voice-pack-actions">
            {tryAgainButton}
            {removeButton}
          </div>
        </>
      );
    if (st.error === "private_folder")
      return (
        <>
          <p role="status" className="voice-pack-status">
            VIKI couldn't make its private voice folder.
          </p>
          <div className="voice-pack-actions">
            {tryAgainButton}
            {removeButton}
          </div>
        </>
      );
    if (st.error === "setup_failed" || st.error === "protocol_error")
      return (
        <>
          <p role="status" className="voice-pack-status">
            The installer stopped unexpectedly.
          </p>
          <div className="voice-pack-actions">
            {tryAgainButton}
            {removeButton}
          </div>
        </>
      );
    return (
      <>
        <p
          role="status"
          className="voice-pack-status"
        >{`VIKI couldn't finish installing natural voices (${st.error}).`}</p>
        <div className="voice-pack-actions">
          {tryAgainButton}
          {removeButton}
        </div>
      </>
    );
  }

  function failedCard(st: VoicePackStatus): ReactElement {
    return (
      <>
        <p role="status" className="voice-pack-status">
          {failedSentence(st.error)}
        </p>
        <div className="voice-pack-actions">
          <button type="button" className="btn go sm" onClick={() => runAction("retry")}>
            Try again
          </button>
          <button type="button" className="btn alt sm" onClick={() => runAction("repair")}>
            Repair
          </button>
          {removeButton}
        </div>
      </>
    );
  }

  function packCard(): ReactElement {
    if (!services)
      return <p className="voice-pack-sub">Natural voices aren't available in this window.</p>;

    const errorLine = actionError !== null && (
      <p className="settings-error" role="alert">
        {actionErrorSentence(actionError)}
      </p>
    );

    if (confirmRemove && status !== null)
      return (
        <>
          {errorLine}
          <p role="status" className="voice-pack-status">
            {`Remove natural voices? Installing them again downloads ${gb(status.downloadBytes)} GB.`}
          </p>
          <div className="voice-pack-actions">
            <button type="button" className="btn alt sm" onClick={removeNow}>
              Remove
            </button>
            <button type="button" className="btn txt sm" onClick={() => setConfirmRemove(false)}>
              Keep
            </button>
          </div>
        </>
      );

    if (status === null || status.state === "unchecked" || status.state === "checking")
      return (
        <>
          {errorLine}
          <Loading className="voice-pack-status">Checking this computer…</Loading>
        </>
      );
    if (status.state === "unsupported")
      return (
        <>
          {errorLine}
          {unsupportedCard(status)}
        </>
      );
    if (status.state === "not_installed")
      return (
        <>
          {errorLine}
          {notInstalledCard(status)}
        </>
      );
    if (status.state === "installing")
      return (
        <>
          {errorLine}
          {installingCard(status)}
        </>
      );
    if (status.state === "install_failed")
      return (
        <>
          {errorLine}
          {installFailedCard(status)}
        </>
      );
    if (status.state === "stopped")
      return (
        <>
          {errorLine}
          <p role="status" className="voice-pack-status">
            Natural voices are installed. They start when VIKI reads a reply.
          </p>
          <p className="voice-pack-sub">{diskLine(status)}</p>
          <div className="voice-pack-actions">{removeButton}</div>
        </>
      );
    if (status.state === "starting")
      return (
        <>
          {errorLine}
          <p role="status" className="voice-pack-status">
            Natural voices are starting…
          </p>
        </>
      );
    if (status.state === "ready")
      return (
        <>
          {errorLine}
          <p role="status" className="voice-pack-status">
            Natural voices are ready.
          </p>
          <p className="voice-pack-sub">{diskLine(status)}</p>
          <div className="voice-pack-actions">{removeButton}</div>
        </>
      );
    if (status.state === "failed")
      return (
        <>
          {errorLine}
          {failedCard(status)}
        </>
      );
    // removing
    return (
      <>
        {errorLine}
        <p role="status" className="voice-pack-status">
          Removing natural voices…
        </p>
      </>
    );
  }

  return (
    <div role="radiogroup" aria-labelledby={labelId} className="voice-engine-options">
      <div className="voice-engine-option">
        <label>
          <input
            type="radio"
            name={`${base}-engine`}
            checked={value === "system"}
            aria-disabled={readOnly || undefined}
            aria-describedby={readOnlyNoticeId ?? undefined}
            onChange={() => pick("system")}
          />
          System voices
        </label>
        <p className="voice-engine-option-desc">This computer's own voices. Always available.</p>
      </div>
      <div className="voice-engine-option">
        <label>
          <input
            type="radio"
            name={`${base}-engine`}
            checked={value === "pack"}
            aria-disabled={readOnly || unsupported || undefined}
            aria-describedby={readOnlyNoticeId ?? (unsupported ? packReasonId : undefined)}
            onChange={() => pick("pack")}
          />
          Natural voices
        </label>
        <p className="voice-engine-option-desc">
          Voices VIKI runs on this PC's NVIDIA graphics card.
        </p>
        <div className="voice-pack-card">{packCard()}</div>
      </div>
      <div className="voice-engine-option">
        <label>
          <input
            type="radio"
            name={`${base}-engine`}
            checked={value === "voicebox"}
            aria-disabled={readOnly || undefined}
            aria-describedby={readOnlyNoticeId ?? undefined}
            onChange={() => pick("voicebox")}
          />
          My own Voicebox app (advanced)
        </label>
        <p className="voice-engine-option-desc">
          Uses the Voicebox app you install and run yourself.
        </p>
        {services && (
          <button
            type="button"
            className="btn alt sm voice-engine-option-action"
            onClick={() => services?.openUrl(VOICEBOX_RELEASES_URL)}
          >
            Get Voicebox
          </button>
        )}
      </div>
    </div>
  );
}
