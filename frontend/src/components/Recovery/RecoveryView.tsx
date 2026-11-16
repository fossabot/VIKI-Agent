/** Shows recovery diagnostics without chat, session, rail, draft or export routes. */
import type { ReactElement, ReactNode } from "react";
import { ShellApiError } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { dataErrorText } from "../Data/dataErrorText";
import "./Recovery.css";

export interface RecoveryViewProps {
  store: ShellStore;
  children: ReactNode;
}

function focusOnMount(element: HTMLElement | null): void {
  element?.focus();
}

export function RecoveryView({ store, children }: RecoveryViewProps): ReactElement {
  const snapshot = useSnapshot(store);
  const reason = snapshot.capabilities?.storage.reason ?? null;

  return (
    <div className="recovery-view">
      <h1 ref={focusOnMount} tabIndex={-1}>
        {t("data.recovery.title")}
      </h1>
      {reason !== null && <p role="alert">{dataErrorText(new ShellApiError(0, reason))}</p>}
      <p>{t("data.recovery.no_export")}</p>
      <section className="recovery-next-steps" aria-labelledby="recovery-next-steps-title">
        <h2 id="recovery-next-steps-title">Before you retry</h2>
        <ol>
          <li>
            Close VIKI Agent before copying or changing app data. Copy the complete data folder,
            including SQLite journals and retained <code>state.json</code>.
          </li>
          <li>
            Do not delete SQLite journals to bypass an error. Resolve disk space or permission
            problems and close other tools holding the database before retrying.
          </li>
          <li>
            For data from a newer app version, use that version. For damaged data, restore a
            complete known-good backup made while VIKI Agent was closed. Keep the failed copy for
            inspection.
          </li>
        </ol>
      </section>
      <div className="recovery-diagnostics">{children}</div>
    </div>
  );
}
