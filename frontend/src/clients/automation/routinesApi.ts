import { boundedJson } from "../boundedResponse";

/** REST client for `/api/v2/routines`: the shapes mirror `src/viki_agent/automation/routines_api.py`. Every response is
 * checked for the fields the UI reads; a missing one is `invalid_response`. */

export interface RoutineSchedule {
  kind: "manual" | "daily" | "weekly";
  times: string[];
  days: number[];
  timezone: string | null;
  start_date: string | null;
  end_date: string | null;
  catch_up: "skip";
}
export interface RoutineDocument {
  name: string;
  purpose: string;
  prompt: string;
  profile_id: string;
  workspace_id: string;
  deadline_minutes: number;
  schedule: RoutineSchedule;
  /** Absent = every scheduled time runs. `card`: a todo card waits for `seat`; `changed`: a note
   * under `folder` changed since the routine's last completed run at this revision. */
  run_only_if?: { kind: "card"; seat: string } | { kind: "changed"; folder: string };
  /** Absent = a confirmation waits until the time limit. */
  on_confirmation?: "stop";
}
export interface RunAttempt {
  attempt_id: string;
  session_id: string;
  state: string;
  outcome: string | null;
  reason: string | null;
  profile_id: string | null;
  profile_revision: number | null;
  native_version: string | null;
  created_at: string;
  dispatched_at: string | null;
  finished_at: string | null;
}
export interface RoutineRun {
  occurrence_id: string;
  kind: string;
  scheduled_for: string | null;
  trigger_id: string | null;
  revision: number | null;
  state: string;
  reason: string | null;
  recorded_at: string;
  attempt: RunAttempt | null;
}
export interface ActiveRun {
  attempt_id: string;
  session_id: string;
  state: string;
  stop_by: string | null;
}
export interface SchedulerStatus {
  state: string;
  attended: boolean;
  paused: boolean;
}
export interface RoutineItem {
  routine_id: string;
  name: string;
  state: "draft" | "tested" | "enabled" | "paused" | "archived";
  current_revision: number;
  state_revision: number;
  schedule: { kind: string; timezone: string | null };
  next_run: string | null;
  last_run: RoutineRun | null;
  /** The newest run with an attempt: a newer skip (an overlap, a pause) can hide it from `last_run`. */
  last_attempt: RoutineRun | null;
  active_run: ActiveRun | null;
}
export interface NextSlot {
  at: string | null;
  local: string;
  zone: string;
  offset_minutes: number | null;
  note: "dst_gap" | "dst_fold" | null;
}
export interface Readiness {
  blockers: string[];
  run_blockers: string[];
  warnings: string[];
}
export interface Resolved {
  profile: {
    profile_id: string;
    name: string;
    revision: number;
    provider: string;
    model: string | null;
    permission_mode: string | null;
    mode_source: "profile" | "settings" | null;
    connection_pinned: boolean;
  } | null;
  workspace: { workspace_id: string; name: string; policy: string; active: boolean } | null;
  first_turn: string | null;
  /** A Bot profile's instructions, memory rule and Bot rules, sent as the system prompt; absent
   * or null when there is none. */
  system_prompt?: string | null;
  deadline_minutes: number;
}
export interface RoutinePreviewData {
  next_five: NextSlot[];
  readiness: Readiness;
  resolved: Resolved;
}
export interface RoutineDetailData extends RoutineItem, RoutinePreviewData {
  document: RoutineDocument;
  tested_revision: number | null;
  activates_at: string;
}
export interface ChangeResult {
  routine_id: string;
  revision: number;
  state: RoutineItem["state"];
  state_revision: number;
}
export interface RunStarted {
  occurrence_id: string;
  attempt_id: string;
  session_id: string;
}
export type RoutineStateAction = "enable" | "pause" | "archive";

export interface RoutinesApi {
  list(signal?: AbortSignal): Promise<{ routines: RoutineItem[]; scheduler: SchedulerStatus }>;
  get(id: string, signal?: AbortSignal): Promise<RoutineDetailData>;
  runs(
    id: string,
    page: { limit: number; before?: string },
    signal?: AbortSignal,
  ): Promise<RoutineRun[]>;
  preview(document: RoutineDocument, signal?: AbortSignal): Promise<RoutinePreviewData>;
  create(commandId: string, document: RoutineDocument): Promise<ChangeResult>;
  revise(
    id: string,
    commandId: string,
    expectedRevision: number,
    document: RoutineDocument,
  ): Promise<ChangeResult>;
  setState(
    id: string,
    action: RoutineStateAction,
    commandId: string,
    expectedStateRevision: number,
  ): Promise<ChangeResult>;
  run(id: string, commandId: string, expectedRevision: number): Promise<RunStarted>;
}

export class RoutinesApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly blockers: readonly string[] = [],
  ) {
    super(code);
    this.name = "RoutinesApiError";
  }
}

const TIMEOUT_MS = 15_000;
const RESPONSE_MAX_BYTES = 2 << 20;
const invalid = (): never => {
  throw new RoutinesApiError(0, "invalid_response");
};
const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isInt = (value: unknown): value is number => Number.isSafeInteger(value);
const isStrings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");
const orNull = (check: (value: unknown) => boolean) => (value: unknown) =>
  value === null || check(value);
const isString = (value: unknown): value is string => typeof value === "string";
const maybeString = orNull(isString);
const maybeInt = orNull(isInt);

function object(value: unknown, checks: Record<string, (value: unknown) => boolean>) {
  if (!isObject(value)) return invalid();
  for (const [key, check] of Object.entries(checks)) if (!check(value[key])) invalid();
  return value;
}

const STATES = new Set(["draft", "tested", "enabled", "paused", "archived"]);

function parseAttempt(value: unknown): RunAttempt | null {
  if (value === null) return null;
  return object(value, {
    attempt_id: isString,
    session_id: isString,
    state: isString,
    outcome: maybeString,
    reason: maybeString,
    profile_id: maybeString,
    profile_revision: maybeInt,
    native_version: maybeString,
    created_at: isString,
    dispatched_at: maybeString,
    finished_at: maybeString,
  }) as unknown as RunAttempt;
}
function parseRun(value: unknown): RoutineRun {
  const run = object(value, {
    occurrence_id: isString,
    kind: isString,
    scheduled_for: maybeString,
    trigger_id: maybeString,
    revision: maybeInt,
    state: isString,
    reason: maybeString,
    recorded_at: isString,
  });
  parseAttempt(run.attempt ?? null);
  return { ...run, attempt: run.attempt ?? null } as unknown as RoutineRun;
}
function parseItem(value: unknown): RoutineItem {
  const item = object(value, {
    routine_id: isString,
    name: isString,
    state: (state) => typeof state === "string" && STATES.has(state),
    current_revision: isInt,
    state_revision: isInt,
    next_run: maybeString,
  });
  const schedule = object(item.schedule, { kind: isString, timezone: maybeString });
  if (item.last_run !== null && item.last_run !== undefined) parseRun(item.last_run);
  // Older servers omit `last_attempt`; require it once every server sends it.
  const lastAttempt = item.last_attempt ?? null;
  if (lastAttempt !== null) parseRun(lastAttempt);
  const active = item.active_run ?? null;
  if (active !== null)
    object(active, {
      attempt_id: isString,
      session_id: isString,
      state: isString,
      stop_by: maybeString,
    });
  return {
    ...item,
    schedule,
    last_run: item.last_run ?? null,
    last_attempt: lastAttempt,
    active_run: active,
  } as RoutineItem;
}
function parseNextFive(value: unknown): NextSlot[] {
  if (!Array.isArray(value)) return invalid();
  // The server sends note "" for an ordinary slot; the UI reads null.
  return value.map((slot) => {
    const checked = object(slot, {
      at: maybeString,
      local: isString,
      zone: isString,
      offset_minutes: maybeInt,
      note: (note) => note === "" || note === "dst_gap" || note === "dst_fold",
    });
    return { ...checked, note: checked.note || null } as NextSlot;
  });
}
function parseReadiness(value: unknown): Readiness {
  return object(value, {
    blockers: isStrings,
    run_blockers: isStrings,
    warnings: isStrings,
  }) as unknown as Readiness;
}
function parseResolved(value: unknown): Resolved {
  const resolved = object(value, {
    first_turn: maybeString,
    system_prompt: (prompt) => prompt === undefined || maybeString(prompt),
    deadline_minutes: isInt,
    profile: orNull(isObject),
    workspace: orNull(isObject),
  });
  if (resolved.profile)
    object(resolved.profile, {
      profile_id: isString,
      name: isString,
      revision: isInt,
      provider: isString,
      model: maybeString,
      permission_mode: maybeString,
      mode_source: (source) => source === null || source === "profile" || source === "settings",
      connection_pinned: (pinned) => typeof pinned === "boolean",
    });
  if (resolved.workspace)
    object(resolved.workspace, {
      workspace_id: isString,
      name: isString,
      policy: isString,
      active: (active) => typeof active === "boolean",
    });
  return resolved as unknown as Resolved;
}
function parsePreview(value: unknown): RoutinePreviewData {
  const data = object(value, {});
  return {
    next_five: parseNextFive(data.next_five),
    readiness: parseReadiness(data.readiness),
    resolved: parseResolved(data.resolved),
  };
}
function parseDetail(value: unknown): RoutineDetailData {
  const data = object(value, {
    document: isObject,
    tested_revision: maybeInt,
    activates_at: isString,
  });
  return {
    ...parseItem(value),
    ...parsePreview(value),
    document: data.document,
    tested_revision: data.tested_revision,
    activates_at: data.activates_at,
  } as RoutineDetailData;
}
function parseChange(value: unknown): ChangeResult {
  return object(value, {
    routine_id: isString,
    revision: isInt,
    state: (state) => typeof state === "string" && STATES.has(state),
    state_revision: isInt,
  }) as unknown as ChangeResult;
}
function parseStarted(value: unknown): RunStarted {
  return object(value, {
    occurrence_id: isString,
    attempt_id: isString,
    session_id: isString,
  }) as unknown as RunStarted;
}

/** The server's code only; its `message` is never kept. Blockers ride a 422's string array. */
function serverError(status: number, body: unknown): RoutinesApiError {
  const error = isObject(body) && isObject(body.error) ? body.error : {};
  const code = typeof error.code === "string" ? error.code : "unavailable";
  const blockers = status === 422 && isStrings(error.blockers) ? error.blockers : [];
  return new RoutinesApiError(status, code, blockers);
}

export function createRoutinesApi(token: string, fetchImpl: typeof fetch = fetch): RoutinesApi {
  async function request(
    path: string,
    method: "GET" | "POST" | "PATCH",
    payload: object | undefined,
    signal: AbortSignal | undefined,
  ): Promise<unknown> {
    if (signal?.aborted) throw new RoutinesApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, {
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(payload ? { "content-type": "application/json" } : {}),
        },
        signal: controller.signal,
        ...(payload ? { body: JSON.stringify(payload) } : {}),
      });
      let body: unknown;
      try {
        body = await boundedJson(response, RESPONSE_MAX_BYTES);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw serverError(response.status, null);
        throw new RoutinesApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, body);
      return body;
    } catch (error) {
      if (error instanceof RoutinesApiError) throw error;
      if (signal?.aborted) throw new RoutinesApiError(0, "cancelled");
      if (timedOut) throw new RoutinesApiError(408, "intake_timeout");
      throw new RoutinesApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  const base = "/api/v2/routines";
  const at = (id: string): string => `${base}/${encodeURIComponent(id)}`;

  return {
    async list(signal) {
      const body = object(await request(base, "GET", undefined, signal), {
        routines: Array.isArray,
        scheduler: isObject,
      });
      const scheduler = object(body.scheduler, {
        state: isString,
        attended: (value) => typeof value === "boolean",
        // Older servers omit `paused`; require it once every server sends it.
        paused: (value) => value === undefined || typeof value === "boolean",
      }) as unknown as SchedulerStatus;
      return {
        routines: (body.routines as unknown[]).map(parseItem),
        scheduler: { ...scheduler, paused: scheduler.paused ?? false },
      };
    },
    async get(id, signal) {
      return parseDetail(await request(at(id), "GET", undefined, signal));
    },
    async runs(id, { limit, before }, signal) {
      const query = new URLSearchParams({ limit: String(limit) });
      if (before !== undefined) query.set("before", before);
      const body = object(await request(`${at(id)}/runs?${query}`, "GET", undefined, signal), {
        runs: Array.isArray,
      });
      return (body.runs as unknown[]).map(parseRun);
    },
    async preview(document, signal) {
      return parsePreview(await request(`${base}/preview`, "POST", { document }, signal));
    },
    async create(commandId, document) {
      return parseChange(
        await request(base, "POST", { command_id: commandId, document }, undefined),
      );
    },
    async revise(id, commandId, expectedRevision, document) {
      return parseChange(
        await request(
          at(id),
          "PATCH",
          { command_id: commandId, expected_revision: expectedRevision, document },
          undefined,
        ),
      );
    },
    async setState(id, action, commandId, expectedStateRevision) {
      return parseChange(
        await request(
          `${at(id)}/${action}`,
          "POST",
          { command_id: commandId, expected_state_revision: expectedStateRevision },
          undefined,
        ),
      );
    },
    async run(id, commandId, expectedRevision) {
      return parseStarted(
        await request(
          `${at(id)}/run`,
          "POST",
          { command_id: commandId, expected_revision: expectedRevision },
          undefined,
        ),
      );
    },
  };
}
