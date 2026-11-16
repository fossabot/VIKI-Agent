import { useId } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";

export const MAX_SESSION_FOLDERS = 8;

export function SessionFolders({
  primary,
  folders,
  selectedIds,
  disabled,
  onChange,
}: {
  primary: WorkspaceJson;
  folders: readonly WorkspaceJson[];
  selectedIds: readonly string[];
  disabled: boolean;
  onChange(workspaceId: string, selected: boolean): void;
}) {
  const hintId = useId();
  return (
    <fieldset className="workbench-session-folders" disabled={disabled}>
      <legend>Folders for this conversation</legend>
      <p>
        <strong>Primary folder (working directory)</strong>: {primary.name}
        <code>{primary.root}</code>
      </p>
      <p className="code-reason">
        Selected folders add context to one conversation. Claude may ask before accessing additional
        folders.
      </p>
      <ul>
        {folders
          .filter(
            (folder) => folder.kind === "code" && folder.workspace_id !== primary.workspace_id,
          )
          .map((folder) => {
            const selected = selectedIds.includes(folder.workspace_id);
            const reason =
              folder.policy !== "ordinary"
                ? "Trust this folder in Workspaces settings first."
                : !folder.root_available
                  ? "This folder is unavailable."
                  : !folder.persistent
                    ? "Register this folder in Workspaces settings first."
                    : !selected && selectedIds.length >= MAX_SESSION_FOLDERS - 1
                      ? "A conversation can include up to 8 folders. Remove an additional folder first."
                      : null;
            const reasonId = `${hintId}-${folder.workspace_id}`;
            return (
              <li key={folder.workspace_id}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={disabled || reason !== null}
                    aria-describedby={reason !== null ? reasonId : undefined}
                    onChange={(event) => onChange(folder.workspace_id, event.target.checked)}
                  />
                  <span>
                    {folder.name} <code>{folder.root}</code>
                  </span>
                </label>
                {reason !== null && (
                  <span id={reasonId} className="code-reason">
                    {reason}
                  </span>
                )}
              </li>
            );
          })}
      </ul>
    </fieldset>
  );
}
