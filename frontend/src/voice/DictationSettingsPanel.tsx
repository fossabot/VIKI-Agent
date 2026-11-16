/**
 * Settings → Chat's dictation section: what the engine would do now, read from
 * `GET /api/v2/voice`, plus the microphone picker. The three `voice.*` keys are ordinary
 * `SettingRow`s; `voiceOptions` gives those rows the options that are installed.
 */

import { type ReactElement, useCallback, useEffect, useState } from "react";
import {
  type DictationApi,
  MICROPHONE_KEY,
  type Microphone,
  type VoiceStatus,
} from "./dictationTypes";

const SETUP = "uv run python scripts/voice_setup.py";
const CPU_SETUP = `${SETUP} --model small-q8_0 --backend cpu`;

const WORDS = {
  title: "Dictation",
  unread: "The dictation state could not be read.",
  ready: "Ready",
  setup: "Speech files are not installed. Run:",
  backend: "The files for the selected CPU or GPU are missing. Run:",
  engine: "Speech recognition is not available on this computer.",
  fallback: "Running on the CPU because the GPU failed:",
  retry: "Try GPU again",
  cpuModel: "No small model is installed for the CPU. To add one, run:",
  slow: "On the laptop used for measurement, this model takes about 15 seconds per update on the CPU.",
  unavailableLanguage: "The saved language is not available for this model, so Auto is used.",
  language: "Dictation language:",
  auto: "Auto",
  privacy: "Audio stays on this computer. Text you send goes to your chat provider.",
  microphone: "Microphone",
  systemDefault: "System default",
  microphoneN: "Microphone",
};

/** The status of `GET /api/v2/voice` while `enabled`, refetched by `refresh`, replaced by a
 * `retryGpu` reply. No `api` (no provider) reads nothing. */
export function useDictationStatus(
  api: DictationApi | null | undefined,
  enabled: boolean,
): {
  status: VoiceStatus | null;
  error: boolean;
  refresh(): void;
  retryGpu(): void;
} {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [error, setError] = useState(false);
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!api || !enabled) return;
    const abort = new AbortController();
    void round;
    api.status(abort.signal).then(
      (next) => {
        setStatus(next);
        setError(false);
      },
      () => {
        if (!abort.signal.aborted) setError(true);
      },
    );
    return () => abort.abort();
  }, [api, enabled, round]);
  const refresh = useCallback(() => setRound((n) => n + 1), []);
  const retryGpu = useCallback(() => {
    api?.clearFallback().then(
      (next) => {
        setStatus(next);
        setError(false);
      },
      () => setError(true),
    );
  }, [api]);
  return { status, error, refresh, retryGpu };
}

/** The options a `voice.*` row lists: what is installed, so a choice cannot fail `setup_needed`.
 * `undefined` leaves the row on the key's own `allowed`. */
export function voiceOptions(
  status: VoiceStatus | null,
  key: string,
): readonly string[] | undefined {
  if (status === null) return undefined;
  if (key === "voice.model") return ["auto", ...status.models];
  if (key === "voice.backend") return ["auto", ...status.backends];
  if (key === "voice.dictation_language") return status.languages;
  return undefined;
}

function stateLine(status: VoiceStatus): ReactElement {
  if (status.state === "ready")
    return (
      <>
        {WORDS.ready}: {[status.model, status.backend, status.device].filter(Boolean).join(", ")}
      </>
    );
  if (status.state === "engine_unavailable") return <>{WORDS.engine}</>;
  return (
    <>
      {status.state === "setup_needed" ? WORDS.setup : WORDS.backend} <code>{SETUP}</code>
    </>
  );
}

function languageName(code: string): string {
  if (code === "auto") return WORDS.auto;
  return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
}

const UNSET = "";

export function DictationSettingsPanel({
  api,
  status,
  error,
  onRetryGpu,
}: {
  api: DictationApi;
  status: VoiceStatus | null;
  error: boolean;
  onRetryGpu(): void;
}): ReactElement {
  const [mics, setMics] = useState<readonly Microphone[]>([]);
  const [chosen, setChosen] = useState(() => localStorage.getItem(MICROPHONE_KEY) ?? UNSET);
  useEffect(() => {
    let live = true;
    api.microphones().then(
      (list) => {
        if (live) setMics(list);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [api]);
  function choose(id: string): void {
    if (id === UNSET) localStorage.removeItem(MICROPHONE_KEY);
    else localStorage.setItem(MICROPHONE_KEY, id);
    setChosen(id);
  }
  return (
    <div className="settings-row" data-setting-key="voice.status">
      <div className="settings-row-label">
        <h3>{WORDS.title}</h3>
      </div>
      <div className="settings-row-control">
        {status === null ? (
          error && <p className="settings-error">{WORDS.unread}</p>
        ) : (
          <>
            <p>{stateLine(status)}</p>
            {status.fallback !== null && (
              <p>
                {WORDS.fallback} {status.fallback.code}{" "}
                <button type="button" className="btn txt sm" onClick={onRetryGpu}>
                  {WORDS.retry}
                </button>
              </p>
            )}
            {status.backend !== "cpu" && status.cpu_model === null && (
              <p>
                {WORDS.cpuModel} <code>{CPU_SETUP}</code>
              </p>
            )}
            {status.slow_on_cpu && <p>{WORDS.slow}</p>}
            <p>
              {status.language_unavailable
                ? WORDS.unavailableLanguage
                : `${WORDS.language} ${languageName(status.language)}`}
            </p>
          </>
        )}
        <p>{WORDS.privacy}</p>
        <label>
          {WORDS.microphone}{" "}
          <select
            className="settings-select"
            value={chosen}
            onChange={(event) => choose(event.target.value)}
          >
            <option value={UNSET}>{WORDS.systemDefault}</option>
            {mics.map((mic, index) => (
              <option key={mic.deviceId} value={mic.deviceId}>
                {mic.label || `${WORDS.microphoneN} ${index + 1}`}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}
