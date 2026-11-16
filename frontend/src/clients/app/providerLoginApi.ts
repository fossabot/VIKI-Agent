import { boundedJson } from "../boundedResponse";

// The wire format is the server's `POST /api/v2/provider/login`.
const MAX = 64 * 1024;
const TIMEOUT = 15_000;

/** The server's typed code, or "unavailable" when the answer carries none. */
export class ProviderLoginApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface ProviderLoginApi {
  open(signal?: AbortSignal): Promise<void>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

export function createProviderLoginApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): ProviderLoginApi {
  return {
    async open(signal) {
      if (signal?.aborted) throw new Error("cancelled");
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, TIMEOUT);
      try {
        const response = await fetchImpl("/api/v2/provider/login", {
          method: "POST",
          signal: controller.signal,
          headers: { authorization: `Bearer ${token}` },
        });
        const answer = record(await boundedJson(response, MAX).catch(() => null));
        if (response.ok && answer && answer.opened === true && Object.keys(answer).length === 1) {
          return;
        }
        // A refusal carries {error: {code, message}}; the code alone is acted on.
        const error = record(answer?.error);
        throw typeof error?.code === "string" && error.code
          ? new ProviderLoginApiError(error.code)
          : new ProviderLoginApiError("unavailable");
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  };
}
