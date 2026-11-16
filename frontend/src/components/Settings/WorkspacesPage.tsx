/** The Workspaces and Vault page: the user's vault card, the other
 * registered vaults, adding folders, and the workspace list — each its own part. Workspace
 * removal lives here too, through `WorkspaceList`'s row menu. `PolicyPanel`, `ProvidersPage` and
 * `DiagnosticsPage`/About share this file's catalog but render their own
 * pages elsewhere. Backend refusal codes for those screens render through `errors.workspaces.ts`
 * / `errors.settings.ts` (`stale_revision`) / `errors.diagnostics.ts`, not from here. */
import { useCallback, useState } from "react";
import type { ShellApi, WorkspaceJson } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CommandArg, CommandId } from "../../commands";
import { commandFor } from "../../commands";
import type { Key } from "../../i18n/index";
import { errorText, t } from "../../i18n/t";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import { AddFolders } from "./AddFolders";
import { OtherVaults } from "./OtherVaults";
import "./Pages.css";
import { VaultCard } from "./VaultCard";
import { WorkspaceList } from "./WorkspaceList";

export interface WorkspacesPageProps {
  api: ShellApi | null;
  store: ShellStore;
  snapshot: ShellSnapshot;
  onCommand: (id: CommandId, arg?: CommandArg) => void;
  /** Wired by the host that knows how to switch the window into the Code view; absent hosts
   * simply get no "Open in Code" menu item (`WorkspaceList`'s own optional prop). */
  onOpenInCode?: (workspaceId: string) => void;
}

/** `chat.workspace_ids` is a JSON array of ids, stored as a string (the settings document
 * holds only JSON-primitive values). Anything else — unset, corrupt, not an array — reads as no
 * ticks, never thrown back at the user. */
function parseTickedIds(effective: unknown): string[] {
  if (typeof effective !== "string") return [];
  try {
    const parsed: unknown = JSON.parse(effective);
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

/** The ids a tick write may still keep after a removal or a policy change: present, ordinary,
 * code-kind rows. A row that is gone, or no longer trusted, drops out silently rather than
 * leaving a stale id a tick checkbox can never show as checked again. */
function ordinaryCodeIds(rows: readonly WorkspaceJson[]): ReadonlySet<string> {
  return new Set(
    rows
      .filter((row) => row.kind === "code" && row.policy === "ordinary")
      .map((row) => row.workspace_id),
  );
}

export function WorkspacesPage({
  api,
  store,
  snapshot,
  onCommand,
  onOpenInCode,
}: WorkspacesPageProps) {
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const [focusTrustId, setFocusTrustId] = useState<string | null>(null);
  const [ticking, setTicking] = useState(false);
  const [tickError, setTickError] = useState<ShellApiError | null>(null);

  const listing = snapshot.workspaces;
  const workspaces = listing?.workspaces ?? [];
  // Gated on the store's own status, never on `errors.workspaces` being non-empty: that code
  // stays populated through an in-flight retry (see state/shell.ts), which must not re-show a
  // banner the retry might be about to clear.
  const workspacesReadFailed = snapshot.workspacesStatus === "error";
  // A quiet Refresh at the foot of the page, available whenever there is a listing to
  // re-read and the last read did not fail outright — the failure line below already offers its
  // own Refresh then, and the two must never both show at once.
  const canRefresh = listing !== null && !workspacesReadFailed;

  function refreshWorkspaces(): void {
    void store.afterMutation(["capabilities", "workspaces"]);
  }

  const activeSummary = snapshot.capabilities?.workspace.active ?? null;
  const activeId = activeSummary?.id ?? null;
  const selectedId = listing?.selected_id ?? null;
  const activeRow =
    activeId !== null ? workspaces.find((row) => row.workspace_id === activeId) : undefined;
  // The same fallback chain VaultCard.tsx computes for its own card's name: the registered row,
  // else the capabilities summary (an unregistered override), else "Chat only" when the active
  // summary is itself the shared scratch. Duplicated rather than imported: VaultCard exports no
  // helper for it, and this page's own `WorkspaceList` summary line needs only the name.
  const vaultName =
    activeRow?.name ??
    (activeSummary !== null && activeSummary.kind !== "scratch" ? activeSummary.name : null) ??
    t("workspaces.other.chat_only");

  // The active row is already represented above, in VaultCard; the list below is everything else
  // a chat can also work in.
  const codeRows = workspaces.filter((row) => row.kind === "code" && row.workspace_id !== activeId);
  const inUseIds = new Set<string>(
    [activeId, selectedId].filter((id): id is string => id !== null),
  );

  const openState = commandFor("workspace.open", snapshot);

  const ticked = new Set(parseTickedIds(snapshot.settings?.effective["chat.workspace_ids"]));
  const settingsBlocked = snapshot.settings === null || snapshot.settings.read_only === true;
  const tickBlockedReason: Key | null = ticking || settingsBlocked ? "error.read_only" : null;

  async function writeTicks(ids: readonly string[]): Promise<void> {
    setTicking(true);
    setTickError(null);
    try {
      await store.patch({ "chat.workspace_ids": JSON.stringify(ids) });
    } catch (error) {
      setTickError(error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"));
    } finally {
      setTicking(false);
    }
  }

  /** Writes the stored ticks ∩ `rows`' ordinary code ids, but only when that actually drops
   * something — a plain refresh must never provoke a write of its own. */
  async function pruneTicks(rows: readonly WorkspaceJson[]): Promise<void> {
    const allowed = ordinaryCodeIds(rows);
    const stored = parseTickedIds(snapshot.settings?.effective["chat.workspace_ids"]);
    const pruned = stored.filter((id) => allowed.has(id));
    if (pruned.length !== stored.length) await writeTicks(pruned);
  }

  function handlePolicyChanged(): void {
    void store.refreshWorkspaces().then(() => {
      void pruneTicks(store.getSnapshot().workspaces?.workspaces ?? []);
    });
  }

  async function handleRemove(id: string): Promise<string | null> {
    if (api === null) return errorText(new ShellApiError(0, "unavailable"));
    try {
      await api.removeWorkspace(crypto.randomUUID(), id);
    } catch (error) {
      const shellError =
        error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable");
      if (shellError.status !== 404) return errorText(shellError);
    }
    await pruneTicks(workspaces.filter((row) => row.workspace_id !== id));
    await store.afterMutation(["capabilities", "workspaces"]);
    return null;
  }

  function handleTrust(workspaceId: string): void {
    setDetailsId(workspaceId);
    setFocusTrustId(workspaceId);
  }

  function handleDetails(id: string): void {
    setDetailsId((current) => (current === id ? null : id));
  }

  const handleFocusDone = useCallback(() => setFocusTrustId(null), []);

  return (
    <section className="settings-page" aria-labelledby="workspaces-page-title">
      <h2 className="settings-page-title" id="workspaces-page-title">
        {t("settings.tab.workspaces")}
      </h2>
      <p className="settings-page-intro">{t("workspaces.lead")}</p>

      {/* A failed first read must never render as if zero vaults were registered; a failed
          refresh or re-read (Refresh, a removal, a policy change) keeps the stale parts on
          screen, with the line below saying the latest read did not land. */}
      {workspacesReadFailed && listing === null ? null : (
        <>
          <section className="ws-page-section">
            <VaultCard api={api} store={store} snapshot={snapshot} onCommand={onCommand} />
            <OtherVaults api={api} store={store} snapshot={snapshot} />
          </section>
          <section className="ws-page-section">
            <AddFolders
              heading={t("workspaces.list.heading")}
              api={api}
              state={openState}
              onAdded={() => store.afterMutation(["capabilities", "workspaces"])}
              onTrust={handleTrust}
            />
            <WorkspaceList
              rows={codeRows}
              vaultName={vaultName}
              ticked={ticked}
              tickBlockedReason={tickBlockedReason}
              onTick={(next) => void writeTicks(next)}
              inUseIds={inUseIds}
              detailsId={detailsId}
              onDetails={handleDetails}
              onTrust={handleTrust}
              focusTrustId={focusTrustId}
              onFocusDone={handleFocusDone}
              api={api}
              allWorkspaces={workspaces}
              onPolicyChanged={handlePolicyChanged}
              onRemove={handleRemove}
              {...(onOpenInCode ? { onOpenInCode } : {})}
            />
            {tickError !== null ? (
              <p className="settings-error" role="alert">
                {errorText(tickError)}
              </p>
            ) : null}
            {canRefresh ? (
              <button
                type="button"
                className="btn txt sm ws-page-refresh"
                onClick={refreshWorkspaces}
              >
                {t("common.button.refresh")}
              </button>
            ) : null}
          </section>
        </>
      )}

      {workspacesReadFailed ? (
        <p className="settings-state" role="status">
          {errorText(new ShellApiError(0, snapshot.errors.workspaces))}{" "}
          <button type="button" className="btn alt sm" onClick={refreshWorkspaces}>
            {t("common.button.refresh")}
          </button>
        </p>
      ) : null}
    </section>
  );
}
