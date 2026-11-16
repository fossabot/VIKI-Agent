import { type ReactElement, useEffect, useRef, useState } from "react";
import type { ArchiveSummary, ChatArchivesApi } from "../../clients/conversations/chatArchivesApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import type { ReadOnlyConversationArchive } from "../../context/conversationTools";
import {
  ARCHIVE_MAX_BYTES,
  deriveArchiveId,
  exportConversationArchive,
  exportConversationMarkdown,
  parseReadOnlyArchiveJson,
  withArchiveMedia,
} from "../../context/conversationTools";
import { Markdown } from "../Conversation/Markdown";
import "./ConversationTools.css";

export interface ArchiveToolsProps {
  readonly archive?: ReadOnlyConversationArchive | undefined;
  readonly onImport: (archive: ReadOnlyConversationArchive) => void;
  readonly api: ChatArchivesApi;
  readonly assets: AssetsApi;
  /** The view's history read answered `transcript_missing`: the cut-selection line then leaves out
   * Open conversation record, whose record is gone. */
  readonly recordMissing?: boolean;
}

export function ArchiveTools({
  archive,
  onImport,
  api,
  assets,
  recordMissing = false,
}: ArchiveToolsProps): ReactElement {
  const cutSelection = archive?.promptCut === true;
  const [saved, setSaved] = useState<readonly ArchiveSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [listError, setListError] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  function loadList(signal: AbortSignal): Promise<void> {
    return api
      .list(signal)
      .then((rows) => {
        setSaved(rows);
        setListError(false);
      })
      .catch(() => {
        if (!signal.aborted) setListError(true);
      });
  }
  // biome-ignore lint/correctness/useExhaustiveDependencies: loadList closes over api, the only reactive input.
  useEffect(() => {
    const controller = new AbortController();
    void loadList(controller.signal);
    return () => {
      controller.abort();
      operation.current?.abort();
    };
  }, [api]);
  async function run(action: (signal: AbortSignal) => Promise<void>): Promise<void> {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError(false);
    setNote(null);
    try {
      await action(controller.signal);
    } catch {
      if (!controller.signal.aborted) setError(true);
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        setBusy(false);
      }
    }
  }
  function download(content: string, type: string, extension: string): void {
    if (new TextEncoder().encode(content).byteLength > ARCHIVE_MAX_BYTES)
      throw new Error("too_large");
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `conversation.${extension}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <section className="archive-tools" aria-label="Export / Import">
      <p>
        Export selected completed turns. JSON includes available attachment copies; Markdown
        includes a manifest. Imported copies are read-only and stay separate from provider history.
      </p>
      <button
        type="button"
        disabled={busy || !archive?.turns.length || cutSelection}
        onClick={() =>
          archive &&
          void run(async () => download(exportConversationMarkdown(archive), "text/markdown", "md"))
        }
      >
        Export Markdown
      </button>
      <button
        type="button"
        disabled={busy || !archive?.turns.length || cutSelection}
        onClick={() =>
          archive &&
          void run(async (signal) => {
            const complete = await withArchiveMedia(archive, assets, signal);
            download(exportConversationArchive(complete), "application/json", "json");
            if (complete.media?.some((item) => "unavailable" in item))
              setNote(
                "Some attachments are no longer stored locally. Their names and metadata are included.",
              );
          })
        }
      >
        Export JSON with attachments
      </button>
      <label>
        Import a read-only copy
        <input
          type="file"
          accept="application/json,.json"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.currentTarget.value = "";
            if (!file) return;
            void run(async (signal) => {
              if (file.size > ARCHIVE_MAX_BYTES) throw new Error("too_large");
              const bytes = await file.arrayBuffer();
              const parsed = parseReadOnlyArchiveJson(new TextDecoder().decode(bytes));
              const id = await deriveArchiveId(bytes);
              try {
                await api.save(id, parsed, signal);
                onImport(await api.get(id, signal));
              } finally {
                await loadList(signal);
              }
            });
          }}
        />
      </label>
      {cutSelection ? (
        <p role="status">
          {`A selected turn's prompt is cut short in this window, so this selection cannot be exported. Unselect that turn, or open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app${recordMissing ? "." : ", Open conversation record for an earlier conversation."}`}
        </p>
      ) : null}
      {busy ? <p role="status">Processing…</p> : null}
      {error ? (
        <p role="alert">Could not process the file. Check it and try again. Limit: 8 MiB.</p>
      ) : null}
      {note ? <p role="status">{note}</p> : null}
      {listError ? (
        <p role="alert">
          Could not load the saved copies.
          <button type="button" disabled={busy} onClick={() => void run(loadList)}>
            Reload
          </button>
        </p>
      ) : null}
      {saved.length ? (
        <details>
          <summary>Saved copies ({saved.length})</summary>
          <ul>
            {saved.map((row) => (
              <li key={row.archive_id}>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void run(async (signal) => onImport(await api.get(row.archive_id, signal)))
                  }
                >
                  {row.created_at.replace("T", " ").slice(0, 19)} ·{" "}
                  {Math.ceil(row.byte_size / 1024)} KiB
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void run(async (signal) => {
                      await api.remove(row.archive_id);
                      await loadList(signal);
                    })
                  }
                >
                  Remove from the app
                </button>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

export function ArchivePreview({
  archive,
  onOpenUrl,
}: {
  archive: ReadOnlyConversationArchive;
  onOpenUrl: (url: string) => void;
}) {
  const [urls, setUrls] = useState<ReadonlyMap<string, string>>(new Map());
  useEffect(() => {
    const map = new Map<string, string>();
    const refs = new Map(
      archive.turns.flatMap((turn) =>
        (turn.attachments ?? []).map((asset) => [asset.asset_id, asset] as const),
      ),
    );
    for (const media of archive.media ?? []) {
      const ref = refs.get(media.asset_id);
      if (ref && "base64" in media) {
        const bytes = Uint8Array.from(atob(media.base64), (char) => char.charCodeAt(0));
        map.set(media.asset_id, URL.createObjectURL(new Blob([bytes], { type: ref.media_type })));
      }
    }
    setUrls(map);
    return () => {
      for (const url of map.values()) URL.revokeObjectURL(url);
    };
  }, [archive]);
  return (
    <article className="archive-preview">
      <h2>{archive.session.title}</h2>
      <p>Read-only copy · no session is started</p>
      {archive.turns.map((turn) => (
        <section key={`${turn.turnId}:${turn.role}`}>
          <h3>{turn.role === "user" ? "You" : "Assistant"}</h3>
          {turn.role === "assistant" ? (
            <Markdown text={turn.text} onOpenUrl={onOpenUrl} />
          ) : (
            <p className="archive-user-text">{turn.text}</p>
          )}
          {turn.attachments?.map((asset) => (
            <figure key={asset.asset_id}>
              {urls.get(asset.asset_id) ? (
                <img src={urls.get(asset.asset_id)} alt={asset.name} loading="lazy" />
              ) : (
                <p>Attachment copy unavailable</p>
              )}
              <figcaption>
                {asset.name} · {asset.width} × {asset.height}
              </figcaption>
            </figure>
          ))}
        </section>
      ))}
    </article>
  );
}
