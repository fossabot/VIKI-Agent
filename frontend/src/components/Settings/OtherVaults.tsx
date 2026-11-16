/** The "Other vaults" line beneath the Workspaces page's own list: every registered
 * vault besides the one active now, plus a one-tap switch back to Chat only unless that is
 * already the active workspace. Renders nothing when neither applies. */
import { useState } from "react";
import type { ShellApi, WorkspaceJson } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { commandFor } from "../../commands";
import { errorText, t } from "../../i18n/t";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import { describedBy, ReasonHint, reasonTitle } from "./ReasonHint";

export interface OtherVaultsProps {
  api: ShellApi | null;
  store: ShellStore;
  snapshot: ShellSnapshot;
}

interface SelectFeedback {
  workspaceId: string | null;
  restartRequired: boolean;
}

export function OtherVaults({ api, store, snapshot }: OtherVaultsProps) {
  const [feedback, setFeedback] = useState<SelectFeedback | null>(null);
  const [selectError, setSelectError] = useState<ShellApiError | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const activeId = snapshot.capabilities?.workspace.active?.id ?? null;
  const activeKind = snapshot.capabilities?.workspace.active?.kind ?? null;
  const otherVaults = (snapshot.workspaces?.workspaces ?? []).filter(
    (workspace) => workspace.kind === "vault" && workspace.workspace_id !== activeId,
  );
  const showChatOnly = activeKind !== "scratch";
  if (otherVaults.length === 0 && !showChatOnly) return null;

  const selectState = commandFor("workspace.select", snapshot);

  async function select(workspaceId: string | null): Promise<void> {
    if (api === null || !selectState.enabled) return;
    setSelectError(null);
    try {
      const result = await api.selectWorkspace(crypto.randomUUID(), workspaceId);
      setFeedback({ workspaceId, restartRequired: result.restart_required });
      await store.afterMutation(["capabilities", "workspaces"]);
    } catch (error) {
      setSelectError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
    }
  }

  async function cancelBootstrap(workspaceId: string): Promise<void> {
    if (api === null) return;
    setCancelling(workspaceId);
    setSelectError(null);
    try {
      await api.cancelBootstrap(crypto.randomUUID(), workspaceId);
      await store.afterMutation(["capabilities", "workspaces"]);
    } catch (error) {
      setSelectError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
    } finally {
      setCancelling(null);
    }
  }

  function vaultSelectButton(id: string, workspaceId: string | null) {
    const reasonId = `other-vault-reason-${id}`;
    const rowFeedback = feedback !== null && feedback.workspaceId === workspaceId ? feedback : null;
    return (
      <>
        <button
          type="button"
          className="btn txt sm"
          aria-disabled={!selectState.enabled}
          aria-describedby={describedBy(reasonId, selectState)}
          title={reasonTitle(selectState)}
          onClick={() => {
            if (selectState.enabled) void select(workspaceId);
          }}
        >
          {t("workspaces.other.use")}
        </button>
        <ReasonHint id={reasonId} state={selectState} />
        {rowFeedback !== null ? (
          <span role="status">
            {rowFeedback.restartRequired
              ? t("common.workspace.restart_required")
              : t("workspaces.select.already_active")}
          </span>
        ) : null}
      </>
    );
  }

  function vaultRow(workspace: WorkspaceJson) {
    const pending = workspace.bootstrap === "pending";
    return (
      <span key={workspace.workspace_id} className="other-vault-row">
        <span>{workspace.name}</span>
        {!workspace.root_available ? (
          <span className="chip">{t("common.chip.not_found")}</span>
        ) : null}
        {pending ? (
          <>
            <span className="chip">{t("workspaces.chip.bootstrap_pending")}</span>
            <button
              type="button"
              className="btn alt sm"
              aria-disabled={cancelling === workspace.workspace_id}
              onClick={() => {
                if (cancelling !== workspace.workspace_id)
                  void cancelBootstrap(workspace.workspace_id);
              }}
            >
              {t("common.button.cancel_bootstrap")}
            </button>
          </>
        ) : (
          vaultSelectButton(workspace.workspace_id, workspace.workspace_id)
        )}
      </span>
    );
  }

  const chatOnlyRow = showChatOnly ? (
    <span key="chat-only" className="other-vault-row">
      <span>
        {t("workspaces.other.chat_only")} {t("workspaces.other.no_folder")}
      </span>
      {vaultSelectButton("chat-only", null)}
    </span>
  ) : null;
  const errorRow =
    selectError !== null ? (
      <span key="error" className="settings-error" role="alert">
        {errorText(selectError)}
      </span>
    ) : null;
  // Every item but the restart note is joined to the previous one with " · "; the note gets
  // only a single leading space instead, because its own catalog text already starts with
  // "· " and a second separator would double the dot.
  const rows = [...otherVaults.map(vaultRow), chatOnlyRow, errorRow].filter((row) => row !== null);
  const line = rows.flatMap((row, index) => (index === 0 ? [row] : [" · ", row]));

  return (
    <p className="settings-note">
      <span>{t("workspaces.other.label")}</span> {line}{" "}
      <span>{t("workspaces.other.restart_note")}</span>
    </p>
  );
}
