/** The Workspaces list: each registered code folder as a row with a tick checkbox (the extra
 * folders new chats also work in, up to `MAX_TICKS`), its trust state, and the row menu (details,
 * open in Code, remove). `detailsId`/`focusTrustId` are props, not local state: the caller decides
 * which row's `PolicyPanel` is open and when focus should jump to its "Trust this folder" button —
 * this component only runs the one-shot focus search, copied from `WorkspacesPage.tsx`'s own
 * `useLayoutEffect` (its local copy stays until that page is rebuilt on top of this one).
 */
import { type ReactElement, useLayoutEffect, useRef, useState } from "react";
import type { ShellApi, WorkspaceJson } from "../../clients/app/shellApi";
import type { CommandState } from "../../commands";
import type { Key } from "../../i18n/index";
import { t, tf } from "../../i18n/t";
import "./Pages.css";
import { PolicyPanel } from "./PolicyPanel";
import { describedBy, ReasonHint, reasonTitle } from "./ReasonHint";
import "./Settings.css";
import "./WorkspaceList.css";
import { RemoveConfirm, WorkspaceRowMenu } from "./WorkspaceRowMenu";

export const MAX_TICKS = 7;

/** A row chats can be given as an extra working folder: a trusted, present, persistent code
 * registration. Scratch and vault rows, and an untrusted or missing code folder, never qualify. */
export function tickable(row: WorkspaceJson): boolean {
  return row.kind === "code" && row.policy === "ordinary" && row.root_available && row.persistent;
}

export interface WorkspaceListProps {
  rows: readonly WorkspaceJson[];
  vaultName: string;
  ticked: ReadonlySet<string>;
  tickBlockedReason: Key | null;
  onTick: (ids: readonly string[]) => void;
  inUseIds: ReadonlySet<string>;
  detailsId: string | null;
  onDetails: (id: string) => void;
  onTrust: (id: string) => void;
  focusTrustId: string | null;
  onFocusDone: () => void;
  api: ShellApi | null;
  allWorkspaces: readonly WorkspaceJson[];
  onPolicyChanged: () => void;
  onRemove: (id: string) => Promise<string | null>;
  onOpenInCode?: (id: string) => void;
}

/** One state chip per row: `protected` first since it is never trust-gated, then whether the
 * folder is there at all, then its trust. */
function stateChip(row: WorkspaceJson): string {
  if (row.policy === "protected") return t("workspaces.policy.protected");
  if (!row.root_available) return t("common.chip.not_found");
  return t(
    row.policy === "ordinary" ? "workspaces.code.chip.trusted" : "workspaces.code.chip.untrusted",
  );
}

/** Why the tick checkbox is off, most specific first: the row's own ineligibility (which folder
 * fact to blame), then the page-wide block the caller passed, then the cap (which only ever stops
 * a *new* tick — an already-ticked row stays enabled so it can still be unticked). `null` means
 * enabled, nothing to describe. */
function tickReason(
  row: WorkspaceJson,
  checked: boolean,
  atCap: boolean,
  tickBlockedReason: Key | null,
): Key | null {
  if (!tickable(row))
    return row.root_available ? "workspaces.row.hint_untrusted" : "workspaces.row.hint_missing";
  if (tickBlockedReason !== null) return tickBlockedReason;
  if (!checked && atCap) return "workspaces.row.tick_cap";
  return null;
}

export function WorkspaceList({
  rows,
  vaultName,
  ticked,
  tickBlockedReason,
  onTick,
  inUseIds,
  detailsId,
  onDetails,
  onTrust,
  focusTrustId,
  onFocusDone,
  api,
  allWorkspaces,
  onPolicyChanged,
  onRemove,
  onOpenInCode,
}: WorkspaceListProps): ReactElement {
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  // Each row's own <li>, keyed by workspace id, so the focus effect below searches only that
  // row's panel for its "Trust this folder" button, never another row's.
  const rowRefs = useRef(new Map<string, HTMLLIElement>());

  useLayoutEffect(() => {
    if (focusTrustId === null) return;
    const row = rowRefs.current.get(focusTrustId);
    if (row === undefined) return;
    const trustLabel = t("workspaces.code.trust");
    function focusTrustButton(panel: HTMLLIElement): boolean {
      const button = [...panel.querySelectorAll<HTMLButtonElement>("button")].find(
        (candidate) => candidate.textContent === trustLabel,
      );
      if (button === undefined) return false;
      button.focus();
      return true;
    }
    if (focusTrustButton(row)) {
      onFocusDone();
      return;
    }
    const observer = new MutationObserver(() => {
      if (focusTrustButton(row)) {
        observer.disconnect();
        onFocusDone();
      }
    });
    observer.observe(row, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [focusTrustId, onFocusDone]);

  function toggle(id: string): void {
    const next = new Set(ticked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    const sorted = rows
      .filter((row) => tickable(row) && next.has(row.workspace_id))
      .map((row) => row.workspace_id)
      .sort();
    onTick(sorted);
  }

  async function confirmRemove(id: string): Promise<void> {
    setRemoveBusy(true);
    setRemoveError(null);
    const error = await onRemove(id);
    setRemoveBusy(false);
    if (error === null) setRemovingId(null);
    else setRemoveError(error);
  }

  if (rows.length === 0) return <p className="settings-note">{t("workspaces.list.empty")}</p>;

  const tickedCount = rows.filter((row) => tickable(row) && ticked.has(row.workspace_id)).length;
  const atCap = tickedCount >= MAX_TICKS;

  return (
    <>
      <ul className="workspaces-list ws-list-card">
        {rows.map((row) => {
          const id = row.workspace_id;
          const checked = tickable(row) && ticked.has(id);
          const reason = tickReason(row, checked, atCap, tickBlockedReason);
          const tickState: CommandState = { enabled: reason === null, reason };
          const tickId = `ws-tick-${id}`;
          const tickReasonId = `ws-tick-reason-${id}`;
          const canRemove = row.policy !== "protected" && !inUseIds.has(id);
          const notFound = !row.root_available;

          return (
            <li
              key={id}
              data-workspace-id={id}
              className="workspace-row"
              ref={(element) => {
                if (element) rowRefs.current.set(id, element);
                else rowRefs.current.delete(id);
              }}
            >
              <div className="settings-card-head">
                <input
                  type="checkbox"
                  id={tickId}
                  className="ws-list-tick"
                  checked={checked}
                  aria-disabled={!tickState.enabled}
                  aria-describedby={describedBy(tickReasonId, tickState)}
                  title={reasonTitle(tickState)}
                  onChange={() => {
                    if (tickState.enabled) toggle(id);
                  }}
                />
                <label htmlFor={tickId} className="settings-card-title workspace-name">
                  {row.name}
                </label>
                <span className="chip dim">{stateChip(row)}</span>
                <div className="settings-actions">
                  <WorkspaceRowMenu
                    name={row.name}
                    onDetails={() => onDetails(id)}
                    {...(onOpenInCode ? { onOpenInCode: () => onOpenInCode(id) } : {})}
                    openDisabled={notFound}
                    {...(canRemove ? { onRemove: () => setRemovingId(id) } : {})}
                  />
                </div>
              </div>
              <ReasonHint id={tickReasonId} state={tickState} />
              <div className="mono workspace-root">{row.root}</div>
              {notFound ? (
                <p className="ws-list-hint settings-note">
                  {t("workspaces.row.hint_missing")}{" "}
                  {canRemove ? (
                    <button type="button" className="btn alt sm" onClick={() => setRemovingId(id)}>
                      {t("workspaces.row.remove")}
                    </button>
                  ) : null}
                </p>
              ) : row.policy === "existing_readonly" && detailsId !== id ? (
                <p className="ws-list-hint settings-note">
                  {t("workspaces.row.hint_untrusted")}{" "}
                  <button type="button" className="btn alt sm" onClick={() => onTrust(id)}>
                    {t("workspaces.code.trust_open")}
                  </button>
                </p>
              ) : null}
              {removingId === id ? (
                <RemoveConfirm
                  name={row.name}
                  busy={removeBusy}
                  error={removeError}
                  onConfirm={() => void confirmRemove(id)}
                  onCancel={() => {
                    setRemovingId(null);
                    setRemoveError(null);
                  }}
                />
              ) : null}
              {detailsId === id ? (
                <PolicyPanel
                  workspaceId={id}
                  api={api}
                  workspaces={allWorkspaces}
                  onChanged={onPolicyChanged}
                />
              ) : null}
            </li>
          );
        })}
      </ul>
      <p className="ws-list-summary settings-note" role="status">
        {t("workspaces.summary.prefix")}{" "}
        <strong>
          {tickedCount === 1
            ? tf("workspaces.summary.one", { vault: vaultName })
            : tf("workspaces.summary.many", { vault: vaultName, n: tickedCount })}
        </strong>{" "}
        {t("workspaces.summary.rule")}
      </p>
    </>
  );
}
