import { describe, expect, it } from "vitest";
import { isQuestionProposalItem } from "../../state/decisionsApi";
import { decisionLoopExamples } from "./fake";
import {
  parseCallDetail,
  parseCalls,
  parseChangeActResult,
  parseDataDeleted,
  parseDataRecord,
  parseDataRecords,
  parseDataSummary,
  parseDeleted,
  parseError,
  parseLauncher,
  parseMission,
  parsePage,
  parsePageMissions,
  parseProposalResult,
  parseQuestionProposal,
  parseTestResult,
} from "./parse";
import { TOPICS } from "./types";

const EXAMPLES = decisionLoopExamples();
function some<T>(found: T | undefined): T {
  if (found === undefined) throw new Error("the examples lack it");
  return found;
}
const ALL_PARSERS: ((value: unknown) => unknown)[] = [
  parseLauncher,
  parsePage,
  parseCalls,
  parseCallDetail,
  parseMission,
  parseTestResult,
  parseError,
  parsePageMissions,
  parseDeleted,
  parseQuestionProposal,
  parseChangeActResult,
  parseDataDeleted,
  parseDataRecord,
  parseDataRecords,
  parseDataSummary,
];

describe("parses every example", () => {
  it("launcher", () => {
    expect(parseLauncher(EXAMPLES.launcher)).toEqual(EXAMPLES.launcher);
  });
  it("page", () => {
    expect(parsePage(EXAMPLES.page)).toEqual(EXAMPLES.page);
  });
  it("parses the learning block exactly", () => {
    expect(parsePage(EXAMPLES.page)?.learning).toEqual(EXAMPLES.page.learning);
  });
  it("reads past the Learning members an earlier server sent", () => {
    // Its Japanese floor and its calibration's per-language scores, whatever they hold.
    const older = {
      ...EXAMPLES.page.learning,
      ja_floor: { start: 0.1, now: 0.1, support: 10 },
      calibration: {
        ...EXAMPLES.page.learning.calibration,
        en: { n: 50, brier: 0.17 },
        ja: { n: 14, brier: null },
      },
    };
    expect(parsePage({ ...EXAMPLES.page, learning: older })?.learning).toEqual(older);
    const odd = { ...older, ja_floor: "x", calibration: { ...older.calibration, ja: -1 } };
    expect(parsePage({ ...EXAMPLES.page, learning: odd })?.learning).toEqual(odd);
    // Any other member still fails the parse.
    const unknown = { ...older, calibration: { ...older.calibration, fr: { n: 1, brier: 0.1 } } };
    expect(parsePage({ ...EXAMPLES.page, learning: unknown })).toBeNull();
  });
  it("calls", () => {
    expect(parseCalls(EXAMPLES.calls)).toEqual(EXAMPLES.calls);
  });
  it("call_detail", () => {
    expect(parseCallDetail(EXAMPLES.call_detail)).toEqual(EXAMPLES.call_detail);
  });
  it("mission", () => {
    expect(parseMission(EXAMPLES.mission)).toEqual(EXAMPLES.mission);
  });
  it("test_result", () => {
    expect(parseTestResult(EXAMPLES.test_result)).toEqual(EXAMPLES.test_result);
  });
  it("error", () => {
    expect(parseError(EXAMPLES.error)).toEqual(EXAMPLES.error);
  });
  it("page.missions (GET missions, POST missions/stop)", () => {
    expect(parsePageMissions(EXAMPLES.page.missions)).toEqual(EXAMPLES.page.missions);
  });
  it("missions.hold is null or an exact {until: stamp | null}", () => {
    const base = EXAMPLES.page.missions;
    for (const hold of [{ until: "2026-10-21T14:00:00.000Z" }, { until: null }, null]) {
      expect(parsePageMissions({ ...base, hold })).not.toBeNull();
    }
    for (const hold of [{ until: "14:00" }, {}, { until: null, extra: 1 }, "x"]) {
      expect(parsePageMissions({ ...base, hold })).toBeNull();
    }
  });
  it("missions.team is an exact {today, limit, running} of counts", () => {
    const base = EXAMPLES.page.missions;
    const { today, limit } = base.team;
    expect(parsePageMissions({ ...base, team: { today, limit, running: 0 } })).not.toBeNull();
    const bad: unknown[] = [
      { today, limit },
      { today, limit, running: 1, extra: 1 },
      { today, limit, running: -1 },
      null,
    ];
    for (const team of bad) expect(parsePageMissions({ ...base, team })).toBeNull();
    const { team: _team, ...withoutTeam } = base;
    expect(parsePageMissions(withoutTeam)).toBeNull();
  });
  it("missions.look is an exact {today, limit} of counts", () => {
    const base = EXAMPLES.page.missions;
    expect(parsePageMissions({ ...base, look: { today: 0, limit: 3 } })).not.toBeNull();
    const bad: unknown[] = [
      { today: 0 },
      { today: 0, limit: 3, extra: 1 },
      { today: -1, limit: 3 },
      null,
    ];
    for (const look of bad) expect(parsePageMissions({ ...base, look })).toBeNull();
    const { look: _look, ...withoutLook } = base;
    expect(parsePageMissions(withoutLook)).toBeNull();
  });
  it("missions.second_look is an exact {today, limit} of counts", () => {
    const base = EXAMPLES.page.missions;
    expect(parsePageMissions({ ...base, second_look: { today: 0, limit: 3 } })).not.toBeNull();
    const bad: unknown[] = [
      { today: 0 },
      { today: 0, limit: 3, extra: 1 },
      { today: -1, limit: 3 },
      null,
    ];
    for (const second_look of bad) expect(parsePageMissions({ ...base, second_look })).toBeNull();
    const { second_look: _second, ...without } = base;
    expect(parsePageMissions(without)).toBeNull();
  });
  it("missions.project_plan is an exact {today, limit} of counts", () => {
    const base = EXAMPLES.page.missions;
    expect(parsePageMissions({ ...base, project_plan: { today: 0, limit: 3 } })).not.toBeNull();
    const bad: unknown[] = [
      { today: 0 },
      { today: 0, limit: 3, extra: 1 },
      { today: -1, limit: 3 },
      null,
    ];
    for (const project_plan of bad) expect(parsePageMissions({ ...base, project_plan })).toBeNull();
    const { project_plan: _plan, ...without } = base;
    expect(parsePageMissions(without)).toBeNull();
  });
  it("missions.checks is null or an exact {today, limit, running} of counts", () => {
    const base = EXAMPLES.page.missions;
    expect(parsePageMissions({ ...base, checks: null })).not.toBeNull();
    expect(
      parsePageMissions({ ...base, checks: { today: 0, limit: 3, running: 0 } }),
    ).not.toBeNull();
    const bad: unknown[] = [
      { today: 0, limit: 3 },
      { today: 0, limit: 3, running: 0, extra: 1 },
      { today: -1, limit: 3, running: 0 },
    ];
    for (const checks of bad) expect(parsePageMissions({ ...base, checks })).toBeNull();
    const { checks: _checks, ...without } = base;
    expect(parsePageMissions(without)).toBeNull();
  });
  it("learning needs saved_checks", () => {
    const { saved_checks: _gone, ...learning } = EXAMPLES.page.learning;
    expect(parsePage({ ...EXAMPLES.page, learning })).toBeNull();
  });
  it("checks actions need a workspace_id", () => {
    const item = some(EXAMPLES.launcher.items.find((i) => i.candidate === "commit_checks"));
    for (const type of ["run_checks", "stop_checks"]) {
      const launcher = (action: object) => ({
        ...EXAMPLES.launcher,
        items: [{ ...item, actions: [action] }],
      });
      const workspace_id = some(item.actions[0]).workspace_id;
      const good = { type, label: "Go", primary: true, workspace_id };
      expect(parseLauncher(launcher(good))).not.toBeNull();
      const { workspace_id: _id, ...bare } = good;
      expect(parseLauncher(launcher(bare))).toBeNull();
    }
  });
  it("seat actions need a routine_id", () => {
    const item = some(EXAMPLES.launcher.items.find((i) => i.candidate === "seat_start"));
    for (const type of ["start_seat", "stop_seat"]) {
      const launcher = (action: object) => ({
        ...EXAMPLES.launcher,
        items: [{ ...item, actions: [action] }],
      });
      const good = {
        type,
        label: "Go",
        primary: true,
        routine_id: some(item.actions[0]).routine_id,
      };
      expect(parseLauncher(launcher(good))).not.toBeNull();
      const { routine_id: _id, ...bare } = good;
      expect(parseLauncher(launcher(bare))).toBeNull();
    }
  });
  it("a deleted count (DELETE episodes)", () => {
    expect(parseDeleted({ deleted: 7 })).toEqual({ deleted: 7 });
  });
  it("question_proposal (GET proposals/{id})", () => {
    expect(parseQuestionProposal(EXAMPLES.question_proposal)).toEqual(EXAMPLES.question_proposal);
  });
  it("change_act (POST changes/{id})", () => {
    expect(parseChangeActResult(EXAMPLES.change_act)).toEqual(EXAMPLES.change_act);
  });
  it("decision data responses", () => {
    expect(parseDataSummary(EXAMPLES.data_summary)).toEqual(EXAMPLES.data_summary);
    expect(parseDataRecords(EXAMPLES.data_decisions)).toEqual(EXAMPLES.data_decisions);
    expect(parseDataRecords(EXAMPLES.data_suggestions)).toEqual(EXAMPLES.data_suggestions);
    expect(parseDataRecords(EXAMPLES.data_calls)).toEqual(EXAMPLES.data_calls);
    expect(parseDataRecord(EXAMPLES.data_decision)).toEqual(EXAMPLES.data_decision);
    expect(parseDataRecord(EXAMPLES.data_suggestion)).toEqual(EXAMPLES.data_suggestion);
    expect(parseDataDeleted(EXAMPLES.data_deleted)).toEqual(EXAMPLES.data_deleted);
  });
});

it("every parser refuses a value that is not the right kind of object", () => {
  for (const parse of ALL_PARSERS) {
    for (const bad of [null, undefined, "x", 42, true, [], {}]) {
      expect(parse(bad)).toBeNull();
    }
  }
});

// ---- a deterministic, programmatic one-field-mutation sweep over every example ----

/** Longer than every string bound this file enforces (the biggest is 20,000), and the wrong
 * shape for every stamp, uuid, item key, display version and enum: wrong for any string field,
 * whatever that field's own rule is. */
const JUNK = "z".repeat(20_001);

type Path = (string | number)[];

/** A deep clone of `root` with the single value at `path` replaced by `value`. */
function withAt(root: unknown, path: Path, value: unknown): unknown {
  if (path.length === 0) return value;
  const next = structuredClone(root) as Record<string, unknown>;
  let node: Record<string, unknown> = next;
  for (let i = 0; i < path.length - 1; i++) {
    node = node[String(path[i])] as Record<string, unknown>;
  }
  node[String(path[path.length - 1])] = value;
  return next;
}

/**
 * Every one-field mutation reachable at and under `node` (found at `path` in `root`), appended to
 * `out` as `{label, value}`: an object gains one unexpected key, and loses each of its own keys
 * one at a time; a string becomes `JUNK` and becomes a number; a number becomes `-1` and becomes
 * a string; a boolean or a `null` becomes `JUNK`. Recurses into every object member and every
 * array element, so one call covers the whole example. Deterministic: same input, same list,
 * same order, every time.
 */
function mutations(
  root: unknown,
  path: Path,
  node: unknown,
  out: { label: string; value: unknown }[],
): void {
  const label = path.length === 0 ? "the root" : path.join(".");
  const at = (value: unknown): unknown => withAt(root, path, value);
  if (Array.isArray(node)) {
    node.forEach((element, index) => {
      mutations(root, [...path, index], element, out);
    });
    return;
  }
  if (node !== null && typeof node === "object") {
    const row = node as Record<string, unknown>;
    const keys = Object.keys(row);
    out.push({
      label: `${label} gains an unexpected key`,
      value: at({ ...row, zz_unexpected: true }),
    });
    for (const key of keys) {
      const without: Record<string, unknown> = { ...row };
      delete without[key];
      out.push({ label: `${label}.${key} is dropped`, value: at(without) });
    }
    for (const key of keys) mutations(root, [...path, key], row[key], out);
    return;
  }
  if (typeof node === "string") {
    out.push({ label: `${label} is too long / the wrong shape`, value: at(JUNK) });
    out.push({ label: `${label} is a number`, value: at(1) });
    return;
  }
  if (typeof node === "number") {
    out.push({ label: `${label} is negative`, value: at(-1) });
    out.push({ label: `${label} is a string`, value: at("x") });
    return;
  }
  if (typeof node === "boolean") {
    out.push({ label: `${label} is a string`, value: at("x") });
    return;
  }
  // node === null
  out.push({ label: `${label} is a junk string instead of null`, value: at(JUNK) });
}

/**
 * The mutation labels that legitimately still parse:
 * - dropping an evidence chip's only `session_id` (its chat-id field) leaves exactly the
 *   plain-label shape `itemEvidence` also accepts (`{label}`, with neither a note pair nor a
 *   chat id), so the item, and the launcher it sits in, both stay valid;
 * - `learning.bias` and a topic's own fitted offset are the only learning numbers this client
 *   accepts negative (bounds [-3, 1] and [-1.5, 1.5]), so the
 *   sweep's generic "becomes -1" mutation on them is still in range and the page stays valid.
 * Every other generated mutation is expected to fail the whole parse.
 */
const EXPECTED_TO_STILL_PARSE =
  /evidence\.\d+\.session_id is dropped|learning\.bias\.(start|now) is negative|learning\.topics\.\d+\.(start|now) is negative/;

/** Runs every one-field mutation of `example` through `parse` and asserts each is refused,
 * except the one documented case above, which this asserts still parses. */
function sweep(name: string, parse: (value: unknown) => unknown, example: unknown): void {
  const cases: { label: string; value: unknown }[] = [];
  mutations(example, [], example, cases);
  it.each(cases.map(({ label, value }) => [label, value] as const))(
    `${name}: %s`,
    (label, mutant) => {
      if (EXPECTED_TO_STILL_PARSE.test(label)) {
        expect(parse(mutant)).not.toBeNull();
      } else {
        expect(parse(mutant)).toBeNull();
      }
    },
  );
}

describe("one-field mutations are all rejected", () => {
  sweep("launcher", parseLauncher, EXAMPLES.launcher);
  sweep("page", parsePage, EXAMPLES.page);
  sweep("calls", parseCalls, EXAMPLES.calls);
  sweep("call_detail", parseCallDetail, EXAMPLES.call_detail);
  sweep("mission", parseMission, EXAMPLES.mission);
  sweep("test_result", parseTestResult, EXAMPLES.test_result);
  sweep("error", parseError, EXAMPLES.error);
  sweep(
    "page.missions (GET missions, POST missions/stop)",
    parsePageMissions,
    EXAMPLES.page.missions,
  );
  sweep("deleted (DELETE episodes)", parseDeleted, { deleted: 7 });
  sweep(
    "question_proposal (GET proposals/{id})",
    parseQuestionProposal,
    EXAMPLES.question_proposal,
  );
  sweep("change_act (POST changes/{id})", parseChangeActResult, EXAMPLES.change_act);
  sweep("data_summary", parseDataSummary, EXAMPLES.data_summary);
  sweep("data_decisions", parseDataRecords, EXAMPLES.data_decisions);
  sweep("data_suggestions", parseDataRecords, EXAMPLES.data_suggestions);
  sweep("data_calls", parseDataRecords, EXAMPLES.data_calls);
  sweep("data_decision", parseDataRecord, EXAMPLES.data_decision);
  sweep("data_suggestion", parseDataRecord, EXAMPLES.data_suggestion);
  sweep("data_deleted", parseDataDeleted, EXAMPLES.data_deleted);
});

// ---- named array bounds: one element past the ceiling is refused ----

describe("named array bounds are enforced", () => {
  it("launcher.items stops at 50", () => {
    const items = Array.from({ length: 51 }, () => structuredClone(EXAMPLES.launcher.items[0]));
    expect(parseLauncher({ ...EXAMPLES.launcher, items })).toBeNull();
  });
  it("launcher.started stops at 50", () => {
    const started = Array.from({ length: 51 }, () => EXAMPLES.launcher.started[0]);
    expect(parseLauncher({ ...EXAMPLES.launcher, started })).toBeNull();
  });
  it("calls.calls stops at 20", () => {
    const calls = Array.from({ length: 21 }, () => structuredClone(EXAMPLES.calls.calls[0]));
    expect(parseCalls({ ...EXAMPLES.calls, calls })).toBeNull();
  });
  it("page.recent stops at 20", () => {
    const recent = Array.from({ length: 21 }, () => structuredClone(EXAMPLES.page.recent[0]));
    expect(parsePage({ ...EXAMPLES.page, recent })).toBeNull();
  });
  it("page.held_back stops at 50", () => {
    const held_back = Array.from({ length: 51 }, () => structuredClone(EXAMPLES.page.held_back[0]));
    expect(parsePage({ ...EXAMPLES.page, held_back })).toBeNull();
  });
  it("data summary days and topics stop at their vocabularies", () => {
    const days = Array.from({ length: 367 }, () => structuredClone(EXAMPLES.data_summary.days[0]));
    const topics = Array.from({ length: TOPICS.length + 1 }, () =>
      structuredClone(EXAMPLES.data_summary.topics[0]),
    );
    expect(parseDataSummary({ ...EXAMPLES.data_summary, days })).toBeNull();
    expect(parseDataSummary({ ...EXAMPLES.data_summary, topics })).toBeNull();
  });
  it("data records cap rows at 50 and calls at 20", () => {
    const rows = Array.from({ length: 51 }, () => structuredClone(EXAMPLES.data_decisions.rows[0]));
    const calls = Array.from({ length: 21 }, () => structuredClone(EXAMPLES.data_calls.rows[0]));
    expect(parseDataRecords({ ...EXAMPLES.data_decisions, rows })).toBeNull();
    expect(parseDataRecords({ ...EXAMPLES.data_calls, rows: calls })).toBeNull();
  });
  it("data evidence caps at 20", () => {
    const evidence = Array.from({ length: 21 }, () =>
      structuredClone(EXAMPLES.data_decision.evidence[0]),
    );
    expect(parseDataRecord({ ...EXAMPLES.data_decision, evidence })).toBeNull();
  });
  it("data text uses the server's title, why, and what bounds", () => {
    expect(
      parseDataRecords({
        ...EXAMPLES.data_calls,
        rows: [{ ...EXAMPLES.data_calls.rows[0], title: "x".repeat(401) }],
      }),
    ).toBeNull();
    expect(parseDataRecord({ ...EXAMPLES.data_decision, why: "x".repeat(401) })).toBeNull();
    expect(parseDataRecord({ ...EXAMPLES.data_suggestion, what: "x".repeat(301) })).toBeNull();
  });
  it("data summary days use an exact calendar-date shape", () => {
    expect(
      parseDataSummary({
        ...EXAMPLES.data_summary,
        days: [{ ...EXAMPLES.data_summary.days[0], day: "2026-1-21" }],
      }),
    ).toBeNull();
  });
});

it("data records refuse rows that do not match their view kind", () => {
  expect(
    parseDataRecords({ ...EXAMPLES.data_decisions, rows: EXAMPLES.data_suggestions.rows }),
  ).toBeNull();
  expect(
    parseDataRecords({ ...EXAMPLES.data_suggestions, rows: EXAMPLES.data_decisions.rows }),
  ).toBeNull();
});

// ---- the Learning block's own fixed-size arrays: boundedArray (length only) plus each
// element's own shape check would let a dropped, doubled or substituted member slip through ----

describe("learningView refuses an incomplete or duplicated Learning array", () => {
  function withLearning(patch: Partial<typeof EXAMPLES.page.learning>): unknown {
    return { ...EXAMPLES.page, learning: { ...EXAMPLES.page.learning, ...patch } };
  }

  it("refuses learning.reach missing a level (the reviewer's probe: alert dropped, 3 of 4)", () => {
    const reach = EXAMPLES.page.learning.reach.filter((row) => row.level !== "alert");
    expect(parsePage(withLearning({ reach }))).toBeNull();
  });

  it("refuses learning.questions missing one (the reviewer's probe: 6 of 7)", () => {
    const questions = EXAMPLES.page.learning.questions.slice(1);
    expect(parsePage(withLearning({ questions }))).toBeNull();
  });

  it("refuses a duplicated learning.weights name at length 5 (the reviewer's probe)", () => {
    const original = EXAMPLES.page.learning.weights;
    const first = some(original[0]);
    const weights = original.map((weight, index) => (index === 1 ? { ...first } : weight));
    expect(weights).toHaveLength(original.length); // still 5; "news" is gone, "goal_link" twice
    expect(parsePage(withLearning({ weights }))).toBeNull();
  });

  it("refuses an empty learning.questions array", () => {
    expect(parsePage(withLearning({ questions: [] }))).toBeNull();
  });

  it("refuses a duplicated learning.topics candidate (one lost, length unchanged)", () => {
    const original = EXAMPLES.page.learning.topics;
    const second = some(original[1]);
    const topics = original.map((topic, index) => (index === 0 ? { ...second } : topic));
    expect(topics).toHaveLength(original.length); // still 11; "approvals" gone, "tasks_due" twice
    expect(parsePage(withLearning({ topics }))).toBeNull();
  });

  it("refuses a calibration bin with a wrong edge", () => {
    const original = EXAMPLES.page.learning.calibration.bins;
    const bins = original.map((bin, index) => (index === 0 ? { ...bin, high: 0.25 } : bin));
    const calibration = { ...EXAMPLES.page.learning.calibration, bins };
    expect(parsePage(withLearning({ calibration }))).toBeNull();
  });
});

describe("learningView refuses a bad second_looks block", () => {
  const good = EXAMPLES.page.learning.second_looks;
  function withSecondLooks(second_looks: unknown): unknown {
    return { ...EXAMPLES.page, learning: { ...EXAMPLES.page.learning, second_looks } };
  }
  it("refuses a learning block without second_looks", () => {
    const { second_looks: _gone, ...learning } = EXAMPLES.page.learning;
    expect(parsePage({ ...EXAMPLES.page, learning })).toBeNull();
  });
  it("refuses triggers out of order", () => {
    expect(
      parsePage(withSecondLooks({ ...good, triggers: [...good.triggers].reverse() })),
    ).toBeNull();
  });
  it("refuses kinds out of order", () => {
    expect(parsePage(withSecondLooks({ ...good, kinds: [...good.kinds].reverse() }))).toBeNull();
  });
  it("refuses a negative count", () => {
    const triggers = good.triggers.map((row, index) => (index === 0 ? { ...row, made: -1 } : row));
    expect(parsePage(withSecondLooks({ ...good, triggers }))).toBeNull();
  });
});

describe("learningView refuses a bad project_plans block", () => {
  const good = EXAMPLES.page.learning.project_plans;
  function withProjectPlans(project_plans: unknown): unknown {
    return { ...EXAMPLES.page, learning: { ...EXAMPLES.page.learning, project_plans } };
  }
  it("accepts the example rows", () => {
    expect(parsePage(withProjectPlans(good))).not.toBeNull();
  });
  it("refuses a learning block without project_plans", () => {
    const { project_plans: _gone, ...learning } = EXAMPLES.page.learning;
    expect(parsePage({ ...EXAMPLES.page, learning })).toBeNull();
  });
  it("refuses rows out of order", () => {
    expect(parsePage(withProjectPlans([...good].reverse()))).toBeNull();
  });
  it("refuses an unknown candidate", () => {
    const rows = good.map((row, index) => (index === 0 ? { ...row, candidate: "approvals" } : row));
    expect(parsePage(withProjectPlans(rows))).toBeNull();
  });
  it("refuses a negative count", () => {
    const rows = good.map((row, index) => (index === 0 ? { ...row, made: -1 } : row));
    expect(parsePage(withProjectPlans(rows))).toBeNull();
  });
});

describe("learningView refuses a bad message_drafts block", () => {
  const good = EXAMPLES.page.learning.message_drafts;
  function withMessageDrafts(message_drafts: unknown): unknown {
    return { ...EXAMPLES.page, learning: { ...EXAMPLES.page.learning, message_drafts } };
  }
  it("accepts the example rows", () => {
    expect(parsePage(withMessageDrafts(good))).not.toBeNull();
  });
  it("refuses a learning block without message_drafts", () => {
    const { message_drafts: _gone, ...learning } = EXAMPLES.page.learning;
    expect(parsePage({ ...EXAMPLES.page, learning })).toBeNull();
  });
  it("refuses rows out of order", () => {
    expect(parsePage(withMessageDrafts([...good].reverse()))).toBeNull();
  });
  it("refuses an unknown candidate", () => {
    const rows = good.map((row, index) => (index === 0 ? { ...row, candidate: "approvals" } : row));
    expect(parsePage(withMessageDrafts(rows))).toBeNull();
  });
  it("refuses a negative count", () => {
    const rows = good.map((row, index) => (index === 0 ? { ...row, made: -1 } : row));
    expect(parsePage(withMessageDrafts(rows))).toBeNull();
  });
});

describe("the page refuses a bad missions inbox block and a learning block without inbox_triage", () => {
  function withMissions(missions: unknown): unknown {
    return { ...EXAMPLES.page, missions };
  }
  it("refuses missions without inbox", () => {
    const { inbox: _gone, ...missions } = EXAMPLES.page.missions;
    expect(parsePage(withMissions(missions))).toBeNull();
  });
  it("refuses an inbox with an extra key", () => {
    const inbox = { ...EXAMPLES.page.missions.inbox, running: 0 };
    expect(parsePage(withMissions({ ...EXAMPLES.page.missions, inbox }))).toBeNull();
  });
  it("refuses an inbox with a negative count", () => {
    const inbox = { ...EXAMPLES.page.missions.inbox, today: -1 };
    expect(parsePage(withMissions({ ...EXAMPLES.page.missions, inbox }))).toBeNull();
  });
  it("refuses a learning block without inbox_triage", () => {
    const { inbox_triage: _gone, ...learning } = EXAMPLES.page.learning;
    expect(parsePage({ ...EXAMPLES.page, learning })).toBeNull();
  });
});

describe("the page refuses a bad message_drafts count block", () => {
  it("refuses a page without message_drafts", () => {
    const { message_drafts: _gone, ...page } = EXAMPLES.page;
    expect(parsePage(page)).toBeNull();
  });
  it("refuses a negative count", () => {
    const message_drafts = { ...EXAMPLES.page.message_drafts, today: -1 };
    expect(parsePage({ ...EXAMPLES.page, message_drafts })).toBeNull();
  });
});

// ---- bespoke shapes the generic sweep above cannot reach: ItemAction and ItemEvidence only
// carry the members their own `type` (or their note/chat choice) uses. ----

describe("ItemAction keeps exactly its own type's members", () => {
  function withAction(action: unknown): unknown {
    const launcher = EXAMPLES.launcher as unknown as Record<string, unknown>;
    const item = EXAMPLES.launcher.items[0] as unknown as Record<string, unknown>;
    return { ...launcher, items: [{ ...item, actions: [action] }] };
  }
  const openNote = some(some(EXAMPLES.launcher.items[0]).actions[1]); // {type: "open_note", ...}

  it("accepts the real open_note action unchanged (sanity check for the two tests below)", () => {
    expect(openNote.type).toBe("open_note");
    expect(parseLauncher(withAction(openNote))).not.toBeNull();
  });
  it("rejects open_note missing its ref", () => {
    const { ref: _ref, ...rest } = openNote as unknown as Record<string, unknown>;
    expect(parseLauncher(withAction(rest))).toBeNull();
  });
  it("rejects open_approvals carrying an id its own type never uses", () => {
    expect(
      parseLauncher(
        withAction({
          type: "open_approvals",
          label: "Open Approvals",
          primary: true,
          session_id: some(EXAMPLES.launcher.items[1]).episode_id,
        }),
      ),
    ).toBeNull();
  });

  it("accepts open_vault with its workspace_id and open_messaging bare", () => {
    const workspace = "315d31aa-25ed-45a7-a4b8-5c582ce841fb";
    const vault = {
      type: "open_vault",
      label: "Open vault",
      primary: true,
      workspace_id: workspace,
    };
    expect(parseLauncher(withAction(vault))).not.toBeNull();
    const messaging = { type: "open_messaging", label: "Open Messaging", primary: true };
    expect(parseLauncher(withAction(messaging))).not.toBeNull();
  });
  it("rejects open_vault without its workspace_id", () => {
    expect(
      parseLauncher(withAction({ type: "open_vault", label: "Open vault", primary: true })),
    ).toBeNull();
  });
  it("accepts open_code with its workspace_id and rejects it without", () => {
    const code = {
      type: "open_code",
      label: "Open in Code",
      primary: true,
      workspace_id: "315d31aa-25ed-45a7-a4b8-5c582ce841fb",
    };
    expect(parseLauncher(withAction(code))).not.toBeNull();
    const { workspace_id: _id, ...bare } = code;
    expect(parseLauncher(withAction(bare))).toBeNull();
  });
  it("rejects open_messaging with an extra key", () => {
    expect(
      parseLauncher(
        withAction({
          type: "open_messaging",
          label: "Open Messaging",
          primary: true,
          workspace_id: "315d31aa-25ed-45a7-a4b8-5c582ce841fb",
        }),
      ),
    ).toBeNull();
  });

  it("accepts open_triage and suggest_inbox bare and rejects either with an extra member", () => {
    for (const [type, label] of [
      ["open_triage", "Triage inbox"],
      ["suggest_inbox", "Suggest destinations"],
    ] as const) {
      expect(parseLauncher(withAction({ type, label, primary: true }))).not.toBeNull();
      const extra = {
        type,
        label,
        primary: true,
        workspace_id: "315d31aa-25ed-45a7-a4b8-5c582ce841fb",
      };
      expect(parseLauncher(withAction(extra))).toBeNull();
    }
  });

  // ---- draft_chat / draft_new: a draft action's own `text` ----
  const sessionId = some(EXAMPLES.launcher.started[0]);

  it("draft actions carry their text", () => {
    const draftChat = {
      type: "draft_chat",
      label: "Draft it in this chat",
      primary: true,
      session_id: sessionId,
      text: "Make this a skill.\nDraft a reusable skill.\tThanks.",
    };
    expect(parseLauncher(withAction(draftChat))).not.toBeNull();
    const draftNew = {
      type: "draft_new",
      label: "Draft it in a new chat",
      primary: true,
      text: "Learn these sources:\n- https://example.com",
    };
    expect(parseLauncher(withAction(draftNew))).not.toBeNull();
  });
  it("rejects draft_chat missing its text", () => {
    expect(
      parseLauncher(
        withAction({
          type: "draft_chat",
          label: "Draft it in this chat",
          primary: true,
          session_id: sessionId,
        }),
      ),
    ).toBeNull();
  });
  it("rejects draft_new carrying a session_id its own type never uses", () => {
    expect(
      parseLauncher(
        withAction({
          type: "draft_new",
          label: "Draft it in a new chat",
          primary: true,
          text: "x",
          session_id: sessionId,
        }),
      ),
    ).toBeNull();
  });
  it("rejects draft text below the 1-character minimum", () => {
    expect(
      parseLauncher(
        withAction({ type: "draft_new", label: "Draft it in a new chat", primary: true, text: "" }),
      ),
    ).toBeNull();
  });
  it("rejects draft text over 4,000 UTF-16 units but accepts exactly 4,000", () => {
    const over = withAction({
      type: "draft_new",
      label: "Draft it in a new chat",
      primary: true,
      text: "a".repeat(4_001),
    });
    const atMax = withAction({
      type: "draft_new",
      label: "Draft it in a new chat",
      primary: true,
      text: "a".repeat(4_000),
    });
    expect(parseLauncher(over)).toBeNull();
    expect(parseLauncher(atMax)).not.toBeNull();
  });
  it("rejects a draft text control character other than newline or tab", () => {
    expect(
      parseLauncher(
        withAction({
          type: "draft_new",
          label: "Draft it in a new chat",
          primary: true,
          text: "bad\u0007text",
        }),
      ),
    ).toBeNull();
  });
});

describe("ItemEvidence keeps exactly a note pair, a chat id, or neither", () => {
  function withEvidence(evidence: unknown): unknown {
    const launcher = EXAMPLES.launcher as unknown as Record<string, unknown>;
    const item = EXAMPLES.launcher.items[0] as unknown as Record<string, unknown>;
    return { ...launcher, items: [{ ...item, evidence: [evidence] }] };
  }
  const noteEvidence = some(some(EXAMPLES.launcher.items[0]).evidence[0]); // {label, workspace_id, ref}
  const chatSessionId = some(some(EXAMPLES.launcher.items[1]).evidence[0]).session_id;

  it("accepts the real note chip unchanged (sanity check for the two tests below)", () => {
    expect(parseLauncher(withEvidence(noteEvidence))).not.toBeNull();
  });
  it("rejects a note chip missing its ref (workspace_id without ref)", () => {
    const { ref: _ref, ...rest } = noteEvidence as unknown as Record<string, unknown>;
    expect(parseLauncher(withEvidence(rest))).toBeNull();
  });
  it("rejects evidence carrying both a note and a chat", () => {
    expect(parseLauncher(withEvidence({ ...noteEvidence, session_id: chatSessionId }))).toBeNull();
  });
});

// ---- a question proposal's view: its two wordings are
// pretty JSON shown in a `<pre>`, and its inbox item is held to this file's own exact rule ----

describe("a question proposal's view", () => {
  const view = EXAMPLES.question_proposal;
  function withProposedText(text: string): unknown {
    return { ...view, proposed_text: text };
  }
  function withItem(patch: Record<string, unknown>): Record<string, unknown> {
    return { ...view.proposal, ...patch };
  }

  it("keeps a wording's newlines and tabs, but no other control character", () => {
    expect(parseQuestionProposal(withProposedText('{\n\t"type": "noul"\n}'))).not.toBeNull();
    expect(parseQuestionProposal(withProposedText("bad\u0007text"))).toBeNull();
  });
  it("refuses a wording over 4,000 UTF-16 units but accepts exactly 4,000", () => {
    expect(parseQuestionProposal(withProposedText("a".repeat(4_001)))).toBeNull();
    expect(parseQuestionProposal(withProposedText("a".repeat(4_000)))).not.toBeNull();
  });
  it("holds its item to the exact shape, past what the inbox's own check lets through", () => {
    for (const item of [
      withItem({ extra: true }),
      withItem({ origin: { ...view.proposal.origin, extra: true } }),
      withItem({ origin: { workspace_id: "vault", session_id: null } }),
      withItem({ title: "Question\nchange" }),
      withItem({ reason: "Too\nvague" }),
    ]) {
      expect(isQuestionProposalItem(item)).toBe(true);
      expect(parseQuestionProposal({ ...view, proposal: item })).toBeNull();
    }
  });
  it("createProposal/actProposal's {proposal} is held to that same exact shape", () => {
    expect(parseProposalResult({ proposal: view.proposal })).toEqual(view.proposal);
    for (const item of [
      withItem({ extra: true }),
      withItem({ origin: { ...view.proposal.origin, extra: true } }),
      withItem({ title: "Question\nchange" }),
    ]) {
      expect(isQuestionProposalItem(item)).toBe(true);
      expect(parseProposalResult({ proposal: item })).toBeNull();
    }
  });
});

it("rejects long_context: true (the view only ever carries the literal false)", () => {
  expect(parseCallDetail({ ...EXAMPLES.call_detail, long_context: true })).toBeNull();
});
