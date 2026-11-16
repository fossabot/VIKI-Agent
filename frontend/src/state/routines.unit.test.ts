import { describe, expect, test, vi } from "vitest";
import type {
  RoutineItem,
  RoutineRun,
  RoutinesApi,
  RunAttempt,
  SchedulerStatus,
} from "../clients/automation/routinesApi";
import { RoutinesApiError } from "../clients/automation/routinesApi";
import {
  attentionCount,
  createRoutinesStore,
  outcomeText,
  reasonText,
  routineAttention,
  stampLabel,
  stateText,
  whenLabel,
} from "./routines";

const SCHEDULER: SchedulerStatus = { state: "running", attended: true, paused: false };

function attempt(over: Partial<RunAttempt> = {}): RunAttempt {
  return {
    attempt_id: "a1",
    session_id: "s1",
    state: "done",
    outcome: "succeeded",
    reason: null,
    profile_id: null,
    profile_revision: null,
    native_version: null,
    created_at: "2026-10-03T00:00:00.000Z",
    dispatched_at: null,
    finished_at: null,
    ...over,
  };
}

function run(over: Partial<RoutineRun> = {}): RoutineRun {
  return {
    occurrence_id: "o1",
    kind: "schedule",
    scheduled_for: null,
    trigger_id: null,
    revision: 1,
    state: "claimed",
    reason: null,
    recorded_at: "2026-10-03T00:00:00.000Z",
    attempt: attempt(),
    ...over,
  };
}

function item(over: Partial<RoutineItem> = {}): RoutineItem {
  return {
    routine_id: "r1",
    name: "Morning",
    state: "enabled",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: null },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    ...over,
  };
}

const lastRun = (over: Partial<RunAttempt>): RoutineRun => run({ attempt: attempt(over) });
const NO_CARDS = { s1: { cards: [] } };
const WITH_CARD = { s1: { cards: [{}] } } as never;

describe("routineAttention", () => {
  const blocked = lastRun({ outcome: "blocked", reason: "quota" });
  const expired = lastRun({ outcome: "expired", reason: "waiting_for_user" });
  const active = { attempt_id: "a1", session_id: "s1", state: "running", stop_by: "T" };

  test.each([
    ["a card on the active run's session", { active_run: active }, WITH_CARD, "waiting"],
    [
      "a card, even when paused with a blocked last run",
      { state: "paused", active_run: active, last_attempt: blocked },
      WITH_CARD,
      "waiting",
    ],
    ["an active run with no card", { active_run: active }, NO_CARDS, null],
    ["an active run whose session is unknown", { active_run: active }, {}, null],
    ["a blocked last run", { last_attempt: blocked }, NO_CARDS, "blocked"],
    [
      "a blocked last run on a draft",
      { state: "draft", last_attempt: blocked },
      NO_CARDS,
      "blocked",
    ],
    [
      "a blocked last run on a paused routine",
      { state: "paused", last_attempt: blocked },
      {},
      null,
    ],
    [
      "a blocked last run on an archived routine",
      { state: "archived", last_attempt: blocked },
      {},
      null,
    ],
    ["an expired last run waiting for the user", { last_attempt: expired }, NO_CARDS, "expired"],
    [
      "an expired last run on a paused routine",
      { state: "paused", last_attempt: expired },
      {},
      null,
    ],
    [
      "an expired last run on an archived routine",
      { state: "archived", last_attempt: expired },
      {},
      null,
    ],
    [
      "an expired last run for another reason",
      { last_attempt: lastRun({ outcome: "expired", reason: "deadline" }) },
      {},
      null,
    ],
    ["a succeeded last run", { last_attempt: lastRun({ outcome: "succeeded" }) }, {}, null],
    ["a failed last run", { last_attempt: lastRun({ outcome: "failed" }) }, {}, null],
    ["a last run with no attempt", { last_run: run({ attempt: null }) }, {}, null],
    ["no last run", {}, {}, null],
  ] as const)("%s", (_name, over, sessions, kind) => {
    expect(
      routineAttention(item(over as Partial<RoutineItem>), sessions as never)?.kind ?? null,
    ).toBe(kind);
  });

  test("carries the stop time and the blocked reason", () => {
    expect(routineAttention(item({ active_run: active }), WITH_CARD)).toEqual({
      kind: "waiting",
      stopBy: "T",
    });
    expect(routineAttention(item({ last_attempt: blocked }), {})).toEqual({
      kind: "blocked",
      reason: "quota",
    });
  });

  test("a run that expires behind a newer overlap skip still needs the user", () => {
    // The skip is the newest occurrence, so it is `last_run`; the run's end is `last_attempt`.
    const skip = run({ occurrence_id: "o2", state: "skipped", reason: "overlap", attempt: null });
    expect(routineAttention(item({ last_run: skip, last_attempt: expired }), NO_CARDS)).toEqual({
      kind: "expired",
    });
  });

  test("counts the routines with attention", () => {
    const items = [
      item({ routine_id: "a", last_attempt: blocked }),
      item({ routine_id: "b", state: "paused", last_attempt: blocked }),
      item({ routine_id: "c", active_run: active }),
      item({ routine_id: "d" }),
    ];
    expect(attentionCount(items, WITH_CARD)).toBe(2);
    expect(attentionCount([], {})).toBe(0);
  });
});

describe("labels", () => {
  const NOW = new Date(2026, 9, 3, 12, 0).getTime();
  const stamp = (day: number, hour: number, minute: number): string =>
    new Date(2026, 9, day, hour, minute).toISOString();

  test("whenLabel: today, tomorrow, later, and across a month end", () => {
    expect(whenLabel(stamp(3, 9, 5), NOW)).toBe("09:05");
    expect(whenLabel(stamp(4, 7, 30), NOW)).toBe("Tomorrow 07:30");
    expect(whenLabel(stamp(5, 7, 30), NOW)).toBe("10/5 07:30");
    expect(
      whenLabel(new Date(2026, 10, 1, 8, 0).toISOString(), new Date(2026, 9, 31, 12).getTime()),
    ).toBe("Tomorrow 08:00");
    expect(whenLabel(stamp(2, 9, 5), NOW)).toBe("10/2 09:05");
  });

  test("stampLabel and a stamp that does not parse", () => {
    expect(stampLabel(stamp(3, 9, 5))).toBe("10/3 09:05");
    expect(stampLabel("soon")).toBe("");
    expect(whenLabel("soon", NOW)).toBe("");
  });

  test("reasonText: known code, unknown code, absent code", () => {
    expect(reasonText("quota")).toBe("Usage limit reached");
    expect(reasonText("no_card")).toBe("No card was waiting on Kanban");
    expect(reasonText("needs_decision")).toBe("A step needed your confirmation");
    expect(reasonText("brand_new")).toBe("Other (brand_new)");
    expect(reasonText("other")).toBe("Other (other)");
    expect(reasonText("constructor")).toBe("Other (constructor)");
    expect(reasonText(null)).toBe("Other (unknown)");
  });

  test("stateText", () => {
    expect(stateText("paused")).toBe("Paused");
    expect(stateText("enabled")).toBe("Enabled");
  });

  test("outcomeText: attempt outcome, running attempt, skipped and claimed occurrences", () => {
    expect(outcomeText(lastRun({ outcome: "succeeded" }))).toBe("Done");
    expect(outcomeText(lastRun({ outcome: "blocked" }))).toBe("Could not start");
    expect(outcomeText(lastRun({ outcome: "blocked", reason: "billing" }))).toBe("Could not start");
    expect(outcomeText(lastRun({ outcome: "blocked", reason: "needs_decision" }))).toBe(
      "Stopped for your decision",
    );
    expect(outcomeText(lastRun({ outcome: null, state: "running" }))).toBe("Running");
    expect(outcomeText(run({ attempt: null, state: "skipped" }))).toBe("Skipped");
    expect(outcomeText(run({ attempt: null, state: "claimed" }))).toBe("Running");
    expect(outcomeText(lastRun({ outcome: "mystery" }))).toBe("Unknown");
  });
});

describe("createRoutinesStore", () => {
  interface Pending {
    resolve(value: { routines: RoutineItem[]; scheduler: SchedulerStatus }): void;
    reject(error: unknown): void;
    signal: AbortSignal | undefined;
  }

  function setup(intervalMs = 1000, activeIntervalMs = 5_000) {
    const pending: Pending[] = [];
    const list = vi.fn(
      (signal?: AbortSignal) =>
        new Promise<{ routines: RoutineItem[]; scheduler: SchedulerStatus }>((resolve, reject) => {
          pending.push({ resolve, reject, signal });
        }),
    );
    const armed = new Map<number, { callback: () => void; ms: number }>();
    let next = 1;
    const timers = {
      set: vi.fn((callback: () => void, ms: number) => {
        const handle = next++;
        armed.set(handle, {
          callback: () => {
            armed.delete(handle);
            callback();
          },
          ms,
        });
        return handle;
      }),
      clear: vi.fn((handle: number) => {
        armed.delete(handle);
      }),
    };
    const store = createRoutinesStore({ list } as unknown as RoutinesApi, {
      intervalMs,
      activeIntervalMs,
      timers,
    });
    return { store, list, pending, armed, timers };
  }

  const flush = async (): Promise<void> => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  };
  const answer = async (pending: Pending, routines: RoutineItem[] = []): Promise<void> => {
    pending.resolve({ routines, scheduler: SCHEDULER });
    await flush();
  };

  test("starts idle; start loads, then ready with the items and the scheduler", async () => {
    const { store, pending } = setup();
    expect(store.getSnapshot()).toEqual({
      status: "idle",
      items: [],
      scheduler: null,
      error: null,
    });
    store.start();
    expect(store.getSnapshot().status).toBe("loading");
    const mine = item();
    await answer(pending[0] as Pending, [mine]);
    expect(store.getSnapshot()).toEqual({
      status: "ready",
      items: [mine],
      scheduler: SCHEDULER,
      error: null,
    });
  });

  test("snapshot identity changes per commit and listeners are notified until unsubscribed", async () => {
    const { store, pending } = setup();
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    const before = store.getSnapshot();
    store.start();
    expect(store.getSnapshot()).not.toBe(before);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    await answer(pending[0] as Pending);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  test("start while started does nothing", () => {
    const { store, list } = setup();
    store.start();
    store.start();
    expect(list).toHaveBeenCalledTimes(1);
  });

  test("re-arms the timer after each poll settles and polls again when it fires", async () => {
    const { store, list, pending, armed, timers } = setup(1234);
    store.start();
    expect(timers.set).not.toHaveBeenCalled();
    await answer(pending[0] as Pending);
    expect(timers.set).toHaveBeenCalledTimes(1);
    expect(timers.set).toHaveBeenLastCalledWith(expect.any(Function), 1234);
    [...armed.values()][0]?.callback();
    expect(list).toHaveBeenCalledTimes(2);
    expect(armed.size).toBe(0);
    await answer(pending[1] as Pending);
    expect(armed.size).toBe(1);
  });

  test("a failure keeps the last items, reports the code, still re-arms, and a success clears it", async () => {
    const { store, pending, armed } = setup();
    store.start();
    const mine = item();
    await answer(pending[0] as Pending, [mine]);
    [...armed.values()][0]?.callback();
    pending[1]?.reject(new RoutinesApiError(503, "unavailable"));
    await flush();
    expect(store.getSnapshot()).toMatchObject({
      status: "error",
      items: [mine],
      error: "unavailable",
    });
    expect(armed.size).toBe(1);
    [...armed.values()][0]?.callback();
    pending[2]?.reject(new Error("boom"));
    await flush();
    expect(store.getSnapshot().error).toBe("unavailable");
    [...armed.values()][0]?.callback();
    await answer(pending[3] as Pending, [mine]);
    expect(store.getSnapshot()).toMatchObject({ status: "ready", error: null });
  });

  describe("active run polling", () => {
    const running = item({
      active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: "T" },
    });

    test("re-arms at 5,000 ms while a run is active, then at the interval once it clears", async () => {
      const { store, pending, armed, timers } = setup(60_000);
      store.start();
      await answer(pending[0] as Pending, [running]);
      expect(timers.set).toHaveBeenLastCalledWith(expect.any(Function), 5_000);
      [...armed.values()][0]?.callback();
      await answer(pending[1] as Pending, [item()]);
      expect(timers.set).toHaveBeenLastCalledWith(expect.any(Function), 60_000);
    });

    test("the option overrides the active interval", async () => {
      const { store, pending, timers } = setup(60_000, 250);
      store.start();
      await answer(pending[0] as Pending, [running]);
      expect(timers.set).toHaveBeenLastCalledWith(expect.any(Function), 250);
    });

    test("a failed poll keeps the active interval while the last items had an active run", async () => {
      const { store, pending, armed, timers } = setup(60_000);
      store.start();
      await answer(pending[0] as Pending, [running]);
      [...armed.values()][0]?.callback();
      pending[1]?.reject(new RoutinesApiError(503, "unavailable"));
      await flush();
      expect(timers.set).toHaveBeenLastCalledWith(expect.any(Function), 5_000);
    });
  });

  test("a later poll keeps the ready status while it is in flight", async () => {
    const { store, pending, armed } = setup();
    store.start();
    await answer(pending[0] as Pending);
    [...armed.values()][0]?.callback();
    expect(store.getSnapshot().status).toBe("ready");
  });

  test("a refresh while a request is in flight runs once after it, however often asked", async () => {
    const { store, list, pending, armed } = setup();
    store.start();
    store.refresh();
    store.refresh();
    store.refresh();
    expect(list).toHaveBeenCalledTimes(1);
    await answer(pending[0] as Pending);
    expect(list).toHaveBeenCalledTimes(2);
    expect(armed.size).toBe(0);
    await answer(pending[1] as Pending);
    expect(list).toHaveBeenCalledTimes(2);
    expect(armed.size).toBe(1);
  });

  test("a refresh with nothing in flight reads now and replaces the armed timer", async () => {
    const { store, list, pending, armed, timers } = setup();
    store.start();
    await answer(pending[0] as Pending);
    expect(armed.size).toBe(1);
    store.refresh();
    expect(list).toHaveBeenCalledTimes(2);
    expect(timers.clear).toHaveBeenCalledTimes(1);
    expect(armed.size).toBe(0);
  });

  test("a refresh while stopped does nothing", () => {
    const { store, list } = setup();
    store.refresh();
    expect(list).not.toHaveBeenCalled();
  });

  test("stop aborts the request, clears the timer and drops a queued refresh", async () => {
    const { store, list, pending, armed } = setup();
    store.start();
    store.refresh();
    store.stop();
    expect(pending[0]?.signal?.aborted).toBe(true);
    expect(store.getSnapshot().status).toBe("idle");
    await answer(pending[0] as Pending, [item()]);
    expect(store.getSnapshot().items).toEqual([]);
    expect(list).toHaveBeenCalledTimes(1);
    expect(armed.size).toBe(0);

    store.start();
    await answer(pending[1] as Pending);
    expect(armed.size).toBe(1);
    store.stop();
    expect(armed.size).toBe(0);
  });

  test("an aborted request that rejects does not report an error", async () => {
    const { store, pending } = setup();
    store.start();
    store.stop();
    pending[0]?.reject(new RoutinesApiError(0, "cancelled"));
    await flush();
    expect(store.getSnapshot().error).toBeNull();
  });

  test("restarting after a stop ignores the old request's late answer", async () => {
    const { store, pending } = setup();
    store.start();
    store.stop();
    store.start();
    const fresh = item({ name: "Fresh" });
    await answer(pending[1] as Pending, [fresh]);
    await answer(pending[0] as Pending, [item({ name: "Stale" })]);
    expect(store.getSnapshot().items).toEqual([fresh]);
  });
});
