import { boundedJson } from "../clients/boundedResponse";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
// A part's text is at most 32 KiB and the block repeats it; JSON escapes can widen both.
const MAX = 512 * 1024;
const TIMEOUT = 15_000;
// The server reads a line number as 1 to 7 decimal digits.
const MAX_LINE = 9_999_999;

export interface VaultExcerptPart {
  kind: "vault_excerpt";
  vault_id: string;
  ref: string;
  start_line: number;
  end_line: number;
  sha256: string;
}
export interface VaultExcerpt {
  part: VaultExcerptPart;
  text: string;
  block: string;
  block_bytes: number;
  revision: string;
  modified_at: string;
}
export interface ExcerptApi {
  excerpt(
    id: string,
    ref: string,
    start: number,
    end: number,
    signal?: AbortSignal,
  ): Promise<VaultExcerpt>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const lineNumber = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= MAX_LINE;

function validExcerpt(
  body: Record<string, unknown>,
): body is Record<string, unknown> & VaultExcerpt {
  const part = record(body.part);
  return (
    part !== null &&
    part.kind === "vault_excerpt" &&
    typeof part.vault_id === "string" &&
    typeof part.ref === "string" &&
    lineNumber(part.start_line) &&
    lineNumber(part.end_line) &&
    typeof part.sha256 === "string" &&
    SHA256.test(part.sha256) &&
    typeof body.text === "string" &&
    typeof body.block === "string" &&
    Number.isSafeInteger(body.block_bytes) &&
    (body.block_bytes as number) >= 0 &&
    typeof body.revision === "string" &&
    typeof body.modified_at === "string"
  );
}

export function createExcerptApi(token: string, fetchImpl: typeof fetch = fetch): ExcerptApi {
  return {
    async excerpt(id, ref, start, end, signal) {
      if (!UUID.test(id)) throw new Error("invalid_workspace");
      if (typeof ref !== "string" || ref === "") throw new Error("invalid_ref");
      if (!lineNumber(start) || !lineNumber(end) || start > end) throw new Error("invalid_range");
      if (signal?.aborted) throw new Error("cancelled");
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, TIMEOUT);
      // Plain digits: the server never percent-decodes a line number.
      const query = `ref=${encodeURIComponent(ref).replace(/%2F/g, "/")}&start=${start}&end=${end}`;
      try {
        const response = await fetchImpl(`/api/v2/vaults/${id}/excerpt?${query}`, {
          signal: controller.signal,
          headers: { authorization: `Bearer ${token}` },
        });
        if (!response.ok) {
          if (response.status === 403) throw new Error("forbidden");
          if (response.status === 404) throw new Error("not_found");
          // The server's error.code (invalid_range, excerpt_too_large, not_active_vault, ...).
          let code = "unavailable";
          try {
            const error = record(record(await boundedJson(response, MAX))?.error);
            if (error && typeof error.code === "string") code = error.code;
          } catch {
            // The body was not a readable JSON error; keep the generic code.
          }
          throw new Error(code);
        }
        const body = record(await boundedJson(response, MAX));
        if (!body || !validExcerpt(body)) throw new Error("invalid_response");
        return body;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  };
}
