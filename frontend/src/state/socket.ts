/** One authenticated v2 connection. Mutations are never queued or replayed. */
import { type ClientMessage, PROTOCOL_VERSION, type ServerMessage } from "../protocol";

export type SocketStatus =
  | "connecting"
  | "open"
  | "reconnecting"
  | "unauthorized"
  | "incompatible"
  | "closed";

export interface WebSocketLike {
  send(data: string): void;
  close(code?: number): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}
export interface SocketClientOptions {
  url: string;
  token: string;
  createSocket?: (url: string) => WebSocketLike;
  retryDelaysMs?: readonly number[];
}
export interface SessionCursor {
  generation: string | null;
  seq: number;
}
interface Subscription {
  cursor: () => SessionCursor;
  requestId: string | null;
  existingOnly: boolean;
  expectedModel?: string | null;
}

const AUTH_REFUSED = 4401;
const INCOMPATIBLE = 4406;
const DEFAULT_RETRY_MS = [500, 1000, 2000, 5000];
/** Mirrors the server's incoming frame limit, including JSON escaping. */
export const MAX_FRAME_BYTES = 1 << 20;
const SAMPLE_UUID = "00000000-0000-0000-0000-000000000000";

export class SocketClient {
  private readonly url: string;
  private readonly token: string;
  private readonly createSocket: (url: string) => WebSocketLike;
  private readonly retryDelaysMs: readonly number[];
  private socket: WebSocketLike | null = null;
  private open = false;
  private closed = false;
  private attempt = 0;
  private epoch = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly tracked = new Map<string, Subscription>();
  private readonly messageHandlers = new Set<(message: ServerMessage) => void>();
  private readonly statusHandlers = new Set<(status: SocketStatus) => void>();

  constructor(options: SocketClientOptions) {
    this.url = options.url;
    this.token = options.token;
    this.createSocket = options.createSocket ?? browserSocket;
    this.retryDelaysMs = options.retryDelaysMs?.length ? options.retryDelaysMs : DEFAULT_RETRY_MS;
  }
  /** An await may continue only while this exact connection remains open. */
  get connectionId(): number {
    return this.epoch;
  }
  isCurrentConnection(id: number): boolean {
    return this.open && !this.closed && this.epoch === id;
  }
  subscriptionId(sessionId: string): string | null {
    return this.tracked.get(sessionId)?.requestId ?? null;
  }
  hasModelExpectation(sessionId: string): boolean {
    return this.tracked.get(sessionId)?.expectedModel !== undefined;
  }
  /** A confirmed barrier ends the first-open comparison; later reconnects use saved prefs. */
  confirmSubscription(sessionId: string, requestId: string): void {
    const subscription = this.tracked.get(sessionId);
    if (subscription?.requestId === requestId) delete subscription.expectedModel;
  }
  connect(): void {
    if (this.closed || this.socket || this.timer !== null) return;
    this.setStatus("connecting");
    this.dial();
  }
  close(): void {
    if (!this.closed) this.stop("closed", 1000);
  }
  /** True proves browser socket handoff only, never server or native acceptance. */
  send(message: ClientMessage): boolean {
    if (message.type === "auth" || !this.open || !this.socket || this.closed) return false;
    if (frameByteLength(message) > MAX_FRAME_BYTES) return false;
    try {
      this.socket.send(JSON.stringify(message));
      return true;
    } catch {
      const socket = this.socket;
      this.disconnected(socket, 1006);
      socket.close(1011);
      return false;
    }
  }
  track(
    sessionId: string,
    cursor: () => SessionCursor,
    existingOnly = false,
    expectedModel?: string | null,
  ): void {
    const previous = this.tracked.get(sessionId);
    if (previous) {
      const changedModel = expectedModel !== undefined && expectedModel !== previous.expectedModel;
      if (changedModel) previous.expectedModel = expectedModel;
      if (existingOnly && !previous.existingOnly) {
        previous.existingOnly = true;
        if (this.open) this.subscribe(sessionId, previous);
      } else if (changedModel && this.open) {
        this.subscribe(sessionId, previous);
      }
      return;
    }
    const subscription: Subscription = {
      cursor,
      requestId: null,
      existingOnly,
      ...(expectedModel !== undefined ? { expectedModel } : {}),
    };
    this.tracked.set(sessionId, subscription);
    if (this.open) this.subscribe(sessionId, subscription);
  }
  untrack(sessionId: string, detach = false): void {
    const requestId = this.tracked.get(sessionId)?.requestId;
    this.tracked.delete(sessionId);
    if (detach && requestId)
      this.send({ type: "session.detach", session_id: sessionId, request_id: requestId });
  }
  onMessage(handler: (message: ServerMessage) => void): () => void {
    this.messageHandlers.add(handler);
    return () => void this.messageHandlers.delete(handler);
  }
  onStatus(handler: (status: SocketStatus) => void): () => void {
    this.statusHandlers.add(handler);
    return () => void this.statusHandlers.delete(handler);
  }
  private subscribe(sessionId: string, subscription: Subscription): void {
    const cursor = subscription.cursor();
    subscription.requestId = crypto.randomUUID();
    this.send({
      type: "session.open",
      session_id: sessionId,
      request_id: subscription.requestId,
      after_seq: cursor.seq,
      ...(cursor.generation ? { generation: cursor.generation } : {}),
      ...(subscription.existingOnly ? { existing_only: true } : {}),
      ...(subscription.expectedModel !== undefined
        ? { expected_model: subscription.expectedModel }
        : {}),
    });
  }
  private dial(): void {
    const socket = this.createSocket(this.url);
    this.socket = socket;
    this.epoch += 1;
    socket.onopen = () => {
      if (this.socket !== socket || this.closed) return;
      this.attempt = 0;
      try {
        socket.send(JSON.stringify({ type: "auth", token: this.token, v: PROTOCOL_VERSION }));
      } catch {
        this.disconnected(socket, 1006);
        socket.close(1011);
        return;
      }
      this.open = true;
      for (const [sessionId, subscription] of this.tracked) {
        if (!this.open) return;
        this.subscribe(sessionId, subscription);
      }
      if (this.open) this.setStatus("open");
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket || this.closed) return;
      let message: ServerMessage;
      try {
        const value: unknown = JSON.parse(String(event.data));
        if (
          !value ||
          typeof value !== "object" ||
          !("v" in value) ||
          value.v !== PROTOCOL_VERSION ||
          !("type" in value) ||
          typeof value.type !== "string" ||
          !("generation" in value) ||
          typeof value.generation !== "string"
        ) {
          this.stop("incompatible", INCOMPATIBLE);
          return;
        }
        message = value as ServerMessage;
      } catch {
        this.stop("incompatible", INCOMPATIBLE);
        return;
      }
      for (const handler of [...this.messageHandlers]) handler(message);
    };
    socket.onclose = (event) => this.disconnected(socket, event.code);
  }
  private disconnected(socket: WebSocketLike, code: number): void {
    if (this.socket !== socket) return;
    this.socket = null;
    this.open = false;
    if (this.closed) return;
    if (code === AUTH_REFUSED || code === INCOMPATIBLE) {
      this.closed = true;
      this.setStatus(code === AUTH_REFUSED ? "unauthorized" : "incompatible");
      return;
    }
    this.setStatus("reconnecting");
    const delay = this.retryDelaysMs[Math.min(this.attempt, this.retryDelaysMs.length - 1)];
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.closed) this.dial();
    }, delay);
  }
  private stop(status: SocketStatus, code: number): void {
    this.closed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    this.open = false;
    socket?.close(code);
    this.setStatus(status);
  }
  private setStatus(status: SocketStatus): void {
    for (const handler of [...this.statusHandlers]) handler(status);
  }
}
export function frameByteLength(message: ClientMessage): number {
  return new TextEncoder().encode(JSON.stringify(message)).length;
}
export function turnSendFits(text: string): boolean {
  return (
    frameByteLength({
      type: "turn.send",
      session_id: SAMPLE_UUID,
      generation: SAMPLE_UUID,
      command_id: SAMPLE_UUID,
      text,
    }) <= MAX_FRAME_BYTES
  );
}
function browserSocket(url: string): WebSocketLike {
  return new WebSocket(url) as WebSocketLike;
}
