import { useEffect, useId, useRef, useState } from "react";
import { type CodeApi, CodeApiError, type CodeFile } from "../../clients/code/codeApi";
import { platformKind } from "../../platform";
import {
  beginBufferSave,
  type CodeBufferBase,
  confirmBufferBase,
  dropBuffer,
  getBuffer,
  noteSaved,
  putBuffer,
  subscribeBufferBase,
  useCodeBuffers,
} from "../../state/codeBuffers";
// Types only: the module itself is reached through `import("./codemirror")` below, so the editor
// stays out of the entry chunk (editorBoundary.unit.test.ts).
import type { EditorHandle } from "./codemirror";
import "./CodeEditor.css";

export interface CodeEditorProps {
  api: CodeApi;
  workspaceId: string;
  policyRevision: number;
  rootPhysicalId: string;
  /** The file as just read, with text, hash, one of the four encodings and a newline but `mixed`.
   * Read once at mount: the caller keys the editor by folder and path. */
  file: CodeFile;
  /** False when a failed read reopened the file from its retained buffer's base. */
  fileVerified: boolean;
  fontSize: number;
  wrap: boolean;
  /** A save is stuck behind a recovery record: the caller refreshes its stopped-saves list. */
  onBlocked(): void;
  onClose(): void;
}

/** Built per render: the read-only refusal names the current platform's remedy. */
function editorCopy() {
  return {
    label: (path: string) => `Edit ${path}`,
    loading: "Loading the editor…",
    loadFailed: "The editor could not load.",
    retry: "Retry",
    save: "Save",
    revert: "Revert",
    close: "Close",
    closeHint: "Save or revert first",
    discard: "Discard your changes?",
    saved: "Saved.",
    diskVersion: "Disk version",
    useDisk: "Use this version",
    conflict:
      "This file changed elsewhere. Your edit is kept. Compare, then save again or use the disk version.",
    cannotEdit: "The file on disk can no longer be edited here.",
    mixed: "The file on disk has mixed line endings.",
    newline: { lf: "LF", crlf: "CRLF", cr: "CR", none: "None" },
    reasons: {
      binary: "This file is binary, so it is not shown.",
      unsupported_encoding: "This file's encoding is not supported, so it is not shown.",
      too_large: "This file is over 1 MiB, so it is not shown.",
    },
    refusal: {
      trust: "This folder isn't trusted, so it can't be saved to. Your edit is kept.",
      changed: "This folder changed. Reopen the Code view, then save again. Your edit is kept.",
      filesystem: "This folder's drive does not support saving from VIKI.",
      readOnly:
        platformKind() === "windows"
          ? "This file is read-only. Clear Read-only in its Properties, then save again."
          : "This file is read-only or locked. Make it writable, then save again.",
      unsafe:
        "VIKI can't save this file safely (it is a link or has other names). Edit it in VS Code.",
      missing: "This file was moved or deleted. Your edit is kept.",
      busy: "Another app is using this file. Close it, then save again.",
      tooLarge: "The saved file would be over 2 MiB, so it can't be saved.",
      invalid: "VIKI can't save this file.",
      recovery:
        "A previous save of this file needs recovery. Restart VIKI Agent, or forget its record under Stopped saves.",
      overloaded: "Saving is busy. Try again in a moment.",
      other: "The file could not be saved. Your edit is kept.",
    },
  };
}

type Copy = ReturnType<typeof editorCopy>;
type RefusalGroup = keyof Copy["refusal"];

// A Map, not an object: a code like "constructor" must fall through to `other`.
const REFUSAL_GROUPS = new Map<string, RefusalGroup>([
  ["trust_required", "trust"],
  ["workspace_changed", "changed"],
  ["root_unavailable", "changed"],
  ["unsupported_filesystem", "filesystem"],
  ["read_only_target", "readOnly"],
  ["unsafe_target", "unsafe"],
  ["unsafe_parent", "unsafe"],
  ["missing_target", "missing"],
  ["target_busy", "busy"],
  ["parent_busy", "busy"],
  ["stage_busy", "busy"],
  ["too_large", "tooLarge"],
  ["invalid_path", "invalid"],
  ["private_path", "invalid"],
  ["invalid_content", "invalid"],
  ["invalid_body", "invalid"],
  ["publication_busy", "recovery"],
  ["recovery_required", "recovery"],
  ["overloaded", "overloaded"],
  ["vault_locked", "overloaded"],
  ["publication_limit", "overloaded"],
]);

/** The file as a save base, or null when it cannot be edited here: no text, no hash, an encoding
 * outside the four, or mixed newlines. */
function editableBase(file: CodeFile): CodeBufferBase | null {
  const { text, sha256, encoding, newline } = file;
  if (text === null || sha256 === null || encoding === null) return null;
  if (newline === null || newline === "mixed") return null;
  return { text, sha256, encoding, newline };
}

const lf = (text: string) => text.replace(/\r\n?/g, "\n");

function remember(workspaceId: string, ref: string, base: CodeBufferBase, text: string) {
  putBuffer({ workspaceId, ref, base, text });
}

interface DiskVersion {
  file: CodeFile;
  /** False when the file can no longer be edited here: no "Use this version", no Save. */
  usable: boolean;
}

interface Start {
  base: CodeBufferBase;
  text: string;
  disk: DiskVersion | null;
  message: string | null;
  verified: boolean;
}

// Only reachable when the caller breaks the file contract; Save stays off.
const NO_BASE: CodeBufferBase = { text: "", sha256: "", encoding: "utf-8", newline: "none" };

/** A stored buffer based on another version than the file just read opens straight in the
 * conflict view, with the file as the disk version. */
function startFrom(copy: Copy, file: CodeFile, stored: ReturnType<typeof getBuffer>): Start {
  const fresh = editableBase(file);
  if (stored !== undefined && stored.base.sha256 === file.sha256) {
    return {
      base: stored.base,
      text: stored.text,
      disk: null,
      message: null,
      verified: fresh !== null,
    };
  }
  if (stored === undefined && fresh !== null) {
    return { base: fresh, text: fresh.text, disk: null, message: null, verified: true };
  }
  return {
    base: fresh ?? stored?.base ?? NO_BASE,
    text: stored?.text ?? "",
    disk: { file, usable: fresh !== null },
    message: fresh !== null ? copy.conflict : copy.cannotEdit,
    verified: fresh !== null,
  };
}

export function CodeEditor({
  api,
  workspaceId,
  policyRevision,
  rootPhysicalId,
  file,
  fileVerified,
  fontSize,
  wrap,
  onBlocked,
  onClose,
}: CodeEditorProps) {
  const copy = editorCopy();
  const ref = file.path;
  const label = copy.label(ref);
  const [start] = useState(() => startFrom(copy, file, getBuffer(workspaceId, ref)));
  const baseRef = useRef(start.base);
  const [base, setBase] = useState(start.base);
  const [disk, setDisk] = useState(start.disk);
  const [message, setMessage] = useState(start.message);
  const [dirty, setDirty] = useState(() => lf(start.text) !== lf(start.base.text));
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const root = useRef<HTMLElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const handle = useRef<EditorHandle | null>(null);
  const latest = useRef({ save: () => {}, fontSize, wrap });
  const hintId = useId();
  const diskId = useId();
  useCodeBuffers();
  const retained = getBuffer(workspaceId, ref) !== undefined;

  const diskLocked = disk !== null && !disk.usable;
  const canSave = status === "ready" && dirty && !busy && !diskLocked;
  const canRevert = status === "ready" && (dirty || retained) && !busy;
  const closeBlocked = dirty || retained || busy;

  useEffect(() => {
    if (fileVerified && start.verified) {
      confirmBufferBase({ workspaceId, ref, base: start.base, text: start.text });
    }
  }, [start, workspaceId, ref, fileVerified]);

  useEffect(
    () =>
      subscribeBufferBase(workspaceId, ref, (next, previousSha256) => {
        if (baseRef.current.sha256 !== previousSha256) return;
        baseRef.current = next;
        setBase(next);
        handle.current?.setBase(next.text);
      }),
    [workspaceId, ref],
  );

  // Declared before the load effect: the editor is created with the latest values.
  useEffect(() => {
    latest.current = { save: () => void save(), fontSize, wrap };
  });
  useEffect(() => handle.current?.setFontSize(fontSize), [fontSize]);
  useEffect(() => handle.current?.setWrap(wrap), [wrap]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is Retry's reload signal; the rest are what the effect reads.
  useEffect(() => {
    const parent = host.current;
    if (parent === null) return;
    // A re-run (Retry) must not bring back the mount-time text. The cleanup has just stored the
    // edit, and no stored buffer means the editor matched its base.
    const text = getBuffer(workspaceId, ref)?.text ?? baseRef.current.text;
    let unmounted = false;
    import("./codemirror")
      .then(({ createEditor }) =>
        createEditor({
          parent,
          text,
          base: baseRef.current.text,
          ref,
          wrap: latest.current.wrap,
          fontSize: latest.current.fontSize,
          label,
          onDirty: (isDirty) => {
            setDirty(isDirty);
            if (isDirty) setSaved(false);
            remember(workspaceId, ref, baseRef.current, handle.current?.text() ?? text);
          },
          onChange: (text) => remember(workspaceId, ref, baseRef.current, text),
          onSave: () => latest.current.save(),
        }),
      )
      .then(
        (created) => {
          // createEditor resolves after its grammar loads, which can be after this unmount.
          if (unmounted) return created.destroy();
          handle.current = created;
          // A save can finish while the editor's grammar is loading.
          created.setBase(baseRef.current.text);
          setStatus("ready");
          // The button that opened the editor is gone, so focus would fall to the page body; focus
          // the user has since put elsewhere stays there.
          const active = document.activeElement;
          if (active === null || active === document.body || root.current?.contains(active)) {
            created.focus();
          }
        },
        () => {
          if (!unmounted) setStatus("failed");
        },
      );
    return () => {
      unmounted = true;
      const current = handle.current;
      if (current === null) return;
      remember(workspaceId, ref, baseRef.current, current.text());
      current.destroy();
      handle.current = null;
    };
  }, [attempt, workspaceId, ref, label]);

  /** Makes `next` the version the editor is compared and saved against. */
  function rebase(next: CodeBufferBase) {
    baseRef.current = next;
    setBase(next);
    handle.current?.setBase(next.text);
  }

  async function showDiskVersion() {
    const diskFile = await api.file(workspaceId, ref);
    const fresh = editableBase(diskFile);
    setDisk({ file: diskFile, usable: fresh !== null });
    setMessage(fresh !== null ? copy.conflict : copy.cannotEdit);
    return fresh;
  }

  async function save() {
    const current = handle.current;
    if (!canSave || current === null) return;
    const sent = current.text();
    const from = baseRef.current;
    const finish = beginBufferSave({ workspaceId, ref, base: from, text: sent });
    setBusy(true);
    setSaved(false);
    setMessage(null);
    try {
      const result = await api.save(workspaceId, ref, {
        policy_revision: policyRevision,
        root_physical_id: rootPhysicalId,
        content: sent,
        expected_sha256: from.sha256,
        encoding: from.encoding,
        newline: from.newline,
      });
      if (result.status !== "conflict") {
        const next = { ...from, text: sent, sha256: result.sha256 ?? from.sha256 };
        if (finish(next, handle.current?.text())) {
          rebase(next);
          setSaved(true);
        }
        // Notify non-editing previews after a successful disk save.
        noteSaved(workspaceId, ref);
      } else if (result.cleanup_pending) {
        onBlocked();
        setMessage(copy.refusal.recovery);
      } else {
        const fresh = await showDiskVersion();
        if (fresh !== null && finish(fresh, handle.current?.text())) rebase(fresh);
      }
    } catch (error) {
      const code = error instanceof CodeApiError ? error.code : null;
      if (code === "publication_busy" || code === "recovery_required") onBlocked();
      setMessage(copy.refusal[(code !== null && REFUSAL_GROUPS.get(code)) || "other"]);
    } finally {
      finish();
      setBusy(false);
    }
  }

  /** The editor goes back to the base, which after a conflict is the disk version. */
  function takeBase() {
    handle.current?.setText(baseRef.current.text);
    dropBuffer(workspaceId, ref);
    if (!diskLocked) {
      setDisk(null);
      setMessage(null);
    }
  }

  /** "Use this version": its button unmounts with the disk pane, so focus goes back to the editor. */
  function takeDiskVersion() {
    takeBase();
    handle.current?.focus();
  }

  return (
    <section ref={root} className="code-editor">
      <header className="code-editor-head">
        <span className="mono">{ref}</span>
        <span className="mono">{base.encoding}</span>
        <span>{copy.newline[base.newline]}</span>
      </header>

      <div className="code-editor-toolbar">
        <button
          type="button"
          aria-disabled={!canSave}
          onClick={() => {
            void save();
          }}
        >
          {copy.save}
        </button>
        <button
          type="button"
          aria-disabled={!canRevert}
          onClick={() => {
            if (canRevert && window.confirm(copy.discard)) takeBase();
          }}
        >
          {copy.revert}
        </button>
        <button
          type="button"
          aria-disabled={closeBlocked}
          aria-describedby={closeBlocked ? hintId : undefined}
          title={closeBlocked ? copy.closeHint : undefined}
          onClick={() => {
            if (!closeBlocked) onClose();
          }}
        >
          {copy.close}
        </button>
        {closeBlocked ? (
          <span id={hintId} className="sr-only">
            {copy.closeHint}
          </span>
        ) : null}
      </div>
      <p role="status" aria-live="polite" className="code-editor-status">
        {saved ? copy.saved : null}
      </p>
      {message !== null ? (
        <p role="alert" className="code-editor-message">
          {message}
        </p>
      ) : null}
      <div className="code-editor-body">
        <div className="code-editor-main">
          {status === "loading" ? <p className="code-reason">{copy.loading}</p> : null}
          {status === "failed" ? (
            <div role="alert" className="code-editor-message">
              <p>{copy.loadFailed}</p>
              <button
                type="button"
                onClick={() => {
                  setStatus("loading");
                  setAttempt((n) => n + 1);
                }}
              >
                {copy.retry}
              </button>
            </div>
          ) : null}
          <div ref={host} className="code-editor-host" />
        </div>
        {disk !== null ? (
          <aside className="code-editor-disk" aria-labelledby={diskId}>
            <h3 id={diskId}>{copy.diskVersion}</h3>
            {disk.file.text !== null ? <pre>{disk.file.text}</pre> : null}
            {disk.file.reason !== null ? (
              <p className="code-reason">{copy.reasons[disk.file.reason]}</p>
            ) : disk.file.newline === "mixed" ? (
              <p className="code-reason">{copy.mixed}</p>
            ) : null}
            {disk.usable ? (
              <button type="button" onClick={takeDiskVersion}>
                {copy.useDisk}
              </button>
            ) : null}
          </aside>
        ) : null}
      </div>
    </section>
  );
}
