/**
 * The Settings control for `voice.read_aloud_voices`: one select per language that has an
 * installed local voice, "Automatic" first. The list comes from `controller.voices`, which keeps
 * only `localService === true` voices, and `voicesFor` filters again, so an online voice is never
 * an option. Languages without a voice are named once, with this OS's steps for adding one.
 * Voice and language names are text nodes. A read-only document follows `SettingRow`: the selects
 * stay focusable with `aria-disabled`, name the notice, and change nothing.
 */

import { type ReactElement, useEffect, useId, useState, useSyncExternalStore } from "react";
import { t, tf } from "../../i18n/t";
import { platformKind } from "../../platform";
import {
  parseVoiceMap,
  type ReadAloud,
  readAloud,
  serializeVoiceMap,
  voicesFor,
} from "../../voice/readAloud";
import { READ_ALOUD_LANGUAGES } from "../../voice/readAloudText";
import { Loading } from "../Loading/Loading";
import "./ReadAloudVoicesField.css";

export interface ReadAloudVoicesFieldProps {
  value: unknown;
  labelId: string;
  readOnlyNoticeId: string | null;
  onChange: (next: string | null) => void;
  controller?: ReadAloud;
}

export function ReadAloudVoicesField({
  value,
  labelId,
  readOnlyNoticeId,
  onChange,
  controller = readAloud,
}: ReadAloudVoicesFieldProps): ReactElement {
  const base = useId();
  const voices = useSyncExternalStore(controller.subscribe, controller.voices);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!controller.supported) return;
    let live = true;
    void controller.loadVoices().then(() => {
      if (live) setLoaded(true);
    });
    return () => {
      live = false;
    };
  }, [controller]);

  if (!controller.supported)
    return <p className="settings-row-desc">{t("readaloud.unsupported")}</p>;
  if (!loaded)
    return (
      <Loading className="settings-row-desc" announce={false}>
        {t("readaloud.settings.loading")}
      </Loading>
    );

  const readOnly = readOnlyNoticeId !== null;
  const map = parseVoiceMap(value);
  const names = new Intl.DisplayNames(["en"], { type: "language" });
  const nameOf = (lang: string): string => names.of(lang) ?? lang;
  const withVoice = READ_ALOUD_LANGUAGES.filter((lang) => voicesFor(lang, voices).length > 0);
  const without = READ_ALOUD_LANGUAGES.filter((lang) => !withVoice.includes(lang));
  const steps = <p className="settings-row-desc">{t(`readaloud.how.${platformKind()}`)}</p>;

  if (withVoice.length === 0)
    return (
      <>
        <p className="settings-row-desc">{t("readaloud.no_voices")}</p>
        {steps}
      </>
    );

  function pick(lang: string, uri: string): void {
    if (readOnly) return;
    const next = { ...map };
    if (uri === "") delete next[lang];
    else next[lang] = uri;
    onChange(serializeVoiceMap(next));
  }

  return (
    <fieldset aria-labelledby={labelId} className="read-aloud-voices">
      {withVoice.map((lang) => {
        const list = voicesFor(lang, voices);
        const stored = Object.hasOwn(map, lang) ? map[lang] : undefined;
        const missing = stored !== undefined && !list.some((voice) => voice.voiceURI === stored);
        const id = `${base}-${lang}`;
        return (
          <div key={lang} className="read-aloud-voices-lang">
            <label htmlFor={id}>{nameOf(lang)}</label>
            <select
              id={id}
              className="settings-select"
              aria-disabled={readOnly || undefined}
              aria-describedby={readOnlyNoticeId ?? undefined}
              value={stored ?? ""}
              onChange={(event) => pick(lang, event.target.value)}
            >
              <option value="">
                {tf("readaloud.settings.automatic", { voice: list[0]?.name ?? "" })}
              </option>
              {list.map((voice) => (
                <option key={voice.voiceURI} value={voice.voiceURI}>
                  {voice.name}
                </option>
              ))}
              {missing && (
                <option value={stored}>
                  {tf("readaloud.settings.not_installed", { voice: stored })}
                </option>
              )}
            </select>
          </div>
        );
      })}
      {without.length > 0 && (
        <>
          <p className="settings-row-desc">
            {tf("readaloud.settings.missing", {
              languages: new Intl.ListFormat(["en"], {
                style: "long",
                type: "conjunction",
              }).format(without.map(nameOf)),
            })}
          </p>
          {steps}
        </>
      )}
    </fieldset>
  );
}
