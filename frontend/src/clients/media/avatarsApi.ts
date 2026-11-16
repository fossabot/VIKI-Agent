/** Authenticated avatar transform/read API. The token is only ever a header. */

import { boundedBlob, boundedJson } from "../boundedResponse";

export type AvatarSize = 24 | 32 | 64 | 128 | 256;

export interface AvatarFileView {
  size: number;
  byte_size: number;
  sha256: string;
}

export interface AvatarManifest {
  avatar_id: string;
  master: AvatarFileView;
  variants: AvatarFileView[];
  total_bytes: number;
  created_at: string;
}

export interface AvatarTransformRequest {
  avatar_id: string;
  source_asset_id: string;
  crop: { x: number; y: number; width: number; height: number };
  rotation: 0 | 90 | 180 | 270;
}

export class AvatarsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("The avatar could not be processed.");
    this.name = "AvatarsApiError";
  }
}

export interface AvatarsApi {
  transform(request: AvatarTransformRequest, signal?: AbortSignal): Promise<AvatarManifest>;
  variant(avatarId: string, size: AvatarSize, signal?: AbortSignal): Promise<Blob>;
}

const AVATAR_SIZES: readonly AvatarSize[] = [24, 32, 64, 128, 256];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
// Longer than the route's 30 s deadline, so a real overrun answers the route's typed 503
// `worker_timeout` instead of the client aborting first and reporting an uncertain outcome.
const TRANSFORM_TIMEOUT_MS = 35_000;
const VARIANT_TIMEOUT_MS = 30_000;

const TRANSFORM_CODES: Record<number, ReadonlySet<string>> = {
  400: new Set([
    "invalid_request",
    "invalid_avatar",
    "invalid_asset",
    "invalid_crop",
    "invalid_rotation",
    "disconnected",
  ]),
  401: new Set(["unauthorized"]),
  403: new Set(["forbidden_origin"]),
  404: new Set(["not_found"]),
  408: new Set(["intake_timeout"]),
  409: new Set(["command_conflict", "source_changed", "asset_changed"]),
  413: new Set(["too_large"]),
  415: new Set(["invalid_representation"]),
  422: new Set(["transform_failed"]),
  429: new Set(["capacity", "overloaded"]),
  503: new Set(["decoder_unavailable", "worker_timeout", "unavailable"]),
};

function isFileView(value: unknown): value is AvatarFileView {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    Object.keys(row).length === 3 &&
    typeof row.size === "number" &&
    typeof row.byte_size === "number" &&
    Number.isSafeInteger(row.byte_size) &&
    row.byte_size >= 0 &&
    typeof row.sha256 === "string" &&
    SHA256_RE.test(row.sha256)
  );
}

function isManifest(value: unknown): value is AvatarManifest {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  if (Object.keys(row).length !== 5) return false;
  if (typeof row.avatar_id !== "string" || !UUID_RE.test(row.avatar_id)) return false;
  if (!isFileView(row.master)) return false;
  if (
    typeof row.total_bytes !== "number" ||
    !Number.isSafeInteger(row.total_bytes) ||
    row.total_bytes < 0
  )
    return false;
  if (typeof row.created_at !== "string") return false;
  if (!Array.isArray(row.variants) || row.variants.length !== AVATAR_SIZES.length) return false;
  return row.variants.every(
    (variant, index) => isFileView(variant) && variant.size === AVATAR_SIZES[index],
  );
}

/** Server code is kept only when it is allowlisted for the response's own status. */
function mappedError(status: number, body: unknown): AvatarsApiError {
  const candidate =
    body && typeof body === "object"
      ? (body as { error?: { code?: unknown } }).error?.code
      : undefined;
  const allowed = typeof candidate === "string" && TRANSFORM_CODES[status]?.has(candidate);
  const code = allowed ? (candidate as string) : status >= 500 ? "unavailable" : "transform_failed";
  return new AvatarsApiError(status, code);
}

export function createAvatarsApi(token: string, fetchImpl: typeof fetch = fetch): AvatarsApi {
  async function transform(
    request: AvatarTransformRequest,
    signal?: AbortSignal,
  ): Promise<AvatarManifest> {
    if (signal?.aborted) throw new AvatarsApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TRANSFORM_TIMEOUT_MS);
    try {
      const response = await fetchImpl("/api/v2/avatars", {
        method: "POST",
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: JSON.stringify(request),
        signal: controller.signal,
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, 16_384);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw mappedError(response.status, null);
        throw new AvatarsApiError(response.status, "invalid_response");
      }
      if (!response.ok) throw mappedError(response.status, parsed);
      if (!isManifest(parsed)) throw new AvatarsApiError(response.status, "invalid_response");
      return parsed;
    } catch (error) {
      if (error instanceof AvatarsApiError) throw error;
      if (signal?.aborted) throw new AvatarsApiError(0, "cancelled");
      if (timedOut) throw new AvatarsApiError(0, "timeout");
      throw new AvatarsApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  async function variant(avatarId: string, size: AvatarSize, signal?: AbortSignal): Promise<Blob> {
    if (!UUID_RE.test(avatarId) || !AVATAR_SIZES.includes(size))
      throw new AvatarsApiError(400, "invalid_avatar");
    if (signal?.aborted) throw new AvatarsApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, VARIANT_TIMEOUT_MS);
    try {
      const response = await fetchImpl(`/api/v2/avatars/${avatarId}/${size}`, {
        headers: { authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new AvatarsApiError(response.status, "read_failed");
      return await boundedBlob(response, 1 << 20);
    } catch (error) {
      if (error instanceof AvatarsApiError) throw error;
      if (signal?.aborted) throw new AvatarsApiError(0, "cancelled");
      if (timedOut) throw new AvatarsApiError(0, "timeout");
      throw new AvatarsApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  return { transform, variant };
}
