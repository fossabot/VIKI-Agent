import { useCallback, useEffect, useRef, useState } from "react";
import { type CodeApi, CodeApiError, type TreeEntry } from "../../clients/code/codeApi";
import { FILE_DRAG_TYPE } from "../Panes/paneDrop";
import "./FileTree.css";

type Dir = {
  phase: "loading" | "ready" | "error";
  entries: TreeEntry[];
  truncated: boolean;
  code: string;
};

const childRef = (parent: string, name: string) => (parent ? `${parent}/${name}` : name);

export function FileTree({
  api,
  workspaceId,
  selected,
  onSelectFile,
  dragFiles,
}: {
  api: CodeApi;
  workspaceId: string;
  selected: string | null;
  onSelectFile: (ref: string) => void;
  /** When true, a file row is a drag source carrying `FILE_DRAG_TYPE`. */
  dragFiles?: boolean;
}) {
  const [dirs, setDirs] = useState<Record<string, Dir>>({});
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const controllers = useRef(new Map<string, AbortController>());

  const load = useCallback(
    (path: string) => {
      const controller = new AbortController();
      controllers.current.get(path)?.abort();
      controllers.current.set(path, controller);
      const current = () => controllers.current.get(path) === controller;
      setDirs((all) => ({
        ...all,
        [path]: { phase: "loading", entries: [], truncated: false, code: "" },
      }));
      api.tree(workspaceId, path, controller.signal).then(
        (tree) => {
          if (!current()) return;
          setDirs((all) => ({
            ...all,
            [path]: { phase: "ready", entries: tree.entries, truncated: tree.truncated, code: "" },
          }));
        },
        (error: unknown) => {
          if (!current() || controller.signal.aborted) return;
          const code = error instanceof CodeApiError ? error.code : "unavailable";
          setDirs((all) => ({
            ...all,
            [path]: { phase: "error", entries: [], truncated: false, code },
          }));
        },
      );
    },
    [api, workspaceId],
  );

  useEffect(() => {
    const running = controllers.current;
    setDirs({});
    setOpen(new Set());
    load("");
    return () => {
      for (const controller of running.values()) controller.abort();
      running.clear();
    };
  }, [load]);

  function toggle(ref: string) {
    const expanding = !open.has(ref);
    setOpen((was) => {
      const next = new Set(was);
      if (expanding) next.add(ref);
      else next.delete(ref);
      return next;
    });
    if (expanding && !dirs[ref]) load(ref);
  }

  function renderDir(path: string) {
    const dir = dirs[path];
    if (!dir || dir.phase === "loading") return null;
    if (dir.phase === "error") {
      return (
        <p role="alert">
          {dir.code === "root_unavailable"
            ? "The folder is not where it was registered."
            : "This folder could not be listed."}
        </p>
      );
    }
    return (
      <>
        {dir.entries.length === 0 && <p>Empty folder</p>}
        <ul>
          {dir.entries.map((entry) => {
            const ref = childRef(path, entry.name);
            if (entry.type === "dir") {
              const expanded = open.has(ref);
              return (
                <li key={entry.name}>
                  <button
                    type="button"
                    className="file-tree-row"
                    aria-expanded={expanded}
                    aria-current={selected === ref ? "true" : undefined}
                    onClick={() => toggle(ref)}
                  >
                    <svg
                      className="ico"
                      width="16"
                      height="16"
                      viewBox="0 0 16 16"
                      aria-hidden="true"
                      focusable="false"
                    >
                      <path
                        d={expanded ? "M2 7V3h5l2 2h5v2 M2 7h13l-2 7H2z" : "M2 3h5l2 2h5v9H2z"}
                      />
                    </svg>
                    {entry.name}
                  </button>
                  {expanded && renderDir(ref)}
                </li>
              );
            }
            if (entry.type === "file") {
              return (
                <li key={entry.name}>
                  <button
                    type="button"
                    className="file-tree-row"
                    aria-current={selected === ref ? "true" : undefined}
                    draggable={dragFiles || undefined}
                    onDragStart={
                      dragFiles
                        ? (event) =>
                            event.dataTransfer.setData(
                              FILE_DRAG_TYPE,
                              JSON.stringify({ workspace_id: workspaceId, ref }),
                            )
                        : undefined
                    }
                    onClick={() => onSelectFile(ref)}
                  >
                    <svg
                      className="ico"
                      width="16"
                      height="16"
                      viewBox="0 0 16 16"
                      aria-hidden="true"
                      focusable="false"
                    >
                      <path d="M3 2h6l4 4v8H3z M9 2v4h4" />
                    </svg>
                    {entry.name}
                  </button>
                </li>
              );
            }
            return <li key={entry.name}>{entry.name}</li>;
          })}
        </ul>
        {dir.truncated && <p>Showing the first 2,000 entries.</p>}
      </>
    );
  }

  return <div className="workbench-tree">{renderDir("")}</div>;
}
