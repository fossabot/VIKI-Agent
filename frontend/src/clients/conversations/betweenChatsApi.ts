/** Bounded, read-only client for the between-chats mailbox (server: src/viki_agent/conversations/between_chats.py). */

import { boundedJson } from "../boundedResponse";

export interface ChatMessage {
  at: string;
  from_name: string;
  from_session_id: string | null;
  to_session_id: string;
  text: string;
}

export interface BetweenChatsResult {
  messages: readonly ChatMessage[];
  partial: boolean;
}

export class BetweenChatsApiError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "BetweenChatsApiError";
  }
}

export interface BetweenChatsApi {
  list(signal?: AbortSignal): Promise<BetweenChatsResult>;
}

const MAX_RESPONSE = 64 << 10;
const TIMEOUT = 15_000;
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/;
const MAX_MESSAGES = 20;
const MAX_AT_CHARS = 40;
const MAX_NAME_CHARS = 64;
const MAX_TEXT_CHARS = 200;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function at(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_AT_CHARS && AT.test(value);
}
function name(value: unknown): value is string {
  return typeof value === "string" && value.length >= 1 && value.length <= MAX_NAME_CHARS;
}
function id(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function nullableId(value: unknown): value is string | null {
  return value === null || id(value);
}
function text(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_TEXT_CHARS;
}

function parseMessage(value: unknown): ChatMessage | null {
  if (!exact(value, ["at", "from_name", "from_session_id", "to_session_id", "text"])) return null;
  if (!at(value.at)) return null;
  if (!name(value.from_name)) return null;
  if (!nullableId(value.from_session_id)) return null;
  if (!id(value.to_session_id)) return null;
  if (!text(value.text)) return null;
  return {
    at: value.at,
    from_name: value.from_name,
    from_session_id: value.from_session_id,
    to_session_id: value.to_session_id,
    text: value.text,
  };
}

function parseResult(value: unknown): BetweenChatsResult | null {
  if (!exact(value, ["messages", "partial"])) return null;
  if (!Array.isArray(value.messages) || value.messages.length > MAX_MESSAGES) return null;
  if (typeof value.partial !== "boolean") return null;
  const messages: ChatMessage[] = [];
  for (const raw of value.messages) {
    const parsed = parseMessage(raw);
    if (parsed === null) return null;
    messages.push(parsed);
  }
  return { messages, partial: value.partial };
}

function serverError(body: unknown): BetweenChatsApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return new BetweenChatsApiError(
    typeof candidate === "string" && CODE.test(candidate) ? candidate : "unavailable",
  );
}

export function createBetweenChatsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): BetweenChatsApi {
  async function request(signal?: AbortSignal): Promise<unknown> {
    if (signal?.aborted) throw new BetweenChatsApiError("cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT);
    try {
      const response = await fetchImpl("/api/v2/between-chats", {
        method: "GET",
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}` },
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, MAX_RESPONSE);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw serverError(null);
        throw new BetweenChatsApiError("invalid_response");
      }
      if (!response.ok) throw serverError(parsed);
      return parsed;
    } catch (error) {
      if (error instanceof BetweenChatsApiError) throw error;
      if (signal?.aborted) throw new BetweenChatsApiError("cancelled");
      if (timedOut) throw new BetweenChatsApiError("timeout");
      throw new BetweenChatsApiError("unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async list(signal) {
      const raw = await request(signal);
      const parsed = parseResult(raw);
      if (parsed === null) throw new BetweenChatsApiError("invalid_response");
      return parsed;
    },
  };
}
