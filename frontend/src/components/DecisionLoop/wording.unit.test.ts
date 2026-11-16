import { expect, test } from "vitest";
import { decisionLoopExamples } from "../../clients/decisionLoop/fake";
import {
  ACTION_TYPES,
  CANDIDATES,
  FEEDBACK_REASONS,
  KINDS,
  type LauncherView,
  MODEL_STATES,
  type ModelState,
} from "../../clients/decisionLoop/types";
import {
  ACTION_LABELS,
  CANDIDATE_LABELS,
  candidateLabel,
  DAILY_CAP_OPTIONS,
  formatMoney,
  formatNumber,
  KIND_LABELS,
  localClock,
  REASON_LABELS,
  RULES_ONLY_MODEL_STATES,
  readShadowSetting,
  relativeTime,
  SHADOW_OPTIONS,
  type StatusLineView,
  statusLine,
} from "./wording";

// --- word maps ---

test("KIND_LABELS covers exactly the five kinds, with the exact approved words", () => {
  expect(Object.keys(KIND_LABELS).sort()).toEqual([...KINDS].sort());
  expect(KIND_LABELS).toEqual({
    inform: "For your information",
    ask: "Question",
    suggest: "Suggestion",
    ready: "Ready for you",
    report: "Report",
  });
});

test("ACTION_LABELS covers exactly the twenty-one action types, matching the backend's own default wording", () => {
  expect(Object.keys(ACTION_LABELS).sort()).toEqual([...ACTION_TYPES].sort());
  expect(ACTION_LABELS).toEqual({
    open_chat: "Open chat",
    open_note: "Open note",
    open_approvals: "Review approvals",
    open_inbox: "Open inbox",
    open_routine: "Open routine",
    open_runs: "Open runs",
    open_context: "See why",
    open_vault: "Open vault",
    open_messaging: "Open Messaging",
    open_code: "Open in Code",
    open_triage: "Triage inbox",
    start_prepare: "Prepare a plan",
    start_run: "Start a chat to do it",
    stop_mission: "Stop",
    start_seat: "Start",
    stop_seat: "Stop",
    draft_chat: "Draft it in this chat",
    draft_new: "Draft it in a new chat",
    run_checks: "Run checks",
    stop_checks: "Stop",
    suggest_inbox: "Suggest destinations",
  });
});

test("REASON_LABELS covers exactly the four feedback reasons", () => {
  expect(Object.keys(REASON_LABELS).sort()).toEqual([...FEEDBACK_REASONS].sort());
  expect(REASON_LABELS).toEqual({
    out_of_date: "Out of date",
    already_knew: "Already knew",
    wrong_topic: "Wrong topic",
    bad_timing: "Bad timing",
  });
});

test("CANDIDATE_LABELS covers the thirty-six candidates plus an unknown fallback", () => {
  expect(Object.keys(CANDIDATE_LABELS).sort()).toEqual([...CANDIDATES, "unknown"].sort());
  expect(CANDIDATE_LABELS).toEqual({
    approvals: "Approvals",
    tasks_due: "Tasks due",
    agent_runs: "Chats and runs",
    inbox: "Inbox",
    uncommitted: "Uncommitted changes",
    routine: "Routines",
    mission_report: "Chats VIKI started",
    card_needs_you: "Cards that need you",
    card_stale: "Cards that stopped moving",
    seat_waiting: "Bot runs waiting for you",
    seat_working: "Bots working on cards",
    seat_start: "Bots that could start",
    seat_report: "Reports on Bot runs",
    vault_recovery: "Recovery in the vault",
    memory_drift: "Memories whose sources changed",
    bridge_stopped: "Stopped messaging bridges",
    chat_waiting: "Chats waiting for you",
    send_uncertain: "Messages that may not have arrived",
    mcp_context: "MCP servers filling context",
    skill_offer: "Skill offers",
    skill_suggest: "Skill suggestions",
    learn_source: "Sources to learn",
    retro_due: "Retro suggestions",
    question_tune: "Question tuning",
    note_link: "Note links",
    note_superseded: "Superseded notes",
    tasks_ticked: "Ticked tasks",
    code_look: "Code changes left uncommitted",
    long_task_look: "Second look at a long task",
    correction_look: "Second look after a correction",
    stalled_project: "Stalled project",
    overdue_tasks: "Overdue tasks to check",
    context_handoff: "Handoff for a full chat",
    source_note: "Notes for fetched pages",
    commit_checks: "Checks on a new commit",
    inbox_triage: "Inbox suggestions",
    unknown: "Other",
  });
  // The documented lookup pattern a caller follows for a topic outside the known set (e.g. the
  // wire's "unwatched", or a future candidate this build does not list yet).
  expect(CANDIDATE_LABELS.unwatched ?? CANDIDATE_LABELS.unknown).toBe("Other");
});

test("candidateLabel reads every known candidate from CANDIDATE_LABELS and falls back to Other", () => {
  for (const candidate of CANDIDATES) {
    expect(candidateLabel(candidate)).toBe(CANDIDATE_LABELS[candidate]);
  }
  expect(candidateLabel("something_new")).toBe("Other");
});

// --- relativeTime ---

test("relativeTime reads just under a minute as just now", () => {
  expect(relativeTime(1_000, 1_000)).toBe("just now");
  expect(relativeTime(1_000, 1_000 + 59_999)).toBe("just now");
});

test("relativeTime counts whole minutes under an hour", () => {
  expect(relativeTime(0, 60_000)).toBe("1 min ago");
  expect(relativeTime(0, 120_000)).toBe("2 min ago");
  expect(relativeTime(0, 3_599_999)).toBe("59 min ago");
});

test("relativeTime counts whole hours under a day", () => {
  expect(relativeTime(0, 3_600_000)).toBe("1 hour ago");
  expect(relativeTime(0, 2 * 3_600_000)).toBe("2 hours ago");
  expect(relativeTime(0, 23 * 3_600_000)).toBe("23 hours ago");
});

test("relativeTime reads a day to two days as yesterday", () => {
  expect(relativeTime(0, 24 * 3_600_000)).toBe("yesterday");
  expect(relativeTime(0, 2 * 24 * 3_600_000 - 1)).toBe("yesterday");
});

test("relativeTime gives a short M/D date at two days and beyond", () => {
  const at = Date.parse("2026-10-01T09:00:00.000Z");
  const now = at + 2 * 24 * 3_600_000;
  const expected = `${new Date(at).getMonth() + 1}/${new Date(at).getDate()}`;
  expect(relativeTime(at, now)).toBe(expected);
  // An ISO stamp and its equivalent epoch-ms number give the same answer.
  expect(relativeTime(new Date(at).toISOString(), now)).toBe(expected);
});

test("relativeTime gives '' for a stamp that does not parse", () => {
  expect(relativeTime("not a date", 1_000)).toBe("");
});

test("relativeTime clamps a stamp after now (clock skew) to just now", () => {
  expect(relativeTime(10_000, 1_000)).toBe("just now");
});

// --- shared helpers ---

test("formatMoney shows cents, and < $0.01 below a cent", () => {
  expect(formatMoney(0.07)).toBe("$0.07");
  expect(formatMoney(0.004)).toBe("< $0.01");
});

test("formatNumber groups thousands in en-US", () => {
  expect(formatNumber(1234567)).toBe("1,234,567");
});

test("DAILY_CAP_OPTIONS lists the five caps", () => {
  expect([...DAILY_CAP_OPTIONS]).toEqual([0.1, 0.25, 0.5, 1, 2]);
});

test("SHADOW_OPTIONS labels the four settings in order", () => {
  expect(SHADOW_OPTIONS.map((o) => [o.value, o.label])).toEqual([
    ["off", "Off"],
    ["pinned", "Pinned"],
    ["latest", "Latest stable"],
    ["preview", "Preview"],
  ]);
});

test("readShadowSetting reads a valid value and defaults to off when missing or invalid", () => {
  expect(readShadowSetting({ "decision.shadow": "latest" })).toBe("latest");
  expect(readShadowSetting({})).toBe("off");
  expect(readShadowSetting({ "decision.shadow": "bogus" })).toBe("off");
  expect(readShadowSetting({ "decision.shadow": 3 })).toBe("off");
});

test("RULES_ONLY_MODEL_STATES holds every model state but ready", () => {
  expect([...RULES_ONLY_MODEL_STATES].sort()).toEqual(
    MODEL_STATES.filter((s) => s !== "ready").sort(),
  );
});

test("localClock gives '' for a stamp that does not parse", () => {
  expect(localClock("not a date")).toBe("");
});

// --- statusLine ---

test("statusLine reads a plain StatusLineView object", () => {
  const view: StatusLineView = {
    state: "on",
    paused_until: null,
    focus_until: null,
    checked_at: null,
    open: 2,
    plans_waiting: 0,
    model: "ready",
  };
  expect(statusLine(view, NOW_MS)).toBe("Watching · 2 open");
});

function launcherWith(overrides: Partial<LauncherView> = {}): LauncherView {
  return { ...decisionLoopExamples().launcher, ...overrides };
}

/** "HH:MM" in local time, computed the same way the implementation does, so the expectation
 * does not hard-code a timezone-dependent string. */
function hhmm(stamp: string): string {
  const at = new Date(stamp);
  const pad2 = (n: number) => String(n).padStart(2, "0");
  return `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
}

const CHECKED_AT = "2026-10-21T14:00:00.000Z";
const NOW_MS = Date.parse(CHECKED_AT) + 2 * 60_000;

test("statusLine reads Watching with the open count and a relative checked time, while on", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: CHECKED_AT,
    paused_until: null,
    focus_until: null,
    model: "ready",
  });
  expect(statusLine(view, NOW_MS)).toBe("Watching · 3 open · checked 2 min ago");
});

test("statusLine omits the checked clause when checked_at is null", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
  });
  expect(statusLine(view, NOW_MS)).toBe("Watching · 3 open");
});

test("statusLine reads Proactive VIKI is off, while off", () => {
  const view = launcherWith({ state: "off", paused_until: null, focus_until: null });
  expect(statusLine(view, NOW_MS)).toBe("Proactive VIKI is off");
});

test("statusLine reads Paused until the local time, while paused", () => {
  const pausedUntil = "2026-10-21T15:00:00.000Z";
  const view = launcherWith({ state: "paused", paused_until: pausedUntil, focus_until: null });
  expect(statusLine(view, NOW_MS)).toBe(`Paused until ${hhmm(pausedUntil)}`);
});

test("statusLine falls back to a bare Paused if paused_until is somehow missing", () => {
  const view = launcherWith({ state: "paused", paused_until: null, focus_until: null });
  expect(statusLine(view, NOW_MS)).toBe("Paused");
});

test("statusLine appends Focus until the local time when focus_until is set", () => {
  const focusUntil = "2026-10-21T16:00:00.000Z";
  const view = launcherWith({
    state: "on",
    open: 1,
    checked_at: null,
    paused_until: null,
    focus_until: focusUntil,
    model: "ready",
  });
  expect(statusLine(view, NOW_MS)).toBe(`Watching · 1 open · Focus until ${hhmm(focusUntil)}`);
});

test("statusLine appends rules only for every rules-only model state, not for ready", () => {
  const rulesOnly: readonly ModelState[] = [
    "rules",
    "no_key",
    "key_rejected",
    "capped",
    "unreachable",
  ];
  for (const model of rulesOnly) {
    const view = launcherWith({
      state: "on",
      open: 1,
      checked_at: null,
      paused_until: null,
      focus_until: null,
      model,
    });
    expect(statusLine(view, NOW_MS), model).toBe("Watching · 1 open · rules only");
  }
  const ready = launcherWith({
    state: "on",
    open: 1,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
  });
  expect(statusLine(ready, NOW_MS)).toBe("Watching · 1 open");
});

test("statusLine combines the base line with both Focus and rules-only suffixes", () => {
  const focusUntil = "2026-10-21T16:00:00.000Z";
  const view = launcherWith({
    state: "on",
    open: 2,
    checked_at: CHECKED_AT,
    paused_until: null,
    focus_until: focusUntil,
    model: "capped",
  });
  expect(statusLine(view, NOW_MS)).toBe(
    `Watching · 2 open · checked 2 min ago · Focus until ${hhmm(focusUntil)} · rules only`,
  );
});

// --- statusLine: plans_waiting ---

test("statusLine joins 1 plan waits for you right after Watching, while on", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
    plans_waiting: 1,
  });
  expect(statusLine(view, NOW_MS)).toBe("Watching · 1 plan waits for you · 3 open");
});

test("statusLine reads N plans wait for you above one", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
    plans_waiting: 4,
  });
  expect(statusLine(view, NOW_MS)).toBe("Watching · 4 plans wait for you · 3 open");
});

test("statusLine omits the plan-waiting clause at 0", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: null,
    paused_until: null,
    focus_until: null,
    model: "ready",
    plans_waiting: 0,
  });
  expect(statusLine(view, NOW_MS)).toBe("Watching · 3 open");
});

test("statusLine combines the plan-waiting clause with the checked clause", () => {
  const view = launcherWith({
    state: "on",
    open: 3,
    checked_at: CHECKED_AT,
    paused_until: null,
    focus_until: null,
    model: "ready",
    plans_waiting: 1,
  });
  expect(statusLine(view, NOW_MS)).toBe(
    "Watching · 1 plan waits for you · 3 open · checked 2 min ago",
  );
});

test("statusLine never shows the plan-waiting clause while off or paused", () => {
  const off = launcherWith({
    state: "off",
    paused_until: null,
    focus_until: null,
    plans_waiting: 2,
  });
  expect(statusLine(off, NOW_MS)).toBe("Proactive VIKI is off");
  const pausedUntil = "2026-10-21T15:00:00.000Z";
  const paused = launcherWith({
    state: "paused",
    paused_until: pausedUntil,
    focus_until: null,
    plans_waiting: 2,
  });
  expect(statusLine(paused, NOW_MS)).toBe(`Paused until ${hhmm(pausedUntil)}`);
});
