import { useEffect, useState } from "react";
import {
  type ChangeEntry,
  type CodeApi,
  CodeApiError,
  type CodeChanges,
} from "../../clients/code/codeApi";

type State =
  | { phase: "loading" }
  | { phase: "ready"; data: CodeChanges }
  | { phase: "error"; code: string };

/** The words for a Git error code. */
export function errorWord(code: string): string {
  switch (code) {
    case "trust_required":
      return "Trust this folder in Settings → Workspaces to see its Git changes.";
    case "not_repository":
      return "This folder is not a Git repository.";
    case "not_repository_root":
      return "Git features need the repository's top folder.";
    default:
      return "Git is not available.";
  }
}

export function ChangesList({
  api,
  workspaceId,
  refreshKey,
  selected,
  onSelect,
}: {
  api: CodeApi;
  workspaceId: string;
  refreshKey: number;
  selected: string | null;
  onSelect: (change: ChangeEntry) => void;
}) {
  const [state, setState] = useState<State>({ phase: "loading" });

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is the caller's reload signal.
  useEffect(() => {
    const controller = new AbortController();
    setState({ phase: "loading" });
    api.changes(workspaceId, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ phase: "ready", data });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          phase: "error",
          code: error instanceof CodeApiError ? error.code : "unavailable",
        });
      },
    );
    return () => controller.abort();
  }, [api, workspaceId, refreshKey]);

  let body = null;
  if (state.phase === "error") {
    body = <p role="alert">{errorWord(state.code)}</p>;
  } else if (state.phase === "ready") {
    const { data } = state;
    if (data.state === "unknown") {
      body = <p role="alert">Git's status could not be read.</p>;
    } else {
      const head = data.head ? <span className="mono">{data.head}</span> : null;
      body = (
        <>
          <p>
            {data.detached ? (
              <>Detached at {head}</>
            ) : (
              <>
                {data.branch}
                {data.branch && head ? " " : null}
                {head}
              </>
            )}
            {typeof data.ahead === "number" && typeof data.behind === "number"
              ? ` ↑${data.ahead} ↓${data.behind}`
              : null}
          </p>
          {data.entries.length === 0 ? (
            <p>No changes.</p>
          ) : (
            <ul>
              {data.entries.map((entry) => {
                const label = (
                  <>
                    <span className="mono">{entry.index + entry.worktree}</span>{" "}
                    {entry.orig_path ? `${entry.orig_path} → ${entry.path}` : entry.path}
                  </>
                );
                return (
                  <li key={`${entry.orig_path ?? ""}>${entry.path}`}>
                    {entry.private ? (
                      <>
                        {label} <span>Private</span>
                      </>
                    ) : (
                      <button
                        type="button"
                        aria-current={selected === entry.path ? "true" : undefined}
                        onClick={() => onSelect(entry)}
                      >
                        {label}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {data.truncated && <p>Showing the first 2,000 changes.</p>}
        </>
      );
    }
  }
  return <div className="workbench-changes">{body}</div>;
}
