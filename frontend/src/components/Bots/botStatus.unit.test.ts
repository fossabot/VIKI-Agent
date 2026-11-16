import { describe, expect, it } from "vitest";
import type { RoutineItem } from "../../clients/automation/routinesApi";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { RoutinesSnapshot } from "../../state/routines";
import { emptyView, type SessionView } from "../../state/view";
import {
  botStatus,
  CHECKING,
  checkerStatus,
  countText,
  NOT_STARTED,
  STATUS_UNAVAILABLE,
  statusCounts,
  type TeamLive,
} from "./botStatus";

const NOW = new Date(2026, 10, 4, 9, 0).getTime();

function bot(over: Partial<BotRow> = {}): BotRow {
  return {
    bot_id: "b1",
    profile_id: "p1",
    workspace_id: "w1",
    chat_session_id: "s1",
    profile: { revision: 1, archived: false, identity: {} as never },
    workspace: { name: "Home", policy: "ordinary", active: true },
    chat: { profile_revision: 1, started: true, archived: false },
    routine_ids: [],
    ...over,
  } as BotRow;
}

function routine(over: Partial<RoutineItem> = {}): RoutineItem {
  return {
    routine_id: "r1",
    name: "Digest",
    state: "enabled",
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    ...over,
  } as RoutineItem;
}

function snap(items: RoutineItem[], over: Partial<RoutinesSnapshot> = {}): RoutinesSnapshot {
  return { status: "ready", items, scheduler: null, error: null, ...over };
}

function view(over: Partial<SessionView>): SessionView {
  return { ...emptyView("s1"), ...over };
}

const running = (kind: string, done = false) =>
  [{ status: "running", blocks: [{ kind, done }] }] as never;

function live(over: Partial<TeamLive> = {}): TeamLive {
  return {
    online: true,
    sessions: {},
    others: [],
    routines: snap([]),
    checker: "unknown",
    sessionRows: [],
    nowMs: NOW,
    ...over,
  };
}

const status = (b: BotRow, l: TeamLive, ws = "w1") => botStatus(b, l, ws);
const owns = (...ids: string[]) => bot({ routine_ids: ids });
const WAITING = { s1: view({ cards: [{}] as never }) };
const BLOCKED = routine({
  last_attempt: { attempt: { outcome: "blocked", reason: "x" } } as never,
});

describe("botStatus", () => {
  it.each([
    ["no profile", bot({ profile: null }), "Its profile is unavailable."],
    [
      "archived profile",
      bot({ profile: { revision: 1, archived: true, identity: {} as never } }),
      "Its profile is archived.",
    ],
    ["no vault", bot({ workspace: null }), "Its vault is unavailable."],
    [
      "archived chat",
      bot({ chat: { profile_revision: 1, started: true, archived: true } }),
      "Its conversation is archived.",
    ],
  ])("unavailable: %s, before everything", (_n, b, text) => {
    expect(status(b, live({ online: false, sessions: WAITING }), "other")).toEqual({
      state: "unavailable",
      text,
    });
  });

  it("away beats offline and a waiting chat", () => {
    expect(status(bot(), live({ online: false, sessions: WAITING }), "w2")).toEqual({
      state: "away",
      text: "In Home",
    });
  });

  it("offline is Status unavailable", () => {
    expect(status(bot(), live({ online: false, sessions: WAITING }))).toBe(STATUS_UNAVAILABLE);
  });

  it("a waiting chat needs you and beats a running chat routine", () => {
    const l = live({
      sessions: WAITING,
      routines: snap([
        routine({ active_run: { attempt_id: "a", session_id: "x", state: "", stop_by: null } }),
      ]),
    });
    expect(status(owns("r1"), l)).toEqual({
      state: "needs_you",
      text: "Waiting for you in its conversation",
    });
  });

  it.each([
    [
      "waiting",
      routine({ active_run: { attempt_id: "a", session_id: "rs", state: "", stop_by: null } }),
      "Digest is waiting for your OK",
    ],
    ["blocked", BLOCKED, "Digest was blocked"],
    [
      "expired",
      routine({
        last_attempt: { attempt: { outcome: "expired", reason: "waiting_for_user" } } as never,
      }),
      "Digest expired waiting for you",
    ],
  ])("owned routine %s needs you, beating thinking", (_n, item, text) => {
    const l = live({
      sessions: { s1: view({ turns: running("thinking") }), rs: view({ cards: [{}] as never }) },
      routines: snap([item]),
    });
    expect(status(owns("r1"), l)).toEqual({ state: "needs_you", text });
  });

  it("thinking, writing, then a running owned routine", () => {
    const b = owns("r1");
    const run = routine({
      active_run: { attempt_id: "a", session_id: "rs", state: "", stop_by: null },
    });
    expect(status(b, live({ sessions: { s1: view({ turns: running("thinking") }) } })).text).toBe(
      "Thinking",
    );
    expect(status(b, live({ sessions: { s1: view({ turns: running("text") }) } })).text).toBe(
      "Writing a reply",
    );
    expect(status(b, live({ routines: snap([run]) }))).toEqual({
      state: "working",
      text: "Running Digest",
    });
  });

  it("working beats live elsewhere", () => {
    const l = live({
      sessions: { s1: view({ turns: running("text") }) },
      others: [{ session_id: "s1", entrypoint: "cli", pid: 1, title: "" }],
    });
    expect(status(bot(), l).state).toBe("working");
  });

  it("live elsewhere with and without a badge", () => {
    const other = (entrypoint: string) => [{ session_id: "s1", entrypoint, pid: 1, title: "" }];
    expect(status(bot(), live({ others: other("cli") }))).toEqual({
      state: "elsewhere",
      text: "Live elsewhere (CLI)",
    });
    expect(status(bot(), live({ others: other("") })).text).toBe("Live elsewhere");
    expect(
      status(
        bot(),
        live({
          others: other(""),
          sessionRows: [{ session_id: "s1", entrypoint: "claude-vscode" } as never],
        }),
      ).text,
    ).toBe("Live elsewhere (VS Code)");
  });

  it("live elsewhere beats a checking snapshot", () => {
    const l = live({
      others: [{ session_id: "s1", entrypoint: "cli", pid: 1, title: "" }],
      routines: snap([], { status: "loading" }),
    });
    expect(status(owns("r1"), l).state).toBe("elsewhere");
  });

  it.each(["idle", "loading"] as const)("%s snapshot with routines is Checking…", (s) => {
    expect(status(owns("r1"), live({ routines: snap([routine()], { status: s }) }))).toBe(CHECKING);
  });

  it.each([
    ["null snapshot", null],
    ["error status", snap([routine()], { status: "error", error: "x" })],
    ["ready with an error", snap([routine()], { error: "x" })],
    ["missing owned id", snap([routine({ routine_id: "other" })])],
  ])("fail closed: %s", (_n, routines) => {
    expect(status(owns("r1"), live({ routines }))).toBe(STATUS_UNAVAILABLE);
  });

  it("routine facts count only from a ready snapshot", () => {
    const l = live({ routines: snap([BLOCKED], { status: "loading" }) });
    expect(status(owns("r1"), l)).toBe(CHECKING);
  });

  it("a Bot with no routines ignores the snapshot", () => {
    expect(status(bot(), live({ routines: null })).text).toBe("Idle");
  });

  it("the earliest next owned routine beats Not started", () => {
    const l = live({
      routines: snap([
        routine({
          routine_id: "r1",
          name: "Late",
          next_run: new Date(2026, 10, 4, 18, 0).toISOString(),
        }),
        routine({
          routine_id: "r2",
          name: "Soon",
          next_run: new Date(2026, 10, 4, 12, 30).toISOString(),
        }),
      ]),
    });
    const b = bot({
      routine_ids: ["r1", "r2"],
      chat: { profile_revision: 1, started: false, archived: false },
    });
    expect(status(b, l)).toEqual({ state: "idle", text: "Next: Soon 12:30" });
  });

  it("skips the next-run text when the stamp has no label", () => {
    const l = live({ routines: snap([routine({ next_run: "garbage" })]) });
    expect(status(owns("r1"), l).text).toBe("Idle");
  });

  it("Not started beats Idle", () => {
    const b = bot({ chat: { profile_revision: 1, started: false, archived: false } });
    expect(status(b, live())).toBe(NOT_STARTED);
    expect(status(bot(), live()).text).toBe("Idle");
  });

  it.each([
    ["a live view with one finished turn", [{ status: "done", blocks: [] }]],
    [
      "a turn in the view counts even when the server still says not started (error turn)",
      [{ status: "error", blocks: [] }],
    ],
  ])("not started on the server but %s is Idle", (_n, turns) => {
    const b = bot({ chat: { profile_revision: 1, started: false, archived: false } });
    const l = live({ sessions: { s1: view({ turns: turns as never }) } });
    expect(status(b, l)).toEqual({ state: "idle", text: "Idle" });
  });

  it("not started on the server and an empty live view is still Not started", () => {
    const b = bot({ chat: { profile_revision: 1, started: false, archived: false } });
    expect(status(b, live({ sessions: { s1: view({}) } }))).toBe(NOT_STARTED);
  });
});

describe("checkerStatus", () => {
  it.each([
    [true, "unknown", STATUS_UNAVAILABLE],
    [false, "running", STATUS_UNAVAILABLE],
    [true, "running", { state: "working", text: "Checking a card" }],
    [true, "listed", { state: "idle", text: "Ready" }],
    [true, "absent", { state: "unavailable", text: "Not in this vault" }],
  ] as const)("online %s, %s", (online, checker, expected) => {
    expect(checkerStatus(live({ online, checker }))).toEqual(expected);
  });
});

describe("counts", () => {
  it("counts needs_you and working", () => {
    expect(
      statusCounts([
        { state: "needs_you", text: "" },
        { state: "working", text: "" },
        { state: "working", text: "" },
        { state: "idle", text: "" },
      ]),
    ).toEqual({ needsYou: 1, working: 2 });
  });

  it.each([
    [{ needsYou: 0, working: 0 }, ""],
    [{ needsYou: 1, working: 0 }, "1 needs you"],
    [{ needsYou: 2, working: 0 }, "2 need you"],
    [{ needsYou: 0, working: 3 }, "3 working"],
    [{ needsYou: 1, working: 3 }, "1 needs you · 3 working"],
  ])("countText %j", (counts, text) => {
    expect(countText(counts)).toBe(text);
  });
});
