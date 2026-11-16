import { type CSSProperties, useCallback, useEffect, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type CodeApi,
  CodeApiError,
  type CodeDiff,
  type CodeFile,
} from "../../clients/code/codeApi";
import {
  claimEditor,
  editorOwner,
  getBuffer,
  releaseEditor,
  savedVersion,
  useCodeBuffers,
} from "../../state/codeBuffers";
import { CodeEditor } from "./CodeEditor";
import { CodeRecovery, type CodeRecoveryApi } from "./CodeRecovery";
import { type CodeOpenRequest, type CodeQuote, codeFolderKey } from "./CodeSidebar";
import { DiffView } from "./DiffView";
import { FileView } from "./FileView";
import "./CodePreview.css";

export interface CodePreviewProps {
  api: CodeApi;
  folders: readonly WorkspaceJson[];
  activeId: string | null;
  request: CodeOpenRequest;
  onQuote: (quote: CodeQuote) => void;
  onOpenInEditor: (workspaceId: string, ref: string, line: number | null) => void;
  onClose: () => void;
  fontSize: number;
  lineWrap: boolean;
  recovery?: CodeRecoveryApi;
  /** This view's name as the owner of a file's editor, one per file in this window. */
  viewId?: string;
  /** Names the view that holds the file's editor in this view's refusal. */
  ownerLabel?: (owner: string) => string;
  /** Shows the view that holds the file's editor; with it, a refusal offers Show editor. */
  onShowEditor?: (owner: string) => void;
  /** Shown in a pane beside the chat rather than over it: no Back to chat, and Close reads
   * "Close file". */
  inPane?: boolean;
  /** Whether the heading takes the keyboard focus on mount; read at mount only. */
  autoFocus?: boolean;
}

type Shown = { request: CodeOpenRequest } & (
  | { phase: "loading" }
  | { phase: "error"; code: string }
  | { phase: "ready"; file: CodeFile | null; diff: CodeDiff | null; diffFailed: boolean }
);

const ERRORS: Record<string, string> = {
  private_path: "This file is private and is not shown.",
  not_found: "This file no longer exists.",
  root_unavailable: "The folder is not where it was registered.",
  trust_required: "Trust this folder in Settings → Workspaces to see its Git changes.",
};

function focusHeading(element: HTMLHeadingElement | null) {
  element?.focus();
}

export function CodePreview({
  api,
  folders,
  activeId,
  request,
  onQuote,
  onOpenInEditor,
  onClose,
  fontSize,
  lineWrap,
  recovery,
  viewId,
  ownerLabel = () => "another pane",
  onShowEditor,
  inPane = false,
  autoFocus = true,
}: CodePreviewProps) {
  const [shown, setShown] = useState<Shown | null>(null);
  const [mode, setMode] = useState<"file" | "diff">("file");
  const [editing, setEditing] = useState<CodeOpenRequest | null>(null);
  const [readVersion, setReadVersion] = useState(0);
  const [recoveryVersion, setRecoveryVersion] = useState(0);
  const reopened = useRef<CodeOpenRequest | null>(null);
  const focusAfterEdit = useRef<CodeOpenRequest | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const focusOnMount = useRef(autoFocus);
  const bindHeading = useCallback((element: HTMLHeadingElement | null) => {
    heading.current = element;
    if (focusOnMount.current) focusHeading(element);
  }, []);
  const editButton = useRef<HTMLButtonElement | null>(null);
  const currentEditAllowed = useRef(false);
  const hintId = useId();
  const ownId = useId();
  const owner = viewId ?? ownId;
  useCodeBuffers();
  const retained = getBuffer(request.folder.workspace_id, request.path) !== undefined;
  const holder = editorOwner(request.folder.workspace_id, request.path);
  const elsewhere = holder !== undefined && holder !== owner ? holder : null;
  const saves = savedVersion(request.folder.workspace_id, request.path);
  const seenSaves = useRef({ request, saves });
  const refreshing = useRef<CodeOpenRequest | null>(null);
  const load = useRef<AbortController | null>(null);
  const closed = useRef<CodeOpenRequest | null>(null);
  const invalidated = useRef<CodeOpenRequest | null>(null);
  const folder = folders.find(
    (row) => row.kind === "code" && row.workspace_id === request.folder.workspace_id,
  );
  const folderKey = codeFolderKey(folder ?? null);
  const matches =
    api === request.api && folderKey === request.folderKey && folder?.root_available === true;
  // Returning to a previous registration cannot renew a selection already invalidated by it.
  if (!matches) invalidated.current = request;
  const admitted = matches && invalidated.current !== request;
  const current = useRef({ api, folderKey, request, activeId, admitted });
  current.current = { api, folderKey, request, activeId, admitted };
  const isCurrent = () =>
    current.current.admitted &&
    current.current.api === api &&
    current.current.folderKey === request.folderKey &&
    current.current.request === request &&
    closed.current !== request;
  const visible =
    admitted && closed.current !== request && shown?.request === request ? shown : null;
  const file = visible?.phase === "ready" ? visible.file : null;
  const editBlocked =
    request.folder.policy !== "ordinary"
      ? "Trust this folder to edit its files."
      : !request.folder.root_physical_id || request.folder.aliased || !folder?.persistent
        ? "This folder can't be saved to now."
        : file?.newline === "mixed"
          ? "This file mixes line endings, so it opens read-only. Edit it in VS Code."
          : [...request.path].length > 512
            ? "This file's path is too long to save from VIKI."
            : !file ||
                file.text === null ||
                file.sha256 === null ||
                file.encoding === null ||
                file.newline === null ||
                file.reason !== null
              ? "This file can't be edited here. Open it in VS Code."
              : elsewhere !== null
                ? `Editing in ${ownerLabel(elsewhere)}.`
                : null;
  currentEditAllowed.current = isCurrent() && editBlocked === null;
  const isEditing = editing === request && isCurrent() && editBlocked === null;

  useEffect(() => {
    if (!admitted || editBlocked !== null) setEditing(null);
  }, [admitted, editBlocked]);
  // Edit takes the file's editor for as long as this view edits the file: Close, another file, an
  // invalidation or unmounting frees it. While another view holds it, editBlocked refuses Edit.
  useEffect(() => {
    if (!isEditing) return;
    claimEditor(request.folder.workspace_id, request.path, owner);
    return () => releaseEditor(request.folder.workspace_id, request.path, owner);
  }, [isEditing, request.folder.workspace_id, request.path, owner]);
  // A save changed the file: a view that is not editing it reads it again, keeping the content it
  // shows, with its mode and scroll, until the new read lands; an editing view reads it again at
  // its editor's Close.
  const ready = visible?.phase === "ready";
  useEffect(() => {
    const seen = seenSaves.current;
    seenSaves.current = { request, saves };
    if (isEditing || seen.request !== request || seen.saves === saves) return;
    refreshing.current = ready ? request : null;
    setReadVersion((version) => version + 1);
  }, [request, saves, isEditing, ready]);
  useEffect(() => {
    if (focusAfterEdit.current !== request) return;
    if (!isCurrent()) {
      focusAfterEdit.current = null;
      return;
    }
    if (!visible || visible.phase === "loading") return;
    focusAfterEdit.current = null;
    (editButton.current ?? heading.current)?.focus();
  });

  // biome-ignore lint/correctness/useExhaustiveDependencies: readVersion is the reread signal of a clean editor Close or a save elsewhere.
  useEffect(() => {
    // A re-read after a save elsewhere leaves the shown content and its mode in place.
    const refresh = refreshing.current === request;
    refreshing.current = null;
    if (!admitted || closed.current === request) return;
    const controller = new AbortController();
    load.current = controller;
    const { signal } = controller;
    const withDiff = request.withDiff && reopened.current !== request;
    const stillCurrent = () =>
      !signal.aborted &&
      load.current === controller &&
      current.current.admitted &&
      current.current.api === api &&
      current.current.folderKey === request.folderKey &&
      current.current.request === request &&
      closed.current !== request;
    if (!refresh) setShown({ request, phase: "loading" });
    Promise.allSettled([
      api.file(request.folder.workspace_id, request.path, signal),
      withDiff
        ? api.diff(request.folder.workspace_id, request.path, signal)
        : Promise.resolve(null),
    ]).then(([file, diff]) => {
      if (!stillCurrent()) return;
      const readFile =
        file.status === "fulfilled" && file.value.path === request.path ? file.value : null;
      const readDiff =
        diff.status === "fulfilled" && diff.value?.path === request.path ? diff.value : null;
      const code =
        file.status === "rejected" && file.reason instanceof CodeApiError
          ? file.reason.code
          : "unavailable";
      // A deleted working file may have a diff; other refusals do not admit its content.
      if (!readFile && (code !== "not_found" || !readDiff)) {
        setShown({ request, phase: "error", code });
        return;
      }
      if (!refresh) setMode(readDiff?.text !== null && readDiff !== null ? "diff" : "file");
      setShown({
        request,
        phase: "ready",
        file: readFile,
        diff: readDiff,
        diffFailed: withDiff && readDiff === null,
      });
    });
    return () => controller.abort();
  }, [api, admitted, request, readVersion]);

  function closeEditor() {
    if (!isCurrent()) return;
    reopened.current = request;
    focusAfterEdit.current = request;
    setEditing(null);
    setMode("file");
    setShown({ request, phase: "loading" });
    setReadVersion((version) => version + 1);
  }

  function close() {
    if (current.current.request !== request) return;
    closed.current = request;
    load.current?.abort();
    setShown(null);
    onClose();
  }

  function viewer() {
    if (!admitted)
      return <p role="alert">This preview is no longer available. Choose the file again.</p>;
    if (closed.current === request) return null;
    if (!visible || visible.phase === "loading") return <p role="status">Loading file…</p>;
    if (visible.phase === "error")
      return <p role="alert">{ERRORS[visible.code] ?? "This file could not be opened."}</p>;
    const { file, diff } = visible;
    if (isEditing && file)
      return (
        <>
          {recovery && (
            <CodeRecovery
              key={request.folderKey}
              recovery={recovery}
              workspaceId={request.folder.workspace_id}
              refreshKey={recoveryVersion}
            />
          )}
          <CodeEditor
            key={`${request.folderKey}\0${request.path}`}
            api={request.api}
            workspaceId={request.folder.workspace_id}
            policyRevision={request.folder.policy_revision}
            rootPhysicalId={request.folder.root_physical_id ?? ""}
            file={file}
            fileVerified
            fontSize={fontSize}
            wrap={lineWrap}
            onBlocked={() => {
              if (isCurrent()) setRecoveryVersion((version) => version + 1);
            }}
            onClose={closeEditor}
          />
        </>
      );
    return (
      <>
        {visible.diffFailed && <p role="alert">The diff could not be loaded.</p>}
        {diff && file && (
          <div className="code-preview-modes">
            {(["file", "diff"] as const).map((view) => (
              <button
                key={view}
                type="button"
                className="btn txt sm"
                aria-pressed={mode === view}
                onClick={() => setMode(view)}
              >
                {view === "file" ? "File" : "Diff"}
              </button>
            ))}
          </div>
        )}
        {diff && (mode === "diff" || !file) ? (
          <DiffView diff={diff} />
        ) : file ? (
          <FileView
            key={`${request.folderKey}:${file.path}`}
            file={file}
            canQuote={request.folder.workspace_id === activeId}
            onQuote={(ref, start, end) => {
              if (
                !isCurrent() ||
                current.current.activeId !== request.folder.workspace_id ||
                ref !== request.path ||
                file.text === null
              )
                return;
              onQuote({
                workspaceId: request.folder.workspace_id,
                text: `\`${ref}:${start === end ? start : `${start}-${end}`}\``,
              });
            }}
            onOpen={(line) => {
              if (isCurrent()) onOpenInEditor(request.folder.workspace_id, file.path, line);
            }}
          />
        ) : null}
      </>
    );
  }

  return (
    <section
      className="code-preview"
      aria-label="Code content"
      data-wrap={lineWrap ? "" : undefined}
      style={{ "--code-font-size": `${fontSize}px` } as CSSProperties}
    >
      <header className="code-preview-heading">
        <h2 className="mono" ref={bindHeading} tabIndex={-1}>
          {request.path}
        </h2>
        <div className="code-preview-actions">
          {!isEditing && file?.text !== null && file && (
            <button
              ref={editButton}
              type="button"
              className="btn txt sm code-preview-edit"
              aria-disabled={editBlocked !== null}
              aria-describedby={editBlocked !== null ? hintId : undefined}
              onClick={() => {
                if (!isCurrent() || !currentEditAllowed.current || editBlocked !== null) return;
                setMode("file");
                setEditing(request);
              }}
            >
              <svg
                className="ico"
                viewBox="0 0 16 16"
                width="16"
                height="16"
                aria-hidden="true"
                focusable="false"
              >
                <path
                  d="m10 3 3 3M3 10l7-7a2.1 2.1 0 0 1 3 3l-7 7-4 1z"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Edit
            </button>
          )}
          {inPane ? null : (
            <button type="button" className="btn txt sm" onClick={close}>
              Back to chat
            </button>
          )}
          <button type="button" className="btn txt sm" onClick={close}>
            {inPane ? "Close file" : "Close code preview"}
          </button>
        </div>
      </header>
      <div className="code-preview-content">
        {!isEditing && file?.text !== null && file && editBlocked !== null && (
          <>
            <p id={hintId} className="code-reason">
              {editBlocked}
            </p>
            {elsewhere !== null && onShowEditor && (
              <button
                type="button"
                className="btn txt sm"
                onClick={() => {
                  if (isCurrent()) onShowEditor(elsewhere);
                }}
              >
                Show editor
              </button>
            )}
          </>
        )}
        {/* While another view holds the editor, the refusal says where the edit is. */}
        {retained && !isEditing && elsewhere === null && (
          <p role="status" className="code-preview-unsaved">
            Unsaved changes are kept in this window.{" "}
            {editBlocked === null && isCurrent()
              ? "Choose Edit to resume them."
              : "Use Open workbench to recover them if this file can't be edited here."}
          </p>
        )}
        {viewer()}
      </div>
    </section>
  );
}
