import { useEffect, useState } from "react";
import { type CodeApi, CodeApiError } from "../../clients/code/codeApi";

const DEBOUNCE_MS = 250;
type Result = { hits: string[]; truncated: boolean } | { code: string };

function searchErrorWord(code: string): string {
  switch (code) {
    case "too_large":
      return "This repository's index is too large to search.";
    case "not_repository":
      return "This folder is not a Git repository.";
    case "not_repository_root":
      return "Git features need the repository's top folder.";
    default:
      return "Search failed.";
  }
}

export function QuickOpen({
  api,
  workspaceId,
  enabled,
  onSelectFile,
}: {
  api: CodeApi;
  workspaceId: string;
  enabled: boolean;
  onSelectFile: (ref: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    const trimmed = query.trim();
    setResult(null);
    if (!enabled || !trimmed) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api.search(workspaceId, trimmed, controller.signal).then(
        (found) => {
          if (!controller.signal.aborted) setResult(found);
        },
        (error: unknown) => {
          if (controller.signal.aborted) return;
          setResult({ code: error instanceof CodeApiError ? error.code : "unavailable" });
        },
      );
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, workspaceId, enabled, query]);

  let body = null;
  if (!enabled) {
    body = <p>Trust this folder to search its tracked files.</p>;
  } else if (result && "code" in result) {
    body = <p role="alert">{searchErrorWord(result.code)}</p>;
  } else if (result) {
    body = (
      <>
        {result.hits.length === 0 ? (
          <p>No matching tracked files.</p>
        ) : (
          <ul>
            {result.hits.map((hit) => (
              <li key={hit}>
                <button type="button" onClick={() => onSelectFile(hit)}>
                  {hit}
                </button>
              </li>
            ))}
          </ul>
        )}
        {result.truncated && <p>Showing the first 50 matches.</p>}
      </>
    );
  }

  return (
    <div className="workbench-quickopen">
      <input
        type="search"
        aria-label="Find a file"
        value={query}
        disabled={!enabled}
        onChange={(event) => setQuery(event.target.value)}
      />
      {body}
    </div>
  );
}
