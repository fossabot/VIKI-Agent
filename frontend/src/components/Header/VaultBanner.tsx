/** Announces other live sessions using the same vault. It can appear beside the reconnect banner; transcript titles render as text and the status does not take focus. */
import type { ReactElement } from "react";
import type { OtherSession } from "../../protocol";
import { vaultBannerText } from "./wrapUp";
import "./VaultBanner.css";

export interface VaultBannerProps {
  /** vault.sessions: the live claude sessions whose cwd is the vault and that we do not own. */
  others: readonly OtherSession[];
}

export function VaultBanner({ others }: VaultBannerProps): ReactElement | null {
  const text = vaultBannerText(others);
  if (!text) return null;
  return (
    <div className="vault-banner jp" role="status" aria-live="polite">
      <span className="vault-banner-dot" aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}
