import { PROTOCOL_VERSION } from "../protocol";
import type {
  VoiceFallback,
  VoiceServerMessage,
  VoiceSocket,
  VoiceSocketHandlers,
} from "./dictationTypes";

/** The wire format is the server's `/ws/voice` (one socket per capture). */
export interface DictationWebSocket {
  binaryType: string;
  send(data: string | ArrayBuffer): void;
  close(code?: number): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

const BAD_MESSAGE = 4400;
const NEVER_OPEN = 1006;
const STOP_REASONS = ["capture_cap", "overloaded"];

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const exactKeys = (row: Record<string, unknown>, keys: string[]) => {
  const have = Object.keys(row);
  return have.length === keys.length && keys.every((key) => key in row);
};

function parseFallback(value: unknown): VoiceFallback | null | undefined {
  if (value === null) return null;
  const row = record(value);
  if (!row || !exactKeys(row, ["backend", "code", "detail"])) return undefined;
  if (!str(row.backend) || !str(row.code) || !(row.detail === null || str(row.detail)))
    return undefined;
  return { backend: row.backend, code: row.code, detail: row.detail };
}

/** Parse one server frame; null when it is not exactly one of the contract's messages. */
export function parseServerMessage(raw: unknown): VoiceServerMessage | null {
  if (!str(raw)) return null;
  let row: Record<string, unknown> | null;
  try {
    row = record(JSON.parse(raw));
  } catch {
    return null;
  }
  if (!row) return null;
  const id = row.capture_id;
  switch (row.type) {
    case "starting":
    case "done":
    case "cancelled":
      return exactKeys(row, ["type", "capture_id"]) && str(id)
        ? ({ type: row.type, capture_id: id } as VoiceServerMessage)
        : null;
    case "capturing": {
      const fallback = parseFallback(row.fallback);
      return exactKeys(row, [
        "type",
        "capture_id",
        "model",
        "backend",
        "device",
        "language",
        "language_unavailable",
        "fallback",
      ]) &&
        str(id) &&
        str(row.model) &&
        str(row.backend) &&
        (row.device === null || str(row.device)) &&
        str(row.language) &&
        typeof row.language_unavailable === "boolean" &&
        fallback !== undefined
        ? {
            type: "capturing",
            capture_id: id,
            model: row.model,
            backend: row.backend,
            device: row.device,
            language: row.language,
            language_unavailable: row.language_unavailable,
            fallback,
          }
        : null;
    }
    case "partial":
      return exactKeys(row, ["type", "capture_id", "segment", "revision", "text"]) &&
        str(id) &&
        count(row.segment) &&
        count(row.revision) &&
        str(row.text)
        ? {
            type: "partial",
            capture_id: id,
            segment: row.segment,
            revision: row.revision,
            text: row.text,
          }
        : null;
    case "final":
      return exactKeys(row, ["type", "capture_id", "segment", "text"]) &&
        str(id) &&
        count(row.segment) &&
        str(row.text)
        ? { type: "final", capture_id: id, segment: row.segment, text: row.text }
        : null;
    case "stopping":
      return exactKeys(row, ["type", "capture_id", "reason"]) &&
        str(id) &&
        str(row.reason) &&
        STOP_REASONS.includes(row.reason)
        ? {
            type: "stopping",
            capture_id: id,
            reason: row.reason as "capture_cap" | "overloaded",
          }
        : null;
    case "error":
      return exactKeys(row, ["type", "capture_id", "code"]) &&
        (id === null || str(id)) &&
        str(row.code)
        ? { type: "error", capture_id: id, code: row.code }
        : null;
    default:
      return null;
  }
}

/** With `after`, the socket is created only once `after` settles; sends wait in the queue
 * meanwhile, and a close before it is created ends it at once with 1000, unconnected. */
export function openDictationSocket(
  url: string,
  token: string,
  handlers: VoiceSocketHandlers,
  createSocket: (url: string) => DictationWebSocket = (target) =>
    new WebSocket(target) as unknown as DictationWebSocket,
  after?: Promise<void>,
): VoiceSocket {
  let created: DictationWebSocket | null = null;
  let open = false;
  let finished = false;
  let closing = false;
  // Sends made before the socket opens wait here; the auth message always goes first.
  const queue: (string | ArrayBuffer)[] = [];
  const finish = (code: number) => {
    if (finished) return;
    finished = true;
    handlers.closed(code);
  };
  const send = (data: string | ArrayBuffer) => {
    if (finished) return;
    if (open) created?.send(data);
    else queue.push(data);
  };
  const command = (type: string, captureId: string) =>
    send(JSON.stringify({ type, capture_id: captureId }));
  const connect = () => {
    if (finished) return; // closed while it waited
    const socket = createSocket(url);
    created = socket;
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      open = true;
      socket.send(JSON.stringify({ type: "auth", token, v: PROTOCOL_VERSION }));
      for (const data of queue.splice(0)) socket.send(data);
    };
    socket.onmessage = (event) => {
      if (finished) return;
      const message = parseServerMessage(event.data);
      if (!message) {
        socket.close(BAD_MESSAGE);
        finish(BAD_MESSAGE);
        return;
      }
      handlers.message(message);
    };
    socket.onclose = (event) => finish(open ? event.code : NEVER_OPEN);
    socket.onerror = null;
  };
  if (after) void after.then(connect, connect);
  else connect();
  return {
    begin: (captureId) => command("begin", captureId),
    audio: (chunk) => send(chunk),
    end: (captureId) => command("end", captureId),
    cancel: (captureId) => command("cancel", captureId),
    close() {
      if (finished || closing) return;
      closing = true;
      if (created) created.close(1000);
      else finish(1000);
    },
  };
}
