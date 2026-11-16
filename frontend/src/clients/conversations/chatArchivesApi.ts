import {
  ARCHIVE_MAX_BYTES,
  exportConversationArchive,
  parseReadOnlyArchive,
  type ReadOnlyConversationArchive,
} from "../../context/conversationTools";
import { boundedJson } from "../boundedResponse";

export interface ArchiveSummary {
  archive_id: string;
  created_at: string;
  byte_size: number;
}
export interface ChatArchivesApi {
  list(signal?: AbortSignal): Promise<readonly ArchiveSummary[]>;
  get(id: string, signal?: AbortSignal): Promise<ReadOnlyConversationArchive>;
  save(id: string, archive: ReadOnlyConversationArchive, signal?: AbortSignal): Promise<void>;
  remove(id: string): Promise<void>;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function createChatArchivesApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): ChatArchivesApi {
  async function request(
    method: string,
    id?: string,
    archive?: ReadOnlyConversationArchive,
    signal?: AbortSignal,
  ): Promise<unknown> {
    if (id !== undefined && !UUID.test(id)) throw new Error("invalid_archive");
    const controller = new AbortController();
    if (signal?.aborted) throw new Error("cancelled");
    const relay = () => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    const timer = setTimeout(relay, 60_000);
    try {
      const response = await fetchImpl(
        `/api/v2/conversation-archives${id && method !== "POST" ? `/${id}` : ""}`,
        {
          method,
          signal: controller.signal,
          headers: {
            authorization: `Bearer ${token}`,
            ...(archive ? { "content-type": "application/json" } : {}),
          },
          ...(archive
            ? {
                body: `{"archive_id":${JSON.stringify(id)},"archive":${exportConversationArchive(archive)}}`,
              }
            : {}),
        },
      );
      if (!response.ok)
        throw new Error(
          response.status === 409
            ? "conflict"
            : response.status === 413
              ? "too_large"
              : "archive_unavailable",
        );
      if (response.status === 204) return null;
      return await boundedJson(response, ARCHIVE_MAX_BYTES * 3 + 65_536);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async list(signal) {
      const raw = await request("GET", undefined, undefined, signal);
      const rows = raw && typeof raw === "object" ? (raw as { archives?: unknown }).archives : null;
      if (
        !Array.isArray(rows) ||
        rows.length > 64 ||
        rows.some(
          (row) =>
            !row ||
            typeof row !== "object" ||
            !UUID.test(row.archive_id) ||
            typeof row.created_at !== "string" ||
            !Number.isSafeInteger(row.byte_size) ||
            row.byte_size < 0 ||
            row.byte_size > ARCHIVE_MAX_BYTES,
        )
      )
        throw new Error("invalid_response");
      return rows as ArchiveSummary[];
    },
    async get(id, signal) {
      const response = await request("GET", id, undefined, signal);
      if (!response || typeof response !== "object") throw new Error("invalid_response");
      return parseReadOnlyArchive((response as { archive?: unknown }).archive);
    },
    async save(id, archive, signal) {
      await request("POST", id, archive, signal);
    },
    async remove(id) {
      await request("DELETE", id);
    },
  };
}
