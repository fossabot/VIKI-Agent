/**
 * The banner at the top of every settings page while the document is read-only:
 * why it is read-only, and the recovery instructions.
 *
 * The guidance is plain recovery instructions, not a link: the opener admits only
 * http/https/mailto (hard rule 7), so a `file:` anchor to the app data folder here would be a
 * control that does nothing.
 *
 * `id` is named by every control's `aria-describedby` on the pages below, which is why the notice
 * is `role="status"` rather than `role="alert"`: it describes a state the user arrived in, and
 * an alert would be re-announced on top of each control they move to.
 *
 * A read-only document that names no reason shows the guidance alone: the two sentences say
 * different things about the user's files, and guessing which one applies would be worse than
 * saying nothing.
 */

import type { ReactElement } from "react";
import { t } from "../../i18n/t";

export interface ReadOnlyNoticeProps {
  id: string;
  /** `SettingsView.reason`; `null` when the backend refused to say which of the two it is. */
  reason: "newer_version" | "corrupt" | null;
}

export function ReadOnlyNotice({ id, reason }: ReadOnlyNoticeProps): ReactElement {
  return (
    <div className="settings-readonly" id={id} role="status">
      {reason !== null && (
        <p>
          {t(
            reason === "corrupt"
              ? "settings.read_only.corrupt"
              : "settings.read_only.newer_version",
          )}
        </p>
      )}
      <p className="settings-readonly-guidance">{t("settings.read_only.guidance")}</p>
    </div>
  );
}
