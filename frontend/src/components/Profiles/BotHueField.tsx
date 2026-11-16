import { useContext, useEffect, useRef, useState } from "react";
import { t } from "../../i18n/t";
import { BotIcon } from "../Bots/BotIcon";
import { BotHuesContext, botHue } from "../Bots/botHues";
import { designFor, type Hue } from "../Bots/botIconDesign";
import { Icon } from "../Icon/Icon";
import "./BotHueField.css";

const HUE_ORDER: readonly Hue[] = [
  "red",
  "orange",
  "amber",
  "green",
  "teal",
  "blue",
  "purple",
  "pink",
];

export function BotHueField({ profileId, name }: { profileId: string; name: string }) {
  const { hues, icons, set } = useContext(BotHuesContext);
  const storedHue = botHue(profileId, name, hues);
  const [choice, setChoice] = useState(storedHue);
  const [saved, setSaved] = useState(false);
  const [failed, setFailed] = useState(false);
  const confirmedHue = useRef(storedHue);
  const pendingChoice = useRef<number | null>(null);
  const requestId = useRef(0);
  const emblem = designFor(profileId, name, icons[profileId]).emblem;

  useEffect(() => {
    confirmedHue.current = storedHue;
    if (pendingChoice.current === null) setChoice(storedHue);
  }, [storedHue]);

  if (!set) return null;

  const pick = (hue: Hue): void => {
    if (hue === choice) return;
    const id = ++requestId.current;
    pendingChoice.current = id;
    setChoice(hue);
    setSaved(false);
    setFailed(false);
    set(profileId, { hue }).then(
      (result) => {
        if (result === "saved") confirmedHue.current = hue;
        if (pendingChoice.current !== id) return;
        pendingChoice.current = null;
        if (result === "saved") {
          setChoice(hue);
          setSaved(true);
        } else {
          setChoice(confirmedHue.current);
        }
      },
      () => {
        if (pendingChoice.current !== id) return;
        pendingChoice.current = null;
        setChoice(confirmedHue.current);
        setFailed(true);
      },
    );
  };

  return (
    <fieldset className="bot-hue-field">
      <legend>{t("bots.hue.legend")}</legend>
      <div className="bot-hue-row">
        {HUE_ORDER.map((hue) => (
          <label key={hue} className="bot-hue-choice" data-bot-hue={hue}>
            <input
              type="radio"
              name={`bot-hue-${profileId}`}
              value={hue}
              checked={hue === choice}
              onChange={() => pick(hue)}
            />
            <span className="bot-hue-avatar">
              <BotIcon seed={profileId} name={name} hue={hue} icon={emblem} />
            </span>
            <span className="bot-hue-choice-name">{t(`bots.hue.${hue}`)}</span>
            {hue === choice ? (
              <span className="bot-hue-selected-check">
                <Icon name="check" />
              </span>
            ) : null}
          </label>
        ))}
      </div>
      <p className="bot-hue-saved" role="status">
        {saved ? t("bots.hue.saved") : ""}
      </p>
      {failed ? (
        <p className="bot-hue-failed" role="alert">
          {t("bots.hue.failed")}
        </p>
      ) : null}
    </fieldset>
  );
}
