import type { ReactElement } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AssetsApi } from "../../clients/media/assetsApi";
import {
  type Attachment,
  type AttachmentLimits,
  type AttachmentSource,
  effectiveLimits,
  type FileRefusal,
  sortFiles,
  type TurnAttachment,
} from "../../turnInput";
import { AttachmentPicker, AttachmentStrip, useAttachmentPicker } from "../Attachments/Attachments";
import type { KeepInVaultContext } from "../Attachments/KeepInVault";
import { Composer, type ComposerProps } from "./Composer";

export interface MediaComposerProps extends ComposerProps {
  api?: AssetsApi | undefined;
  attachments: readonly Attachment[];
  onAttachments: (attachments: readonly Attachment[]) => void;
  destinationLabel: string;
  limits?: AttachmentLimits;
  /** Given only where a vault write is offered; it reaches the strip's keep control. */
  keepInVault?: KeepInVaultContext | undefined;
}

const RESTORED_PREFIX = "restored-";

/** `unavailable` is set once the strip's own read of this restored attachment's local copy
 * has come back 410 (gone) or 404 (never here); see `onUnavailable` below. */
function restored(ref: Attachment, unavailable: boolean): TurnAttachment {
  return {
    localId: `${RESTORED_PREFIX}${ref.asset_id}`,
    file: new File([], ref.name, { type: ref.media_type }),
    name: ref.name,
    byteSize: ref.byte_size,
    mediaType: ref.media_type,
    status: unavailable ? "error" : "ready",
    progress: unavailable ? null : 100,
    asset: ref,
    error: unavailable ? "unavailable" : null,
    failure: null,
    source: ref.source,
    modified_at: ref.modified_at,
  };
}

function mergeRefs(primary: readonly Attachment[], extra: readonly Attachment[]): Attachment[] {
  const seen = new Set<string>();
  const result: Attachment[] = [];
  for (const item of [...primary, ...extra]) {
    if (seen.has(item.asset_id)) continue;
    seen.add(item.asset_id);
    result.push(item);
  }
  return result;
}

type SortReason = FileRefusal | "capabilities_pending";

interface FileRefusalItem {
  id: number;
  name: string | null;
  reason: SortReason;
}

interface DocumentOfferItem {
  id: number;
  file: File;
}

/** One line of copy per file-sort refusal, so a refused file's name and its reason always travel
 * together. `name` is `null` only for `capabilities_pending`, whose copy names no file. `{n}` is
 * the effective per-image or per-turn MiB limit, or the effective attachment count. */
function refusalMessage(
  item: FileRefusalItem,
  cap: { maxFileBytes: number; maxAttachments: number; maxTurnBytes: number },
): string {
  const name = item.name ?? "";
  switch (item.reason) {
    case "capabilities_pending":
      return "Checking the image limits. Wait a moment, then add the image again.";
    case "file_too_large":
      return `"${name}" is too large (${Math.floor(cap.maxFileBytes / 1_048_576)} MiB per image).`;
    case "attachment_count":
      return `"${name}" was not added (${cap.maxAttachments} attachments at most).`;
    case "turn_too_large":
      return `"${name}" was not added (${Math.floor(cap.maxTurnBytes / 1_048_576)} MiB per message).`;
    default:
      return `"${name}" can't be attached (images must be PNG, JPEG or WebP). Copy the part you need and paste it into the message.`;
  }
}

export function MediaComposer(props: MediaComposerProps): ReactElement {
  const { api, ...rest } = props;
  if (!api) return <Composer {...rest} />;
  return <MediaComposerWithApi {...rest} api={api} />;
}

function MediaComposerWithApi({
  api,
  attachments: persisted,
  onAttachments,
  destinationLabel,
  limits,
  keepInVault,
  ...props
}: MediaComposerProps & { api: AssetsApi }): ReactElement {
  const [capabilities, setCapabilities] = useState<
    (AttachmentLimits & { maxDimension: number }) | null
  >(null);
  const [capabilityFailed, setCapabilityFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setCapabilities(null);
    setCapabilityFailed(false);
    if (api.capabilities)
      void api
        .capabilities(controller.signal)
        .then((value) => {
          if (!controller.signal.aborted) setCapabilities(value);
        })
        .catch(() => {
          if (!controller.signal.aborted) setCapabilityFailed(true);
        });
    return () => controller.abort();
  }, [api]);
  const effective = limits ?? capabilities ?? undefined;
  const capabilitiesPending = Boolean(api.capabilities && !capabilities);
  const persistedRef = useRef(persisted);
  persistedRef.current = persisted;
  const delivered = useRef(new Set(persisted.map((item) => item.asset_id)));
  const [refusals, setRefusals] = useState<readonly FileRefusalItem[]>([]);
  const [documentOffers, setDocumentOffers] = useState<readonly DocumentOfferItem[]>([]);
  const editorRef = useRef(props);
  editorRef.current = props;
  const documentDestination = useRef(props.editorKey);
  const documentGeneration = useRef(0);
  const documentInsertions = useRef(new WeakSet<DocumentOfferItem>());
  if (documentDestination.current !== props.editorKey) {
    documentDestination.current = props.editorKey;
    documentGeneration.current += 1;
    setDocumentOffers([]);
  }
  useEffect(
    () => () => {
      documentGeneration.current += 1;
    },
    [],
  );
  const nextItemId = useRef(0);
  const addRefusals = useCallback(
    (items: readonly { name: string | null; reason: SortReason }[]) => {
      if (!items.length) return;
      setRefusals((current) => [
        ...current,
        ...items.map((item) => ({ ...item, id: nextItemId.current++ })),
      ]);
    },
    [],
  );
  const dismissRefusal = useCallback((id: number) => {
    setRefusals((current) => current.filter((item) => item.id !== id));
  }, []);
  // `addFiles` re-validates the whole batch against its own ref, which two batches handed to
  // `handleFiles` before either has rendered can already be ahead of the count `sortFiles` saw.
  // Remembering the batch just handed to it turns that single reason into one refusal line per
  // file, instead of letting the batch vanish with nothing shown.
  const lastPickerBatch = useRef<readonly File[]>([]);
  const onPickerReject = useCallback(
    (reason: string) => {
      addRefusals(
        lastPickerBatch.current.map((file) => ({ name: file.name, reason: reason as FileRefusal })),
      );
    },
    [addRefusals],
  );
  const [unavailableAssets, setUnavailableAssets] = useState<ReadonlySet<string>>(new Set());
  const onUnavailable = useCallback((localId: string) => {
    if (!localId.startsWith(RESTORED_PREFIX)) return;
    const assetId = localId.slice(RESTORED_PREFIX.length);
    setUnavailableAssets((current) =>
      current.has(assetId) ? current : new Set(current).add(assetId),
    );
  }, []);
  const onChange = useCallback(
    (items: readonly TurnAttachment[]) => {
      const ready = items.flatMap((item) =>
        item.status === "ready" && item.asset && !delivered.current.has(item.asset.asset_id)
          ? [{ ...item.asset, modified_at: item.modified_at, source: item.source }]
          : [],
      );
      if (!ready.length) return;
      for (const item of ready) delivered.current.add(item.asset_id);
      const next = mergeRefs(persistedRef.current, ready);
      persistedRef.current = next;
      onAttachments(next);
    },
    [onAttachments],
  );
  const picker = useAttachmentPicker({
    api,
    destinationId: props.editorKey ?? "new",
    ...(effective ? { limits: effective } : {}),
    existingAttachments: persisted,
    onChange,
    onReject: onPickerReject,
  });
  // A delivered picker row hides itself from `allItems` for good (below), once its asset is in
  // `delivered`: the strip then shows it only through `persisted`. If `persisted` later drops
  // that asset (a same-key draft conflict resolved with "use-server"), the row still sits in the
  // picker's own list and still counts toward `addFiles`'s limit, refusing a batch the user
  // cannot see anywhere. Clearing it here, whenever `persisted` itself changes, keeps that count
  // matched to what the strip shows.
  const prevPersistedRef = useRef(persisted);
  if (prevPersistedRef.current !== persisted) {
    prevPersistedRef.current = persisted;
    const persistedIds = new Set(persisted.map((item) => item.asset_id));
    const orphaned = picker.attachments.filter(
      (item) =>
        item.asset &&
        delivered.current.has(item.asset.asset_id) &&
        !persistedIds.has(item.asset.asset_id),
    );
    if (orphaned.length)
      picker.clear(
        orphaned.map((item) => item.localId),
        false,
      );
  }
  const allItems = useMemo(() => {
    const persistedIds = new Set(persisted.map((item) => item.asset_id));
    return [
      ...persisted.map((item) => restored(item, unavailableAssets.has(item.asset_id))),
      ...picker.attachments.filter(
        (item) =>
          !item.asset ||
          (!persistedIds.has(item.asset.asset_id) && !delivered.current.has(item.asset.asset_id)),
      ),
    ];
  }, [persisted, picker.attachments, unavailableAssets]);
  const pending = picker.attachments.some((item) => item.status !== "ready");
  const hasUnavailable = allItems.some((item) => item.error === "unavailable");
  const uploading = picker.attachments.some(
    (item) => item.status === "queued" || item.status === "uploading",
  );
  const hasFailed = picker.attachments.some((item) => item.status === "error") || hasUnavailable;
  const outsideLimits = Boolean(
    capabilities &&
      (persisted.length > (capabilities.maxAttachments ?? 10) ||
        persisted.reduce((sum, item) => sum + item.byte_size, 0) >
          (capabilities.maxTurnBytes ?? 0) ||
        persisted.some(
          (item) =>
            item.byte_size > (capabilities.maxFileBytes ?? 0) ||
            item.width > capabilities.maxDimension ||
            item.height > capabilities.maxDimension,
        )),
  );
  const remove = useCallback(
    (localId: string) => {
      if (localId.startsWith(RESTORED_PREFIX)) {
        const assetId = localId.slice(RESTORED_PREFIX.length);
        picker.clear(
          picker.attachments
            .filter((item) => item.asset?.asset_id === assetId)
            .map((item) => item.localId),
          false,
        );
        const next = persistedRef.current.filter((item) => item.asset_id !== assetId);
        persistedRef.current = next;
        onAttachments(next);
      } else {
        const removed = picker.attachments.find((item) => item.localId === localId)?.asset
          ?.asset_id;
        const remaining = picker.attachments
          .filter((item) => item.localId !== localId && item.status === "ready" && item.asset)
          .flatMap((item) =>
            item.asset
              ? [{ ...item.asset, modified_at: item.modified_at, source: item.source }]
              : [],
          );
        picker.remove(localId, false);
        onAttachments(
          mergeRefs(
            persisted.filter((item) => item.asset_id !== removed),
            remaining,
          ),
        );
      }
    },
    [onAttachments, persisted, picker],
  );
  const cap = effectiveLimits(effective);
  const handleFiles = useCallback(
    (files: readonly File[], source: AttachmentSource = "picker") => {
      if (capabilitiesPending) {
        addRefusals([{ name: null, reason: "capabilities_pending" }]);
        return;
      }
      const bytes = allItems.reduce((sum, item) => sum + item.byteSize, 0);
      const sorted = sortFiles(files, bytes, allItems.length, effective);
      if (sorted.images.length) {
        lastPickerBatch.current = sorted.images;
        picker.addFiles(sorted.images, source);
      }
      if (sorted.documents.length)
        setDocumentOffers((current) => [
          ...current,
          ...sorted.documents.map((file) => ({ id: nextItemId.current++, file })),
        ]);
      addRefusals(sorted.refused);
    },
    [capabilitiesPending, allItems, effective, picker, addRefusals],
  );
  const insertDocument = useCallback(
    async (offer: DocumentOfferItem) => {
      if (documentInsertions.current.has(offer)) return;
      documentInsertions.current.add(offer);
      const owner = documentGeneration.current;
      let text: string;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(await offer.file.arrayBuffer());
      } catch {
        if (owner !== documentGeneration.current) return;
        setDocumentOffers((current) => current.filter((item) => item.id !== offer.id));
        addRefusals([{ name: offer.file.name, reason: "unsupported_type" }]);
        return;
      }
      if (owner !== documentGeneration.current) return;
      const editor = editorRef.current;
      const value = editor.value ?? "";
      const separator = value.length > 0 && !value.endsWith("\n") ? "\n\n" : "";
      const inserted = `${value}${separator}${text}`;
      // A second file read can finish before the controlled value rerenders.
      editorRef.current = { ...editor, value: inserted };
      editor.onEdit?.(inserted, inserted.length, inserted.length);
      setDocumentOffers((current) => current.filter((item) => item.id !== offer.id));
    },
    [addRefusals],
  );
  const onSend = useCallback(
    async (text: string): Promise<boolean> => {
      const submitted = picker.attachments
        .filter((item) => item.status === "ready")
        .map((item) => item.localId);
      const result = await props.onSend(text);
      if (result) {
        picker.clear(submitted, false);
      }
      return result;
    },
    [picker, props.onSend],
  );
  const blockedReason = uploading
    ? "Wait for the attachments to finish uploading."
    : hasFailed
      ? "Remove or retry the attachments that failed."
      : outsideLimits
        ? "The attachments exceed the destination's limits."
        : props.blockedReason;
  return (
    <Composer
      {...props}
      attachmentControl={<AttachmentPicker onFiles={handleFiles} disabled={capabilitiesPending} />}
      attachmentStrip={
        <section aria-label={destinationLabel}>
          {capabilityFailed ? (
            <p role="status">
              Image attachments are unavailable. Reopen this conversation to try again.
            </p>
          ) : null}
          {allItems.length && capabilities ? (
            <p className="attachment-limits">
              Image limits: {capabilities.maxAttachments} /{" "}
              {Math.floor((capabilities.maxFileBytes ?? 0) / 1_048_576)} MiB each ·{" "}
              {Math.floor((capabilities.maxTurnBytes ?? 0) / 1_048_576)} MiB total ·{" "}
              {capabilities.maxDimension}px
            </p>
          ) : null}
          {outsideLimits ? (
            <p role="alert">
              These attachments exceed the destination's limits. Remove an image or choose a smaller
              one.
            </p>
          ) : null}
          {allItems.length ? (
            <p className="attachment-destination">Send to: {destinationLabel}</p>
          ) : null}
          {allItems.length ? (
            <p className="attachment-hidden-context">
              Instruction files such as CLAUDE.md, hooks and tool results are handled by the
              provider and are not shown here.
            </p>
          ) : null}
          {documentOffers.length ? (
            <ul className="attachment-document-offers">
              {documentOffers.map((offer) => (
                <li key={offer.id}>
                  <p>{`"${offer.file.name}" can be inserted into the message as text.`}</p>
                  <button type="button" onClick={() => void insertDocument(offer)}>
                    Insert into message
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {refusals.length ? (
            <ul role="alert" className="attachment-refusals">
              {refusals.map((item) => (
                <li key={item.id}>
                  {refusalMessage(item, cap)}{" "}
                  <button type="button" onClick={() => dismissRefusal(item.id)}>
                    Dismiss
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <AttachmentStrip
            api={api}
            attachments={allItems}
            onRemove={remove}
            onRetry={picker.retry}
            onCancel={picker.cancel}
            onUnavailable={onUnavailable}
            {...(keepInVault ? { keepInVault } : {})}
          />
        </section>
      }
      hasAttachments={allItems.length > 0}
      submitDisabled={
        props.submitDisabled ||
        pending ||
        outsideLimits ||
        hasUnavailable ||
        (allItems.length > 0 && capabilitiesPending)
      }
      blockedReason={blockedReason}
      onFiles={handleFiles}
      onSend={onSend}
    />
  );
}
