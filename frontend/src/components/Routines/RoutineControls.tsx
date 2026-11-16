/**
 * Pause-all and notification settings for routines. Each change is one settings patch; a
 * rejection shows `save_failed`, a success asks the parent to refresh. Checking "notify" while the
 * browser permission is undecided asks for it first.
 */
import { type ReactElement, useState } from "react";
import type { SchedulerStatus } from "../../clients/automation/routinesApi";
import { t } from "../../i18n/t";

export interface RoutineControlValues {
  paused: boolean;
  notificationsEnabled: boolean;
  quietStart: string | null;
  quietEnd: string | null;
  permission: "default" | "granted" | "denied" | "unsupported";
}

export interface RoutineControlsProps {
  scheduler: SchedulerStatus | null;
  controls: RoutineControlValues;
  onSettings(changes: Record<string, boolean | string | null>): Promise<void>;
  onRequestPermission(): Promise<void>;
  onRefresh(): void;
}

const QUIET_TIMES = Array.from({ length: 48 }, (_, i) => {
  const hour = String(Math.floor(i / 2)).padStart(2, "0");
  return `${hour}:${i % 2 ? "30" : "00"}`;
});

export function RoutineControls({
  controls,
  onSettings,
  onRequestPermission,
  onRefresh,
}: RoutineControlsProps): ReactElement {
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  async function apply(run: () => Promise<void>) {
    setBusy(true);
    setFailed(false);
    try {
      await run();
      onRefresh();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  const { permission, notificationsEnabled } = controls;
  const notice =
    notificationsEnabled && permission !== "granted" ? (
      <p className="routines-controls-note">
        {t(`routines.controls.permission_${permission}`)}{" "}
        {permission === "default" && (
          <button
            type="button"
            className="btn alt"
            disabled={busy}
            onClick={() => void apply(onRequestPermission)}
          >
            {t("routines.controls.permission_request")}
          </button>
        )}
      </p>
    ) : null;

  const quiet = (key: "quiet_start" | "quiet_end", label: string, value: string | null) => (
    <label className="routines-controls-quiet">
      <span className="lbl">{label}</span>
      <select
        value={value ?? ""}
        disabled={busy}
        onChange={(event) =>
          void apply(() => onSettings({ [`notifications.${key}`]: event.target.value || null }))
        }
      >
        <option value="">{t("routines.controls.quiet_none")}</option>
        {QUIET_TIMES.map((time) => (
          <option key={time} value={time}>
            {time}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <section className="routines-controls" aria-label={t("routines.controls.title")}>
      <button
        type="button"
        className="btn alt"
        disabled={busy}
        onClick={() => void apply(() => onSettings({ "routines.paused": !controls.paused }))}
      >
        {t(controls.paused ? "routines.controls.resume" : "routines.controls.pause_all")}
      </button>
      <label className="routines-controls-notify">
        <input
          type="checkbox"
          checked={notificationsEnabled}
          disabled={busy}
          onChange={(event) => {
            const on = event.target.checked;
            void apply(async () => {
              if (on && permission === "default") await onRequestPermission();
              await onSettings({ "notifications.enabled": on });
            });
          }}
        />
        {t("routines.controls.notify")}
      </label>
      <fieldset className="routines-controls-quiet-group">
        <legend>{t("routines.controls.quiet")}</legend>
        {quiet("quiet_start", t("routines.controls.quiet_from"), controls.quietStart)}
        {quiet("quiet_end", t("routines.controls.quiet_to"), controls.quietEnd)}
      </fieldset>
      {notice}
      {failed && (
        <p className="routines-controls-note" role="alert">
          {t("routines.controls.save_failed")}
        </p>
      )}
    </section>
  );
}
