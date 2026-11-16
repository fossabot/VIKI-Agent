/** Typed attachment input shared by the composer lane and the upload surface. */

export const MAX_ATTACHMENTS = 10;
export const MAX_FILE_BYTES = 20 * 1024 * 1024;
export const MAX_TURN_BYTES = 50 * 1024 * 1024;
export const ACCEPTED_MEDIA_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

export type AcceptedMediaType = (typeof ACCEPTED_MEDIA_TYPES)[number];
export type AttachmentStatus = "queued" | "uploading" | "ready" | "error" | "cancelled";

export type AttachmentSource = "picker" | "paste" | "drop" | "history";

export interface UnavailableAttachment {
  name: string;
  media_type: string;
  byte_size: number;
  reason: "not_in_local_assets" | "invalid_native_media" | "too_large" | "limit";
}

export interface Attachment {
  asset_id: string;
  media_type: AcceptedMediaType;
  byte_size: number;
  width: number;
  height: number;
  name: string;
  modified_at: string | null;
  source: AttachmentSource;
}

export interface UploadedAttachment {
  asset_id: string;
  media_type: AcceptedMediaType;
  byte_size: number;
  width: number;
  height: number;
  name: string;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const encoder = new TextEncoder();

/** Validates the bounded draft wire without retaining any untrusted object references. */
export function parseAttachments(value: unknown): readonly Attachment[] | null {
  if (!Array.isArray(value) || value.length > MAX_ATTACHMENTS) return null;
  const ids = new Set<string>();
  let total = 0;
  const parsed: Attachment[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const row = item as Record<string, unknown>;
    const media = row.media_type;
    const source = row.source;
    const id = row.asset_id;
    const bytes = row.byte_size;
    const width = row.width;
    const height = row.height;
    const name = row.name;
    const modified = row.modified_at;
    if (
      typeof id !== "string" ||
      !UUID.test(id) ||
      ids.has(id) ||
      !ACCEPTED_MEDIA_TYPES.includes(media as AcceptedMediaType) ||
      typeof bytes !== "number" ||
      !Number.isSafeInteger(bytes) ||
      bytes < 1 ||
      bytes > MAX_FILE_BYTES ||
      typeof width !== "number" ||
      !Number.isSafeInteger(width) ||
      width < 1 ||
      typeof height !== "number" ||
      !Number.isSafeInteger(height) ||
      height < 1 ||
      width * height > 40_000_000 ||
      typeof name !== "string" ||
      name.length === 0 ||
      encoder.encode(name).byteLength > 512 ||
      /[\\/\0]/.test(name) ||
      (modified !== null && (typeof modified !== "string" || !ISO_DATE.test(modified))) ||
      !["picker", "paste", "drop", "history"].includes(source as string)
    )
      return null;
    ids.add(id);
    total += bytes;
    if (total > MAX_TURN_BYTES) return null;
    parsed.push({
      asset_id: id,
      media_type: media as AcceptedMediaType,
      byte_size: bytes,
      width,
      height,
      name,
      modified_at: modified as string | null,
      source: source as AttachmentSource,
    });
  }
  return parsed;
}

export interface TurnAttachment {
  localId: string;
  file: File;
  name: string;
  byteSize: number;
  mediaType: string;
  status: AttachmentStatus;
  progress: number | null;
  asset: UploadedAttachment | null;
  error: string | null;
  failure: { code: string; retryable: boolean } | null;
  source: AttachmentSource;
  modified_at: string | null;
}

export interface AttachmentLimits {
  maxAttachments?: number;
  maxFileBytes?: number;
  maxTurnBytes?: number;
}

export function effectiveLimits(limits: AttachmentLimits = {}) {
  return {
    maxAttachments: Math.min(MAX_ATTACHMENTS, limits.maxAttachments ?? MAX_ATTACHMENTS),
    maxFileBytes: Math.min(MAX_FILE_BYTES, limits.maxFileBytes ?? MAX_FILE_BYTES),
    maxTurnBytes: Math.min(MAX_TURN_BYTES, limits.maxTurnBytes ?? MAX_TURN_BYTES),
  };
}

export function validateAttachmentFiles(
  files: readonly File[],
  existingBytes = 0,
  existingCount = 0,
  limits: AttachmentLimits = {},
): string | null {
  const cap = effectiveLimits(limits);
  if (existingCount + files.length > cap.maxAttachments) return "attachment_count";
  let bytes = existingBytes;
  for (const file of files) {
    if (!ACCEPTED_MEDIA_TYPES.includes(file.type as AcceptedMediaType)) return "unsupported_type";
    if (file.size > cap.maxFileBytes) return "file_too_large";
    bytes += file.size;
  }
  if (bytes > cap.maxTurnBytes) return "turn_too_large";
  return null;
}

/** A `.txt`, `.md` or `.markdown` file at most this large can be offered as a text insert
 * instead of an attachment. */
export const MAX_TEXT_INSERT_BYTES = 100 * 1024;

const TEXT_INSERT_EXTENSIONS = [".txt", ".md", ".markdown"];

function isTextInsertName(name: string): boolean {
  const lower = name.toLowerCase();
  return TEXT_INSERT_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

export type FileRefusal =
  | "unsupported_type"
  | "file_too_large"
  | "attachment_count"
  | "turn_too_large";

/** Sorts a dropped, picked or pasted batch one file at a time, in order: an accepted image
 * type is checked against the per-file size, then the running count and turn bytes (each
 * accepted image adds to both); a text-insert-eligible file goes to `documents` whatever its
 * browser MIME type; everything else is `unsupported_type`. Never throws or aborts the batch. */
export function sortFiles(
  files: readonly File[],
  existingBytes = 0,
  existingCount = 0,
  limits: AttachmentLimits = {},
): { images: File[]; documents: File[]; refused: { name: string; reason: FileRefusal }[] } {
  const cap = effectiveLimits(limits);
  const images: File[] = [];
  const documents: File[] = [];
  const refused: { name: string; reason: FileRefusal }[] = [];
  let count = existingCount;
  let bytes = existingBytes;
  for (const file of files) {
    if (ACCEPTED_MEDIA_TYPES.includes(file.type as AcceptedMediaType)) {
      if (file.size > cap.maxFileBytes) {
        refused.push({ name: file.name, reason: "file_too_large" });
      } else if (count + 1 > cap.maxAttachments) {
        refused.push({ name: file.name, reason: "attachment_count" });
      } else if (bytes + file.size > cap.maxTurnBytes) {
        refused.push({ name: file.name, reason: "turn_too_large" });
      } else {
        images.push(file);
        count += 1;
        bytes += file.size;
      }
    } else if (isTextInsertName(file.name) && file.size <= MAX_TEXT_INSERT_BYTES) {
      documents.push(file);
    } else {
      refused.push({ name: file.name, reason: "unsupported_type" });
    }
  }
  return { images, documents, refused };
}
