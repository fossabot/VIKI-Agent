/**
 * The avatar editor: choose a local image file, preview an integer square crop on its rotated,
 * EXIF-normalized grid, and see the sizes it will produce. Choosing a file decodes it locally and
 * uploads nothing; Save uploads it with the avatar purpose, transforms it and reports the new id.
 */
import {
  type ChangeEvent,
  type KeyboardEvent,
  type PointerEvent,
  type ReactElement,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { AssetsApiError } from "../../clients/media/assetsApi";
import { AvatarsApiError } from "../../clients/media/avatarsApi";
import { MediaContext } from "../../context/mediaContext";
import { useModalFocus } from "../Dialog/useModalFocus";
import {
  type Crop,
  cropForKey,
  initialCrop,
  moveCrop,
  type Rotation,
  rotateBy,
  rotatedSize,
  transformCrop,
  zoomCrop,
} from "./crop";
import { useIdentity } from "./identity";
import "./AvatarEditor.css";

const PREVIEW_BOX = 256;
const SAMPLE_SIZES = [24, 32, 64, 128] as const;
const MAX_FILE_BYTES = 5 << 20;
const AVATAR_MAX_PIXELS = 20_000_000;

async function defaultDecode(file: File): Promise<ImageBitmap> {
  return createImageBitmap(file, { imageOrientation: "from-image" });
}

function readUint16BE(bytes: Uint8Array, offset: number): number {
  return ((bytes[offset] ?? 0) << 8) | (bytes[offset + 1] ?? 0);
}

function readUint32BE(bytes: Uint8Array, offset: number): number {
  return (
    (((bytes[offset] ?? 0) << 24) |
      ((bytes[offset + 1] ?? 0) << 16) |
      ((bytes[offset + 2] ?? 0) << 8) |
      (bytes[offset + 3] ?? 0)) >>>
    0
  );
}

function readUint16LE(bytes: Uint8Array, offset: number): number {
  return (bytes[offset] ?? 0) | ((bytes[offset + 1] ?? 0) << 8);
}

function readUint24LE(bytes: Uint8Array, offset: number): number {
  return (bytes[offset] ?? 0) | ((bytes[offset + 1] ?? 0) << 8) | ((bytes[offset + 2] ?? 0) << 16);
}

function readUint32LE(bytes: Uint8Array, offset: number): number {
  return (
    (bytes[offset] ?? 0) |
    ((bytes[offset + 1] ?? 0) << 8) |
    ((bytes[offset + 2] ?? 0) << 16) |
    ((bytes[offset + 3] ?? 0) << 24)
  );
}

function pngPixels(head: Uint8Array): number | null {
  if (
    head.length < 24 ||
    head[0] !== 0x89 ||
    head[1] !== 0x50 ||
    head[2] !== 0x4e ||
    head[3] !== 0x47 ||
    head[12] !== 0x49 ||
    head[13] !== 0x48 ||
    head[14] !== 0x44 ||
    head[15] !== 0x52
  ) {
    return null;
  }
  return readUint32BE(head, 16) * readUint32BE(head, 20);
}

/** The first true SOFn frame header's width and height: any 0xC0-0xCF marker except DHT (C4),
 * JPG (C8) and DAC (CC), which share that range but never start a frame. Walks the segments from
 * SOI and returns null at any sequence libjpeg would resync on: a non-0xFF byte where a marker
 * belongs, or a stuffed zero (0xFF 0x00) after any 0xFF fill. */
function jpegPixels(head: Uint8Array): number | null {
  if (head.length < 4 || head[0] !== 0xff || head[1] !== 0xd8) return null;
  let offset = 2;
  while (offset + 1 < head.length) {
    if (head[offset] !== 0xff) return null;
    let marker = head[offset + 1] ?? 0;
    offset += 2;
    while (marker === 0xff && offset < head.length) {
      marker = head[offset] ?? 0;
      offset += 1;
    }
    if (marker === 0x00) return null;
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd9)) continue;
    if (offset + 7 >= head.length) return null;
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      return readUint16BE(head, offset + 3) * readUint16BE(head, offset + 5);
    }
    offset += readUint16BE(head, offset);
  }
  return null;
}

/** Ported from `_refuse_oversized_webp` (`src/viki_agent/media/media_worker.py`): the same VP8X, VP8L and VP8 chunk
 * layouts and byte offsets, read for their pixel count rather than only refused above a limit. */
function webpPixels(head: Uint8Array): number | null {
  if (
    head.length < 16 ||
    head[0] !== 0x52 ||
    head[1] !== 0x49 ||
    head[2] !== 0x46 ||
    head[3] !== 0x46 ||
    head[8] !== 0x57 ||
    head[9] !== 0x45 ||
    head[10] !== 0x42 ||
    head[11] !== 0x50
  ) {
    return null;
  }
  const chunk = String.fromCharCode(head[12] ?? 0, head[13] ?? 0, head[14] ?? 0, head[15] ?? 0);
  if (chunk === "VP8X" && head.length >= 30) {
    return (readUint24LE(head, 24) + 1) * (readUint24LE(head, 27) + 1);
  }
  if (chunk === "VP8L" && head.length >= 25 && head[20] === 0x2f) {
    const fields = readUint32LE(head, 21);
    return ((fields & 0x3fff) + 1) * (((fields >> 14) & 0x3fff) + 1);
  }
  if (
    chunk === "VP8 " &&
    head.length >= 30 &&
    head[23] === 0x9d &&
    head[24] === 0x01 &&
    head[25] === 0x2a
  ) {
    return (readUint16LE(head, 26) & 0x3fff) * (readUint16LE(head, 28) & 0x3fff);
  }
  return null;
}

/** The declared pixel count read from the header bytes handed to it: a PNG's IHDR and a WebP's
 * first chunk at their fixed offsets, a JPEG's SOFn found by walking its segments from SOI and
 * refusing any sequence libjpeg would resync on. Whether those bytes are the whole file is the
 * caller's claim. `null` for a truncated header or any other format Chromium still decodes (a GIF
 * or a BMP); the caller refuses `null` before any decode, and the post-decode `AVATAR_MAX_PIXELS`
 * check stays as the backstop for a header Chromium reads differently. */
export function headerPixels(head: Uint8Array): number | null {
  return pngPixels(head) ?? jpegPixels(head) ?? webpPixels(head);
}

/** Reads the whole file, at most `MAX_FILE_BYTES` by the time it is called, so no padding
 * segment can push a JPEG's frame header out of reach. */
async function defaultMeasurePixels(file: File): Promise<number | null> {
  return headerPixels(new Uint8Array(await file.arrayBuffer()));
}

function previewGeometry(
  bitmap: ImageBitmap,
  rotation: Rotation,
): { scale: number; offsetX: number; offsetY: number } {
  const { width, height } = rotatedSize(bitmap.width, bitmap.height, rotation);
  const scale = Math.min(PREVIEW_BOX / width, PREVIEW_BOX / height);
  return {
    scale,
    offsetX: (PREVIEW_BOX - width * scale) / 2,
    offsetY: (PREVIEW_BOX - height * scale) / 2,
  };
}

/** Draws `bitmap`, right-angle rotated, at `scale` with its rotated top-left at (offsetX, offsetY). */
function paintRotated(
  ctx: CanvasRenderingContext2D,
  bitmap: ImageBitmap,
  rotation: Rotation,
  scale: number,
  offsetX: number,
  offsetY: number,
): void {
  ctx.save();
  ctx.translate(offsetX, offsetY);
  ctx.scale(scale, scale);
  if (rotation === 90) {
    ctx.translate(bitmap.height, 0);
    ctx.rotate(Math.PI / 2);
  } else if (rotation === 180) {
    ctx.translate(bitmap.width, bitmap.height);
    ctx.rotate(Math.PI);
  } else if (rotation === 270) {
    ctx.translate(0, bitmap.width);
    ctx.rotate((3 * Math.PI) / 2);
  }
  ctx.drawImage(bitmap, 0, 0);
  ctx.restore();
}

function drawPreview(
  canvas: HTMLCanvasElement | null,
  bitmap: ImageBitmap,
  rotation: Rotation,
): void {
  const ctx = canvas?.getContext("2d") ?? null;
  if (!ctx) return;
  ctx.clearRect(0, 0, PREVIEW_BOX, PREVIEW_BOX);
  const { scale, offsetX, offsetY } = previewGeometry(bitmap, rotation);
  paintRotated(ctx, bitmap, rotation, scale, offsetX, offsetY);
}

function drawSample(
  canvas: HTMLCanvasElement | null,
  bitmap: ImageBitmap,
  rotation: Rotation,
  crop: Crop,
  size: number,
): void {
  const ctx = canvas?.getContext("2d") ?? null;
  if (!ctx) return;
  ctx.clearRect(0, 0, size, size);
  const scale = size / crop.edge;
  paintRotated(ctx, bitmap, rotation, scale, -crop.x * scale, -crop.y * scale);
}

type Notice =
  | "too_large"
  | "too_many_pixels"
  | "unusable"
  | "unreadable"
  | "capacity"
  | "overloaded"
  | "processing"
  | "worker_timeout"
  | "decoder_unavailable"
  | "failed";

/** The editor's own copy for an error code; the server's message is never shown. */
function noticeFor(error: unknown): Notice {
  const code =
    error instanceof AssetsApiError || error instanceof AvatarsApiError ? error.code : null;
  if (code === "too_large" || code === "capacity" || code === "overloaded") return code;
  if (code === "decoder_unavailable") return "decoder_unavailable";
  if (code === "transform_failed" || code === "decode_failed") return "unusable";
  if (code === "worker_timeout") return "worker_timeout";
  return "failed";
}

function noticeText(notice: Notice): string {
  switch (notice) {
    case "too_large":
      return "The image must be 5 MiB or less.";
    case "too_many_pixels":
      return "The image must be 20 megapixels or less.";
    case "unusable":
      return "This image cannot become an avatar.";
    case "unreadable":
      return "This file could not be read. Choose it again.";
    case "capacity":
      return "Image storage is full.";
    case "overloaded":
      return "Another image is being processed. Wait a moment, then try again.";
    case "processing":
      return "Still processing. Save again to finish.";
    case "worker_timeout":
      return "Saving took too long. Save again.";
    case "decoder_unavailable":
      // The same install-fix sentence the chat attachment path shows (Attachments.tsx).
      return "The image checker is not installed. Quit VIKI and run uv sync.";
    case "failed":
      return "Could not save the avatar.";
  }
}

/** What one avatar id was minted for: a retry of the same target reuses the id. */
interface SaveTarget {
  file: File;
  rotation: Rotation;
  x: number;
  y: number;
  edge: number;
}

function sameTarget(a: SaveTarget, b: SaveTarget): boolean {
  return (
    a.file === b.file &&
    a.rotation === b.rotation &&
    a.x === b.x &&
    a.y === b.y &&
    a.edge === b.edge
  );
}

export interface AvatarEditorProps {
  onSaved: (avatarId: string) => void;
  onCancel: () => void;
  decode?: (file: File) => Promise<ImageBitmap>;
  measurePixels?: (file: File) => Promise<number | null>;
}

export function AvatarEditor({
  onSaved,
  onCancel,
  decode = defaultDecode,
  measurePixels = defaultMeasurePixels,
}: AvatarEditorProps): ReactElement {
  const assets = useContext(MediaContext);
  const { avatars } = useIdentity();
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useModalFocus({ active: true, dialogRef, initialFocusRef: cancelRef, onDismiss: cancel });

  const [bitmap, setBitmap] = useState<ImageBitmap | null>(null);
  const [rotation, setRotation] = useState<Rotation>(0);
  const [crop, setCrop] = useState<Crop | null>(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  // A completion whose generation is no longer current (a newer choice, a changed crop or
  // rotation, Cancel or unmount) is ignored.
  const decodeGeneration = useRef(0);
  const saveGeneration = useRef(0);
  const saveController = useRef<AbortController | null>(null);
  const fileRef = useRef<File | null>(null);
  const minted = useRef<(SaveTarget & { avatarId: string }) | null>(null);
  // One upload per file: a retry, or a new crop of the same file, reuses the stored source.
  const uploaded = useRef<{ file: File; assetId: string } | null>(null);

  useEffect(() => {
    return () => {
      decodeGeneration.current += 1;
      saveGeneration.current += 1;
      saveController.current?.abort();
    };
  }, []);

  const previewRef = useRef<HTMLCanvasElement | null>(null);
  const sampleRefs = useRef<Map<number, HTMLCanvasElement | null>>(new Map());
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    startCrop: Crop;
  } | null>(null);

  // The previous bitmap (if any) is closed on every change, including on unmount.
  useEffect(() => {
    return () => {
      bitmap?.close();
    };
  }, [bitmap]);

  useEffect(() => {
    if (!bitmap || !crop) return;
    drawPreview(previewRef.current, bitmap, rotation);
    for (const size of SAMPLE_SIZES) {
      drawSample(sampleRefs.current.get(size) ?? null, bitmap, rotation, crop, size);
    }
  }, [bitmap, rotation, crop]);

  /** Ignores the save in flight and aborts its requests. */
  function dropSave(): void {
    saveGeneration.current += 1;
    saveController.current?.abort();
    saveController.current = null;
    setSaving(false);
    setNotice(null);
  }

  function changeCrop(next: Crop): void {
    if (crop && next.x === crop.x && next.y === crop.y && next.edge === crop.edge) return;
    dropSave();
    setCrop(next);
  }

  async function chooseFile(file: File): Promise<void> {
    // Any new choice supersedes a save in flight and a decode still pending, even one refused
    // before it is ever decoded.
    dropSave();
    const generation = ++decodeGeneration.current;
    if (file.size > MAX_FILE_BYTES) {
      setNotice("too_large");
      return;
    }
    let measured: number | null;
    try {
      measured = await measurePixels(file);
    } catch {
      // A rejected read (a NotReadableError: the file was moved, deleted or rewritten after the
      // pick) is not the same as a header the parsers made sense of and refused; it gets its own
      // notice, and only for the still-current choice.
      if (generation === decodeGeneration.current) setNotice("unreadable");
      return;
    }
    // Refused regardless of staleness: a superseded choice must never start an expensive decode
    // for a file already known to be oversized. The notice itself is only for the current choice.
    // An unmeasured file is refused too: Chromium would decode it in full before any pixel check,
    // and the worker admits only PNG, JPEG and WebP anyway.
    if (measured === null || measured > AVATAR_MAX_PIXELS) {
      if (generation === decodeGeneration.current) {
        setNotice(measured === null ? "unusable" : "too_many_pixels");
      }
      return;
    }
    // A choice superseded while the measure was pending starts no decode and sets no notice.
    if (generation !== decodeGeneration.current) return;
    let next: ImageBitmap;
    try {
      next = await decode(file);
    } catch {
      if (generation === decodeGeneration.current) setNotice("unusable");
      return;
    }
    if (generation !== decodeGeneration.current) {
      next.close();
      return;
    }
    if (next.width * next.height > AVATAR_MAX_PIXELS) {
      next.close();
      setNotice("too_many_pixels");
      return;
    }
    fileRef.current = file;
    setBitmap(next);
    setRotation(0);
    setCrop(initialCrop(next.width, next.height));
  }

  async function save(): Promise<void> {
    const file = fileRef.current;
    if (!assets || !avatars || !file || !crop) return;
    const target: SaveTarget = { file, rotation, x: crop.x, y: crop.y, edge: crop.edge };
    const avatarId =
      minted.current && sameTarget(minted.current, target)
        ? minted.current.avatarId
        : crypto.randomUUID();
    minted.current = { ...target, avatarId };
    const controller = new AbortController();
    saveController.current = controller;
    const generation = ++saveGeneration.current;
    const current = (): boolean => generation === saveGeneration.current;
    setSaving(true);
    setNotice(null);
    try {
      let sourceId = uploaded.current?.file === file ? uploaded.current.assetId : null;
      if (sourceId === null) {
        const row = await assets.upload(file, { purpose: "avatar", signal: controller.signal });
        sourceId = row.asset_id;
        uploaded.current = { file, assetId: sourceId };
        if (!current()) return;
      }
      const request = {
        avatar_id: avatarId,
        source_asset_id: sourceId,
        crop: transformCrop(crop),
        rotation,
      };
      try {
        await avatars.transform(request, controller.signal);
      } catch (error) {
        if (!(error instanceof AvatarsApiError && error.code === "timeout")) throw error;
        if (!current()) return;
        // The transform may still have committed: one read of the smallest variant tells.
        const found = await avatars.variant(avatarId, 24, controller.signal).then(
          () => true,
          () => false,
        );
        if (!current()) return;
        if (!found) {
          setNotice("processing");
          return;
        }
      }
      if (current()) onSaved(avatarId);
    } catch (error) {
      // A conflict means the id is burned server-side (an interrupted attempt's leftover
      // directory): the editor never reuses it, so the next Save mints a fresh one.
      if (error instanceof AvatarsApiError && error.code === "command_conflict") {
        minted.current = null;
      }
      // The transform found no source asset (it expired between upload and transform): the held
      // source id is stale, so the next Save uploads the file again instead of reusing it.
      if (error instanceof AvatarsApiError && error.code === "not_found") {
        uploaded.current = null;
      }
      if (current()) setNotice(noticeFor(error));
    } finally {
      if (current()) {
        saveController.current = null;
        setSaving(false);
      }
    }
  }

  function cancel(): void {
    decodeGeneration.current += 1;
    dropSave();
    setBitmap(null);
    onCancel();
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void chooseFile(file);
  }

  const geometry = bitmap ? previewGeometry(bitmap, rotation) : null;
  const rotatedDims = bitmap ? rotatedSize(bitmap.width, bitmap.height, rotation) : null;

  function handleOverlayKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    // Ctrl, Alt and Meta chords (e.g. the browser's own Ctrl+plus/minus zoom) are left alone.
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (!crop || !rotatedDims) return;
    const next = cropForKey(event.key, event.shiftKey, crop, rotatedDims.width, rotatedDims.height);
    if (next) {
      event.preventDefault();
      changeCrop(next);
    }
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>): void {
    if (!crop) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startCrop: crop,
    };
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>): void {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !geometry || !rotatedDims) return;
    const dx = Math.round((event.clientX - drag.startX) / geometry.scale);
    const dy = Math.round((event.clientY - drag.startY) / geometry.scale);
    changeCrop(moveCrop(drag.startCrop, dx, dy, rotatedDims.width, rotatedDims.height));
  }

  function handlePointerUp(event: PointerEvent<HTMLDivElement>): void {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  }

  function handleZoom(direction: 1 | -1): void {
    if (!crop || !rotatedDims) return;
    changeCrop(zoomCrop(crop, direction, rotatedDims.width, rotatedDims.height));
  }

  function handleRotate(turn: 1 | -1): void {
    if (!bitmap) return;
    const nextRotation = rotateBy(rotation, turn);
    const nextDims = rotatedSize(bitmap.width, bitmap.height, nextRotation);
    dropSave();
    setRotation(nextRotation);
    setCrop(initialCrop(nextDims.width, nextDims.height));
  }

  return (
    <div
      ref={dialogRef}
      className="avatar-editor"
      role="dialog"
      aria-modal="true"
      aria-labelledby="avatar-editor-title"
      tabIndex={-1}
    >
      <div id="avatar-editor-title">Edit avatar</div>
      <label htmlFor="avatar-editor-file">Choose image</label>
      <input
        id="avatar-editor-file"
        type="file"
        accept="image/png,image/jpeg,image/webp"
        onChange={handleChange}
      />
      {bitmap && crop && geometry ? (
        <>
          <div
            className="avatar-editor-preview"
            style={{ width: PREVIEW_BOX, height: PREVIEW_BOX }}
          >
            <canvas ref={previewRef} width={PREVIEW_BOX} height={PREVIEW_BOX} />
            {/* A <fieldset> (Biome's suggestion for role="group") carries form semantics and its
                own box model that don't fit a crop rectangle positioned over a canvas. */}
            {/* biome-ignore lint/a11y/useSemanticElements: see above */}
            <div
              className="avatar-editor-crop-overlay"
              role="group"
              // The crop overlay takes keyboard focus and arrow-key input on purpose, so it
              // needs a tab stop.
              // biome-ignore lint/a11y/noNoninteractiveTabindex: see above
              tabIndex={0}
              aria-label="Crop"
              aria-describedby="avatar-editor-crop-help"
              style={{
                left: geometry.offsetX + crop.x * geometry.scale,
                top: geometry.offsetY + crop.y * geometry.scale,
                width: crop.edge * geometry.scale,
                height: crop.edge * geometry.scale,
              }}
              onKeyDown={handleOverlayKeyDown}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerCancel={handlePointerUp}
              onLostPointerCapture={handlePointerUp}
            />
          </div>
          <p id="avatar-editor-crop-help" className="sr-only">
            Arrow keys move the crop 1 pixel, Shift and arrow keys move 10. Plus and minus zoom.
          </p>
          <p className="avatar-editor-crop-readout">
            Crop <span>{`${crop.x}, ${crop.y}, ${crop.edge}`}</span>
          </p>
          <div className="avatar-editor-controls">
            <button type="button" className="btn alt sm" onClick={() => handleZoom(1)}>
              Zoom in
            </button>
            <button type="button" className="btn alt sm" onClick={() => handleZoom(-1)}>
              Zoom out
            </button>
            <button type="button" className="btn alt sm" onClick={() => handleRotate(-1)}>
              Rotate left
            </button>
            <button type="button" className="btn alt sm" onClick={() => handleRotate(1)}>
              Rotate right
            </button>
          </div>
          <div className="avatar-editor-samples">
            {SAMPLE_SIZES.map((size) => (
              <div className="avatar-editor-sample" key={size}>
                <canvas
                  ref={(element) => {
                    sampleRefs.current.set(size, element);
                  }}
                  width={size}
                  height={size}
                />
                <span>{size}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
      {notice ? <p role="alert">{noticeText(notice)}</p> : null}
      <button
        type="button"
        className="btn go"
        disabled={!assets || !avatars || !bitmap || !crop || saving}
        onClick={() => void save()}
      >
        Save
      </button>
      <button type="button" className="btn txt" ref={cancelRef} onClick={cancel}>
        Cancel
      </button>
    </div>
  );
}
