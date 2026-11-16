/**
 * The one text-path input used by create, open and the Data view's
 * export destination field: a labelled `<input>`, an optional mono example line, and an error
 * line bound with `aria-describedby`/`aria-invalid`. An opt-in `browse` prop (ADR-050) adds a
 * `btn alt sm` "Browse…" button beside the input, calling the OS
 * folder picker through `useFolderPicker` (FolderPicker.tsx), when one is actually available.
 * Opted out, unavailable, or outside a `FolderPickerProvider`, the field is a plain text input
 * with no browse button (the opener still admits http/https/mailto only, and `<input
 * webkitdirectory>` still yields no path on its own) — and no native form submission either way:
 * Enter calls the same handler the submit button does; the caller decides what "submit" means.
 */
import { type KeyboardEvent, type ReactElement, useState } from "react";
import { ShellApiError } from "../../clients/app/shellApi";
import { errorText, t } from "../../i18n/t";
import { useFolderPicker } from "./FolderPicker";
import "./Onboarding.css";

/** React's synthetic `KeyboardEvent` has no `isComposing` of its own — it lives on
 * `nativeEvent` (Composer.tsx / Cards.tsx keep the same local copy rather than importing one
 * another's `ime.ts`; this is that same small predicate,
 * copied here for the same reason). `keyCode === 229` covers the IME/browser pairs that send
 * that instead of `isComposing`. */
export function isComposingKeydown(event: KeyboardEvent<HTMLElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

export interface PathFormProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  onEnter: () => void;
  error?: string | undefined;
  exampleText?: string | undefined;
  disabled?: boolean;
  /** Opts into a `Browse…` button beside the input (ADR-050); it only actually renders when a
   * `FolderPickerProvider` ancestor also reports a picker available — otherwise this prop makes
   * no difference and the field is the plain input, same as when it is left out entirely. */
  browse?: boolean;
}

export function PathForm({
  id,
  label,
  value,
  onChange,
  onBlur,
  onEnter,
  error,
  exampleText,
  disabled = false,
  browse = false,
}: PathFormProps): ReactElement {
  const picker = useFolderPicker();
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);
  const showBrowse = browse && picker?.status.available === true;

  async function handleBrowse(): Promise<void> {
    if (picker === null) return;
    setPicking(true);
    setPickError(null);
    try {
      // Single pick: this field holds exactly one path. Cancel answers with [], which does
      // nothing — the field keeps whatever it already had, not an error.
      const paths = await picker.pick(label, false);
      const picked = paths[0];
      if (picked !== undefined) onChange(picked);
    } catch (caught) {
      const shellError =
        caught instanceof ShellApiError ? caught : new ShellApiError(0, "unavailable");
      setPickError(errorText(shellError));
    } finally {
      setPicking(false);
    }
  }

  const errorId = `${id}-error`;
  const exampleId = `${id}-example`;
  const describedBy =
    error !== undefined ? errorId : exampleText !== undefined ? exampleId : undefined;
  const input = (
    <input
      id={id}
      type="text"
      spellCheck={false}
      autoComplete="off"
      maxLength={4096}
      value={value}
      disabled={disabled}
      aria-invalid={error !== undefined}
      {...(describedBy !== undefined ? { "aria-describedby": describedBy } : {})}
      onChange={(event) => onChange(event.target.value)}
      onBlur={onBlur}
      onKeyDown={(event) => {
        if (event.key !== "Enter" || isComposingKeydown(event)) return;
        event.preventDefault();
        onEnter();
      }}
    />
  );
  return (
    <div className="path-form-field">
      <label htmlFor={id}>{label}</label>
      {showBrowse ? (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {input}
          <button
            type="button"
            className="btn alt sm"
            aria-disabled={picking}
            onClick={() => {
              if (picking) return;
              void handleBrowse();
            }}
          >
            {t("onboarding.picker.browse")}
          </button>
        </div>
      ) : (
        input
      )}
      {exampleText !== undefined ? (
        <p id={exampleId} className="path-example mono">
          {exampleText}
        </p>
      ) : null}
      {error !== undefined ? (
        <p id={errorId} className="field-error" role="alert">
          {error}
        </p>
      ) : null}
      {pickError !== null ? (
        <p className="field-error" role="alert">
          {pickError}
        </p>
      ) : null}
    </div>
  );
}
