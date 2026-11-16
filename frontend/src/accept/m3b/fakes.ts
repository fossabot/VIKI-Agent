/**
 * Scripted v2 component fixtures behind a real createViki. Automatic mode supplies the
 * declared server responses; manual mode requires each session event to be delivered explicitly.
 * Nothing here touches the network.
 */
import type { Rest } from "../../api";
import type {
  AgentInfo,
  GateRow,
  Rails,
  SearchHit,
  SessionRow,
  SessionSnapshot,
} from "../../protocol";
import type { WebSocketLike } from "../../state/socket";
import { createViki, type Viki } from "../../state/viki";

export const GENERATION = "11111111-1111-4111-8111-111111111111";
export const REQUEST = "22222222-2222-4222-8222-222222222222";

export function snapshotData(): SessionSnapshot["data"] {
  return {
    events: [],
    truncated: false,
    state: {
      live: false,
      info: null,
      mode: "",
      blocked: null,
      closed: false,
      error: null,
      compacting: false,
      context_percent: null,
      rate_limit: null,
    },
  };
}

/** A scripted v2 server for component integration. Manual mode preserves every supplied byte. */
export class FakeSocket implements WebSocketLike {
  controlStatus: "applied" | "rejected" | "unknown" = "applied";
  sent: Record<string, unknown>[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  private readonly subscriptions = new Map<string, string>();
  private readonly sequences = new Map<string, number>();

  constructor(private readonly automatic = true) {}

  send(data: string): void {
    const message = JSON.parse(data) as Record<string, unknown>;
    this.sent.push(message);
    if (message.type === "session.open") {
      this.subscriptions.set(String(message.session_id), String(message.request_id));
      if (this.automatic) this.synchronize(String(message.session_id));
    } else if (this.automatic && typeof message.command_id === "string") {
      this.acknowledge(message, this.controlStatus);
    }
  }

  close(): void {}

  open(): void {
    this.onopen?.({});
  }

  receive(message: unknown): void {
    if (this.automatic && message && typeof message === "object") {
      const frame = { ...message } as Record<string, unknown>;
      const id = String(frame.session_id ?? "");
      // An explicit blank request ID is a fixture placeholder; real, including stale, IDs
      // are never rewritten. Protocol/adversarial tests use automatic=false.
      if (frame.request_id === "" && frame.type !== "vault.sessions") {
        const request =
          frame.type === "history.page"
            ? this.sent.findLast((item) => item.type === "history.more" && item.session_id === id)
                ?.request_id
            : this.subscriptions.get(id);
        frame.request_id = request ?? "";
      }
      if (typeof frame.seq === "number")
        this.sequences.set(id, Math.max(this.sequences.get(id) ?? 0, frame.seq));
      if (
        (frame.type === "permission.requested" || frame.type === "question.asked") &&
        !frame.native_request
      ) {
        const binding = {
          runtime_id: GENERATION,
          request_id: crypto.randomUUID(),
          turn_id: REQUEST,
        };
        frame.native_request = binding;
      }
      message = frame;
    }
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  synchronize(
    sessionId: string,
    generation = GENERATION,
    seq = 0,
    data?: SessionSnapshot["data"],
  ): void {
    const request_id = this.subscriptions.get(sessionId);
    if (!request_id) throw new Error("A session.open is required before synchronize");
    this.sequences.set(sessionId, seq);
    const identity = { v: 2, session_id: sessionId, generation, request_id, seq };
    this.receive({ ...identity, type: "session.snapshot", data: data ?? snapshotData() });
    this.receive({ ...identity, type: "session.synced", data: {} });
  }

  acknowledge(
    message: Record<string, unknown>,
    status: "applied" | "rejected" | "unknown" = "applied",
  ): void {
    const id = String(message.session_id);
    if (
      this.automatic &&
      status === "applied" &&
      (message.type === "permission.respond" ||
        message.type === "question.answer" ||
        message.type === "session.close")
    ) {
      const seq = (this.sequences.get(id) ?? 0) + 1;
      this.receive({
        v: 2,
        type: message.type === "session.close" ? "session.closed" : "request.answered",
        session_id: id,
        generation: message.generation,
        request_id: this.subscriptions.get(id),
        seq,
        data:
          message.type === "session.close"
            ? { reason: "requested" }
            : { tool_use_id: message.tool_use_id },
        ...(message.native_request ? { native_request: message.native_request } : {}),
      });
    }
    this.receive({
      v: 2,
      type: "control.receipt",
      session_id: id,
      generation: message.generation,
      command_id: message.command_id,
      data: {
        status,
        reason: message.type === "turn.send" ? "native_unconfirmed" : "",
        ...(message.type === "session.fork"
          ? { target: { session_id: message.fork_id, generation: GENERATION } }
          : {}),
      },
    });
  }
}

/** A Viki over a fake socket; `socket.open()` brings the status to "open". */
export function fakeViki(): { viki: Viki; socket: FakeSocket } {
  const socket = new FakeSocket();
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => socket,
  });
  viki.client.connect();
  return { viki, socket };
}

export interface FakeRestData {
  sessions?: SessionRow[];
  rails?: Rails;
  hits?: SearchHit[];
}

export interface FakeRest extends Rest {
  /** "sessions", "rails" or "search:<q>", in call order. */
  calls: string[];
}

/** Resolves every call at once with the given data (empty by default). */
export function fakeRest(data: FakeRestData = {}): FakeRest {
  const calls: string[] = [];
  return {
    calls,
    sessions: async () => {
      calls.push("sessions");
      return { sessions: data.sessions ?? [], state: "ready", reason: "", omitted: 0 };
    },
    search: async (q) => {
      calls.push(`search:${q}`);
      return {
        hits: data.hits ?? [],
        state: "ready",
        reason: "",
        scanned: 0,
        total: 0,
        request_id: "",
      };
    },
    rails: async () => {
      calls.push("rails");
      return data.rails ?? emptyRails();
    },
  };
}

/** Rejects every call with `error` (a RestError or anything else). */
export function failingRest(error: unknown): FakeRest {
  const calls: string[] = [];
  const fail = async (label: string) => {
    calls.push(label);
    throw error;
  };
  return {
    calls,
    sessions: () => fail("sessions"),
    search: (q) => fail(`search:${q}`),
    rails: () => fail("rails"),
  };
}

export function emptyRails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 0, sources: 0, inbox: 0 },
    git: {
      state: "not_repository",
      ref_state: "unknown",
      branch: "",
      head: "",
      committed_at: "",
      dirty: null,
    },
  };
}

export function sessionRow(overrides: Partial<SessionRow> & { session_id: string }): SessionRow {
  return {
    title: "",
    first_prompt: "",
    entrypoint: "viki-agent",
    modified: "",
    cli_version: "2.1.259",
    other: false,
    ...overrides,
  };
}

export function agent(overrides: Partial<AgentInfo> & { name: string }): AgentInfo {
  return { description: "", tools: [], model: "", last_run: "", ...overrides };
}

export function gate(overrides: Partial<GateRow> & { id: string }): GateRow {
  return { file: "2026-09-10.md", item: "", label: "", owner_applies: false, ...overrides };
}

/** A local-time ISO stamp: tests build dates with the local constructor so the expected labels
 * hold in any time zone. */
export function localIso(year: number, month1: number, day: number, hour = 0, minute = 0): string {
  return new Date(year, month1 - 1, day, hour, minute).toISOString();
}
