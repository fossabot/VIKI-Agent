/**
 * The inline reason a provider that cannot start a session is not ready:
 * one sentence, chosen in a fixed priority from `provider.claude`, and a "Set up provider"
 * button. `id="readiness-reason"` is what the top-bar New session button's `aria-describedby`
 * and the composer's refusal note point at — there is no separate error card. Renders null when
 * capabilities are unknown or the provider can already start.
 */
import type { ReactElement } from "react";
import type { CommandArg, CommandId } from "../../commands";
import type { Key } from "../../i18n/index";
import { t } from "../../i18n/t";
import type { ShellSnapshot } from "../../state/shell";
import "./Banners.css";

export interface ReadinessBannerProps {
  snapshot: ShellSnapshot;
  onCommand: (id: CommandId, arg?: CommandArg) => void;
}

function reasonKey(snapshot: ShellSnapshot): Key | null {
  const provider = snapshot.capabilities?.provider.claude;
  if (provider === undefined) return null;
  if (provider.cli === "config_dir_override") return "shell.readiness.config_dir";
  if (provider.cli === "unusable") return "shell.readiness.batch_shim";
  if (provider.cli === "missing") return "shell.readiness.cli_missing";
  if (provider.billing === "blocked") return "shell.readiness.billing";
  // Storage before login: `can_start` (src/viki_agent/app/diagnostics.py) is exactly
  // `cli == "ready" and billing != "blocked" and storage.state == "ready"` -- login is not a
  // term in it. By this line cli and billing have already cleared above, so under this banner's
  // own `can_start === false` guard the only term left that can be false is storage. `signed_out`
  // is therefore unreachable today; it stays as the honest row if `can_start` ever grows a login
  // term, rather than silently shadowing the true cause.
  if (snapshot.capabilities?.storage.state !== "ready") return "shell.readiness.storage";
  if (provider.login === "signed_out") return "shell.readiness.signed_out";
  return null;
}

export function ReadinessBanner({
  snapshot,
  onCommand,
}: ReadinessBannerProps): ReactElement | null {
  if (snapshot.capabilities === null) return null;
  if (snapshot.capabilities.provider.claude.can_start) return null;
  const key = reasonKey(snapshot);
  if (key === null) return null;

  // provider.setup's own rule (commands.ts) is `capabilities !== null && !can_start`; both already
  // hold by this point (the early returns above), so the button is always enabled here.
  return (
    <div id="readiness-reason" className="readiness-banner" role="status">
      <span>{t(key)}</span>
      <button type="button" className="btn alt sm" onClick={() => onCommand("provider.setup")}>
        {t("cmd.provider.setup")}
      </button>
    </div>
  );
}
