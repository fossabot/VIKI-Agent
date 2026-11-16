import { boundedJson } from "../boundedResponse";

// The wire format is the server's `POST /api/v2/code/{workspace_id}/terminal`.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX = 64 * 1024;
const TIMEOUT = 15_000;

/** The server's typed code, or "unavailable" when the answer carries none. */
export class TerminalApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface TerminalApi {
  open(workspaceId: string, worktreeId: string | null, signal?: AbortSignal): Promise<void>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const uuid = (value: unknown) => typeof value === "string" && UUID.test(value);

export function createTerminalApi(token: string, fetchImpl: typeof fetch = fetch): TerminalApi {
  return {
    async open(workspaceId, worktreeId, signal) {
      if (!uuid(workspaceId)) throw new TerminalApiError("invalid_workspace");
      if (worktreeId !== null && !uuid(worktreeId)) throw new TerminalApiError("invalid_workspace");
      if (signal?.aborted) throw new Error("cancelled");
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, TIMEOUT);
      try {
        const response = await fetchImpl(`/api/v2/code/${workspaceId}/terminal`, {
          method: "POST",
          signal: controller.signal,
          headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
          body: JSON.stringify({ worktree_id: worktreeId }),
        });
        const answer = record(await boundedJson(response, MAX).catch(() => null));
        if (response.ok && answer && answer.opened === true && Object.keys(answer).length === 1) {
          return;
        }
        // A refusal carries {error: {code, message}}; the code alone is acted on.
        const error = record(answer?.error);
        throw typeof error?.code === "string" && error.code
          ? new TerminalApiError(error.code)
          : new TerminalApiError("unavailable");
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  };
}
