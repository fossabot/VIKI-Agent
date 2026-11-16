/**
 * The status bar's one not-ready notice: Claude Code signed out while
 * still able to start, an outdated Claude Code version, and git missing, are the three parts no
 * other banner already states — `Shell/ReadinessBanner.tsx` explains every `can_start=false`
 * reason, and Settings › Providers/Workspaces/Diagnostics cover the rest. Joined " · ", in that
 * order, for however many apply; null when none do.
 */
import { t } from "../../i18n/t";
import type { ShellSnapshot } from "../../state/shell";

export function notReadyNotice(snapshot: ShellSnapshot): string | null {
  const provider = snapshot.capabilities?.provider.claude;
  const parts: string[] = [];
  if (provider?.can_start && provider.login === "signed_out") {
    parts.push(t("shell.status.claude_signed_out"));
  }
  if (provider?.version_status === "older") {
    parts.push(t("shell.status.claude_outdated"));
  }
  if (snapshot.capabilities?.git.state === "missing") {
    parts.push(t("shell.status.git_missing"));
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}
