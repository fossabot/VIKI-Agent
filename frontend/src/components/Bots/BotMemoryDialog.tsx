import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import type { Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import { draftFits } from "../../state/editors";
import { parseDisplayMetadata } from "../../state/noteMetadata";
import { useReportUnsavedWork } from "../../state/unsavedWork";
import type { VaultApi } from "../../state/vaultApi";
import { fallbackTitle } from "../../state/vaultOverviewStats";
import { Markdown } from "../Conversation/Markdown";
import { ConfirmDialog } from "../Dialog/ConfirmDialog";
import { useModalFocus } from "../Dialog/useModalFocus";
import { FRONTMATTER_SCAN_LINES, readerBody } from "../VaultBrowser/readerBody";
import { replacementRequest } from "./BotMemoryReplacement";
import { type BotMemoryListing, type BotMemoryRow, listBotMemory } from "./botMemory";
import "./BotMemoryDialog.css";

const MAX_NOTE_BYTES = 2 * 1024 * 1024;
const FRONTMATTER_ERRORS = new Set([
  "invalid_line",
  "duplicate_key",
  "too_many_lines",
  "unterminated",
]);

interface BotIdentity {
  bot_id: string;
  name: string;
}

interface ReadNote {
  ref: string;
  title: string;
  text: string;
  sha256: string;
  displayTitle: string;
  displayMetadata: ReturnType<typeof parseDisplayMetadata>;
  /** The file has CRLF line endings or a BOM, which the editor's text does not carry. */
  keepEndings: boolean;
  /** A lone CR, or CRLF beside LF: a textarea hands both back as LF, so the request could not
   * keep them. */
  mixedEndings: boolean;
  api: BotMemoryDialogProps["api"];
  workspaceId: string;
  botId: string;
}

type ListingState =
  | { phase: "loading" }
  | { phase: "error" }
  | {
      phase: "ready";
      value: BotMemoryListing;
      api: BotMemoryDialogProps["api"];
      workspaceId: string;
      botId: string;
    };

export interface BotMemoryDialogProps {
  api: Pick<VaultApi, "entries" | "note" | "metadata">;
  workspaceId: string;
  bot: BotIdentity;
  onClose: () => void;
  /** The vault's own reader; the button is absent without it (app-kept memory has none). */
  onOpenInVault?: (ref: string) => void;
  /** Where the memory lives: the vault's `bot-memory/` (default) or the app's own data folder. */
  kept?: "vault" | "app";
  onOpenUrl: (url: string) => void;
  /** Hands one existing file to VS Code; the button is absent without it. */
  onOpenInEditor?: (ref: string) => void;
  /**
   * Prepares an unsent message asking the Bot to replace a file; resolves false when it could not.
   * The app writes nothing in `bot-memory/`. The Edit button is absent without this.
   */
  onDraftReplacement?: (request: string) => Promise<boolean>;
}

/** The leading `---` block exactly as written, which `readerBody` drops from the shown body. */
function memoryFrontmatter(text: string): string | null {
  const lines = text.split("\n");
  if ((lines[0] ?? "").trim() !== "---") return null;
  const close = lines.slice(1, FRONTMATTER_SCAN_LINES).findIndex((line) => line.trim() === "---");
  return close === -1 ? null : lines.slice(0, close + 2).join("\n");
}

const lf = (text: string): string => text.replaceAll("\r\n", "\n");

function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validTitle(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.isWellFormed() &&
    [...value].length <= 512
  );
}

function boundedUtf8(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.isWellFormed() &&
    new TextEncoder().encode(value).byteLength <= MAX_NOTE_BYTES
  );
}

function noteResult(
  raw: unknown,
  requestedRef: string,
  api: BotMemoryDialogProps["api"],
  workspaceId: string,
  botId: string,
): ReadNote {
  const body = object(raw);
  const value = body ? object(body.note) : null;
  if (
    !value ||
    value.ref !== requestedRef ||
    !validTitle(value.title) ||
    !boundedUtf8(value.text) ||
    !boundedUtf8(value.source) ||
    typeof value.byte_size !== "number" ||
    !Number.isSafeInteger(value.byte_size) ||
    value.byte_size < 0 ||
    value.byte_size > MAX_NOTE_BYTES ||
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.sha256) ||
    (value.frontmatter_error !== null &&
      (typeof value.frontmatter_error !== "string" ||
        !FRONTMATTER_ERRORS.has(value.frontmatter_error)))
  )
    throw new Error("invalid_response");
  const hasMetadata = Object.hasOwn(value, "display_metadata");
  const displayMetadata = hasMetadata ? parseDisplayMetadata(value.display_metadata) : null;
  if (hasMetadata && !displayMetadata) throw new Error("invalid_response");
  const displayTitle =
    displayMetadata?.display_title ??
    (/^(skill|readme|index)(?:\.(?:md|markdown))?$/iu.test(value.title)
      ? fallbackTitle(requestedRef)
      : value.title);
  return {
    ref: requestedRef,
    title: value.title,
    text: value.text,
    sha256: value.sha256,
    displayTitle,
    displayMetadata,
    keepEndings: value.text.includes("\r\n") || value.source.startsWith("\uFEFF"),
    mixedEndings:
      /\r(?!\n)/.test(value.source) ||
      (value.source.includes("\r\n") && /(?<!\r)\n/.test(value.source)),
    api,
    workspaceId,
    botId,
  };
}

const DETAIL_KEYS: Record<Exclude<BotMemoryRow["details"], null>, Key> = {
  absent: "bots.memory.metadata.absent",
  unsupported: "bots.memory.metadata.unsupported",
  truncated: "bots.memory.metadata.truncated",
  changed: "bots.memory.metadata.changed",
  unreadable: "bots.memory.metadata.unreadable",
  unknown: "bots.memory.metadata.unknown",
  notRead: "bots.memory.metadata.notRead",
};

function detailText(row: BotMemoryRow): string | null {
  if (!row.details) return null;
  return t(DETAIL_KEYS[row.details]);
}

function listingStatus(value: BotMemoryListing): string[] {
  const statuses: string[] = [];
  if (value.partial || value.omitted > 0) statuses.push(tf("bots.memory.partial", {}));
  if (value.metadataPartial) statuses.push(t("bots.memory.metadata.unknown"));
  return statuses;
}

export function BotMemoryDialog({
  api,
  workspaceId,
  bot,
  onClose,
  onOpenInVault,
  kept = "vault",
  onOpenUrl,
  onOpenInEditor,
  onDraftReplacement,
}: BotMemoryDialogProps): ReactElement {
  const [editing, setEditing] = useState<{ ref: string; sha256: string; value: string } | null>(
    null,
  );
  const [draftState, setDraftState] = useState<
    "idle" | "saving" | "prepared" | "error" | "tooLarge"
  >("idle");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [refresh, setRefresh] = useState(0);
  // The `refresh` whose scan last settled: while a newer one runs, the shown file may be replaced.
  const [scanned, setScanned] = useState(0);
  const [listing, setListing] = useState<ListingState>({ phase: "loading" });
  const [selectedRef, setSelectedRef] = useState("");
  const [selectedNote, setSelectedNote] = useState<ReadNote | null>(null);
  const [listingGeneration, setListingGeneration] = useState(0);
  const [noteError, setNoteError] = useState(false);
  const [noteLoading, setNoteLoading] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const editRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const editorWasOpen = useRef(false);
  const headingId = useId();
  const noteId = useId();
  const selectedGeneration = useRef(0);
  const current = useRef({
    api,
    workspaceId,
    botId: bot.bot_id,
    selectedRef,
    refresh,
    listingGeneration,
  });
  current.current = {
    api,
    workspaceId,
    botId: bot.bot_id,
    selectedRef,
    refresh,
    listingGeneration,
  };
  const readyListing =
    listing.phase === "ready" &&
    listing.api === api &&
    listing.workspaceId === workspaceId &&
    listing.botId === bot.bot_id
      ? listing
      : null;
  const readyListingRef = useRef(readyListing);
  readyListingRef.current = readyListing;

  // Closing with typed text asks first; the confirmation card handles its own Escape.
  function requestClose(): void {
    if (edited) setConfirmDiscard(true);
    else onClose();
  }
  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: requestClose });

  useEffect(() => {
    const controller = new AbortController();
    const isCurrent = () =>
      current.current.api === api &&
      current.current.workspaceId === workspaceId &&
      current.current.botId === bot.bot_id &&
      current.current.refresh === refresh;
    const keepSelection = readyListingRef.current !== null;
    setListing((previous) =>
      keepSelection && previous.phase === "ready" ? previous : { phase: "loading" },
    );
    void listBotMemory(api, workspaceId, bot.bot_id, controller.signal, isCurrent, kept).then(
      (value) => {
        if (controller.signal.aborted || !isCurrent()) return;
        setListing({ phase: "ready", value, api, workspaceId, botId: bot.bot_id });
        setScanned(refresh);
        setListingGeneration((version) => version + 1);
        // A file picked while the scan ran stays selected when its row is still listed.
        const preferredRef = current.current.selectedRef;
        const next =
          keepSelection && preferredRef && value.rows.some((row) => row.ref === preferredRef)
            ? preferredRef
            : (value.rows[0]?.ref ?? "");
        if (!keepSelection || next !== preferredRef) {
          selectedGeneration.current += 1;
          setSelectedRef(next);
        }
      },
      () => {
        if (controller.signal.aborted || !isCurrent()) return;
        setListing({ phase: "error" });
        setScanned(refresh);
      },
    );
    return () => controller.abort();
  }, [api, workspaceId, bot.bot_id, kept, refresh]);

  useEffect(() => {
    const currentListing = readyListingRef.current;
    if (!selectedRef || !currentListing) {
      setSelectedNote(null);
      setNoteError(false);
      setNoteLoading(false);
      return;
    }
    const selected = currentListing.value.rows.find((row) => row.ref === selectedRef);
    if (!selected) return;
    const controller = new AbortController();
    const generation = ++selectedGeneration.current;
    const isCurrent = () =>
      current.current.api === api &&
      current.current.workspaceId === workspaceId &&
      current.current.botId === bot.bot_id &&
      current.current.listingGeneration === listingGeneration &&
      current.current.selectedRef === selectedRef &&
      selectedGeneration.current === generation;
    setSelectedNote(null);
    setNoteError(false);
    setNoteLoading(true);
    void api
      .note(workspaceId, selectedRef, controller.signal)
      .then(
        (raw) => {
          if (controller.signal.aborted || !isCurrent()) return;
          const result = noteResult(raw, selectedRef, api, workspaceId, bot.bot_id);
          if (!isCurrent()) return;
          setSelectedNote(result);
          if (result.displayMetadata) {
            setListing((state) => {
              if (
                state.phase !== "ready" ||
                state.api !== api ||
                state.workspaceId !== workspaceId ||
                state.botId !== bot.bot_id
              )
                return state;
              return {
                ...state,
                value: {
                  ...state.value,
                  rows: state.value.rows.map((row) =>
                    row.ref === selectedRef
                      ? {
                          ...row,
                          title: result.displayTitle,
                          details:
                            result.displayMetadata?.state === "ready"
                              ? null
                              : (result.displayMetadata?.state ?? row.details),
                        }
                      : row,
                  ),
                },
              };
            });
          }
          setNoteLoading(false);
        },
        () => {
          if (controller.signal.aborted || !isCurrent()) return;
          setNoteError(true);
          setNoteLoading(false);
        },
      )
      .catch(() => {
        if (controller.signal.aborted || !isCurrent()) return;
        setNoteError(true);
        setNoteLoading(false);
      });
    return () => controller.abort();
  }, [api, workspaceId, bot.bot_id, selectedRef, listingGeneration]);

  function select(ref: string): void {
    if (ref === selectedRef) return;
    selectedGeneration.current += 1;
    setSelectedRef(ref);
  }

  const rows = readyListing?.value.rows ?? [];
  const selectedRow = rows.find((row) => row.ref === selectedRef) ?? null;
  const scanStatuses = readyListing ? listingStatus(readyListing.value) : [];
  const shownNote =
    selectedNote &&
    selectedNote.ref === selectedRef &&
    selectedNote.api === api &&
    selectedNote.workspaceId === workspaceId &&
    selectedNote.botId === bot.bot_id
      ? selectedNote
      : null;
  const folder = kept === "app" ? (readyListing?.value.folder ?? null) : null;
  const frontmatter = shownNote ? memoryFrontmatter(shownNote.text) : null;
  // An edit belongs to the file it started from: another Bot's file or changed bytes hide it.
  const editor =
    editing && shownNote?.ref === editing.ref && shownNote.sha256 === editing.sha256
      ? editing
      : null;
  const edited =
    editor !== null && editor.value !== lf(shownNote?.text ?? "") && editor.value.trim() !== "";
  const scanning = scanned !== refresh;
  // Closing or reloading the window drops typed text too; the dialog's own Close asks first.
  useReportUnsavedWork("memory", edited);

  // Edit disables the button that holds focus, and Cancel or Save removes the textarea that holds
  // it; each would leave the keyboard on the page body behind this modal dialog.
  const editorOpen = editor !== null;
  useEffect(() => {
    if (editorOpen) textareaRef.current?.focus();
    else if (editorWasOpen.current) editRef.current?.focus();
    editorWasOpen.current = editorOpen;
  }, [editorOpen]);

  async function saveEdit(): Promise<void> {
    if (!editor || !edited || !onDraftReplacement || draftState === "saving") return;
    const request = replacementRequest(
      folder ? `${folder}/${editor.ref}` : editor.ref,
      editor.sha256,
      editor.value,
      shownNote?.keepEndings === true,
    );
    // The host checks the draft this joins; a request too large alone gets its own message.
    if (!draftFits(request)) {
      setDraftState("tooLarge");
      return;
    }
    setDraftState("saving");
    try {
      const prepared = await onDraftReplacement(request);
      setDraftState(prepared ? "prepared" : "error");
      if (prepared) setEditing(null);
    } catch {
      setDraftState("error");
    }
  }

  return (
    <div className="bot-memory-dialog-backdrop">
      <div
        ref={dialogRef}
        className="bot-memory-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
      >
        <header className="bot-memory-dialog-head">
          <div>
            <h2 id={headingId}>{t("bots.memory")}</h2>
            <p>
              {kept === "app"
                ? folder
                  ? tf("bots.memory.sourceApp", { folder })
                  : t("bots.memory.keptApp")
                : tf("bots.memory.source", { ref: `bot-memory/${bot.bot_id}` })}
            </p>
          </div>
          <button ref={closeRef} type="button" className="btn txt sm" onClick={requestClose}>
            {t("common.button.close")}
          </button>
        </header>
        {confirmDiscard ? (
          <ConfirmDialog
            label={t("bots.memory.discard.label")}
            body={t("bots.memory.discard.body")}
            confirmLabel={t("bots.memory.discard.confirm")}
            destructive
            onConfirm={onClose}
            onCancel={() => setConfirmDiscard(false)}
          />
        ) : null}
        <div className="bot-memory-dialog-actions">
          {onOpenInVault ? (
            <button
              type="button"
              className="btn alt sm"
              disabled={!selectedRow}
              onClick={() => selectedRow && onOpenInVault(selectedRow.ref)}
            >
              {t("bots.memory.openInVault")}
            </button>
          ) : null}
          {onOpenInEditor ? (
            <button
              type="button"
              className="btn alt sm"
              disabled={!selectedRow}
              onClick={() => selectedRow && onOpenInEditor(selectedRow.ref)}
            >
              {t("bots.memory.openInEditor")}
            </button>
          ) : null}
          {onDraftReplacement && (kept === "vault" || folder) ? (
            <button
              ref={editRef}
              type="button"
              className="btn alt sm"
              disabled={!shownNote || shownNote.mixedEndings || editor !== null || scanning}
              onClick={() => {
                if (!shownNote) return;
                setDraftState("idle");
                setEditing({
                  ref: shownNote.ref,
                  sha256: shownNote.sha256,
                  value: lf(shownNote.text),
                });
              }}
            >
              {t("bots.memory.edit")}
            </button>
          ) : null}
          <button
            type="button"
            className="btn txt sm"
            disabled={editor !== null || scanning}
            onClick={() => setRefresh((value) => value + 1)}
          >
            {t("bots.memory.refresh")}
          </button>
        </div>
        {onOpenInEditor ? <p className="bots-note">{t("bots.memory.editorHint")}</p> : null}
        {draftState === "prepared" ? (
          <p role="status">{tf("bots.memory.edit.prepared", { name: bot.name })}</p>
        ) : null}
        {listing.phase === "loading" ? <p role="status">{t("bots.memory.loading")}</p> : null}
        {listing.phase === "error" ? <p role="alert">{t("bots.memory.error")}</p> : null}
        {readyListing &&
        rows.length === 0 &&
        !readyListing.value.partial &&
        readyListing.value.omitted === 0 ? (
          <p>{tf("bots.memory.empty", { name: bot.name })}</p>
        ) : null}
        {scanStatuses.map((status) => (
          <p key={status} role="status">
            {status}
          </p>
        ))}
        {readyListing && rows.length > 0 ? (
          <div className="bot-memory-dialog-body">
            <nav className="bot-memory-files" aria-label={t("bots.memory.allFiles")}>
              <ul>
                {rows.map((row) => (
                  <li key={row.ref}>
                    <button
                      type="button"
                      className="btn txt"
                      aria-pressed={row.ref === selectedRef}
                      disabled={editor !== null}
                      onClick={() => select(row.ref)}
                    >
                      <span>{row.title}</span>
                      <code>{row.ref}</code>
                    </button>
                    {row.details ? <small>{detailText(row)}</small> : null}
                  </li>
                ))}
              </ul>
            </nav>
            <section className="bot-memory-reader" aria-label={selectedRef}>
              {selectedRow ? (
                <code className="bot-memory-selected-ref">{selectedRow.ref}</code>
              ) : null}
              {noteLoading ? <p role="status">{t("bots.memory.loading")}</p> : null}
              {noteError ? <p role="alert">{t("bots.memory.fileError")}</p> : null}
              {shownNote && editor ? (
                <div className="bot-memory-editor">
                  <textarea
                    ref={textareaRef}
                    aria-label={t("bots.memory.edit.label")}
                    aria-describedby={noteId}
                    spellCheck={false}
                    // Text typed after Save would not be in the request the draft receives.
                    readOnly={draftState === "saving"}
                    value={editor.value}
                    onChange={(event) => {
                      const value = event.target.value;
                      setEditing({ ...editor, value });
                    }}
                  />
                  <p className="bots-note" id={noteId}>
                    {tf("bots.memory.edit.note", { name: bot.name })}
                  </p>
                  {shownNote.keepEndings ? (
                    <p className="bots-note">
                      {tf("bots.memory.edit.endings", { name: bot.name })}
                    </p>
                  ) : null}
                  <div className="bot-memory-dialog-actions">
                    <button
                      type="button"
                      className="btn go sm"
                      disabled={!edited || draftState === "saving"}
                      onClick={() => void saveEdit()}
                    >
                      {t("bots.memory.edit.save")}
                    </button>
                    <button
                      type="button"
                      className="btn txt sm"
                      onClick={() => {
                        setEditing(null);
                        setDraftState("idle");
                      }}
                    >
                      {t("common.button.cancel")}
                    </button>
                  </div>
                  {draftState === "error" || draftState === "tooLarge" ? (
                    <p role="alert">
                      {t(
                        draftState === "tooLarge"
                          ? "bots.memory.edit.tooLarge"
                          : "bots.memory.edit.error",
                      )}
                    </p>
                  ) : null}
                </div>
              ) : shownNote ? (
                <article className="md">
                  {onDraftReplacement && shownNote.mixedEndings ? (
                    <p className="bots-note">
                      {tf("bots.memory.edit.mixedEndings", { name: bot.name })}
                    </p>
                  ) : null}
                  <h3>{shownNote.displayTitle}</h3>
                  {frontmatter ? <pre className="bot-memory-frontmatter">{frontmatter}</pre> : null}
                  <Markdown
                    text={readerBody(shownNote.text, shownNote.displayTitle)}
                    onOpenUrl={onOpenUrl}
                  />
                </article>
              ) : null}
            </section>
          </div>
        ) : null}
      </div>
    </div>
  );
}
