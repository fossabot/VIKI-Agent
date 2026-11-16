/** Authenticated CAS metadata for the session organization VIKI Agent keeps. */
import { boundedJson } from "../boundedResponse";

export const SESSION_COLORS = ["blue", "green", "orange", "purple", "red", "slate"] as const;
export type SessionColor = (typeof SESSION_COLORS)[number];

export interface SessionMetadata {
  session_id: string;
  revision: number;
  title: string;
  pinned: boolean;
  archived: boolean;
  unread?: boolean;
  color?: SessionColor | null;
}

export type SessionMetadataPatch = Partial<
  Pick<SessionMetadata, "title" | "pinned" | "archived" | "unread" | "color">
>;

export class SessionMetadataError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly currentRevision: number | null = null,
  ) {
    super("Session metadata could not be updated.");
    this.name = "SessionMetadataError";
  }
}

interface MetadataApiOptions {
  fetch?: typeof fetch;
  timeoutMs?: number;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const validRow = (value: unknown): value is SessionMetadata => {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.session_id === "string" &&
    UUID.test(row.session_id) &&
    typeof row.revision === "number" &&
    Number.isSafeInteger(row.revision) &&
    row.revision >= 1 &&
    typeof row.title === "string" &&
    [...row.title].length <= 256 &&
    typeof row.pinned === "boolean" &&
    typeof row.archived === "boolean" &&
    (!("unread" in row) || typeof row.unread === "boolean") &&
    (!("color" in row) || row.color === null || SESSION_COLORS.includes(row.color as SessionColor))
  );
};

export interface SessionMetadataApi {
  list(signal?: AbortSignal): Promise<readonly SessionMetadata[]>;
  patch(
    sessionId: string,
    expectedRevision: number,
    patch: SessionMetadataPatch,
    signal?: AbortSignal,
  ): Promise<SessionMetadata>;
  /** Delete a row by its expected revision. 204 -> true, 404 -> false, 409 throws "conflict". */
  remove(sessionId: string, expectedRevision: number, signal?: AbortSignal): Promise<boolean>;
}

export function createSessionMetadataApi(
  token: string,
  options: MetadataApiOptions = {},
): SessionMetadataApi {
  const fetchImpl = options.fetch ?? fetch;
  const timeoutMs = options.timeoutMs ?? 15_000;
  async function request<T>(path: string, init: RequestInit, signal?: AbortSignal): Promise<T> {
    if (!UUID.test(path.split("/").pop() ?? "") && path !== "/api/v2/session-metadata")
      throw new SessionMetadataError(400, "invalid_session_id");
    const controller = new AbortController();
    if (signal?.aborted) throw new SessionMetadataError(0, "cancelled");
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    try {
      const response = await fetchImpl(path, {
        ...init,
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
      });
      let body: unknown = null;
      try {
        body = await boundedJson(response, 8 << 20);
      } catch {
        /* error body may be empty */
      }
      if (!response.ok) {
        const envelope = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
        const error =
          envelope.error && typeof envelope.error === "object"
            ? (envelope.error as Record<string, unknown>)
            : {};
        const current = typeof error.current_revision === "number" ? error.current_revision : null;
        throw new SessionMetadataError(
          response.status,
          typeof error.code === "string" ? error.code : "http_error",
          current,
        );
      }
      return body as T;
    } catch (error) {
      if (controller.signal.aborted && !signal?.aborted)
        throw new SessionMetadataError(0, "timeout");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async list(signal) {
      const body = await request<unknown>("/api/v2/session-metadata", { method: "GET" }, signal);
      if (
        !body ||
        typeof body !== "object" ||
        !Array.isArray((body as Record<string, unknown>).sessions) ||
        (body as { sessions: unknown[] }).sessions.length > 4096 ||
        !(body as { sessions: unknown[] }).sessions.every(validRow)
      )
        throw new SessionMetadataError(0, "invalid_response");
      const rows = (body as { sessions: SessionMetadata[] }).sessions;
      if (new Set(rows.map((row) => row.session_id)).size !== rows.length)
        throw new SessionMetadataError(0, "invalid_response");
      return rows;
    },
    async patch(sessionId, expectedRevision, patch, signal) {
      if (!UUID.test(sessionId) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0)
        throw new SessionMetadataError(400, "invalid_input");
      const body = await request<unknown>(
        `/api/v2/session-metadata/${encodeURIComponent(sessionId)}`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ expected_revision: expectedRevision, patch }),
        },
        signal,
      );
      if (
        !validRow(body) ||
        body.session_id !== sessionId ||
        body.revision < expectedRevision ||
        body.revision > expectedRevision + 1 ||
        Object.entries(patch).some(([key, value]) => body[key as keyof SessionMetadata] !== value)
      )
        throw new SessionMetadataError(0, "invalid_response");
      return body;
    },
    async remove(sessionId, expectedRevision, signal) {
      if (!UUID.test(sessionId) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0)
        throw new SessionMetadataError(400, "invalid_input");
      try {
        await request<unknown>(
          `/api/v2/session-metadata/${encodeURIComponent(sessionId)}`,
          {
            method: "DELETE",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ expected_revision: expectedRevision }),
          },
          signal,
        );
        return true;
      } catch (error) {
        if (error instanceof SessionMetadataError && error.status === 404) return false;
        throw error;
      }
    },
  };
}
