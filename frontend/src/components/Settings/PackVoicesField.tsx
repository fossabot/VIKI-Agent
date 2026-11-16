/**
 * The Settings control for `voice.pack_voices`: one row per language the voice pack speaks
 * (`PACK_LANGUAGES`, filtered from `READ_ALOUD_LANGUAGES` for their display order), each a select
 * offering Kokoro's voices (labelled "(not downloaded)" while Kokoro is missing), all nine Qwen3
 * presets, and the system voice. `PackVoiceNotes` (the pack's own health notes) sits above the
 * fieldset, sharing this window's one `usePackStatus` poll. A pick of the default (empty map)
 * deletes the language from the map; any other pick stores it; both go through `onChange` as
 * `serializeVoiceMap` does for `voice.read_aloud_voices`. Read-only follows `ReadAloudVoicesField`:
 * selects stay focusable with `aria-disabled` and a change is dropped rather than sent.
 *
 * Each row's Preview button (when `api` is set and the row's value isn't the system voice) plays
 * `PREVIEW_SAMPLES[lang]` over the shared read-aloud controller, under its own
 * `settings:preview:<lang>` owner, so previewing one language stops another's and stops hands-free
 * or a reply's own Read. `PreviewCell` keeps its own pending/message state per row; the controller
 * alone decides who is actually speaking.
 *
 * Language names in the row label and each row's own hint are English, from `Intl.DisplayNames`.
 * The Kokoro-gap hint (c) and the no-fallback half of hint (d) would otherwise repeat under every
 * affected row, so both collapse into one full-width line after the last row instead, naming
 * languages like `PackVoiceNotes` does (`LANGUAGE_LIST`/`languageName`, imported from it); a row
 * explicitly set to the system voice is left out of both and keeps its own per-row fallback note.
 */
import {
  Fragment,
  type ReactElement,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { platformKind } from "../../platform";
import {
  effectiveVoice,
  KOKORO_LANGUAGES,
  KOKORO_VOICES,
  modelOf,
  type PackVoice,
  PREVIEW_SAMPLES,
  parsePackVoices,
  QWEN_VOICES,
  SYSTEM_VOICE,
} from "../../voice/packVoices";
import {
  parseVoiceMap,
  pickVoice,
  type ReadAloud,
  readAloud,
  serializeVoiceMap,
} from "../../voice/readAloud";
import { READ_ALOUD_LANGUAGES } from "../../voice/readAloudText";
import { type VoiceboxApi, VoiceboxContext, voiceboxProblem } from "../../voice/voiceboxApi";
import { createVoiceboxFeed, startAudio } from "../../voice/voiceboxFeed";
import {
  installedState,
  PACK_LANGUAGES,
  type SpeechEngine,
  VoicePackContext,
  type VoicePackServices,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { Loading } from "../Loading/Loading";
import { LANGUAGE_LIST, languageName, PackVoiceNotes, usePackStatus } from "./PackVoiceNotes";
import "./PackVoicesField.css";

export interface PackVoicesFieldProps {
  value: unknown;
  systemVoices: unknown;
  labelId: string;
  readOnlyNoticeId: string | null;
  engine: SpeechEngine;
  onChange: (next: string | null) => void;
  controller?: ReadAloud;
  api?: VoiceboxApi;
  services?: VoicePackServices | null;
}

const ENGLISH_LANGUAGE_NAMES = new Intl.DisplayNames(["en"], { type: "language" });

/** "<name> — <gender>, <accent>" for English Kokoro voices, else "<name> — <gender>" — the row
 * already says the language for every non-English Kokoro voice. */
function kokoroOptionLabel(voice: PackVoice): string {
  return voice.accent
    ? `${voice.name} — ${voice.gender}, ${voice.accent}`
    : `${voice.name} — ${voice.gender}`;
}

/** "<name> — <gender>, <native language>": a Qwen3 preset is not locked to the row's language, so
 * its own language is named instead, always in English. */
function qwenOptionLabel(voice: PackVoice): string {
  return `${voice.name} — ${voice.gender}, ${ENGLISH_LANGUAGE_NAMES.of(voice.lang) ?? voice.lang}`;
}

interface PreviewCellProps {
  lang: string;
  voice: string;
  api: VoiceboxApi;
  controller: ReadAloud;
  packStatus: VocePackStatusOrNull;
  kokoroInstalled: boolean;
}

type VocePackStatusOrNull = VoicePackStatus | null;

/** One row's Preview/Stop button and its own pending/failure line. Per-row state (`pending`,
 * `cold`, `message`) lives here; whether this row is actually speaking comes from the shared
 * controller (`statusOf`), so another row's preview — or any other reader — taking over is always
 * reflected without this cell tracking it itself. */
function PreviewCell({
  lang,
  voice,
  api,
  controller,
  packStatus,
  kokoroInstalled,
}: PreviewCellProps): ReactElement {
  const owner = `settings:preview:${lang}`;
  const liveStatus = useSyncExternalStore(controller.subscribe, () => controller.statusOf(owner));
  const [pending, setPending] = useState(false);
  const [cold, setCold] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.stopIf(owner);
    };
  }, [controller, owner]);

  const playing = liveStatus === "speaking" || liveStatus === "paused";
  const busy = pending || playing;

  async function preview(): Promise<void> {
    const ticket = controller.begin(owner);
    setMessage(null);
    if (packStatus === null || !installedState(packStatus.state)) {
      controller.stopIf(owner);
      setMessage(voiceboxProblem("voicebox_pack_missing", "", "pack"));
      return;
    }
    if (modelOf(voice) === "kokoro" && !kokoroInstalled) {
      controller.stopIf(owner);
      setMessage("Add the fast voice first.");
      return;
    }
    setCold(packStatus.state === "stopped" || packStatus.state === "starting");
    setPending(true);
    const feed = createVoiceboxFeed((text, signal) => api.speech(text, lang, signal));
    feed.push(PREVIEW_SAMPLES[lang] ?? "");
    feed.end();
    const result = await startAudio(controller, ticket, owner, feed);
    if (!mounted.current) return;
    setPending(false);
    if (result === "playing" || result === "stale") return;
    const code = result.failed;
    controller.stopIf(owner);
    setMessage(
      code.startsWith("voicebox_")
        ? voiceboxProblem(code, "", "pack")
        : "The preview didn't play. Try again.",
    );
  }

  function stop(): void {
    controller.stopIf(owner);
    setPending(false);
  }

  const waitingText = cold
    ? "Starting natural voices… The first preview can take over a minute."
    : "Preparing the preview…";

  return (
    <>
      <button
        type="button"
        className="btn txt sm"
        onClick={() => {
          if (busy) stop();
          else void preview();
        }}
      >
        {busy ? "Stop" : "Preview"}
      </button>
      {pending && (cold ? <p role="status">{waitingText}</p> : <Loading>{waitingText}</Loading>)}
      {!pending && message !== null && <p role="status">{message}</p>}
    </>
  );
}

export function PackVoicesField({
  value,
  systemVoices,
  labelId,
  readOnlyNoticeId,
  engine,
  onChange,
  controller: controllerProp,
  api: apiProp,
  services: servicesProp,
}: PackVoicesFieldProps): ReactElement {
  const controller = controllerProp ?? readAloud;
  const contextApi = useContext(VoiceboxContext);
  const api = apiProp ?? contextApi;
  const contextServices = useContext(VoicePackContext);
  const services = servicesProp ?? contextServices;

  // usePackStatus(null) is a no-op (no read, no poll): while this field isn't showing the pack
  // engine's own rows there is nothing here worth reading status for.
  const { status, act } = usePackStatus(engine === "pack" ? services : null);
  const voices = useSyncExternalStore(controller.subscribe, controller.voices);
  useEffect(() => {
    void controller.loadVoices();
  }, [controller]);
  const base = useId();

  if (engine !== "pack")
    return (
      <p className="settings-row-desc">
        Select Natural voices under Read replies aloud with to use this setting.
      </p>
    );

  const readOnly = readOnlyNoticeId !== null;
  const map = parsePackVoices(value);
  const systemMap = parseVoiceMap(systemVoices);
  const kokoroInstalled = status?.kokoro ?? false;
  const nameOf = (lang: string): string => ENGLISH_LANGUAGE_NAMES.of(lang) ?? lang;
  const windows = platformKind() === "windows";
  const languages = READ_ALOUD_LANGUAGES.filter((lang) =>
    (PACK_LANGUAGES as readonly string[]).includes(lang),
  );
  // Hint (c) and the no-fallback half of hint (d), collapsed: every language that would otherwise
  // carry the per-row note, minus any row explicitly set to the system voice (it keeps its own
  // fallback note instead, rendered per-row below).
  const kokoroGapLanguages = kokoroInstalled
    ? languages.filter(
        (lang) =>
          !KOKORO_LANGUAGES.includes(lang) && effectiveVoice(map, lang, kokoroInstalled) !== null,
      )
    : [];
  const noFallbackLanguages = languages.filter((lang) => {
    const effectiveId = effectiveVoice(map, lang, kokoroInstalled);
    return effectiveId !== null && pickVoice(lang, voices, systemMap) === null;
  });

  function pick(lang: string, voiceId: string): void {
    if (readOnly) return;
    const next = { ...map };
    if (voiceId === effectiveVoice({}, lang, kokoroInstalled)) delete next[lang];
    else next[lang] = voiceId;
    onChange(serializeVoiceMap(next));
  }

  function addWindowsVoice(): void {
    void services?.api.openSpeechSettings().catch(() => {});
  }

  return (
    <>
      <PackVoiceNotes status={status} map={map} onChange={onChange} act={act} />
      <fieldset aria-labelledby={labelId} className="pack-voices">
        {languages.map((lang) => {
          const id = `${base}-${lang}`;
          const defaultId = effectiveVoice({}, lang, kokoroInstalled);
          const effectiveId = effectiveVoice(map, lang, kokoroInstalled);
          const selectValue = effectiveId ?? SYSTEM_VOICE;
          const systemVoice = pickVoice(lang, voices, systemMap);

          let hintD: ReactElement | null = null;
          if (effectiveId === null) {
            hintD =
              systemVoice !== null ? (
                <p>{`${systemVoice.name} reads ${nameOf(lang)}.`}</p>
              ) : (
                <p>
                  {`No system voice for ${nameOf(lang)} is installed, so its replies aren't read aloud.`}
                  {windows && services && (
                    <>
                      {" "}
                      <button type="button" className="btn txt sm" onClick={addWindowsVoice}>
                        Add a Windows voice…
                      </button>
                    </>
                  )}
                </p>
              );
          }

          return (
            <Fragment key={lang}>
              <label htmlFor={id}>{nameOf(lang)}</label>
              <select
                id={id}
                className="settings-select"
                aria-disabled={readOnly || undefined}
                aria-describedby={readOnlyNoticeId ?? undefined}
                value={selectValue}
                onChange={(event) => pick(lang, event.target.value)}
              >
                {KOKORO_LANGUAGES.includes(lang) && (
                  <optgroup label={`Kokoro — fast${kokoroInstalled ? "" : " (not downloaded)"}`}>
                    {KOKORO_VOICES.filter((voice) => voice.lang === lang).map((voice) => (
                      <option key={voice.id} value={voice.id}>
                        {kokoroOptionLabel(voice)}
                        {voice.id === defaultId ? " (default)" : ""}
                      </option>
                    ))}
                  </optgroup>
                )}
                <optgroup label="Qwen3 — more expressive, needs a strong GPU">
                  {QWEN_VOICES.map((voice) => (
                    <option key={voice.id} value={voice.id}>
                      {qwenOptionLabel(voice)}
                      {voice.id === defaultId ? " (default)" : ""}
                    </option>
                  ))}
                </optgroup>
                <option value={SYSTEM_VOICE}>System voice</option>
              </select>
              {api && effectiveId !== null ? (
                <PreviewCell
                  lang={lang}
                  voice={effectiveId}
                  api={api}
                  controller={controller}
                  packStatus={status}
                  kokoroInstalled={kokoroInstalled}
                />
              ) : (
                <span />
              )}
              {hintD}
            </Fragment>
          );
        })}
        {kokoroGapLanguages.length > 0 && (
          <p>{`Kokoro doesn't speak ${LANGUAGE_LIST.format(kokoroGapLanguages.map(languageName))} here.`}</p>
        )}
        {noFallbackLanguages.length > 0 && (
          <p>
            {`No system voice to fall back on for ${LANGUAGE_LIST.format(noFallbackLanguages.map(languageName))}.`}
          </p>
        )}
      </fieldset>
    </>
  );
}
