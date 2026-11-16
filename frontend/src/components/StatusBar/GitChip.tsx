/**
 * The status bar's git chip: a disclosure button with a popover, so a code workspace's git
 * status can be shown the same way as the vault's. Renders nothing without a vault or a code
 * folder to report on (`code` wins when both are given, since the two never describe the same
 * thing on screen at once).
 *
 * A code folder's status is polled: one `changes` read, armed again REFRESH_MS after the
 * previous one settles (never a plain interval, so overlapping requests never pile up) — the
 * same re-arm-on-settle shape `state/routines.ts` polls with, at the same interval. The chip
 * keeps showing its last answer while a refresh is in flight, including across a `refreshKey`
 * bump; only a `workspaceId` change (a different code target) resets it to "checking".
 */
import { type FocusEvent, useEffect, useId, useRef, useState } from "react";
import {
  type ChangeEntry,
  type CodeApi,
  CodeApiError,
  type CodeChanges,
} from "../../clients/code/codeApi";
import { errorWord } from "../Workbench/ChangesList";
import "./StatusBar.css";

export interface GitStatus {
  branch: string;
  dirty: boolean | null;
  sha: string;
  refState?: "branch" | "detached" | "unknown";
  checking?: boolean;
}

export interface GitChipProps {
  /** The vault's own git status. Ignored while `code` is given. */
  vault?: GitStatus;
  /** The vault's last commit, shown as a popover row and folded into the trigger's title. */
  lastCommit?: { sha: string; time: string };
  vaultName?: string;
  /** A code folder's git status instead of the vault's: read and polled by this component. */
  code?: { api: CodeApi; workspaceId: string; name: string; onOpen: (path: string) => void };
  /** Bumped by the caller (e.g. a Refresh button) to force an immediate re-read. */
  refreshKey?: string;
}

type CodeTarget = NonNullable<GitChipProps["code"]>;

type CodeState =
  | { phase: "loading" }
  | { phase: "ready"; workspaceId: string; data: CodeChanges }
  | { phase: "error"; workspaceId: string; code: string };

// The rail's own refresh interval (state/routines.ts's default intervalMs).
const REFRESH_MS = 60_000;

function vaultBranchWord(vault: GitStatus): string {
  return vault.branch || (vault.refState === "detached" ? "detached HEAD" : "Git");
}

function vaultStateWord(vault: GitStatus): string {
  if (vault.dirty === null) return vault.checking ? "checking" : "status unknown";
  return vault.dirty ? "dirty" : "clean";
}

function vaultLabel(vault: GitStatus): string {
  return `${vaultBranchWord(vault)} · ${vaultStateWord(vault)}${vault.sha ? ` · ${vault.sha}` : ""}`;
}

/** "" outside the one phase that actually knows a branch. */
function codeBranchWord(state: CodeState): string {
  if (state.phase !== "ready") return "";
  if (state.data.state === "unknown") return "";
  return state.data.detached ? "detached HEAD" : state.data.branch || "Git";
}

function codeStateWord(state: CodeState): string {
  if (state.phase === "loading") return "checking";
  if (state.phase === "error") return "status unknown";
  if (state.data.state === "unknown") return "status unknown";
  const n = state.data.entries.length;
  return n ? `${n}${state.data.truncated ? "+" : ""} changed` : "clean";
}

function codeHead(state: CodeState): string {
  if (state.phase !== "ready") return "";
  if (state.data.state === "unknown" || !state.data.head) return "";
  return state.data.head.slice(0, 7);
}

function codeLabel(name: string, state: CodeState): string {
  const head = codeHead(state);
  return `${codeBranchWord(state) || name} · ${codeStateWord(state)}${head ? ` · ${head}` : ""}`;
}

/** The shared shape both modes render through, so the markup below never branches on `code`
 * versus `vault` itself — only on the fields of whichever view was built. */
interface ChipView {
  label: string;
  title: string;
  repository: string;
  branch: string;
  state: string;
  head: string;
  lastCommit: string;
  /** The text shared by the hidden description span and the title's own suffix. */
  descriptionText: string | null;
  errorText: string | null;
  showEntries: boolean;
  entries: readonly ChangeEntry[];
}

function codeView(code: CodeTarget, state: CodeState): ChipView {
  // A settled answer belongs to the workspace it was read for. Once `code.workspaceId` moves on
  // (a caller that changes folder without a new key), a stored answer for the old one renders as
  // not-yet-answered rather than as that old folder's status.
  const current: CodeState =
    state.phase !== "loading" && state.workspaceId !== code.workspaceId
      ? { phase: "loading" }
      : state;
  let errorText: string | null = null;
  let showEntries = false;
  let entries: readonly ChangeEntry[] = [];
  if (current.phase === "error") {
    errorText = errorWord(current.code);
  } else if (current.phase === "ready") {
    if (current.data.state === "unknown") errorText = errorWord("unavailable");
    else {
      showEntries = true;
      entries = current.data.entries;
    }
  }
  return {
    label: codeLabel(code.name, current),
    title: `Git: ${code.name}`,
    repository: `${code.name} (code folder)`,
    branch: codeBranchWord(current),
    state: codeStateWord(current),
    head: codeHead(current),
    lastCommit: "",
    descriptionText: null,
    errorText,
    showEntries,
    entries,
  };
}

function vaultView(
  vault: GitStatus,
  vaultName: string | undefined,
  lastCommit: { sha: string; time: string } | undefined,
): ChipView {
  const name = vaultName ?? "vault";
  const descriptionText = lastCommit ? `last commit ${lastCommit.sha} ${lastCommit.time}` : null;
  return {
    label: vaultLabel(vault),
    title: descriptionText ? `Git: ${name} · ${descriptionText}` : `Git: ${name}`,
    repository: `${name} (vault)`,
    branch: vaultBranchWord(vault),
    state: vaultStateWord(vault),
    head: vault.sha,
    lastCommit: lastCommit?.time ?? "",
    descriptionText,
    errorText: null,
    showEntries: false,
    entries: [],
  };
}

function row(term: string, value: string) {
  return value ? (
    <>
      <dt>{term}</dt>
      <dd>{value}</dd>
    </>
  ) : null;
}

export function GitChip({ vault, lastCommit, vaultName, code, refreshKey }: GitChipProps) {
  const [open, setOpen] = useState(false);
  const [codeState, setCodeState] = useState<CodeState>({ phase: "loading" });
  const wrapRef = useRef<HTMLSpanElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const descId = useId();

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is the caller's reload signal.
  useEffect(() => {
    // One `changes` read, armed again REFRESH_MS after the previous one settles — never a
    // plain interval, so overlapping requests never pile up. The last answer stays on screen
    // while a re-read is in flight; a genuinely new target (a different workspaceId) still
    // shows "checking" — codeView masks a stored answer that doesn't match it.
    if (!code) return;
    const { api, workspaceId } = code;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    function read() {
      api.changes(workspaceId, controller.signal).then(
        (data) => {
          if (controller.signal.aborted) return;
          setCodeState({ phase: "ready", workspaceId, data });
          timer = setTimeout(read, REFRESH_MS);
        },
        (error: unknown) => {
          if (controller.signal.aborted) return;
          setCodeState({
            phase: "error",
            workspaceId,
            code: error instanceof CodeApiError ? error.code : "unavailable",
          });
          timer = setTimeout(read, REFRESH_MS);
        },
      );
    }
    read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [code?.api, code?.workspaceId, refreshKey]);

  // Escape and outside mousedown. Focus leaving the wrap (Tab or otherwise) is the onBlur
  // handler below, not a window listener: closing it here as well would race, and the window
  // keydown can't tell a Tab that stays inside the popover from one that leaves it.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        setOpen(false);
        toggleRef.current?.focus();
        return;
      }
    }
    function onOutside(event: MouseEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    }
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("mousedown", onOutside);
    popoverRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("mousedown", onOutside);
    };
  }, [open]);

  function onWrapBlur(event: FocusEvent<HTMLSpanElement>): void {
    if (wrapRef.current && !wrapRef.current.contains(event.relatedTarget as Node)) setOpen(false);
  }

  let view: ChipView;
  if (code) {
    view = codeView(code, codeState);
  } else if (vault) {
    view = vaultView(vault, vaultName, lastCommit);
  } else {
    return null;
  }

  return (
    // The wrap is a focus-containment boundary for the popover disclosure below, not a control:
    // onBlur only detects focus leaving it, the same role onOutside's mousedown listener plays.
    // biome-ignore lint/a11y/noStaticElementInteractions: see above
    <span className="statusbar-git" ref={wrapRef} onBlur={onWrapBlur}>
      <button
        type="button"
        ref={toggleRef}
        className="git-chip mono"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={view.title}
        aria-describedby={view.descriptionText !== null ? descId : undefined}
        onClick={() => setOpen((shown) => !shown)}
      >
        <span className="git-dot" aria-hidden="true" />
        <span>{view.label}</span>
      </button>
      {view.descriptionText !== null ? (
        <span id={descId} hidden>
          {view.descriptionText}
        </span>
      ) : null}
      {open ? (
        <div
          role="dialog"
          aria-label="Git"
          className="git-popover menu-panel"
          tabIndex={-1}
          ref={popoverRef}
        >
          <dl>
            {row("Repository", view.repository)}
            {row("Branch", view.branch)}
            {row("State", view.state)}
            {row("HEAD", view.head)}
            {row("Last commit", view.lastCommit)}
          </dl>
          {view.errorText !== null ? (
            <p>{view.errorText}</p>
          ) : view.showEntries ? (
            <ul>
              {view.entries.slice(0, 10).map((entry) => {
                const text = (
                  <>
                    <span className="mono">{entry.index + entry.worktree}</span>{" "}
                    {entry.orig_path ? `${entry.orig_path} → ${entry.path}` : entry.path}
                  </>
                );
                return (
                  <li key={`${entry.orig_path ?? ""}>${entry.path}`}>
                    {entry.private ? (
                      <>{text} Private</>
                    ) : (
                      <button
                        type="button"
                        className="btn txt sm"
                        onClick={() => {
                          setOpen(false);
                          code?.onOpen(entry.path);
                        }}
                      >
                        {text}
                      </button>
                    )}
                  </li>
                );
              })}
              {view.entries.length > 10 ? <li>{`and ${view.entries.length - 10} more`}</li> : null}
            </ul>
          ) : null}
        </div>
      ) : null}
    </span>
  );
}
