import { type ChangeEvent, useContext, useRef, useState } from "react";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import { MediaContext } from "../../context/mediaContext";
import { ACCEPTED_MEDIA_TYPES, type Attachment, type UnavailableAttachment } from "../../turnInput";
import { useAssetPreviewState } from "../Attachments/Attachments";
import { useModalFocus } from "../Dialog/useModalFocus";
import { Loading } from "../Loading/Loading";
import "./AttachmentImages.css";

function RelinkControl({
  assetId,
  relink,
  onRelinked,
}: {
  assetId: string;
  relink: (assetId: string, file: File, signal?: AbortSignal) => Promise<void>;
  onRelinked: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<"mismatch" | "failed" | null>(null);
  const [pending, setPending] = useState(false);
  const choose = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    event.target.value = "";
    if (!file) return;
    setPending(true);
    try {
      await relink(assetId, file);
      setError(null);
      onRelinked();
    } catch (thrown) {
      setError(
        thrown instanceof AssetsApiError && thrown.code === "asset_mismatch"
          ? "mismatch"
          : "failed",
      );
    } finally {
      setPending(false);
    }
  };
  return (
    <>
      <button type="button" disabled={pending} onClick={() => inputRef.current?.click()}>
        Choose the same file to restore
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_MEDIA_TYPES.join(",")}
        hidden
        onChange={choose}
      />
      {error === "mismatch" ? (
        <p role="status">The chosen file does not match the sent image.</p>
      ) : error === "failed" ? (
        <p role="status">The image could not be restored.</p>
      ) : null}
    </>
  );
}

function Preview({
  asset,
  api,
  onRelinked,
}: {
  asset: Attachment;
  api: AssetsApi;
  onRelinked: () => void;
}) {
  const { url, state, retryable, retry } = useAssetPreviewState(api, asset);
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useModalFocus({
    active: open && Boolean(url),
    dialogRef,
    initialFocusRef: closeRef,
    onDismiss: close,
  });
  return (
    <>
      <button
        type="button"
        className="message-image-button"
        disabled={!url}
        aria-label={`Preview: ${asset.name}`}
        onClick={() => setOpen(true)}
      >
        {url ? <img src={url} alt={asset.name} loading="lazy" /> : <span>{asset.name}</span>}
      </button>
      {state === "loading" ? <Loading>Loading preview…</Loading> : null}
      {state === "expired" ? (
        <>
          <p>Attachment no longer stored locally</p>
          {api.relink ? (
            <RelinkControl assetId={asset.asset_id} relink={api.relink} onRelinked={onRelinked} />
          ) : null}
        </>
      ) : state === "unavailable" ? (
        <p>Attachment could not be loaded</p>
      ) : null}
      {retryable ? (
        <button type="button" onClick={retry}>
          Retry preview
        </button>
      ) : null}
      {open && url ? (
        <div className="message-image-overlay">
          <div
            ref={dialogRef}
            className="message-image-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={asset.name}
            tabIndex={-1}
          >
            <button ref={closeRef} type="button" onClick={close}>
              Close preview
            </button>
            <img src={url} alt={asset.name} />
            <p>
              {asset.name} · {asset.width} × {asset.height} · {asset.byte_size.toLocaleString()} B
            </p>
          </div>
        </div>
      ) : null}
    </>
  );
}

function RelinkablePreview({ asset, api }: { asset: Attachment; api: AssetsApi }) {
  const [remountKey, setRemountKey] = useState(0);
  return (
    <Preview
      key={remountKey}
      asset={asset}
      api={api}
      onRelinked={() => setRemountKey((current) => current + 1)}
    />
  );
}

export function AttachmentImages({
  attachments,
  unavailable = [],
}: {
  attachments: readonly Attachment[];
  unavailable?: readonly UnavailableAttachment[];
}) {
  const api = useContext(MediaContext);
  const unavailableReason = (reason: UnavailableAttachment["reason"]): string => {
    if (reason === "not_in_local_assets") return "This image has no copy in local attachments.";
    if (reason === "too_large" || reason === "limit")
      return "This image exceeds the history preview limits.";
    return "The image data in this history could not be read.";
  };
  return (
    <div className="message-images">
      {attachments.map((asset, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: Sent image order is immutable; identical native blocks may repeat the same asset.
        <figure className="message-image" key={`${asset.asset_id}-${index}`}>
          {api ? <RelinkablePreview asset={asset} api={api} /> : null}
          <figcaption>
            {asset.name} · {asset.width} × {asset.height}
          </figcaption>
        </figure>
      ))}
      {unavailable.map((asset, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: Unavailable native blocks have no asset ID and their sent order is immutable.
        <figure className="message-image message-image-unavailable" key={`unavailable-${index}`}>
          <figcaption>{asset.name}</figcaption>
          <p>{unavailableReason(asset.reason)}</p>
          <small>
            {asset.media_type}
            {asset.byte_size > 0 ? ` · ${asset.byte_size.toLocaleString()} B` : ""}
          </small>
        </figure>
      ))}
    </div>
  );
}
