/**
 * The Settings control for `voice.voicebox_profile`: one select carrying "Off" plus every
 * profile a check found, and a "Check Voicebox"/"Check again" button that reads `api.status()`.
 * With no `api` (no `VoiceboxContext` provider and no override), the field renders only a notice.
 * A stored profile id the current check does not list stays selected under a fallback option, so
 * a gone or not-yet-checked choice is never silently dropped. Read-only follows
 * `ReadAloudVoicesField`: the select stays focusable with `aria-disabled` and names the notice,
 * and a change is dropped rather than sent. English literals: Voicebox strings are not part of
 * the i18n catalog.
 *
 * This field never probes the user's own Voicebox app before they opt in. While `engine`
 * isn't "voicebox" — the radio in `VoiceEngineField` that picks this row's own setting — it skips
 * the select, the mount auto-check and the Check button entirely, down to a one-line notice.
 */

import { type ReactElement, useContext, useEffect, useRef, useState } from "react";
import { type VoiceboxApi, VoiceboxContext, type VoiceboxStatus } from "../../voice/voiceboxApi";
import {
  type SpeechEngine,
  VOICEBOX_RELEASES_URL,
  VoicePackContext,
} from "../../voice/voicePackApi";

export interface VoiceboxFieldProps {
  value: unknown;
  url: string;
  labelId: string;
  readOnlyNoticeId: string | null;
  onChange: (next: string | null) => void;
  engine: SpeechEngine;
  api?: VoiceboxApi;
}

type Outcome = VoiceboxStatus | "error" | null;

/** The status line's text, or null before any check has settled. */
function statusLine(outcome: Outcome, url: string): string | null {
  if (outcome === null) return null;
  if (outcome === "error") return "Couldn't check Voicebox.";
  if (outcome.state === "no_settings") return "Voicebox settings aren't available here.";
  if (outcome.state === "unreachable")
    return `Voicebox isn't answering at ${url}. Start the Voicebox app, then select Check again.`;
  if (outcome.state === "unexpected")
    return `Something other than Voicebox answered at ${url}. Check the Voicebox address.`;
  if (outcome.profiles.length === 0)
    return "Voicebox has no voices yet. Create a voice profile in Voicebox, then select Check again.";
  if (outcome.gpu === false) return "Voicebox is running without a GPU, so speech will be slow.";
  return "Voicebox is running.";
}

export function VoiceboxField({
  value,
  url,
  labelId,
  readOnlyNoticeId,
  onChange,
  engine,
  api: apiProp,
}: VoiceboxFieldProps): ReactElement {
  const contextApi = useContext(VoiceboxContext);
  const api = apiProp ?? contextApi;
  const packServices = useContext(VoicePackContext);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const autoCheck = useRef(typeof value === "string" && engine === "voicebox").current;

  // Runs once: the auto-check is a mount-time decision from the initial api/value/engine, never
  // retriggered by a later prop change (a later flip to "voicebox" asks the user to press
  // Check Voicebox themselves rather than probing on their behalf).
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above.
  useEffect(() => {
    if (!autoCheck || !api) return;
    let live = true;
    api.status().then(
      (next) => {
        if (live) setOutcome(next);
      },
      () => {
        if (live) setOutcome("error");
      },
    );
    return () => {
      live = false;
    };
  }, []);

  if (engine !== "voicebox")
    return (
      <p className="settings-row-desc">
        Select My own Voicebox app (advanced) under Read replies aloud with to use this setting.
      </p>
    );
  if (!api) return <p className="settings-row-desc">Voicebox isn't available in this window.</p>;

  const readOnly = readOnlyNoticeId !== null;
  const stored = typeof value === "string" && value !== "" ? value : null;
  const profiles = outcome !== null && outcome !== "error" ? outcome.profiles : [];
  const missing = stored !== null && !profiles.some((profile) => profile.id === stored);
  const names = new Intl.DisplayNames(["en"], { type: "language" });

  function pick(next: string): void {
    if (readOnly) return;
    onChange(next === "" ? null : next);
  }

  // An arrow function expression, not a declaration: TS only carries the `!api` narrowing above
  // into a closure defined at this point, not into a hoisted function declaration.
  const check = (): void => {
    api.status().then(setOutcome, () => setOutcome("error"));
  };

  const line = statusLine(outcome, url);
  const unreachable = outcome !== null && outcome !== "error" && outcome.state === "unreachable";

  return (
    <>
      <select
        aria-labelledby={labelId}
        className="settings-select"
        aria-disabled={readOnly || undefined}
        aria-describedby={readOnlyNoticeId ?? undefined}
        value={stored ?? ""}
        onChange={(event) => pick(event.target.value)}
      >
        <option value="">Off</option>
        {profiles.map((profile) => (
          <option key={profile.id} value={profile.id}>
            {`${profile.name} (${names.of(profile.language) ?? profile.language})`}
          </option>
        ))}
        {missing && (
          <option value={stored ?? ""}>
            {outcome === null ? "Chosen voice" : "Chosen voice (not in Voicebox)"}
          </option>
        )}
      </select>
      <button type="button" className="btn txt sm" onClick={check}>
        {outcome === null ? "Check Voicebox" : "Check again"}
      </button>
      {line !== null && <p role="status">{line}</p>}
      {unreachable && packServices && (
        <button
          type="button"
          className="btn txt sm"
          onClick={() => packServices.openUrl(VOICEBOX_RELEASES_URL)}
        >
          Get Voicebox
        </button>
      )}
    </>
  );
}
