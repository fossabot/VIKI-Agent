/**
 * The "Add folders" flow on the Workspaces settings page: the OS multi-select folder
 * picker by default, with "Type a path instead" as the typed fallback — the single-folder
 * code form (replay-on-retry and field-error mechanics), plus one result row of its own on
 * success. Every opened path becomes a
 * code-kind registration through `ShellApi.openCodeFolder`; its `created` flag tells a fresh
 * registration (201) from one already on file (200) — the backend doubles as the contract
 * (`tests/workspace_code/test_workspace_api_code.py`'s `test_open_code_folder_returns_201_then_200`).
 */
import { type ReactNode, useRef, useState } from "react";
import type { OpenVaultResult, ShellApi } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CommandState } from "../../commands";
import { errorText, t, tf } from "../../i18n/t";
import { EXAMPLE_PATH_KEY, platformKind } from "../../platform";
import { pickerReasonText, useFolderPicker } from "../Onboarding/FolderPicker";
import { PathForm } from "../Onboarding/PathForm";
import { ambiguousFailure, fieldMessage } from "../Onboarding/useVaultAction";
import "./AddFolders.css";
import { describedBy, ReasonHint, reasonTitle } from "./ReasonHint";

export interface AddFoldersProps {
  heading: string;
  api: ShellApi | null;
  state: CommandState;
  /** Awaited once processing finishes: a capabilities+workspaces refresh, same as every other
   * registration action on this page. Documented as never rejecting (its own refreshes do not). */
  onAdded: () => Promise<void>;
  /** An added row's own Trust… (not-ordinary rows only); the caller decides what trusting means. */
  onTrust: (workspaceId: string) => void;
}

type ResultRow =
  | { kind: "added"; workspaceId: string; name: string; chip: string; ordinary: boolean }
  | { kind: "already"; name: string }
  | { kind: "refused"; path: string; reason: string };

/** The last "/" or "\\"-separated segment, for a refused row's own name — there is no
 * `WorkspaceJson` to read one from when the path was never registered. */
function lastSegment(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut === -1 ? path : path.slice(cut + 1);
}

function toShellError(caught: unknown): ShellApiError {
  return caught instanceof ShellApiError ? caught : new ShellApiError(0, "unavailable");
}

/** `result.created` is the 201/200 split. The chip text mirrors WorkspacesPage's own
 * `policyChip` for a code row: every folder this flow opens is `kind: "code"`, so only the
 * trusted/untrusted split actually applies.
 * Treats any non-"ordinary" policy as untrusted; "protected" never reaches a
 * code-kind workspace in practice, so that third policyChip branch is not reproduced here. */
function successRow(result: OpenVaultResult): ResultRow {
  const { workspace } = result;
  if (!result.created) return { kind: "already", name: workspace.name };
  const ordinary = workspace.policy === "ordinary";
  return {
    kind: "added",
    workspaceId: workspace.workspace_id,
    name: workspace.name,
    chip: t(ordinary ? "workspaces.code.chip.trusted" : "workspaces.code.chip.untrusted"),
    ordinary,
  };
}

function resultSummary(rows: readonly ResultRow[]): string {
  const added = rows.filter((row) => row.kind === "added").length;
  return rows.length === 1
    ? tf("workspaces.add.result_one", { added })
    : tf("workspaces.add.result", { added, total: rows.length });
}

function resultRow(
  row: ResultRow,
  index: number,
  onTrust: (workspaceId: string) => void,
): ReactNode {
  if (row.kind === "already") {
    return (
      <li key={index}>
        <span aria-hidden="true">–</span>
        <span className="ws-add-row-text">
          {row.name} {t("workspaces.add.already")}
        </span>
      </li>
    );
  }
  if (row.kind === "refused") {
    return (
      <li key={index}>
        <span aria-hidden="true">✕</span>
        <span className="ws-add-row-text">
          {lastSegment(row.path)} {tf("workspaces.add.refused", { reason: row.reason })}
        </span>
      </li>
    );
  }
  return (
    <li key={index}>
      <span aria-hidden="true">✓</span>
      <span className="ws-add-row-text">
        {row.name} {tf("workspaces.add.added", { state: row.chip })}
      </span>
      {!row.ordinary ? (
        <button type="button" className="btn alt sm" onClick={() => onTrust(row.workspaceId)}>
          {t("workspaces.code.trust_open")}
        </button>
      ) : null}
    </li>
  );
}

export function AddFolders({ heading, api, state, onAdded, onTrust }: AddFoldersProps) {
  const picker = useFolderPicker();
  const [typedOpen, setTypedOpen] = useState(false);
  const [typedPath, setTypedPath] = useState("");
  const [typedError, setTypedError] = useState<string | null>(null);
  const [typedBusy, setTypedBusy] = useState(false);
  const typedCommandIdRef = useRef(crypto.randomUUID());
  // The path last submitted, so a same-path retry after an ambiguous failure replays its id —
  // the exact mechanics WorkspacesPage's old code form used.
  const typedFailedPathRef = useRef<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<ShellApiError | null>(null);
  const [results, setResults] = useState<ResultRow[] | null>(null);

  const available = picker?.status.available === true;
  const disabled = !state.enabled;

  async function openPaths(paths: string[]): Promise<void> {
    if (api === null) return;
    const rows: ResultRow[] = [];
    for (const path of paths) {
      try {
        rows.push(successRow(await api.openCodeFolder(crypto.randomUUID(), path)));
      } catch (caught) {
        rows.push({ kind: "refused", path, reason: errorText(toShellError(caught)) });
      }
    }
    setResults(rows);
    await onAdded();
  }

  async function pickFolders(): Promise<void> {
    if (api === null || picker === null || picking) return;
    setPicking(true);
    setPickError(null);
    try {
      const paths = await picker.pick(t("workspaces.add.pick_title"), picker.status.multiple);
      if (paths.length > 0) await openPaths(paths);
    } catch (caught) {
      setPickError(toShellError(caught));
    } finally {
      setPicking(false);
    }
  }

  async function submitTyped(): Promise<void> {
    if (api === null || typedPath === "" || typedBusy) return;
    if (typedFailedPathRef.current !== typedPath) typedCommandIdRef.current = crypto.randomUUID();
    setTypedBusy(true);
    setTypedError(null);
    try {
      const result = await api.openCodeFolder(typedCommandIdRef.current, typedPath);
      typedFailedPathRef.current = null;
      setResults([successRow(result)]);
      setTypedOpen(false);
      setTypedPath("");
      await onAdded();
    } catch (caught) {
      const shellError = toShellError(caught);
      typedFailedPathRef.current = ambiguousFailure(shellError) ? typedPath : null;
      setTypedError(fieldMessage(shellError));
    } finally {
      setTypedBusy(false);
    }
  }

  return (
    <div className="ws-add">
      <h3 className="lbl">{heading}</h3>
      {available ? (
        <div className="settings-actions">
          <button
            type="button"
            className="btn go sm"
            aria-disabled={disabled || picking}
            aria-describedby={describedBy("ws-add-reason", state)}
            title={reasonTitle(state)}
            onClick={() => {
              if (state.enabled && !picking) void pickFolders();
            }}
          >
            {t("workspaces.add.button")}
          </button>
          <button
            type="button"
            className="btn txt sm"
            aria-disabled={disabled}
            aria-describedby={describedBy("ws-add-reason", state)}
            title={reasonTitle(state)}
            onClick={() => {
              if (state.enabled) setTypedOpen((open) => !open);
            }}
          >
            {t("workspaces.type_path")}
          </button>
        </div>
      ) : (
        <p className="settings-note">{pickerReasonText(picker?.status.reason ?? null)}</p>
      )}
      <ReasonHint id="ws-add-reason" state={state} />
      {typedOpen || !available ? (
        <form
          className="onboarding-form"
          aria-label={t("workspaces.add.button")}
          aria-busy={typedBusy}
          onSubmit={(event) => event.preventDefault()}
        >
          <PathForm
            id="open-code-path"
            label={t("workspaces.open_code.path")}
            value={typedPath}
            onChange={setTypedPath}
            onEnter={() => void submitTyped()}
            error={typedError ?? undefined}
            exampleText={t(EXAMPLE_PATH_KEY[platformKind()])}
            disabled={typedBusy || disabled}
          />
          <button
            type="button"
            className="btn go sm"
            aria-disabled={typedBusy || typedPath === "" || disabled}
            aria-describedby={describedBy("ws-add-reason", state)}
            title={reasonTitle(state)}
            onClick={() => {
              if (!typedBusy && typedPath !== "" && state.enabled) void submitTyped();
            }}
          >
            {t("workspaces.add.submit")}
          </button>
        </form>
      ) : null}
      {pickError !== null ? (
        <p className="settings-error" role="alert">
          {errorText(pickError)}
        </p>
      ) : null}
      {results !== null ? (
        <div role="status" className="ws-add-results">
          <p>{resultSummary(results)}</p>
          <ul className="ws-add-rows">
            {results.map((row, index) => resultRow(row, index, onTrust))}
          </ul>
          <button type="button" className="btn alt sm" onClick={() => setResults(null)}>
            {t("workspaces.add.done")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
