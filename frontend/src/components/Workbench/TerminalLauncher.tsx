import { useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { type TerminalApi, TerminalApiError } from "../../clients/code/terminalApi";
import { useRoots } from "../../state/sessionRoots";
import { rootRefusal } from "./Worktrees";

const UNSUPPORTED_PLATFORM = "The terminal needs Windows, macOS or Linux.";
const NOT_READY = "This worktree is not ready.";

const REFUSALS: Record<string, string> = {
  start_failed: "The terminal could not start.",
  unsupported_platform: UNSUPPORTED_PLATFORM,
  worktree_not_ready: NOT_READY,
  unknown_worktree: NOT_READY,
};

/** The words for a terminal refusal code. */
export function terminalRefusal(code: string): string {
  const words = Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : undefined;
  return words ?? rootRefusal(code);
}

const copy = {
  note: "Opens a terminal in its own window, in this folder or one of its worktrees, with your permissions. It keeps running after VIKI closes.",
  openIn: "Open in",
  thisFolder: "This folder",
  worktree: (branch: string) => `${branch} (worktree)`,
  open: "Open terminal here",
  opening: "Opening…",
  opened: "Opened the terminal.",
};

/** An unsupported platform or an untrusted folder shows its refusal without starting anything. */
export function TerminalLauncher({
  api,
  folder,
  canOpen,
}: {
  api: TerminalApi;
  folder: WorkspaceJson;
  canOpen: boolean;
}) {
  if (!canOpen) return <p>{terminalRefusal("unsupported_platform")}</p>;
  if (folder.policy !== "ordinary") return <p>{rootRefusal("trust_required")}</p>;
  return <Panel api={api} folder={folder} />;
}

function Panel({ api, folder }: { api: TerminalApi; folder: WorkspaceJson }) {
  const ready = useRoots().worktrees.filter(
    (row) => row.workspace_id === folder.workspace_id && row.state === "ready",
  );
  const [chosen, setChosen] = useState<string | null>(null);
  // A worktree that is gone or not ready falls back to the folder itself.
  const worktreeId = ready.find((row) => row.worktree_id === chosen)?.worktree_id ?? null;
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const call = useRef<AbortController | null>(null);

  async function open() {
    if (busy) return;
    call.current?.abort();
    const controller = new AbortController();
    call.current = controller;
    setBusy(true);
    setRefusal(null);
    setMessage("");
    try {
      await api.open(folder.workspace_id, worktreeId, controller.signal);
      setMessage(copy.opened);
    } catch (error) {
      setRefusal(error instanceof TerminalApiError ? error.code : "unavailable");
    }
    setBusy(false);
  }

  return (
    <div className="terminal-launcher">
      <p>{copy.note}</p>
      <label>
        {copy.openIn}
        <select
          value={worktreeId ?? ""}
          onChange={(event) => setChosen(event.target.value || null)}
        >
          <option value="">{copy.thisFolder}</option>
          {ready.map((row) => (
            <option key={row.worktree_id} value={row.worktree_id}>
              {copy.worktree(row.branch)}
            </option>
          ))}
        </select>
      </label>
      <button type="button" className="btn go sm" disabled={busy} onClick={() => void open()}>
        {busy ? copy.opening : copy.open}
      </button>
      <p role="status" aria-live="polite">
        {message}
      </p>
      {refusal !== null && <p role="alert">{terminalRefusal(refusal)}</p>}
    </div>
  );
}
