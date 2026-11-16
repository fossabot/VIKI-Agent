import {
  type KeyboardEvent,
  type ReactElement,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { t } from "../../i18n/t";
import { BotIcon } from "../Bots/BotIcon";
import { BotHuesContext, type BotLookResult } from "../Bots/botHues";
import { BOT_ICON_IDS, type Emblem, type Hue } from "../Bots/botIconDesign";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./BotIconDialog.css";

const OPTIONS: readonly (Emblem | null)[] = [null, ...BOT_ICON_IDS];

export interface BotIconDialogProps {
  profileId: string;
  name: string;
  hue: Hue;
  icon: Emblem | null;
  onClose: () => void;
  onSaved: (icon: Emblem | null) => void;
}

export function BotIconDialog({
  profileId,
  name,
  hue,
  icon,
  onClose,
  onSaved,
}: BotIconDialogProps): ReactElement {
  const { set } = useContext(BotHuesContext);
  const [selection, setSelection] = useState<Emblem | null>(icon);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);
  const busy = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstRadioRef = useRef<HTMLInputElement>(null);
  const radioRefs = useRef<Array<HTMLInputElement | null>>([]);
  const headingId = useId();

  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: firstRadioRef,
    onDismiss: dismiss,
  });

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (failed) radioRefs.current[OPTIONS.indexOf(selection)]?.focus();
  }, [failed, selection]);

  function dismiss(): void {
    if (!busy.current) onClose();
  }

  async function saveChoice(choice: Emblem | null): Promise<void> {
    if (!set || busy.current) return;
    busy.current = true;
    dialogRef.current?.focus();
    setSaving(true);
    setFailed(false);
    let result: BotLookResult;
    try {
      result = await set(profileId, { icon: choice });
    } catch {
      busy.current = false;
      if (mounted.current) {
        setSaving(false);
        setFailed(true);
      }
      return;
    }
    busy.current = false;
    if (mounted.current) setSaving(false);
    if (result === "superseded") {
      if (mounted.current) onClose();
      return;
    }
    onSaved(choice);
    if (mounted.current) onClose();
  }

  function moveRadio(event: KeyboardEvent<HTMLDivElement>): void {
    if (busy.current) return;
    if (
      event.key !== "ArrowRight" &&
      event.key !== "ArrowDown" &&
      event.key !== "ArrowLeft" &&
      event.key !== "ArrowUp" &&
      event.key !== "Enter"
    )
      return;
    if (!(event.target instanceof HTMLInputElement)) return;
    event.preventDefault();
    if (event.key === "Enter") {
      void saveChoice(selection);
      return;
    }
    const currentIndex = Number(event.target.dataset.index);
    const delta = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
    const nextIndex = (currentIndex + delta + OPTIONS.length) % OPTIONS.length;
    const next = OPTIONS[nextIndex] ?? null;
    setSelection(next);
    radioRefs.current[nextIndex]?.focus();
  }

  return createPortal(
    <div className="bot-icon-dialog-backdrop">
      <div
        ref={dialogRef}
        className="bot-icon-dialog"
        role="dialog"
        aria-modal="true"
        aria-busy={saving}
        aria-labelledby={headingId}
        tabIndex={-1}
      >
        <h2 id={headingId}>{t("bots.icon.choose")}</h2>
        <div
          className="bot-icon-dialog-options"
          role="radiogroup"
          aria-label={t("bots.icon.choose")}
          onKeyDown={moveRadio}
        >
          {OPTIONS.map((option, index) => {
            const label = option === null ? t("bots.icon.automatic") : t(`bots.icon.${option}`);
            return (
              <label className="bot-icon-dialog-option" key={option ?? "automatic"}>
                <input
                  ref={(element) => {
                    radioRefs.current[index] = element;
                    if (index === 0) firstRadioRef.current = element;
                  }}
                  type="radio"
                  name={`bot-icon-${profileId}`}
                  value={option ?? "automatic"}
                  data-index={index}
                  checked={selection === option}
                  disabled={saving}
                  onChange={() => {
                    if (!busy.current) setSelection(option);
                  }}
                />
                <BotIcon seed={profileId} name={name} hue={hue} icon={option ?? undefined} />
                <span>{label}</span>
              </label>
            );
          })}
        </div>
        {failed ? (
          <p className="bot-icon-dialog-error" role="alert">
            {t("bots.icon.failed")}
          </p>
        ) : null}
        <div className="bot-icon-dialog-actions">
          <button
            type="button"
            className="btn alt sm"
            disabled={!set || saving}
            onClick={() => void saveChoice(selection)}
          >
            {t("bots.icon.use")}
          </button>
          <button type="button" className="btn txt sm" disabled={saving} onClick={dismiss}>
            {t("bots.icon.cancel")}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
