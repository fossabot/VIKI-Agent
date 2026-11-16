/** Acceptance (frozen): the pure row helpers. Dates are built in local time. */
import { expect, test } from "vitest";
import {
  agentStatus,
  groupSessions,
  hereNowLabel,
  rowTitle,
  runningAgents,
  timeLabel,
} from "../../state/rows";
import type { AppSnapshot } from "../../state/store";
import { emptyView, type RoutingBlock, type SessionView } from "../../state/view";
import { agent, localIso, sessionRow } from "./fakes";

const NOW = new Date(2026, 8, 12, 18, 0).getTime(); // 2026-09-12 18:00 local

const a = sessionRow({
  session_id: "a",
  title: "Dashboard v1 → reverted",
  modified: localIso(2026, 9, 12, 17, 20),
});
const b = sessionRow({ session_id: "b", title: "Linter", modified: localIso(2026, 9, 12, 9, 5) });
const c = sessionRow({
  session_id: "c",
  title: "Yesterday's",
  modified: localIso(2026, 9, 11, 23, 59),
});
const d = sessionRow({ session_id: "d", title: "Old", modified: localIso(2026, 9, 1, 10, 0) });
const o = sessionRow({
  session_id: "o",
  title: "Other tool",
  modified: localIso(2026, 9, 12, 12, 0),
  other: true,
});

test("groupSessions: Today / Yesterday · MM-DD / Earlier, newest first, others apart", () => {
  const { groups, other } = groupSessions([d, b, o, c, a], NOW);
  expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday · 09-11", "Earlier"]);
  expect(groups.map((g) => g.rows.map((r) => r.session_id))).toEqual([["a", "b"], ["c"], ["d"]]);
  expect(other.map((r) => r.session_id)).toEqual(["o"]);
});

test("groupSessions omits empty groups and sorts the others newest first", () => {
  const o2 = sessionRow({ session_id: "o2", modified: localIso(2026, 9, 12, 13, 0), other: true });
  const { groups, other } = groupSessions([o, d, o2], NOW);
  expect(groups.map((g) => g.label)).toEqual(["Earlier"]);
  expect(other.map((r) => r.session_id)).toEqual(["o2", "o"]);
  expect(groupSessions([], NOW)).toEqual({ groups: [], other: [] });
});

test("timeLabel: HH:MM today, MM-DD otherwise, '' for nothing or garbage", () => {
  expect(timeLabel(localIso(2026, 9, 12, 9, 5), NOW)).toBe("09:05");
  expect(timeLabel(localIso(2026, 9, 12, 17, 20), NOW)).toBe("17:20");
  expect(timeLabel(localIso(2026, 9, 11, 23, 59), NOW)).toBe("09-11");
  expect(timeLabel(localIso(2026, 1, 2, 0, 0), NOW)).toBe("01-02");
  expect(timeLabel("", NOW)).toBe("");
  expect(timeLabel("not a date", NOW)).toBe("");
});

test("rowTitle falls back from title to first_prompt to Untitled session", () => {
  expect(rowTitle(a)).toBe("Dashboard v1 → reverted");
  expect(rowTitle(sessionRow({ session_id: "x", first_prompt: "hello" }))).toBe("hello");
  expect(rowTitle(sessionRow({ session_id: "x" }))).toBe("Untitled session");
});

function routing(id: string, name: string, status: RoutingBlock["status"]): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: name,
    model: "",
    description: "",
    reason: "",
    status,
    tools: [],
  };
}

function viewWith(id: string, blocks: RoutingBlock[][]): SessionView {
  return {
    ...emptyView(id),
    turns: blocks.map((turnBlocks, i) => ({
      id: `t${i}`,
      user: null,
      blocks: turnBlocks,
      status: "running" as const,
      usage: null,
      durationMs: 0,
      history: false,
    })),
  };
}

test("runningAgents: distinct running routing agents over every view, first seen first", () => {
  const snapshot: AppSnapshot = {
    sessions: {
      s1: viewWith("s1", [
        [routing("r1", "researcher", "running"), routing("r2", "librarian", "completed")],
        [routing("r3", "researcher", "running")],
      ]),
      s2: viewWith("s2", [
        [routing("r4", "curator", "running"), routing("r5", "connector", "failed")],
      ]),
    },
    others: [],
    status: "open",
  };
  expect(runningAgents(snapshot)).toEqual(["researcher", "curator"]);
  expect(runningAgents({ sessions: {}, others: [], status: "open" })).toEqual([]);
});

test("agentStatus: here now, never, or idle · <time>", () => {
  expect(agentStatus(agent({ name: "researcher" }), ["researcher"], NOW)).toBe("here now");
  expect(agentStatus(agent({ name: "connector" }), ["researcher"], NOW)).toBe("never");
  expect(
    agentStatus(agent({ name: "librarian", last_run: localIso(2026, 8, 31, 8, 0) }), [], NOW),
  ).toBe("idle · 08-31");
  expect(
    agentStatus(agent({ name: "curator", last_run: localIso(2026, 9, 12, 8, 0) }), [], NOW),
  ).toBe("idle · 08:00");
});

test("hereNowLabel counts VIKI and the running agents", () => {
  expect(hereNowLabel(true, [])).toBe("1 here now");
  expect(hereNowLabel(true, ["researcher", "curator"])).toBe("3 here now");
  expect(hereNowLabel(false, ["researcher"])).toBe("1 here now");
  expect(hereNowLabel(false, [])).toBe("");
});
