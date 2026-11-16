/** Authenticated binary asset upload/read API. Tokens are headers only, never URL parameters. */

import {
  ACCEPTED_MEDIA_TYPES,
  type AttachmentLimits,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TURN_BYTES,
  parseAttachments,
  type UploadedAttachment,
} from "../../turnInput";
import { boundedBlob, boundedJson } from "../boundedResponse";

export interface UploadOptions {
  signal?: AbortSignal;
  onProgress?: (percent: number) => void;
  purpose?: "avatar";
}

export class AssetsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("The attachment could not be uploaded.");
    this.name = "AssetsApiError";
  }
}

function encodedName(name: string): string {
  return encodeURIComponent(name).replace(/'/g, "%27");
}

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

/** Trims a display name to 512 UTF-8 bytes, keeping a short extension and never splitting a
 * surrogate pair. */
export function uploadName(name: string): string {
  let value = name.toWellFormed();
  while (utf8Length(value) > 512) {
    const dot = value.lastIndexOf(".");
    const extension = dot > 0 ? value.slice(dot) : "";
    const keepExtension = extension !== "" && utf8Length(extension) <= 16;
    const stem = keepExtension ? value.slice(0, dot) : value;
    const codePoints = Array.from(stem);
    codePoints.pop();
    value = keepExtension ? codePoints.join("") + extension : codePoints.join("");
  }
  return value;
}

const UPLOAD_CODES: Record<number, ReadonlySet<string>> = {
  400: new Set([
    "invalid_name",
    "invalid_body",
    "empty",
    "incomplete",
    "disconnected",
    "invalid_request",
    "invalid_purpose",
  ]),
  401: new Set(["unauthorized"]),
  403: new Set(["forbidden_origin"]),
  408: new Set(["intake_timeout"]),
  413: new Set(["too_large"]),
  415: new Set(["decode_failed", "invalid_representation"]),
  429: new Set(["capacity", "overloaded"]),
  503: new Set([
    "decoder_unavailable",
    "unavailable",
    "worker_timeout",
    "worker_unavailable",
    "corrupt",
    "protection",
    "busy",
  ]),
};

const RELINK_CODES: Record<number, ReadonlySet<string>> = {
  ...UPLOAD_CODES,
  404: new Set(["not_found"]),
  409: new Set(["asset_mismatch", "asset_changed"]),
};

/** Server message text is never kept, only a code allowlisted for the response's own status. */
function uploadError(
  request: XMLHttpRequest,
  codes: Record<number, ReadonlySet<string>> = UPLOAD_CODES,
): AssetsApiError {
  let code = "upload_failed";
  if (request.responseText.length <= 4096) {
    try {
      const value = JSON.parse(request.responseText) as { error?: { code?: unknown } };
      const candidate = value.error?.code;
      if (typeof candidate === "string" && codes[request.status]?.has(candidate)) code = candidate;
    } catch {
      // not JSON: keep upload_failed
    }
  }
  return new AssetsApiError(request.status, code);
}

export interface AssetsApi {
  upload(file: File, options?: UploadOptions): Promise<UploadedAttachment>;
  read(assetId: string, signal?: AbortSignal): Promise<Blob>;
  capabilities?(signal?: AbortSignal): Promise<AttachmentLimits & { maxDimension: number }>;
  relink?(assetId: string, file: File, signal?: AbortSignal): Promise<void>;
}

const ASSET_TIMEOUT_MS = 30_000;

export function createAssetsApi(token: string, fetchImpl: typeof fetch = fetch): AssetsApi {
  async function capabilities(
    signal?: AbortSignal,
  ): Promise<AttachmentLimits & { maxDimension: number }> {
    if (signal?.aborted) throw new AssetsApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = () => controller.abort();
    const timer = setTimeout(relay, ASSET_TIMEOUT_MS);
    signal?.addEventListener("abort", relay, { once: true });
    try {
      const response = await fetchImpl("/api/v2/attachment-capabilities", {
        headers: { authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      const value = await boundedJson(response, 16_384);
      if (!response.ok || !value || typeof value !== "object")
        throw new AssetsApiError(response.status, "capabilities_unavailable");
      const row = value as Record<string, unknown>;
      const within = (item: unknown, maximum: number): item is number =>
        typeof item === "number" && Number.isSafeInteger(item) && item >= 1 && item <= maximum;
      if (
        row.provider !== "claude" ||
        !Array.isArray(row.media_types) ||
        row.media_types.length !== ACCEPTED_MEDIA_TYPES.length ||
        !ACCEPTED_MEDIA_TYPES.every((type) => (row.media_types as unknown[]).includes(type)) ||
        !within(row.max_files, MAX_ATTACHMENTS) ||
        !within(row.max_file_bytes, MAX_FILE_BYTES) ||
        !within(row.max_turn_bytes, MAX_TURN_BYTES) ||
        !within(row.max_dimension, 40_000_000)
      )
        throw new AssetsApiError(0, "invalid_response");
      return {
        maxAttachments: row.max_files,
        maxFileBytes: row.max_file_bytes,
        maxTurnBytes: row.max_turn_bytes,
        maxDimension: row.max_dimension,
      };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  async function read(assetId: string, signal?: AbortSignal): Promise<Blob> {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(assetId))
      throw new AssetsApiError(400, "invalid_asset");
    if (signal?.aborted) throw new AssetsApiError(0, "cancelled");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ASSET_TIMEOUT_MS);
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    const init: RequestInit = {
      method: "GET",
      headers: { authorization: `Bearer ${token}` },
      signal: controller.signal,
    };
    try {
      const response = await fetchImpl(`/api/v2/assets/${encodeURIComponent(assetId)}`, init);
      if (!response.ok)
        throw new AssetsApiError(
          response.status,
          response.status === 410 ? "expired" : "read_failed",
        );
      const blob = await boundedBlob(response, MAX_FILE_BYTES);
      return blob;
    } catch (error) {
      if (controller.signal.aborted && !signal?.aborted) throw new AssetsApiError(0, "timeout");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  function upload(file: File, options: UploadOptions = {}): Promise<UploadedAttachment> {
    return new Promise((resolve, reject) => {
      if (options.signal?.aborted) {
        reject(new AssetsApiError(0, "cancelled"));
        return;
      }
      const request = new XMLHttpRequest();
      request.timeout = ASSET_TIMEOUT_MS;
      let settled = false;
      const cleanup = (): void => options.signal?.removeEventListener("abort", abort);
      const finishError = (error: Error): void => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const abort = (): void => {
        request.abort();
        finishError(new AssetsApiError(0, "cancelled"));
      };
      options.signal?.addEventListener("abort", abort, { once: true });
      request.upload.onprogress = (event) => {
        if (event.lengthComputable)
          options.onProgress?.(Math.round((event.loaded / event.total) * 100));
      };
      request.onerror = () => finishError(new AssetsApiError(0, "network_error"));
      request.ontimeout = () => finishError(new AssetsApiError(0, "timeout"));
      request.onabort = () => finishError(new AssetsApiError(0, "cancelled"));
      request.onload = () => {
        if (settled) return;
        if (request.status < 200 || request.status >= 300) {
          finishError(uploadError(request));
          return;
        }
        try {
          const value = JSON.parse(request.responseText) as Record<string, unknown>;
          const parsed = parseAttachments([{ ...value, modified_at: null, source: "picker" }]);
          if (parsed?.length !== 1) throw new Error("invalid response");
          settled = true;
          cleanup();
          const row = parsed[0];
          if (!row) throw new Error("invalid response");
          const { asset_id, media_type, byte_size, width, height, name } = row;
          resolve({ asset_id, media_type, byte_size, width, height, name });
        } catch {
          finishError(new AssetsApiError(request.status, "invalid_response"));
        }
      };
      request.open("POST", "/api/v2/assets");
      request.setRequestHeader("Authorization", `Bearer ${token}`);
      request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      request.setRequestHeader("X-File-Name", encodedName(uploadName(file.name)));
      if (options.purpose === "avatar") request.setRequestHeader("X-Viki-Asset-Purpose", "avatar");
      request.send(file);
    });
  }

  function relink(assetId: string, file: File, signal?: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new AssetsApiError(0, "cancelled"));
        return;
      }
      const request = new XMLHttpRequest();
      request.timeout = ASSET_TIMEOUT_MS;
      let settled = false;
      const cleanup = (): void => signal?.removeEventListener("abort", abort);
      const finishError = (error: Error): void => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const abort = (): void => {
        request.abort();
        finishError(new AssetsApiError(0, "cancelled"));
      };
      signal?.addEventListener("abort", abort, { once: true });
      request.onerror = () => finishError(new AssetsApiError(0, "network_error"));
      request.ontimeout = () => finishError(new AssetsApiError(0, "timeout"));
      request.onabort = () => finishError(new AssetsApiError(0, "cancelled"));
      request.onload = () => {
        if (settled) return;
        settled = true;
        cleanup();
        if (request.status === 204) {
          resolve();
          return;
        }
        reject(uploadError(request, RELINK_CODES));
      };
      request.open("POST", `/api/v2/assets/${encodeURIComponent(assetId)}/relink`);
      request.setRequestHeader("Authorization", `Bearer ${token}`);
      request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      request.send(file);
    });
  }

  return { upload, read, capabilities, relink };
}
