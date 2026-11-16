/**
 * The voice pack's own health notes, shown under the Natural voice picker (`PackVoicesField`)
 * once the pack is `installedState`: an offer to add Kokoro-82M when it's missing, the
 * speed measured on this PC per model, two misconfiguration warnings — each with its own fix action
 * unless the add offer above already shows it — and the hands-free replies that recently fell back
 * to the system voice. `usePackStatus` is the shared poll: it is exported so the picker can read
 * the same status instead of polling twice. English literals only, same as
 * `VoiceEngineField`.
 */

import { type ReactElement, useCallback, useEffect, useState } from "react";
import {
  effectiveVoice,
  KOKORO_LANGUAGES,
  modelOf,
  SLOW_FACTOR,
  SYSTEM_VOICE,
} from "../../voice/packVoices";
import { voiceboxProblem } from "../../voice/voiceboxApi";
import {
  installedState,
  PACK_LANGUAGES,
  type PackAction,
  type VoicePackServices,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import "./PackVoiceNotes.css";

const POLL_MS = 5_000;
const INSTALLING_POLL_MS = 1_000;

export interface UsePackStatus {
  status: VoicePackStatus | null;
  act: (action: PackAction) => void;
}

/** Reads the pack's status once on mount, then keeps polling: every 5 s normally, every 1 s while
 * `state` is "installing" so a download's progress keeps up. `act` never trusts its own answer —
 * it always re-reads status afterward, success or not. Any failed read, from the poll or after an
 * act, leaves the last known status as it was. */
export function usePackStatus(services: VoicePackServices | null): UsePackStatus {
  const [status, setStatus] = useState<VoicePackStatus | null>(null);

  useEffect(() => {
    if (!services) return;
    let live = true;
    services.api.status().then(
      (next) => {
        if (live) setStatus(next);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [services]);

  const installing = status?.state === "installing";
  useEffect(() => {
    if (!services) return;
    let live = true;
    const id = setInterval(
      () => {
        services.api.status().then(
          (next) => {
            if (live) setStatus(next);
          },
          () => {},
        );
      },
      installing ? INSTALLING_POLL_MS : POLL_MS,
    );
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [services, installing]);

  const act = useCallback(
    (action: PackAction) => {
      if (!services) return;
      services.api
        .act(action)
        .catch(() => {})
        .then(() => services.api.status())
        .then(
          (next) => setStatus(next),
          () => {},
        );
    },
    [services],
  );

  return { status, act };
}

export interface PackVoiceNotesProps {
  status: VoicePackStatus | null;
  map: Readonly<Record<string, string>>;
  onChange: (next: string | null) => void;
  act: (action: PackAction) => void;
}

const MODEL_NAME: Record<"kokoro" | "qwen", string> = { kokoro: "Kokoro", qwen: "Qwen3" };
const LANGUAGE_NAMES = new Intl.DisplayNames(["en"], { type: "language" });
/** Shared with `PackVoicesField`'s own language-list summaries. */
export const LANGUAGE_LIST = new Intl.ListFormat(["en"], { style: "long", type: "conjunction" });

/** A language's English name: shared with `PackVoicesField`'s own summaries. */
export function languageName(lang: string): string {
  return LANGUAGE_NAMES.of(lang) ?? lang;
}

/** "about <n>" for a speech-time-over-audio-time factor: below 1 inverts it ("40" for 0.025,
 * i.e. 40 times faster), at or above 1 keeps it as the slowdown itself ("4.1" for 4.1). */
function timesText(factor: number): string {
  const times = factor < 1 ? Math.round(1 / factor) : factor;
  return Number.isInteger(times) ? String(times) : times.toFixed(1);
}

/** The languages `map` currently reads through `model`, defaults included — `effectiveVoice` per
 * language of `PACK_LANGUAGES`, same resolution the picker itself will show. */
function languagesOn(
  map: Readonly<Record<string, string>>,
  kokoroInstalled: boolean,
  model: "kokoro" | "qwen",
): string[] {
  return PACK_LANGUAGES.filter((lang) => {
    const voice = effectiveVoice(map, lang, kokoroInstalled);
    return voice !== null && modelOf(voice) === model;
  });
}

function withoutPicks(map: Readonly<Record<string, string>>, langs: readonly string[]): string {
  const next = { ...map };
  for (const lang of langs) delete next[lang];
  return JSON.stringify(next);
}

function withSystemVoice(map: Readonly<Record<string, string>>, langs: readonly string[]): string {
  const next = { ...map };
  for (const lang of langs) next[lang] = SYSTEM_VOICE;
  return JSON.stringify(next);
}

export function PackVoiceNotes({
  status,
  map,
  onChange,
  act,
}: PackVoiceNotesProps): ReactElement | null {
  if (status === null || !installedState(status.state)) return null;
  const kokoroInstalled = status.kokoro;

  // Progress is always null here: non-null progress pairs only with state "installing", which
  // fails the installedState gate above, so a download's progress shows only on the engine card,
  // never in these notes.
  const addOffer = !kokoroInstalled && (
    <>
      <p>
        {
          "Add the fast voice: Kokoro (0.35 GB) reads English, Chinese, French, Portuguese, Spanish and Italian much faster than Qwen3."
        }
      </p>
      <div className="voice-pack-actions">
        <button type="button" className="btn go sm" onClick={() => act("install")}>
          Add the fast voice
        </button>
      </div>
    </>
  );

  // Warning (b)'s own languages per model: SLOW_FACTOR exceeded and something currently resolves
  // to it. Computed ahead of the speed lines so a warned model's own line (it already carries the
  // figure) can be skipped below instead of repeating it.
  const slowModels = (["kokoro", "qwen"] as const).flatMap((model) => {
    const factor = status.speed[model];
    if (factor === null || factor <= SLOW_FACTOR) return [];
    const langs = languagesOn(map, kokoroInstalled, model);
    return langs.length > 0 ? [{ model, factor, langs }] : [];
  });
  const warnedModels = new Set(slowModels.map((entry) => entry.model));

  const speedLines = (["kokoro", "qwen"] as const).flatMap((model) => {
    if (warnedModels.has(model)) return [];
    const factor = status.speed[model];
    if (factor === null) return [];
    const word = factor < 1 ? "faster" : "slower";
    return [
      <p key={model}>
        {`${MODEL_NAME[model]}: about ${timesText(factor)} times ${word} than speech on this PC.`}
      </p>,
    ];
  });
  // "Not measured yet" means neither model has a figure at all — not just that `speedLines` is
  // empty, which also happens when a measured model's only line moved into its warning sentence.
  const speedBlock =
    status.speed.kokoro === null && status.speed.qwen === null ? (
      <p>Speed on this PC: not measured yet. Preview a voice to measure it.</p>
    ) : (
      speedLines
    );

  const missingKokoroPick = Object.values(map).some((voice) => modelOf(voice) === "kokoro");
  // No button of its own: this only renders while Kokoro is missing, and the add offer above is
  // always showing then too, so its own "Add the fast voice" button could never render.
  const warningA = !kokoroInstalled && missingKokoroPick && (
    <p className="pack-voice-warning">
      {
        "Some languages are set to the fast voice, which isn't downloaded, so the system voice reads them."
      }
    </p>
  );

  const warningB = slowModels.map(({ model, factor, langs }) => {
    const englishAffected = langs.includes("en");
    const waitClause =
      langs.length === PACK_LANGUAGES.length
        ? "every reply waits for it"
        : `replies in ${LANGUAGE_LIST.format(langs.map(languageName))} wait for it`;
    const sentence = `${MODEL_NAME[model]} makes speech about ${timesText(factor)} times slower than it plays on this PC, so ${waitClause}${englishAffected ? ", and hands-free answers come from the system voice" : ""}.`;
    const kokoroLangs = langs.filter((lang) => KOKORO_LANGUAGES.includes(lang));

    // "Use the fast voice" drops those picks when Kokoro is installed and covers some affected
    // language. When Kokoro is missing instead, no button here at all — the add offer above is
    // always showing then too, and already offers to install it. Otherwise no affected language is
    // Kokoro's, so the only real fix is to stop waiting on the slow model for them.
    let action: ReactElement | null;
    if (kokoroLangs.length === 0) {
      action = (
        <button
          type="button"
          className="btn alt sm"
          onClick={() => onChange(withSystemVoice(map, langs))}
        >
          Use the system voice for them
        </button>
      );
    } else if (kokoroInstalled) {
      action = (
        <button
          type="button"
          className="btn alt sm"
          onClick={() => onChange(withoutPicks(map, kokoroLangs))}
        >
          Use the fast voice
        </button>
      );
    } else {
      action = null;
    }

    return (
      <div key={model}>
        <p className="pack-voice-warning">{sentence}</p>
        {action && <div className="voice-pack-actions">{action}</div>}
      </div>
    );
  });

  const fallbackReason = (code: string): string => {
    if (code === "voicebox_timeout")
      return "The natural voice took too long to start, so the system voice read it.";
    return code.startsWith("voicebox_")
      ? voiceboxProblem(code, "", "pack")
      : "The natural voice didn't play.";
  };
  const fallbackBlock = status.fallbacks.length > 0 && (
    <>
      <p>Recent hands-free answers the system voice read:</p>
      <ul className="pack-voice-fallbacks">
        {status.fallbacks.map((fallback) => (
          <li key={fallback.at}>
            {`${new Date(fallback.at).toLocaleTimeString()} · ${fallbackReason(fallback.code)}`}
          </li>
        ))}
      </ul>
    </>
  );

  return (
    <div className="pack-voice-notes">
      {addOffer}
      {speedBlock}
      {warningA}
      {warningB}
      {fallbackBlock}
    </div>
  );
}
