import { boundedJson } from "../boundedResponse";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export class SessionWindowsError extends Error {
  constructor(readonly code: string) {
    super("The session window could not be opened.");
  }
}
export interface SessionWindowsApi {
  open(sessionId: string, signal?: AbortSignal): Promise<void>;
}

export function createSessionWindowsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): SessionWindowsApi {
  return {
    async open(sessionId, signal) {
      if (!UUID.test(sessionId)) throw new SessionWindowsError("invalid_input");
      if (signal?.aborted) throw new SessionWindowsError("cancelled");
      const controller = new AbortController();
      const abort = () => controller.abort();
      const timer = setTimeout(abort, 15_000);
      signal?.addEventListener("abort", abort, { once: true });
      try {
        const response = await fetchImpl("/api/v2/session-windows", {
          method: "POST",
          signal: controller.signal,
          headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        });
        const value: unknown = await boundedJson(response, 8192).catch(() => null);
        const row = value && typeof value === "object" ? (value as Record<string, unknown>) : null;
        if (response.ok && row?.opened === true && Object.keys(row).length === 1) return;
        const error =
          row?.error && typeof row.error === "object"
            ? (row.error as Record<string, unknown>)
            : null;
        throw new SessionWindowsError(typeof error?.code === "string" ? error.code : "unavailable");
      } catch (error) {
        if (controller.signal.aborted)
          throw new SessionWindowsError(signal?.aborted ? "cancelled" : "timeout");
        throw error;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  };
}
