/**
 * "No vault is connected" / "A vault is registered" / the registration is broken. Shares the
 * banner slot with `ReconnectBanner` and `VaultBanner` (they stack);
 * renders null unless `capabilities.workspace.chat_only` is known true.
 *
 * `restart_required` alone is not proof the next launch will switch: the backend also sets it
 * for the launch that fell back to scratch *because* the selected vault's root is gone or its
 * bootstrap never finished (workspaces.py's `_resolve_active`), and nothing clears the stale
 * selection, so that launch repeats every time. The workspaces listing is the only data that can
 * tell the two apart, so the "registered" sentence is trusted only once that listing confirms
 * the selected row is actually usable; an unread or stale listing cannot disprove it, so it is
 * trusted by default rather than asserting a cause the shell cannot verify.
 */
import type { ReactElement } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { type CommandArg, type CommandId, commandFor } from "../../commands";
import type { Key } from "../../i18n/index";
import { t } from "../../i18n/t";
import type { ShellSnapshot } from "../../state/shell";
import "./Banners.css";

export interface ChatOnlyBannerProps {
  snapshot: ShellSnapshot;
  onCommand: (id: CommandId, arg?: CommandArg) => void;
}

function CommandButton({
  id,
  snapshot,
  onCommand,
  className = "btn alt sm",
}: {
  id: CommandId;
  snapshot: ShellSnapshot;
  onCommand: (id: CommandId) => void;
  className?: string;
}): ReactElement {
  const state = commandFor(id, snapshot);
  const reasonId = `chat-only-banner-${id}-reason`;
  return (
    <button
      type="button"
      className={className}
      aria-disabled={!state.enabled}
      {...(state.reason !== null ? { "aria-describedby": reasonId, title: t(state.reason) } : {})}
      onClick={() => {
        if (!state.enabled) return;
        onCommand(id);
      }}
    >
      <span>{t(`cmd.${id}`)}</span>
      {state.reason !== null ? (
        <span id={reasonId} className="sr-only">
          {t(state.reason)}
        </span>
      ) : null}
    </button>
  );
}

/** The catalog row naming why the selected registration cannot take over, or null when the
 * listing does not (yet, or ever) disprove the default "will switch next launch" reading: it
 * has not been read, or no longer lists the selected id. Mirrors `_resolve_active`'s own
 * predicate in workspaces.py (`bootstrap != "pending" and current_root(...) is not None`) so a
 * fresh "Open a folder" registration -- `bootstrap: "none"`, never "complete" -- is never
 * misread as broken. It also mirrors the `_untrusted_code` skip: an untrusted code folder is
 * never activated. */
export function selectedRow(snapshot: ShellSnapshot): WorkspaceJson | undefined {
  const id = snapshot.capabilities?.workspace.selected_id;
  if (id === null || id === undefined) return undefined;
  return snapshot.workspaces?.workspaces.find((candidate) => candidate.workspace_id === id);
}

export function selectedVaultProblem(snapshot: ShellSnapshot): Key | null {
  const row = selectedRow(snapshot);
  if (row === undefined) return null;
  if (row.kind === "code" && row.policy !== "ordinary") return "error.trust_required";
  if (row.bootstrap === "pending") return "workspaces.chip.bootstrap_pending";
  if (!row.root_available) return "error.root_unavailable";
  return null;
}

export function ChatOnlyBanner({ snapshot, onCommand }: ChatOnlyBannerProps): ReactElement | null {
  const workspace = snapshot.capabilities?.workspace;
  if (workspace?.chat_only !== true) return null;

  if (workspace.restart_required) {
    const problem = selectedVaultProblem(snapshot);
    if (problem !== null) {
      return (
        <div className="chat-only-banner" role="status" aria-live="polite">
          <span>{t(problem)}</span>
          <div className="chat-only-banner-actions">
            <CommandButton
              id="workspace.create"
              snapshot={snapshot}
              onCommand={onCommand}
              className="btn go sm"
            />
            <CommandButton id="workspace.open" snapshot={snapshot} onCommand={onCommand} />
            <CommandButton id="workspace.select" snapshot={snapshot} onCommand={onCommand} />
          </div>
        </div>
      );
    }
    return (
      <div className="chat-only-banner" role="status" aria-live="polite">
        <span>
          {t(
            selectedRow(snapshot)?.kind === "code"
              ? "shell.chat_only.registered_code"
              : "shell.chat_only.registered",
          )}
        </span>
        <CommandButton id="workspace.select" snapshot={snapshot} onCommand={onCommand} />
      </div>
    );
  }

  return (
    <div className="chat-only-banner" role="status" aria-live="polite">
      <span>{t("shell.chat_only.text")}</span>
      <div className="chat-only-banner-actions">
        <CommandButton
          id="workspace.create"
          snapshot={snapshot}
          onCommand={onCommand}
          className="btn go sm"
        />
        <CommandButton id="workspace.open" snapshot={snapshot} onCommand={onCommand} />
      </div>
    </div>
  );
}
