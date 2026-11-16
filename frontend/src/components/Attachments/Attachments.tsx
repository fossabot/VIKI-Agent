import {
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type ReactElement,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import {
  ACCEPTED_MEDIA_TYPES,
  type AttachmentLimits,
  type AttachmentSource,
  type TurnAttachment,
  type UploadedAttachment,
  validateAttachmentFiles,
} from "../../turnInput";
import { KeepInVault, type KeepInVaultContext } from "./KeepInVault";
import "./Attachments.css";

const labels = {
  add: "Add attachment",
  drop: "Drop images here",
  uploading: "Uploading…",
  ready: "Ready",
  retry: "Retry",
  remove: "Remove",
  cancel: "Cancel",
  unavailable: "Unavailable",
  queued: "Waiting to upload",
  cancelled: "Cancelled",
  picker: "File picker",
  paste: "Clipboard",
  drag: "Dropped file",
  history: "Conversation history",
} as const;

/** One line of upload-refusal copy per server code, so a refusal explains itself instead of a
 * generic failure. Grouped codes share a row; an unlisted code falls back to the last row's line. */
const FAILURE_COPY_ROWS: { codes: readonly string[]; text: string }[] = [
  {
    codes: ["capacity"],
    text: "Attachment storage is full. Unsent attachments free up after 24 hours and sent copies after 30 days; resetting Drafts or Send history frees them sooner.",
  },
  {
    codes: ["decode_failed"],
    text: "This is not a PNG, JPEG or WebP still image of up to 40 megapixels.",
  },
  {
    codes: ["decoder_unavailable"],
    text: "The image checker is not installed. Quit VIKI and run uv sync.",
  },
  {
    codes: ["unavailable"],
    text: "The image could not be checked or saved. If this keeps happening, see Troubleshooting.",
  },
  {
    codes: ["invalid_name"],
    text: "The file name cannot be used (512 UTF-8 bytes at most).",
  },
  {
    codes: ["too_large"],
    text: "Too large (20 MiB per image).",
  },
  {
    codes: ["empty"],
    text: "The file is empty.",
  },
  {
    codes: ["protection", "corrupt"],
    text: "The attachment store cannot be used safely.",
  },
  {
    codes: ["unauthorized"],
    text: "Reopen VIKI Agent.",
  },
  {
    codes: ["forbidden_origin", "invalid_request", "invalid_body", "invalid_representation"],
    text: "The upload was not accepted.",
  },
  {
    codes: ["overloaded"],
    text: "Another image is being processed. Wait a moment, then retry.",
  },
  {
    codes: ["busy"],
    text: "The app's data was busy.",
  },
  {
    codes: ["worker_timeout", "intake_timeout"],
    text: "The image could not be checked in time.",
  },
  {
    codes: ["worker_unavailable"],
    text: "The image check could not start.",
  },
  {
    codes: ["incomplete", "disconnected"],
    text: "The upload stopped part way.",
  },
  {
    codes: ["network_error"],
    text: "Could not reach the app's server.",
  },
  {
    codes: ["timeout"],
    text: "No response came back.",
  },
  {
    codes: ["invalid_response", "upload_failed"],
    text: "Upload failed.",
  },
];
const FAILURE_COPY = new Map<string, string>(
  FAILURE_COPY_ROWS.flatMap((row) => row.codes.map((code) => [code, row.text] as const)),
);
const DEFAULT_FAILURE_COPY = "Upload failed.";

function failureCopy(code: string | undefined): string {
  return (code !== undefined ? FAILURE_COPY.get(code) : undefined) ?? DEFAULT_FAILURE_COPY;
}

const RETRYABLE_FAILURE_CODES = new Set([
  "overloaded",
  "busy",
  "worker_timeout",
  "intake_timeout",
  "worker_unavailable",
  "incomplete",
  "disconnected",
  "unavailable",
  "network_error",
  "timeout",
  "invalid_response",
  "upload_failed",
]);

function failureFrom(cause: unknown): { code: string; retryable: boolean } {
  const code = cause instanceof AssetsApiError ? cause.code : "upload_failed";
  return { code, retryable: RETRYABLE_FAILURE_CODES.has(code) };
}

export interface AttachmentPickerProps {
  onFiles: (files: readonly File[], source?: AttachmentSource) => void;
  disabled?: boolean;
}

export function AttachmentPicker({
  onFiles,
  disabled = false,
}: AttachmentPickerProps): ReactElement {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const files = (event: ChangeEvent<HTMLInputElement>): void => {
    if (event.target.files?.length) onFiles([...event.target.files], "picker");
    event.target.value = "";
  };
  const drop = (event: DragEvent<HTMLElement>): void => {
    event.preventDefault();
    // Without this the composer's own outer drop handler also fires for the same drop (the
    // picker sits inside it), so one dropped file was uploaded and attached twice.
    event.stopPropagation();
    setDragging(false);
    if (!disabled && event.dataTransfer.files.length)
      onFiles([...event.dataTransfer.files], "drop");
  };
  const paste = (event: ClipboardEvent<HTMLElement>): void => {
    if (disabled) return;
    const images = [...event.clipboardData.items]
      .filter((item) => item.kind === "file" && ACCEPTED_MEDIA_TYPES.includes(item.type as never))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (images.length) onFiles(images, "paste");
  };
  return (
    <fieldset
      className={`attachment-picker${dragging ? " attachment-drop-active" : ""}`}
      onDragEnter={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={() => setDragging(false)}
      onDrop={drop}
      onPaste={paste}
    >
      <input
        ref={inputRef}
        className="attachment-picker-input"
        type="file"
        accept={ACCEPTED_MEDIA_TYPES.join(",")}
        multiple
        onChange={files}
        disabled={disabled}
        aria-label={labels.add}
      />
      <button
        type="button"
        className="attachment-picker-button"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        aria-label={labels.add}
      >
        ＋
      </button>
    </fieldset>
  );
}

export interface AttachmentPickerState {
  attachments: readonly TurnAttachment[];
  addFiles(files: readonly File[], source?: AttachmentSource): void;
  remove(localId: string, notify?: boolean): void;
  retry(localId: string): void;
  cancel(localId: string): void;
  clear(localIds?: readonly string[], notify?: boolean): void;
}

export interface UseAttachmentPickerOptions {
  api: AssetsApi;
  destinationId: string;
  limits?: AttachmentLimits;
  existingCount?: number;
  existingBytes?: number;
  existingAttachments?: readonly { asset_id: string; byte_size: number }[];
  onChange?: (attachments: readonly TurnAttachment[]) => void;
  onReject?: (reason: string) => void;
}

export function useAttachmentPicker({
  api,
  destinationId,
  limits,
  existingCount = 0,
  existingBytes = 0,
  existingAttachments,
  onChange,
  onReject,
}: UseAttachmentPickerOptions): AttachmentPickerState {
  const [attachments, setAttachments] = useState<readonly TurnAttachment[]>([]);
  const destinationRef = useRef(destinationId);
  const generation = useRef(0);
  const jobs = useRef(new Map<string, AbortController>());
  const attachmentsRef = useRef<readonly TurnAttachment[]>(attachments);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  attachmentsRef.current = attachments;
  if (destinationRef.current !== destinationId) {
    destinationRef.current = destinationId;
    generation.current += 1;
    for (const controller of jobs.current.values()) controller.abort();
    jobs.current.clear();
    // The aborted rows have no future under the new destination. Dropping them here, not just
    // stopping their jobs, keeps the next runner's `find` (below) from picking up a `queued` row
    // that a later addFiles batch never meant it to see.
    const survivors = attachmentsRef.current.filter(
      (row) => row.status !== "queued" && row.status !== "uploading",
    );
    if (survivors.length !== attachmentsRef.current.length) {
      attachmentsRef.current = survivors;
      setAttachments(survivors);
    }
  }
  const update = useCallback((next: readonly TurnAttachment[]) => {
    attachmentsRef.current = next;
    setAttachments(next);
    onChangeRef.current?.(next);
  }, []);
  useEffect(
    () => () => {
      generation.current += 1;
      for (const controller of jobs.current.values()) controller.abort();
      jobs.current.clear();
    },
    [],
  );

  const patch = useCallback(
    (localId: string, changes: Partial<TurnAttachment>) => {
      const rows = attachmentsRef.current;
      if (!rows.some((row) => row.localId === localId)) return;
      update(rows.map((row) => (row.localId === localId ? { ...row, ...changes } : row)));
    },
    [update],
  );

  // One upload runs at a time for the whole picker, across every addFiles batch and retry, so
  // the app never causes its own `overloaded` refusal by racing its own uploads. The loop belongs
  // to the generation it started under: an unmount or a destination change bumps that generation,
  // and the loop stops instead of moving on to the next queued file under a picker no longer live.
  const running = useRef(false);
  const pump = useCallback(async () => {
    const owner = generation.current;
    for (;;) {
      if (owner !== generation.current) return;
      const item = attachmentsRef.current.find((row) => row.status === "queued");
      if (!item) return;
      const controller = new AbortController();
      jobs.current.set(item.localId, controller);
      patch(item.localId, { status: "uploading" });
      try {
        const asset = await api.upload(item.file, {
          signal: controller.signal,
          onProgress: (progress) => {
            if (owner === generation.current && !controller.signal.aborted)
              patch(item.localId, { status: "uploading", progress });
          },
        });
        if (owner !== generation.current) return;
        if (!controller.signal.aborted)
          patch(item.localId, { status: "ready", progress: 100, asset, failure: null });
      } catch (cause) {
        if (owner !== generation.current) return;
        if (!controller.signal.aborted)
          patch(item.localId, { status: "error", progress: null, failure: failureFrom(cause) });
      } finally {
        jobs.current.delete(item.localId);
      }
    }
  }, [api, patch]);
  const runQueue = useCallback(() => {
    if (running.current) return;
    running.current = true;
    void pump().finally(() => {
      running.current = false;
    });
  }, [pump]);

  const addFiles = useCallback(
    (files: readonly File[], source: AttachmentSource = "picker") => {
      const current = attachmentsRef.current;
      const localIds = new Set(
        current.flatMap((item) => (item.asset ? [item.asset.asset_id] : [])),
      );
      const external = existingAttachments?.filter((item) => !localIds.has(item.asset_id));
      const baseCount = external ? external.length : existingCount;
      const baseBytes = external
        ? external.reduce((sum, item) => sum + item.byte_size, 0)
        : existingBytes;
      const error = validateAttachmentFiles(
        files,
        baseBytes + current.reduce((n, item) => n + item.byteSize, 0),
        baseCount + current.length,
        limits,
      );
      if (error) {
        onReject?.(error);
        return;
      }
      const added = files.map(
        (file, index): TurnAttachment => ({
          localId: `${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`,
          file,
          name: file.name,
          byteSize: file.size,
          mediaType: file.type,
          status: "queued",
          progress: 0,
          asset: null,
          error: null,
          failure: null,
          source,
          modified_at: file.lastModified ? new Date(file.lastModified).toISOString() : null,
        }),
      );
      update([...current, ...added]);
      runQueue();
    },
    [existingAttachments, existingBytes, existingCount, limits, onReject, runQueue, update],
  );

  const remove = useCallback(
    (localId: string, notify = true) => {
      jobs.current.get(localId)?.abort();
      const next = attachmentsRef.current.filter((item) => item.localId !== localId);
      if (notify) update(next);
      else {
        attachmentsRef.current = next;
        setAttachments(next);
      }
    },
    [update],
  );
  const retry = useCallback(
    (localId: string) => {
      const item = attachmentsRef.current.find((row) => row.localId === localId);
      if (item?.status !== "error" || !item.failure?.retryable) return;
      patch(localId, { status: "queued", progress: 0, failure: null });
      runQueue();
    },
    [patch, runQueue],
  );
  const cancel = useCallback(
    (localId: string) => {
      jobs.current.get(localId)?.abort();
      update(
        attachmentsRef.current.map((item) =>
          item.localId === localId
            ? { ...item, status: "cancelled" as const, progress: null }
            : item,
        ),
      );
    },
    [update],
  );
  const clear = useCallback(
    (localIds?: readonly string[], notify = true) => {
      const ids = localIds ? new Set(localIds) : null;
      for (const [id, controller] of jobs.current)
        if (!ids || ids.has(id)) {
          controller.abort();
          jobs.current.delete(id);
        }
      const next = ids ? attachmentsRef.current.filter((item) => !ids.has(item.localId)) : [];
      if (notify) update(next);
      else {
        attachmentsRef.current = next;
        setAttachments(next);
      }
    },
    [update],
  );
  return { attachments, addFiles, remove, retry, cancel, clear };
}

export interface AttachmentStripProps {
  attachments: readonly TurnAttachment[];
  onRemove: (localId: string) => void;
  onRetry: (localId: string) => void;
  onCancel: (localId: string) => void;
  api?: AssetsApi;
  /** The asset's local copy is gone (410) or never existed (404), found via the thumbnail read
   * below — there is no separate probe. Any other read failure is not reported here. */
  onUnavailable?: (localId: string) => void;
  /** Present only where a vault write is offered; without it no keep control renders. */
  keepInVault?: KeepInVaultContext;
}

export function AttachmentStrip({
  attachments,
  onRemove,
  onRetry,
  onCancel,
  api,
  onUnavailable,
  keepInVault,
}: AttachmentStripProps): ReactElement | null {
  if (!attachments.length) return null;
  return (
    <ul className="attachment-strip" aria-label={labels.add}>
      {attachments.map((item) => (
        <li className="attachment-item" key={item.localId}>
          {item.asset && api ? (
            <AssetThumb
              api={api}
              asset={item.asset}
              localId={item.localId}
              onUnavailable={onUnavailable}
            />
          ) : null}
          <span className="attachment-item-name" title={item.name}>
            {item.name}
          </span>
          <span className="attachment-item-metadata">
            {item.byteSize < 1024
              ? `${item.byteSize} B`
              : item.byteSize < 1_048_576
                ? `${(item.byteSize / 1024).toFixed(1)} KiB`
                : `${(item.byteSize / 1_048_576).toFixed(1)} MiB`}
            {" · "}
            {item.source === "paste"
              ? labels.paste
              : item.source === "drop"
                ? labels.drag
                : item.source === "history"
                  ? labels.history
                  : labels.picker}
            {item.asset ? ` · ${item.asset.media_type}` : null}
            {item.modified_at ? (
              <time dateTime={item.modified_at}>
                {new Date(item.modified_at).toLocaleString("en-US")}
              </time>
            ) : null}
          </span>
          <span
            className={item.status === "error" ? "attachment-item-error" : "attachment-item-status"}
          >
            {item.status === "uploading"
              ? `${labels.uploading} ${item.progress ?? 0}%`
              : item.status === "ready"
                ? labels.ready
                : item.status === "error"
                  ? item.error === "unavailable"
                    ? labels.unavailable
                    : failureCopy(item.failure?.code)
                  : item.status === "queued"
                    ? labels.queued
                    : labels.cancelled}
          </span>
          {item.status === "error" && item.failure?.retryable ? (
            <button
              className="attachment-item-action"
              type="button"
              onClick={() => onRetry(item.localId)}
            >
              {labels.retry}
            </button>
          ) : null}
          {item.status === "uploading" ? (
            <button
              className="attachment-item-action"
              type="button"
              onClick={() => onCancel(item.localId)}
            >
              {labels.cancel}
            </button>
          ) : null}
          {keepInVault && item.status === "ready" && item.asset ? (
            <KeepInVault attachment={item.asset} vault={keepInVault} />
          ) : null}
          {item.status !== "ready" || item.asset ? (
            <button
              className="attachment-item-action"
              type="button"
              onClick={() => onRemove(item.localId)}
            >
              {labels.remove}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function AssetThumb({
  api,
  asset,
  localId,
  onUnavailable,
}: {
  api: AssetsApi;
  asset: UploadedAttachment;
  localId: string;
  onUnavailable?: ((localId: string) => void) | undefined;
}): ReactElement {
  const { url, status } = useAssetPreviewState(api, asset);
  // This is the one read of the asset's local copy: a persisted attachment gone (410) or never
  // stored here (404) is reported to the strip through this same thumbnail read, not a second
  // one. Any other failure just leaves the placeholder below.
  useEffect(() => {
    if (status === 410 || status === 404) onUnavailable?.(localId);
  }, [status, localId, onUnavailable]);
  return url ? (
    <img className="attachment-thumb" src={url} alt="" />
  ) : (
    <span className="attachment-thumb-placeholder" aria-hidden="true" />
  );
}

export function useAssetPreviewState(api: AssetsApi, asset: UploadedAttachment | null) {
  const id = asset?.asset_id ?? null;
  const owner = useRef<{ api: AssetsApi; id: string | null; controller?: AbortController }>({
    api,
    id,
  });
  if (owner.current.api !== api || owner.current.id !== id) owner.current = { api, id };
  const request = owner.current;
  const [preview, setPreview] = useState<{
    owner: typeof request;
    url: string | null;
    state: "loading" | "ready" | "expired" | "unavailable";
    status: number | null;
    retryable: boolean;
  }>({ owner: request, url: null, state: "loading", status: null, retryable: false });
  useEffect(() => {
    const { api, id } = request;
    if (!id) return;
    let objectUrl: string | null = null;
    let reading = true;
    const controller = new AbortController();
    request.controller = controller;
    setPreview({ owner: request, url: null, state: "loading", status: null, retryable: false });
    void api
      .read(id, controller.signal)
      .then((blob) => {
        if (owner.current !== request || controller.signal.aborted) return;
        reading = false;
        objectUrl = URL.createObjectURL(blob);
        setPreview({
          owner: request,
          url: objectUrl,
          state: "ready",
          status: null,
          retryable: false,
        });
      })
      .catch((error: unknown) => {
        if (owner.current !== request || controller.signal.aborted) return;
        const status = error instanceof AssetsApiError ? error.status : null;
        // Only read failures can be transient; HTTP 503 also covers corrupt/protection.
        const retryable =
          reading &&
          (error instanceof TypeError ||
            (error instanceof AssetsApiError && error.status === 0 && error.code === "timeout"));
        setPreview({
          owner: request,
          url: null,
          state: status === 410 ? "expired" : "unavailable",
          status,
          retryable,
        });
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [request]);
  const current =
    preview.owner === request
      ? preview
      : { owner: request, url: null, state: "loading" as const, status: null, retryable: false };
  const retry = () => {
    if (
      owner.current !== request ||
      request.controller?.signal.aborted !== false ||
      !current.retryable
    )
      return;
    owner.current = { api, id };
    setPreview({
      owner: owner.current,
      url: null,
      state: "loading",
      status: null,
      retryable: false,
    });
  };
  return {
    url: current.url,
    state: current.state,
    status: current.status,
    retryable: current.retryable,
    retry,
  };
}
export function useAssetPreview(api: AssetsApi, asset: UploadedAttachment | null): string | null {
  return useAssetPreviewState(api, asset).url;
}
