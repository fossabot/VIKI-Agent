import { renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { RoutineItem, RoutineRun, RunAttempt } from "../clients/automation/routinesApi";
import {
  browserNotifier,
  inQuietHours,
  type NotificationHost,
  type Notifier,
  type NotifyPermission,
  noticesFor,
  type RoutineNotificationOptions,
  signalsOf,
  useRoutineNotifications,
} from "./routineNotifications";
import type { RoutinesSnapshot } from "./routines";

const NAME = "Quarterly tax review";

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

function item(over: Partial<RoutineItem> = {}): RoutineItem {
  return {
    routine_id: "r1",
    name: NAME,
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

/** The newest occurrence holds the attempt, so the server sends it as both runs. */
const finished = (id: string, outcome: string): RoutineItem => {
  const last = run({ attempt: attempt({ attempt_id: id, outcome }) });
  return item({ last_run: last, last_attempt: last });
};

const running = (id: string): RoutineItem =>
  item({ active_run: { attempt_id: id, session_id: "s1", state: "running", stop_by: null } });

const WITH_CARD = { s1: { cards: [{}] } } as never;
const NO_CARD = { s1: { cards: [] } } as never;

const ready = (...items: RoutineItem[]): RoutinesSnapshot => ({
  status: "ready",
  items,
  scheduler: null,
  error: null,
});

describe("inQuietHours", () => {
  const at = (hh: number, mm: number): Date => new Date(2026, 9, 3, hh, mm);
  test.each([
    ["22:00", "07:00", at(23, 30), true], // across midnight, before it
    ["22:00", "07:00", at(3, 0), true], // across midnight, after it
    ["22:00", "07:00", at(7, 0), false], // end is exclusive
    ["22:00", "07:00", at(22, 0), true], // start is inclusive
    ["22:00", "07:00", at(12, 0), false],
    ["09:00", "17:30", at(9, 0), true],
    ["09:00", "17:30", at(17, 29), true],
    ["09:00", "17:30", at(17, 30), false],
    ["09:00", "17:30", at(8, 59), false],
    ["09:00", "09:00", at(9, 0), false], // equal bounds: off
    [null, "07:00", at(3, 0), false],
    ["22:00", null, at(23, 0), false],
    [null, null, at(3, 0), false],
  ])("%s to %s at %s is %s", (start, end, now, quiet) => {
    expect(inQuietHours(start, end, now)).toBe(quiet);
  });
});

describe("noticesFor", () => {
  test("a new waiting attempt, a new finished attempt, and nothing for what was already there", () => {
    const before = signalsOf([item()], NO_CARD);
    expect(noticesFor(before, signalsOf([running("a2")], WITH_CARD))).toEqual([
      { kind: "waiting", routineId: "r1", tag: "viki-routine-a2-waiting" },
    ]);
    expect(noticesFor(before, signalsOf([finished("a2", "failed")], NO_CARD))).toEqual([
      { kind: "not_done", routineId: "r1", tag: "viki-routine-a2-done" },
    ]);
    const same = signalsOf([finished("a2", "succeeded")], NO_CARD);
    expect(noticesFor(same, same)).toEqual([]);
  });

  test("a routine absent from the earlier signals is a first sighting", () => {
    const next = signalsOf([running("a1"), finished("a9", "succeeded")], WITH_CARD);
    expect(noticesFor(new Map(), next)).toEqual([]);
  });

  test("an attempt without an outcome is not finished", () => {
    const before = signalsOf([item()], NO_CARD);
    expect(noticesFor(before, signalsOf([finished("a2", null as never)], NO_CARD))).toEqual([]);
  });

  test("a run that expires behind a newer overlap skip is a finished attempt", () => {
    // Run a1 waits on a card; the next instant is skipped (overlap) while it goes on, so the
    // skip is `last_run`; a1 then expires waiting for the user.
    const skip = run({ occurrence_id: "o2", state: "skipped", reason: "overlap", attempt: null });
    const a1 = (over: Partial<RunAttempt>) =>
      run({ attempt: attempt({ attempt_id: "a1", state: "running", outcome: null, ...over }) });
    const waiting = item({
      last_run: skip,
      last_attempt: a1({}),
      active_run: { attempt_id: "a1", session_id: "s1", state: "running", stop_by: null },
    });
    const expired = item({
      last_run: skip,
      last_attempt: a1({ state: "done", outcome: "expired", reason: "waiting_for_user" }),
    });
    expect(noticesFor(signalsOf([waiting], WITH_CARD), signalsOf([expired], NO_CARD))).toEqual([
      { kind: "not_done", routineId: "r1", tag: "viki-routine-a1-done" },
    ]);
  });
});

function fakeNotifier(permission: NotifyPermission = "granted") {
  const shown: { title: string; body: string; tag: string; onClick: () => void }[] = [];
  const notifier: Notifier = {
    permission: () => permission,
    request: async () => permission,
    show: (title, body, tag, onClick) => shown.push({ title, body, tag, onClick }),
  };
  return { notifier, shown };
}

function mount(over: Partial<RoutineNotificationOptions> = {}) {
  const fake = fakeNotifier();
  const onOpen = vi.fn();
  const base: RoutineNotificationOptions = {
    snapshot: ready(item()),
    sessions: NO_CARD,
    enabled: true,
    quietStart: null,
    quietEnd: null,
    notifier: fake.notifier,
    isFocused: () => false,
    now: () => new Date(2026, 9, 3, 12, 0),
    onOpen,
    ...over,
  };
  const view = renderHook((props: RoutineNotificationOptions) => useRoutineNotifications(props), {
    initialProps: base,
  });
  let props = base;
  return {
    ...fake,
    onOpen,
    update(next: Partial<RoutineNotificationOptions>) {
      props = { ...props, ...next };
      view.rerender(props);
    },
  };
}

describe("useRoutineNotifications", () => {
  test("the first ready snapshot is silent, even with a run waiting and one finished", () => {
    const { shown } = mount({
      snapshot: ready(running("a1"), finished("a0", "failed")),
      sessions: WITH_CARD,
    });
    expect(shown).toEqual([]);
  });

  test("a snapshot that is not ready sets no baseline", () => {
    const { shown, update } = mount({ snapshot: { ...ready(), status: "loading" } });
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    expect(shown).toEqual([]);
  });

  test("a new waiting attempt notifies once with generic text", () => {
    const { shown, update } = mount();
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    expect(shown).toHaveLength(1);
    expect(shown[0]).toMatchObject({
      title: "VIKI",
      body: "A routine is waiting for confirmation",
      tag: "viki-routine-a1-waiting",
    });
  });

  test("the card appearing in an attempt already running is the waiting transition", () => {
    const { shown, update } = mount({ snapshot: ready(running("a1")), sessions: NO_CARD });
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    expect(shown.map((s) => s.tag)).toEqual(["viki-routine-a1-waiting"]);
  });

  test("a new finished attempt notifies as done or not done", () => {
    const { shown, update } = mount({ snapshot: ready(finished("a1", "succeeded")) });
    update({ snapshot: ready(finished("a2", "succeeded")) });
    update({ snapshot: ready(finished("a3", "blocked")) });
    expect(shown.map((s) => [s.tag, s.body])).toEqual([
      ["viki-routine-a2-done", "A routine run has finished"],
      ["viki-routine-a3-done", "A routine run did not complete"],
    ]);
  });

  test("a snapshot repeating what was already seen does not notify again", () => {
    const { shown, update } = mount();
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    update({ snapshot: ready(finished("a1", "succeeded")) });
    update({ snapshot: ready(finished("a1", "succeeded")) });
    expect(shown.map((s) => s.tag)).toEqual(["viki-routine-a1-waiting", "viki-routine-a1-done"]);
  });

  test("a routine first seen after the baseline is silent", () => {
    const { shown, update } = mount();
    const other = (id: string) => ({ ...running(id), routine_id: "r2" });
    update({ snapshot: ready(item(), other("a5")), sessions: WITH_CARD });
    expect(shown).toEqual([]);
    update({ snapshot: ready(item(), other("a6")), sessions: WITH_CARD });
    expect(shown.map((s) => s.tag)).toEqual(["viki-routine-a6-waiting"]);
  });

  describe.each([
    ["the page is focused", { isFocused: () => true }, { isFocused: () => false }],
    [
      "in quiet hours",
      { quietStart: "11:00", quietEnd: "13:00" },
      { quietStart: null, quietEnd: null },
    ],
    ["notifications are disabled", { enabled: false }, { enabled: true }],
    ["permission is not granted", { notifier: fakeNotifier("denied").notifier }, null],
  ] as [string, Partial<RoutineNotificationOptions>, Partial<RoutineNotificationOptions> | null][])(
    "%s",
    (_label, hold, release) => {
      test("the notice is dropped and not replayed once the hold ends", () => {
        const { shown, notifier, update } = mount(hold);
        update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
        expect(shown).toEqual([]);
        // the hold ends; the same snapshot content arrives again
        update({ ...(release ?? { notifier }) });
        update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
        expect(shown).toEqual([]);
        // a genuinely new attempt after the hold notifies
        update({ snapshot: ready(running("a2")), sessions: WITH_CARD });
        expect(shown.map((s) => s.tag)).toEqual(["viki-routine-a2-waiting"]);
      });
    },
  );

  test("title, body and tag never hold the routine name", () => {
    const { shown, update } = mount();
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    update({ snapshot: ready(finished("a1", "failed")) });
    expect(shown).toHaveLength(2);
    for (const notice of shown)
      expect(`${notice.title} ${notice.body} ${notice.tag}`).not.toContain(NAME);
  });

  test("a click through the browser notifier focuses the window and opens the routine", () => {
    const made: { onclick: (() => void) | null; close: () => void }[] = [];
    class FakeNotification {
      static permission = "granted";
      onclick: (() => void) | null = null;
      close = vi.fn();
      constructor(
        readonly title: string,
        readonly options: NotificationOptions,
      ) {
        made.push(this);
      }
    }
    const win = { focus: vi.fn(), Notification: FakeNotification } as unknown as NotificationHost;
    const { onOpen, update } = mount({ notifier: browserNotifier(win) });
    update({ snapshot: ready(running("a1")), sessions: WITH_CARD });
    expect(made).toHaveLength(1);
    expect(onOpen).not.toHaveBeenCalled();
    made[0]?.onclick?.();
    expect(win.focus).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("r1");
    expect(made[0]?.close).toHaveBeenCalledTimes(1);
  });
});

describe("browserNotifier", () => {
  test("without the Notification API it reports unsupported and shows nothing", async () => {
    const notifier = browserNotifier({ focus: vi.fn() });
    expect(notifier.permission()).toBe("unsupported");
    expect(await notifier.request()).toBe("unsupported");
    expect(() => notifier.show("t", "b", "tag", vi.fn())).not.toThrow();
  });

  test("it reads the permission and asks the browser for it", async () => {
    const requestPermission = vi.fn(async () => "granted");
    const win = {
      focus: vi.fn(),
      Notification: { permission: "default", requestPermission },
    } as unknown as NotificationHost;
    const notifier = browserNotifier(win);
    expect(notifier.permission()).toBe("default");
    expect(await notifier.request()).toBe("granted");
    expect(requestPermission).toHaveBeenCalledTimes(1);
  });
});
