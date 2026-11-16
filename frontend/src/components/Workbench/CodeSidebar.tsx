import { type KeyboardEvent, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { CodeApi } from "../../clients/code/codeApi";
import { dirtyRefs, useCodeBuffers } from "../../state/codeBuffers";
import { ChangesList } from "./ChangesList";
import { FileTree } from "./FileTree";
import "./CodeSidebar.css";

export interface CodeQuote {
  workspaceId: string;
  text: string;
}

export interface CodeOpenRequest {
  readonly api: CodeApi;
  readonly folder: Readonly<WorkspaceJson>;
  readonly folderKey: string;
  readonly path: string;
  readonly withDiff: boolean;
}

export interface CodeSidebarProps {
  api: CodeApi;
  folders: readonly WorkspaceJson[];
  activeId: string | null;
  selected: CodeOpenRequest | null;
  onOpen: (request: CodeOpenRequest | null) => void;
  onOpenWorkbench: () => void;
}

/** Pin the registration's read identity, including a revoked policy or reused workspace ID. */
export function codeFolderKey(folder: Readonly<WorkspaceJson> | null): string {
  return folder
    ? JSON.stringify([
        folder.workspace_id,
        folder.root,
        folder.root_physical_id,
        folder.policy_revision,
        folder.policy,
        folder.root_available,
        folder.aliased,
      ])
    : "";
}

type Tab = "files" | "changes";
const TABS: readonly Tab[] = ["files", "changes"];

export function CodeSidebar({
  api,
  folders,
  activeId,
  selected,
  onOpen,
  onOpenWorkbench,
}: CodeSidebarProps) {
  useCodeBuffers();
  const [chosen, setChosen] = useState<string | null>(selected?.folder.workspace_id ?? activeId);
  const [tab, setTab] = useState<Tab>(selected?.withDiff ? "changes" : "files");
  const [refreshKey, setRefreshKey] = useState(0);
  const uid = useId();
  const registered = folders.filter((folder) => folder.kind === "code");
  const folder =
    registered.find((row) => row.workspace_id === chosen) ??
    registered.find((row) => row.workspace_id === activeId) ??
    registered[0] ??
    null;
  const folderKey = codeFolderKey(folder);
  const currentFolder = useRef(folderKey);
  const currentApi = useRef(api);
  currentFolder.current = folderKey;
  currentApi.current = api;
  const visible =
    selected && selected.folderKey === folderKey && selected.api === api ? selected : null;
  const unsavedRefs = folder ? dirtyRefs(folder.workspace_id) : [];

  function open(path: string, withDiff: boolean) {
    if (
      !folder?.root_available ||
      currentFolder.current !== folderKey ||
      currentApi.current !== api
    )
      return;
    // The registered row can be replaced or reused; the selection keeps its original identity.
    onOpen({ api, folder: { ...folder }, folderKey, path, withDiff });
  }

  function moveTab(event: KeyboardEvent<HTMLButtonElement>) {
    const index = TABS.indexOf(tab);
    const next =
      event.key === "ArrowRight"
        ? TABS[(index + 1) % TABS.length]
        : event.key === "ArrowLeft"
          ? TABS[(index + TABS.length - 1) % TABS.length]
          : event.key === "Home"
            ? TABS[0]
            : event.key === "End"
              ? TABS[TABS.length - 1]
              : null;
    if (!next) return;
    event.preventDefault();
    setTab(next);
    document.getElementById(`${uid}-tab-${next}`)?.focus();
  }

  return (
    <section className="code-sidebar" aria-label="Code sidebar">
      {!folder ? (
        <p>No code folders yet. Open one in Settings → Workspaces.</p>
      ) : (
        <>
          <div className="code-sidebar-folder">
            <label>
              Code folder
              <select
                value={folder.workspace_id}
                onChange={(event) => {
                  onOpen(null);
                  setChosen(event.target.value);
                }}
              >
                {registered.map((row) => (
                  <option key={row.workspace_id} value={row.workspace_id}>
                    {dirtyRefs(row.workspace_id).length > 0 ? `${row.name} (unsaved)` : row.name}
                  </option>
                ))}
              </select>
            </label>
            <span className="chip">{folder.policy === "ordinary" ? "Trusted" : "Not trusted"}</span>
          </div>
          {unsavedRefs.length > 0 && (
            <fieldset className="code-sidebar-unsaved">
              <legend>Unsaved changes</legend>
              <ul>
                {unsavedRefs.map((ref) => (
                  <li key={ref}>
                    <button
                      type="button"
                      className="btn txt sm"
                      disabled={!folder.root_available}
                      aria-current={visible?.path === ref ? "true" : undefined}
                      onClick={() => open(ref, false)}
                    >
                      {ref}
                    </button>
                  </li>
                ))}
              </ul>
            </fieldset>
          )}
          {!folder.root_available ? (
            <p role="alert">The folder is not where it was registered.</p>
          ) : (
            <>
              <div className="code-sidebar-tools">
                <div role="tablist" aria-label="Code navigation">
                  {TABS.map((id) => (
                    <button
                      type="button"
                      role="tab"
                      key={id}
                      id={`${uid}-tab-${id}`}
                      aria-controls={`${uid}-panel-${id}`}
                      aria-selected={tab === id}
                      tabIndex={tab === id ? 0 : -1}
                      className="btn txt sm"
                      onClick={() => setTab(id)}
                      onKeyDown={moveTab}
                    >
                      {id === "files" ? "Files" : "Changes"}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="btn txt sm"
                  onClick={() => {
                    setRefreshKey((key) => key + 1);
                    if (visible) open(visible.path, visible.withDiff);
                  }}
                >
                  Refresh
                </button>
              </div>
              <div
                className="code-sidebar-list"
                role="tabpanel"
                id={`${uid}-panel-${tab}`}
                aria-labelledby={`${uid}-tab-${tab}`}
              >
                {tab === "files" ? (
                  <FileTree
                    key={`${folderKey}:${refreshKey}`}
                    api={api}
                    workspaceId={folder.workspace_id}
                    selected={visible?.path ?? null}
                    onSelectFile={(ref) => open(ref, false)}
                    dragFiles
                  />
                ) : (
                  <ChangesList
                    key={folderKey}
                    api={api}
                    workspaceId={folder.workspace_id}
                    refreshKey={refreshKey}
                    selected={visible?.path ?? null}
                    onSelect={(change) => {
                      if (!change.private) open(change.path, true);
                    }}
                  />
                )}
              </div>
            </>
          )}
        </>
      )}
      <button
        type="button"
        className="btn txt sm code-sidebar-workbench"
        onClick={() => onOpenWorkbench()}
      >
        Open workbench
      </button>
    </section>
  );
}
