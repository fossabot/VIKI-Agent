import { describe, expect, it } from "vitest";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "./fake";
import {
  ACTION_TYPES,
  CANDIDATES,
  type DataQuery,
  DecisionLoopApiError,
  FEEDBACK_HELD,
  FEEDBACK_SHOWN,
  type FeedbackBody,
  KEY_STATES,
  KINDS,
  LAUNCHER_LEVELS,
  LAUNCHER_STATES,
  LEVELS,
  MISSION_STATES,
  MISSION_WORK,
  MODEL_ERROR_CODES,
  MODEL_STATES,
  OUTCOMES,
  POLICIES,
  type PresenceBody,
  TOPICS,
  VERSION_SETTINGS,
} from "./types";

// examples.json is imported as plain JSON, so this walk is what proves it matches types.ts: the
// wire shapes again, as data. A leaf names a JSON kind; a key ending "?" may be absent.
type Leaf =
  | "string"
  | "number"
  | "real"
  | "count"
  | "boolean"
  | "false"
  | "stamp"
  | "stamp_or_empty"
  | "uuid"
  | "hex64";
class OneOf {
  constructor(readonly values: readonly string[]) {}
}
class OrNull {
  constructor(readonly shape: Shape) {}
}
class ArrayOf {
  constructor(readonly shape: Shape) {}
}
type Shape = Leaf | OneOf | OrNull | ArrayOf | { readonly [key: string]: Shape };
const oneOf = (values: readonly string[]) => new OneOf(values);
const orNull = (shape: Shape) => new OrNull(shape);
const arrayOf = (shape: Shape) => new ArrayOf(shape);

const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const matches = (value: unknown, pattern: RegExp) =>
  typeof value === "string" && pattern.test(value);
const LEAVES: Record<Leaf, (value: unknown) => boolean> = {
  string: (value) => typeof value === "string",
  number: (value) => typeof value === "number" && Number.isFinite(value) && value >= 0,
  // `learning.bias` and a topic's own fitted offset are the only learning numbers the client
  // lets go negative (bounds [-3, 1] and [-1.5, 1.5]).
  real: (value) => typeof value === "number" && Number.isFinite(value),
  count: (value) => Number.isInteger(value) && (value as number) >= 0,
  boolean: (value) => typeof value === "boolean",
  false: (value) => value === false,
  stamp: (value) => matches(value, STAMP),
  stamp_or_empty: (value) => value === "" || matches(value, STAMP),
  uuid: (value) => matches(value, UUID),
  hex64: (value) => matches(value, /^[0-9a-f]{64}$/),
};

const FEEDBACK = [...FEEDBACK_SHOWN, ...FEEDBACK_HELD];
const ITEM: Shape = {
  item_key: "hex64",
  episode_id: "uuid",
  candidate: oneOf(CANDIDATES),
  kind: oneOf(KINDS),
  level: oneOf(LEVELS),
  what: "string",
  why: "string",
  evidence: arrayOf({
    label: "string",
    "workspace_id?": "uuid",
    "ref?": "string",
    "session_id?": "uuid",
  }),
  actions: arrayOf({
    type: oneOf(ACTION_TYPES),
    label: "string",
    primary: "boolean",
    "session_id?": "uuid",
    "workspace_id?": "uuid",
    "ref?": "string",
    "routine_id?": "uuid",
    "call_id?": "uuid",
    "mission_id?": "uuid",
    "text?": "string",
  }),
  created_at: "stamp",
  mission_id: orNull("uuid"),
};
const MISSION: Shape = {
  mission_id: "uuid",
  session_id: "uuid",
  work: oneOf(MISSION_WORK),
  state: oneOf(MISSION_STATES),
  title: "string",
  started_at: "stamp",
  finished_at: orNull("stamp"),
  reason: "string",
};
const BY_WORK: Shape = { prepare: "count", run: "count" };
const USED: Shape = { used: "count", max: "count" };
// QuestionProposalState (state/decisionsApi.ts); not exported there, so named again here.
const QUESTION_STATES = ["open", "approved", "replaced", "rejected"] as const;
// ChangeKind / ChangeProposalState (state/decisionsApi.ts); not exported there, so named again
// here, as QUESTION_STATES is above.
const CHANGE_KINDS = ["note_link", "note_superseded", "tasks_ticked"] as const;
const CHANGE_STATES = ["open", "approved", "rejected"] as const;
const TRIAL_RATE: Shape = {
  yes_n: "count",
  yes_rate: orNull("number"),
  no_n: "count",
  no_rate: orNull("number"),
};
const DATA_EVIDENCE: Shape = arrayOf({
  label: "string",
  "workspace_id?": "uuid",
  "ref?": "string",
  "session_id?": "uuid",
});
const DATA_DECISION_ROW: Shape = {
  id: "uuid",
  at: "stamp",
  entry: oneOf(["decision", "report"]),
  topic: oneOf(TOPICS),
  level: orNull(oneOf(LEVELS)),
  policy: oneOf(POLICIES),
  answer: orNull(oneOf(FEEDBACK)),
  kept: orNull(oneOf(["open_suggestion"])),
};
const DATA_SUGGESTION_ROW: Shape = {
  id: "hex64",
  at: "stamp",
  created_at: "stamp",
  title: "string",
  topic: oneOf(CANDIDATES),
  state: oneOf(["open", "acted", "dismissed", "stale", "done"]),
  level: oneOf(LEVELS),
  kept: orNull(oneOf(["open", "chat"])),
};
const VIEWS: Record<string, Shape> = {
  launcher: {
    state: oneOf(LAUNCHER_STATES),
    level: oneOf(LAUNCHER_LEVELS),
    count: "count",
    nudge: "count",
    glow: "count",
    working: "count",
    open: "count",
    checked_at: orNull("stamp"),
    paused_until: orNull("stamp"),
    focus_until: orNull("stamp"),
    model: oneOf(MODEL_STATES),
    items: arrayOf(ITEM),
    started: arrayOf("uuid"),
    review: { due: "boolean", count: "count" },
    plans_waiting: "count",
  },
  page: {
    enabled: "boolean",
    paused_until: "stamp_or_empty",
    focus_until: "stamp_or_empty",
    status: { open: "count", checked_at: orNull("stamp") },
    model: {
      key: oneOf(KEY_STATES),
      version: { setting: oneOf(VERSION_SETTINGS), display: "string" },
      state: oneOf(MODEL_STATES),
      last_error: orNull(oneOf(MODEL_ERROR_CODES)),
      last_test: orNull({ at: "stamp", latency_ms: "count" }),
    },
    today: {
      decisions: "count",
      model_calls: "count",
      tokens: "count",
      cost_usd: "number",
      cap_usd: "number",
      capped: "boolean",
      shadow_calls: "count",
    },
    privacy: {
      last_call: orNull({ call_id: "uuid", at: "stamp", tokens: "count" }),
      held_back_today: "count",
    },
    muted: arrayOf(oneOf(CANDIDATES)),
    held_back: arrayOf({
      episode_id: "uuid",
      created_at: "stamp",
      candidate: oneOf(CANDIDATES),
      level: oneOf(LEVELS),
      reason: "string",
      why: "string",
      feedback: orNull(oneOf(FEEDBACK)),
    }),
    recent: arrayOf({
      episode_id: "uuid",
      created_at: "stamp",
      candidate: oneOf(CANDIDATES),
      level: oneOf(LEVELS),
      policy: oneOf(POLICIES),
      reason: "string",
      why: "string",
      feedback: orNull(oneOf(FEEDBACK)),
    }),
    counts: {
      shown: "count",
      useful: "count",
      wrong: "count",
      later: "count",
      mute: "count",
      missed: "count",
      right_to_hold: "count",
    },
    missions: {
      running: arrayOf(MISSION),
      today: BY_WORK,
      limits: BY_WORK,
      hold: orNull({ until: orNull("stamp") }),
      team: { today: "count", limit: "count", running: "count" },
      look: { today: "count", limit: "count" },
      second_look: { today: "count", limit: "count" },
      project_plan: { today: "count", limit: "count" },
      checks: orNull({ today: "count", limit: "count", running: "count" }),
      inbox: { today: "count", limit: "count" },
    },
    changes: { today: "count", limit: "count" },
    message_drafts: { today: "count", limit: "count" },
    data: { episodes: "count", oldest: orNull("stamp"), max: "count", retention_days: "count" },
    learning: {
      enabled: "boolean",
      labels: "count",
      min_labels: "count",
      fitted_at: orNull("stamp"),
      since: orNull("stamp"),
      attention_price: "number",
      bias: { start: "real", now: "real" },
      weights: arrayOf({ name: "string", label: "string", start: "number", now: "number" }),
      topics: arrayOf({
        candidate: oneOf(CANDIDATES),
        start: "real",
        now: "real",
        support: "count",
      }),
      reach: arrayOf({ level: oneOf(LEVELS), start: "number", now: "number", support: "count" }),
      costs: arrayOf({ level: oneOf(LEVELS), start: "number", now: "number", support: "count" }),
      calibration: {
        n: "count",
        brier: orNull("number"),
        bins: arrayOf({
          low: "number",
          high: "number",
          n: "count",
          mean_p: orNull("number"),
          rate: orNull("number"),
        }),
      },
      questions: arrayOf({
        name: "string",
        label: "string",
        yes_n: "count",
        yes_rate: orNull("number"),
        no_n: "count",
        no_rate: orNull("number"),
      }),
      shadow: {
        setting: oneOf(["off", ...VERSION_SETTINGS]),
        decisions: "count",
        agree: "count",
        kind_agree: "count",
        labels: "count",
        brier: orNull("number"),
        live_brier: orNull("number"),
      },
      second_looks: {
        triggers: arrayOf({
          candidate: oneOf(["long_task_look", "correction_look"]),
          made: "count",
          accepted: "count",
          rejected: "count",
          nothing: "count",
          reasons: {
            out_of_date: "count",
            already_knew: "count",
            wrong_topic: "count",
            bad_timing: "count",
          },
          paused: "boolean",
        }),
        kinds: arrayOf({
          kind: oneOf(["flaw", "approach", "missing"]),
          rejected_in_a_row: "count",
          paused: "boolean",
        }),
      },
      project_plans: arrayOf({
        candidate: oneOf(["stalled_project", "overdue_tasks"]),
        made: "count",
        accepted: "count",
        rejected: "count",
        nothing: "count",
        reasons: {
          out_of_date: "count",
          already_knew: "count",
          wrong_topic: "count",
          bad_timing: "count",
        },
        paused: "boolean",
      }),
      message_drafts: arrayOf({
        candidate: oneOf(["context_handoff", "source_note"]),
        made: "count",
        accepted: "count",
        rejected: "count",
        nothing: "count",
        reasons: {
          out_of_date: "count",
          already_knew: "count",
          wrong_topic: "count",
          bad_timing: "count",
        },
        paused: "boolean",
      }),
      saved_checks: arrayOf({
        candidate: oneOf(["commit_checks"]),
        made: "count",
        accepted: "count",
        rejected: "count",
        nothing: "count",
        reasons: {
          out_of_date: "count",
          already_knew: "count",
          wrong_topic: "count",
          bad_timing: "count",
        },
        paused: "boolean",
      }),
      inbox_triage: arrayOf({
        candidate: oneOf(["inbox_triage"]),
        made: "count",
        accepted: "count",
        rejected: "count",
        nothing: "count",
        reasons: {
          out_of_date: "count",
          already_knew: "count",
          wrong_topic: "count",
          bad_timing: "count",
        },
        paused: "boolean",
      }),
    },
  },
  calls: {
    enabled: "boolean",
    latest: orNull(oneOf(POLICIES)),
    calls: arrayOf({
      call_id: "uuid",
      at: "stamp",
      title: "string",
      candidate: oneOf(CANDIDATES),
      tokens: "count",
      session_id: orNull("uuid"),
    }),
  },
  call_detail: {
    call_id: "uuid",
    at: "stamp",
    title: "string",
    candidate: oneOf(CANDIDATES),
    session_id: orNull("uuid"),
    version: "string",
    version_setting: oneOf(VERSION_SETTINGS),
    latency_ms: "count",
    cost_usd: "number",
    long_context: "false",
    request: USED,
    largest_question: USED,
    split: arrayOf({ part: "string", tokens: "count" }),
    questions: arrayOf({ name: "string", label: "string", answer: "string" }),
    playbook_version: "string",
    state_text: "string",
    questions_text: "string",
    held_back: "count",
    outcome: oneOf(OUTCOMES),
    failure: orNull(oneOf(MODEL_ERROR_CODES)),
  },
  mission: MISSION,
  test_result: {
    ok: "boolean",
    latency_ms: orNull("count"),
    version: orNull("string"),
    error: orNull(oneOf(MODEL_ERROR_CODES)),
  },
  question_proposal: {
    proposal: {
      kind: oneOf(["question_proposal"]),
      id: "uuid",
      origin: { workspace_id: "uuid", session_id: orNull("string") },
      revision: "hex64",
      state: oneOf(QUESTION_STATES),
      attention: "boolean",
      title: "string",
      name: "string",
      created_at: "stamp",
      updated_at: "stamp",
      approved_at: orNull("stamp"),
      rejected_at: orNull("stamp"),
      reason: orNull("string"),
    },
    label: "string",
    current_text: "string",
    proposed_text: "string",
    tried: {
      decisions: "count",
      agree: "count",
      labels: "count",
      live: TRIAL_RATE,
      trial: TRIAL_RATE,
    },
  },
  change_act: {
    change: {
      kind: oneOf(["change_proposal"]),
      id: "uuid",
      origin: { workspace_id: "uuid", session_id: orNull("string") },
      revision: "hex64",
      state: oneOf(CHANGE_STATES),
      attention: "boolean",
      title: "string",
      change: oneOf(CHANGE_KINDS),
      ref: "string",
      created_at: "stamp",
      updated_at: "stamp",
      approved_at: orNull("stamp"),
      rejected_at: orNull("stamp"),
      reason: orNull("string"),
    },
    draft: orNull("string"),
  },
  data_summary: {
    as_of: "stamp",
    today: "string",
    decisions: {
      decisions: "count",
      reports: "count",
      oldest: orNull("stamp"),
      newest: orNull("stamp"),
      max: "count",
      retention_days: "count",
    },
    suggestions: {
      open: "count",
      closed: "count",
      oldest: orNull("stamp"),
      newest: orNull("stamp"),
      max_open: "count",
      closed_days: "count",
    },
    calls: {
      count: "count",
      oldest: orNull("stamp"),
      newest: orNull("stamp"),
      max: "count",
    },
    chats: {
      count: "count",
      running: "count",
      oldest: orNull("stamp"),
      newest: orNull("stamp"),
    },
    proposals: {
      wordings: "count",
      changes: "count",
      oldest: orNull("stamp"),
      newest: orNull("stamp"),
      max_wordings: "count",
      max_changes: "count",
    },
    preferences: { muted: "count", learned: "boolean", updated: orNull("stamp") },
    key: { saved: "boolean", updated: orNull("stamp") },
    days: arrayOf({
      day: "string",
      held: "count",
      listed: "count",
      attention: "count",
      reports: "count",
      useful: "count",
      wrong: "count",
      later: "count",
      mute: "count",
      right_to_hold: "count",
      missed: "count",
    }),
    topics: arrayOf({
      topic: oneOf(TOPICS),
      held: "count",
      shown: "count",
      reports: "count",
      useful: "count",
      wrong: "count",
    }),
  },
  data_decisions: {
    kind: oneOf(["decisions"]),
    total: "count",
    deletable: "count",
    next: orNull({ at: "stamp", id: "uuid" }),
    rows: arrayOf(DATA_DECISION_ROW),
  },
  data_suggestions: {
    kind: oneOf(["suggestions"]),
    total: "count",
    deletable: "count",
    next: orNull({ at: "stamp", id: "hex64" }),
    rows: arrayOf(DATA_SUGGESTION_ROW),
  },
  data_calls: {
    kind: oneOf(["calls"]),
    total: "count",
    deletable: "count",
    next: orNull({ at: "stamp", id: "uuid" }),
    rows: arrayOf({
      id: "uuid",
      at: "stamp",
      title: "string",
      topic: orNull(oneOf(CANDIDATES)),
      outcome: oneOf(OUTCOMES),
      tokens: "count",
      cost_usd: "number",
    }),
  },
  data_decision: {
    kind: oneOf(["decisions"]),
    id: "uuid",
    at: "stamp",
    entry: oneOf(["decision", "report"]),
    topic: oneOf(TOPICS),
    level: orNull(oneOf(LEVELS)),
    policy: oneOf(POLICIES),
    why: "string",
    evidence: DATA_EVIDENCE,
    answer: orNull(oneOf(FEEDBACK)),
    answer_reason: orNull("string"),
    answered_at: orNull("stamp"),
    shown: "count",
    opened_at: orNull("stamp"),
    acted_at: orNull("stamp"),
    tokens: orNull("count"),
    cost_usd: orNull("number"),
    kept: orNull(oneOf(["open_suggestion"])),
  },
  data_suggestion: {
    kind: oneOf(["suggestions"]),
    id: "hex64",
    title: "string",
    topic: oneOf(CANDIDATES),
    state: oneOf(["open", "acted", "dismissed", "stale", "done"]),
    level: oneOf(LEVELS),
    created_at: "stamp",
    updated_at: "stamp",
    shown_at: orNull("stamp"),
    opened_at: orNull("stamp"),
    what: "string",
    why: "string",
    evidence: DATA_EVIDENCE,
    kept: orNull(oneOf(["open", "chat"])),
  },
  data_deleted: { deleted: "count", kept: "count" },
  error: { error: { code: "string", message: "string" } },
};
// The nullable members an example leaves null because its own state rules a value out.
const NULL_IN_EXAMPLES: Record<string, string> = {
  "launcher.paused_until": "the launcher is on, not paused",
  "page.missions.hold": "the example holds nothing",
  "page.missions.hold.until": "the example holds nothing",
  "page.missions.running[].finished_at": "a running mission has not finished",
  "call_detail.failure": "the call was answered",
  "test_result.error": "the test passed",
  "question_proposal.proposal.origin.session_id": "a question proposal is never chat-scoped",
  "change_act.change.origin.session_id": "a change proposal is never chat-scoped",
  "change_act.change.rejected_at": "the example change is approved, not rejected",
  "change_act.change.reason": "a reason is set only after a rejection",
  "data_calls.next": "model calls are never paged",
  "data_calls.next.at": "model calls are never paged",
  "data_calls.next.id": "model calls are never paged",
};

/** Every member path `shape` names, into `all`, and those that may be null, into `nullable`. */
function paths(shape: Shape, path: string, all: Set<string>, nullable: Set<string>): void {
  if (shape instanceof OrNull) {
    nullable.add(path);
    paths(shape.shape, path, all, nullable);
    return;
  }
  all.add(path);
  if (shape instanceof ArrayOf) {
    paths(shape.shape, `${path}[]`, all, nullable);
  } else if (typeof shape === "object" && !(shape instanceof OneOf)) {
    for (const [key, inner] of Object.entries(shape)) {
      paths(inner, `${path}.${key.replace(/\?$/, "")}`, all, nullable);
    }
  }
}

/** Walks `value` against `shape`: each misfit goes into `problems`, each path that held a value
 * (not null) into `filled`. */
function check(
  value: unknown,
  shape: Shape,
  path: string,
  problems: string[],
  filled: Set<string>,
): void {
  if (shape instanceof OrNull) {
    if (value !== null) check(value, shape.shape, path, problems, filled);
    return;
  }
  if (value === null || value === undefined) {
    problems.push(`${path} is ${value}`);
    return;
  }
  filled.add(path);
  if (shape instanceof OneOf) {
    if (typeof value !== "string" || !shape.values.includes(value)) {
      problems.push(`${path} is ${JSON.stringify(value)}`);
    }
  } else if (shape instanceof ArrayOf) {
    if (!Array.isArray(value)) {
      problems.push(`${path} is not an array`);
    } else {
      for (const element of value) check(element, shape.shape, `${path}[]`, problems, filled);
    }
  } else if (typeof shape === "string") {
    if (!LEAVES[shape](value)) problems.push(`${path} is not ${shape}: ${JSON.stringify(value)}`);
  } else if (typeof value !== "object" || Array.isArray(value)) {
    problems.push(`${path} is not an object`);
  } else {
    const found = value as Record<string, unknown>;
    for (const key of Object.keys(found)) {
      if (!(key in shape) && !(`${key}?` in shape)) problems.push(`${path}.${key} is extra`);
    }
    for (const [spec, inner] of Object.entries(shape)) {
      const key = spec.replace(/\?$/, "");
      if (key in found) {
        check(found[key], inner, `${path}.${key}`, problems, filled);
      } else if (key === spec) {
        problems.push(`${path}.${key} is missing`);
      }
    }
  }
}

describe("examples.json", () => {
  const examples = decisionLoopExamples();
  const all = new Set<string>();
  const nullable = new Set<string>();
  const problems: string[] = [];
  const filled = new Set<string>();
  for (const [name, shape] of Object.entries(VIEWS)) {
    paths(shape, name, all, nullable);
    check((examples as unknown as Record<string, unknown>)[name], shape, name, problems, filled);
  }

  it("holds one example per response view and the nullable list", () => {
    expect(Object.keys(examples).sort()).toEqual(["_nullable", ...Object.keys(VIEWS)].sort());
  });

  it("gives each view exactly its members, each of its JSON type, null only where allowed", () => {
    expect(problems).toEqual([]);
  });

  it("lists exactly the members that may be null", () => {
    expect([...examples._nullable].sort()).toEqual([...nullable].sort());
  });

  it("fills every member, every optional one and every array, but the four held null", () => {
    expect([...all].filter((path) => !filled.has(path)).sort()).toEqual(
      Object.keys(NULL_IN_EXAMPLES).sort(),
    );
  });

  it("covers every action type and kind, each item's one primary action first", () => {
    const items = examples.launcher.items;
    expect(new Set(items.flatMap((item) => item.actions.map((action) => action.type)))).toEqual(
      new Set(ACTION_TYPES),
    );
    expect(new Set(items.map((item) => item.kind))).toEqual(new Set(KINDS));
    for (const item of items) {
      expect(item.actions.map((action) => action.primary)).toEqual(
        item.actions.map((_action, index) => index === 0),
      );
    }
  });

  it("shows versions as numbers and never names the model", () => {
    expect([
      examples.page.model.version.display,
      examples.call_detail.version,
      examples.test_result.version,
    ]).toEqual(["1.14.0", "1.14.0", "1.14.0"]);
    expect(JSON.stringify(examples)).not.toMatch(/jev|system ?1/i);
  });
});

const EXAMPLES = decisionLoopExamples();
function some<T>(found: T | undefined): T {
  if (found === undefined) throw new Error("the examples lack it");
  return found;
}
const BADGE_ITEM = some(EXAMPLES.launcher.items.find((item) => item.level === "badge"));
const INBOX_ITEM = some(EXAMPLES.launcher.items.find((item) => item.candidate === "inbox"));
const RUNNING = some(EXAMPLES.page.missions.running[0]);
const UNLABELED = some(EXAMPLES.page.held_back.find((row) => row.feedback === null));
const OTHER_CALL = some(
  EXAMPLES.calls.calls.find((call) => call.call_id !== EXAMPLES.call_detail.call_id),
);
const CHANGE = EXAMPLES.change_act.change;
const UNKNOWN_ID = "00000000-0000-4000-8000-000000000000";
const KEY = "example-key-not-real";
const PRESENCE: PresenceBody = {
  window: "8d3f5a2c-6b1e-4f7a-9c0d-2e4f6a8b0c1d",
  focused: true,
  typing: false,
  chat: null,
  chat_title: null,
  note: { workspace_id: "315d31aa-25ed-45a7-a4b8-5c582ce841fb", ref: "10-projects/x/tasks.md" },
  launcher: false,
  page: {
    native_waiting: 1,
    runs: { working: 0, review: 1 },
    git: { state: "known", dirty: 2 },
    routine: null,
    skill: null,
    suggest: null,
    second_look: null,
    context: null,
    sources: null,
  },
};
const onItem = (itemKey: string, value: FeedbackBody["value"] = "later"): FeedbackBody => ({
  item_key: itemKey,
  episode_id: null,
  value,
  reason: null,
  source: "launcher",
});

async function refusal(pending: Promise<unknown>): Promise<DecisionLoopApiError> {
  const error = await pending.then(
    () => undefined,
    (caught: unknown) => caught,
  );
  expect(error).toBeInstanceOf(DecisionLoopApiError);
  return error as DecisionLoopApiError;
}

describe("createFakeDecisionLoopApi", () => {
  it("answers every read from the examples", async () => {
    const api = createFakeDecisionLoopApi();
    expect(await api.launcher()).toEqual(EXAMPLES.launcher);
    expect(await api.openLauncher()).toEqual(EXAMPLES.launcher);
    expect(await api.page()).toEqual(EXAMPLES.page);
    expect(await api.calls()).toEqual(EXAMPLES.calls);
    expect(await api.call(EXAMPLES.call_detail.call_id)).toEqual(EXAMPLES.call_detail);
    expect(await api.missions()).toEqual(EXAMPLES.page.missions);
    expect(await api.test()).toEqual(EXAMPLES.test_result);
    expect(await api.report("unwatched", "page")).toEqual(EXAMPLES.page);
    await expect(api.presence(PRESENCE)).resolves.toBeUndefined();
    await expect(api.observe("shown", [BADGE_ITEM.item_key])).resolves.toBeUndefined();
  });

  it("hands out copies, so no test reaches its state or the shared examples", async () => {
    const api = createFakeDecisionLoopApi();
    const view = await api.launcher();
    view.state = "off";
    view.items.length = 0;
    (await api.page()).muted.push("inbox");
    (await api.missions()).running.length = 0;
    expect(await api.launcher()).toEqual(EXAMPLES.launcher);
    expect(await api.page()).toEqual(EXAMPLES.page);
    api.data.page.enabled = false;
    expect((await createFakeDecisionLoopApi().page()).enabled).toBe(true);
    expect(decisionLoopExamples()).toEqual(EXAMPLES);
  });

  it("starts from the documents a seed names, copied", async () => {
    const seed = decisionLoopExamples();
    seed.launcher.state = "off";
    seed.launcher.items = [];
    const api = createFakeDecisionLoopApi({ launcher: seed.launcher });
    seed.launcher.state = "paused";
    expect(await api.launcher()).toMatchObject({ state: "off", items: [] });
    expect(await api.page()).toEqual(EXAMPLES.page);
  });

  it("records each call in order with a copy of its arguments, signals left out", async () => {
    const api = createFakeDecisionLoopApi();
    const body = onItem(BADGE_ITEM.item_key);
    await api.observe("shown", [BADGE_ITEM.item_key]);
    await api.feedback(body);
    body.value = "wrong";
    await api.page(new AbortController().signal);
    await api.saveKey(KEY);
    expect(api.requests).toEqual([
      { method: "observe", args: ["shown", [BADGE_ITEM.item_key]] },
      { method: "feedback", args: [onItem(BADGE_ITEM.item_key)] },
      { method: "page", args: [] },
      { method: "saveKey", args: [KEY] },
    ]);
  });

  it("takes an item off the launcher on feedback and counts again", async () => {
    const api = createFakeDecisionLoopApi();
    const view = await api.feedback(onItem(BADGE_ITEM.item_key));
    expect(view.items.map((item) => item.item_key)).not.toContain(BADGE_ITEM.item_key);
    expect(view).toMatchObject({
      open: EXAMPLES.launcher.open - 1,
      count: EXAMPLES.launcher.count - 1,
      level: EXAMPLES.launcher.level,
    });
    expect((await api.page()).status.open).toBe(EXAMPLES.launcher.open - 1);
    expect(await refusal(api.feedback(onItem(BADGE_ITEM.item_key)))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
    await Promise.all(view.items.map((item) => api.feedback(onItem(item.item_key, "useful"))));
    expect(await api.launcher()).toMatchObject({ items: [], open: 0, count: 0, level: "idle" });
  });

  it("labels a held-back episode on feedback by episode", async () => {
    const api = createFakeDecisionLoopApi();
    const held = (episodeId: string): FeedbackBody => ({
      item_key: null,
      episode_id: episodeId,
      value: "missed",
      reason: null,
      source: "page",
    });
    expect(await api.feedback(held(UNLABELED.episode_id))).toEqual(EXAMPLES.launcher);
    const row = (await api.page()).held_back.find((r) => r.episode_id === UNLABELED.episode_id);
    expect(row?.feedback).toBe("missed");
    expect(await refusal(api.feedback(held(UNKNOWN_ID)))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("removes and saves the key, and no answer carries it", async () => {
    const api = createFakeDecisionLoopApi();
    expect((await api.removeKey()).model).toMatchObject({ key: "missing", state: "no_key" });
    expect((await api.launcher()).model).toBe("no_key");
    const saved = await api.saveKey(KEY);
    expect(saved.model).toMatchObject({ key: "saved", state: "ready" });
    expect((await api.launcher()).model).toBe("ready");
    expect(JSON.stringify([saved, await api.launcher()])).not.toContain(KEY);
  });

  it("empties the history on deleteEpisodes and says how many episodes went", async () => {
    const api = createFakeDecisionLoopApi();
    expect(await api.deleteEpisodes()).toEqual({ deleted: EXAMPLES.page.data.episodes });
    const page = await api.page();
    expect(page).toMatchObject({
      held_back: [],
      recent: [],
      privacy: { last_call: null },
      data: { episodes: 0, oldest: null },
      status: { open: 0 },
    });
    expect(new Set(Object.values(page.counts))).toEqual(new Set([0]));
    expect(await api.calls()).toMatchObject({ latest: null, calls: [] });
    expect(await api.launcher()).toMatchObject({ items: [], open: 0, count: 0, level: "idle" });
    expect(await refusal(api.call(EXAMPLES.call_detail.call_id))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("answers a listed call's detail under that call's own facts", async () => {
    const api = createFakeDecisionLoopApi();
    const { call_id, at, title, candidate, session_id } = OTHER_CALL;
    expect(call_id).not.toBe(EXAMPLES.call_detail.call_id);
    expect(await api.call(call_id)).toEqual({
      ...EXAMPLES.call_detail,
      call_id,
      at,
      title,
      candidate,
      session_id,
    });
    expect(await refusal(api.call(UNKNOWN_ID))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("starts and stops VIKI's chats", async () => {
    const api = createFakeDecisionLoopApi();
    const started = await api.startMission(INBOX_ITEM.item_key, "run");
    expect(started).toEqual({
      ...EXAMPLES.mission,
      work: "run",
      state: "running",
      title: INBOX_ITEM.what,
      finished_at: null,
      reason: "",
    });
    const missions = await api.missions();
    expect(missions.running.map((m) => m.mission_id)).toEqual([
      RUNNING.mission_id,
      started.mission_id,
    ]);
    expect(missions.today.run).toBe(EXAMPLES.page.missions.today.run + 1);
    expect((await api.launcher()).working).toBe(2);
    expect((await api.launcher()).started).toContain(started.session_id);

    const stopped = await api.stopMission(RUNNING.mission_id);
    expect(stopped).toMatchObject({
      mission_id: RUNNING.mission_id,
      state: "stopped",
      reason: "owner",
    });
    expect(stopped.finished_at).toMatch(STAMP);
    expect((await api.launcher()).working).toBe(1);
    expect(await refusal(api.stopMission(RUNNING.mission_id))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
    expect((await api.stopAllMissions()).running).toEqual([]);
    expect((await api.launcher()).working).toBe(0);
    expect(await refusal(api.startMission("0".repeat(64), "prepare"))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("records both seat calls and answers the example missions", async () => {
    const api = createFakeDecisionLoopApi();
    const id = EXAMPLES.page.missions.running[0]?.mission_id ?? "";
    expect(await api.startSeat(id)).toEqual(EXAMPLES.page.missions);
    expect(await api.stopSeat(id)).toEqual(EXAMPLES.page.missions);
    expect(api.requests).toEqual([
      { method: "startSeat", args: [id] },
      { method: "stopSeat", args: [id] },
    ]);
  });

  it("records both check calls and answers the example missions with its checks block", async () => {
    const api = createFakeDecisionLoopApi();
    const id = "802cfd61-3418-4373-8d49-3f5e5b019d92";
    expect(await api.runChecks(id)).toEqual(EXAMPLES.page.missions);
    expect(await api.stopChecks(id)).toEqual(EXAMPLES.page.missions);
    expect(EXAMPLES.page.missions.checks).toEqual({ today: 1, limit: 3, running: 0 });
    expect(EXAMPLES.page.learning.saved_checks.length).toBeGreaterThan(0);
    expect(api.requests).toEqual([
      { method: "runChecks", args: [id] },
      { method: "stopChecks", args: [id] },
    ]);
  });

  it("records suggestInbox and answers the example missions with its inbox block", async () => {
    const api = createFakeDecisionLoopApi();
    const key = "a".repeat(64);
    expect(await api.suggestInbox(key)).toEqual(EXAMPLES.page.missions);
    expect(EXAMPLES.page.missions.inbox).toBeDefined();
    expect(EXAMPLES.page.learning.inbox_triage).toBeDefined();
    expect(api.requests).toEqual([{ method: "suggestInbox", args: [key] }]);
  });

  it("unmutes a topic", async () => {
    const api = createFakeDecisionLoopApi();
    const muted = some(EXAMPLES.page.muted[0]);
    const page = await api.preference({ name: "unmute", candidate: muted });
    expect(page.muted).not.toContain(muted);
    expect(await api.preference({ name: "review_seen" })).toEqual(page);
  });

  it("forgets what was learned on reset_learning, keeping every start value", async () => {
    const api = createFakeDecisionLoopApi();
    const before = (await api.page()).learning;
    expect(before.labels).toBeGreaterThan(0);
    expect(before.bias.now).not.toBe(before.bias.start); // sanity: the example has already learned
    // sanity: the example already carries support, so zeroing it below is a real assertion.
    expect(before.topics.some((t) => t.support > 0)).toBe(true);
    expect(before.reach.some((r) => r.support > 0)).toBe(true);
    expect(before.costs.some((c) => c.support > 0)).toBe(true);
    // sanity: the example's attention price, calibration and shadow are not already empty.
    expect(before.attention_price).not.toBe(0.3);
    expect(before.calibration.n).toBeGreaterThan(0);
    expect(before.questions.some((q) => q.yes_n > 0 || q.no_n > 0)).toBe(true);
    expect(before.shadow.decisions).toBeGreaterThan(0);

    const { learning } = await api.preference({ name: "reset_learning" });
    expect(learning).toMatchObject({ labels: 0, fitted_at: null });
    expect(learning.since).not.toBe(before.since);
    expect(learning.bias.now).toBe(learning.bias.start);
    expect(learning.weights.map((w) => w.now)).toEqual(learning.weights.map((w) => w.start));
    expect(learning.topics.map((t) => t.now)).toEqual(learning.topics.map((t) => t.start));
    expect(learning.reach.map((r) => r.now)).toEqual(learning.reach.map((r) => r.start));
    expect(learning.costs.map((c) => c.now)).toEqual(learning.costs.map((c) => c.start));
    // the server zeroes every support count with no snapshot (wire.py::learning_view, `learned`
    // None) -- exactly the state right after a real reset_learning.
    expect(learning.topics.every((t) => t.support === 0)).toBe(true);
    expect(learning.reach.every((r) => r.support === 0)).toBe(true);
    expect(learning.costs.every((c) => c.support === 0)).toBe(true);
    // the server also restarts attention_price at the start λ and empties calibration, question
    // rates and the shadow comparison: Reset erases the rows they were read from too
    // (arbiter.py::START_LAMBDA, service.py::_learning).
    expect(learning.attention_price).toBe(0.3);
    expect(learning.calibration).toMatchObject({ n: 0, brier: null });
    expect(
      learning.calibration.bins.every(
        (bin) => bin.n === 0 && bin.mean_p === null && bin.rate === null,
      ),
    ).toBe(true);
    expect(
      learning.questions.every(
        (q) => q.yes_n === 0 && q.yes_rate === null && q.no_n === 0 && q.no_rate === null,
      ),
    ).toBe(true);
    expect(learning.shadow).toMatchObject({
      decisions: 0,
      agree: 0,
      kind_agree: 0,
      labels: 0,
      brier: null,
      live_brier: null,
    });
  });

  it("also forgets the learned block on deleteEpisodes, leaving since and attention_price alone", async () => {
    // Delete all decisions forgets the snapshot it was learned from, but unlike Reset it
    // never restarts learning: λ and `learn_since` stay exactly as they were (service.py's
    // `_delete_all` calls `forget_learning(scope, restart_at=None)`).
    const api = createFakeDecisionLoopApi();
    const before = (await api.page()).learning;
    expect(before.labels).toBeGreaterThan(0);

    await api.deleteEpisodes();
    const { learning } = await api.page();
    expect(learning).toMatchObject({
      labels: 0,
      fitted_at: null,
      since: before.since,
      attention_price: before.attention_price,
    });
    expect(learning.bias.now).toBe(learning.bias.start);
    expect(learning.weights.map((w) => w.now)).toEqual(learning.weights.map((w) => w.start));
    expect(learning.topics.every((t) => t.now === t.start && t.support === 0)).toBe(true);
    expect(learning.reach.every((r) => r.now === r.start && r.support === 0)).toBe(true);
    expect(learning.costs.every((c) => c.now === c.start && c.support === 0)).toBe(true);
    expect(learning.calibration).toMatchObject({ n: 0, brier: null });
    expect(
      learning.calibration.bins.every(
        (bin) => bin.n === 0 && bin.mean_p === null && bin.rate === null,
      ),
    ).toBe(true);
    expect(
      learning.questions.every(
        (q) => q.yes_n === 0 && q.yes_rate === null && q.no_n === 0 && q.no_rate === null,
      ),
    ).toBe(true);
    expect(learning.shadow).toMatchObject({
      decisions: 0,
      agree: 0,
      kind_agree: 0,
      labels: 0,
      brier: null,
      live_brier: null,
    });
  });

  it("drops a labelled weekly-check row from the launcher's review count, due false at 0", async () => {
    // stats.py::review's count is the audit sample's unlabelled rows; labelling one through
    // feedback-by-episode (the weekly check's own path) must drop it too, due going false the
    // moment the count reaches 0.
    const seed = decisionLoopExamples();
    seed.launcher.review = { due: true, count: 1 };
    const api = createFakeDecisionLoopApi({ launcher: seed.launcher });
    const held = (episodeId: string): FeedbackBody => ({
      item_key: null,
      episode_id: episodeId,
      value: "missed",
      reason: null,
      source: "page",
    });

    await api.feedback(held(UNLABELED.episode_id));
    expect((await api.launcher()).review).toEqual({ due: false, count: 0 });
  });

  it("marks the weekly check seen on review_seen", async () => {
    const seed = decisionLoopExamples();
    seed.launcher.review = { due: true, count: 3 };
    const api = createFakeDecisionLoopApi({ launcher: seed.launcher });
    expect((await api.launcher()).review).toEqual({ due: true, count: 3 });
    await api.preference({ name: "review_seen" });
    expect((await api.launcher()).review).toEqual({ due: false, count: 3 });
  });

  it("fails a scripted call once, with the server's status for its code", async () => {
    const api = createFakeDecisionLoopApi();
    api.fail("page", "unavailable");
    expect(await refusal(api.page())).toMatchObject({ code: "unavailable", status: 503 });
    expect(await api.page()).toEqual(EXAMPLES.page);

    api.fail("feedback", "busy");
    expect(await refusal(api.feedback(onItem(BADGE_ITEM.item_key)))).toMatchObject({
      code: "busy",
      status: 429,
    });
    expect(await api.launcher()).toEqual(EXAMPLES.launcher);

    api.fail("startMission", "off");
    expect(await refusal(api.startMission(INBOX_ITEM.item_key, "run"))).toMatchObject({
      code: "off",
      status: 0,
    });
    api.fail("test", "no_folder", 409);
    expect(await refusal(api.test())).toMatchObject({ code: "no_folder", status: 409 });
    expect(await api.missions()).toEqual(EXAMPLES.page.missions);
    expect(api.requests.map((request) => request.method)).toEqual([
      "page",
      "page",
      "feedback",
      "launcher",
      "startMission",
      "test",
      "missions",
    ]);
  });
});

describe("createFakeDecisionLoopApi: question proposals", () => {
  it("answers proposal by id and refuses an unknown one", async () => {
    const api = createFakeDecisionLoopApi();
    expect(await api.proposal(EXAMPLES.question_proposal.proposal.id)).toEqual(
      EXAMPLES.question_proposal,
    );
    expect(await refusal(api.proposal(UNKNOWN_ID))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("opens a proposal under the given name, with the question as proposed_text", async () => {
    const api = createFakeDecisionLoopApi();
    const question = { type: "noul" };
    const created = await api.createProposal("skill_offer", question);
    expect(created).toMatchObject({
      name: "skill_offer",
      state: "open",
      attention: true,
      approved_at: null,
      rejected_at: null,
      reason: null,
    });
    const fetched = await api.proposal(created.id);
    expect(fetched.proposed_text).toBe(JSON.stringify(question, null, 2));
    expect(fetched.tried).toEqual({
      decisions: 0,
      agree: 0,
      labels: 0,
      live: { yes_n: 0, yes_rate: null, no_n: 0, no_rate: null },
      trial: { yes_n: 0, yes_rate: null, no_n: 0, no_rate: null },
    });
  });

  it("approves an open proposal and refuses acting on it again", async () => {
    const api = createFakeDecisionLoopApi();
    const created = await api.createProposal("news", { type: "noul" });
    const approved = await api.actProposal(created.id, "approve", created.revision);
    expect(approved).toMatchObject({ state: "approved", attention: false });
    expect(approved.approved_at).toMatch(STAMP);
    expect(await refusal(api.actProposal(created.id, "approve", created.revision))).toMatchObject({
      code: "not_applicable",
    });
  });

  it("rejects an open proposal with a reason", async () => {
    const api = createFakeDecisionLoopApi();
    const opened = await api.createProposal("news", { type: "noul" });
    const rejected = await api.actProposal(opened.id, "reject", opened.revision, "not needed");
    expect(rejected).toMatchObject({ state: "rejected", attention: false, reason: "not needed" });
  });

  it("undoes an approved proposal with a reject", async () => {
    const api = createFakeDecisionLoopApi();
    const opened = await api.createProposal("news", { type: "noul" });
    const approved = await api.actProposal(opened.id, "approve", opened.revision);
    const undone = await api.actProposal(approved.id, "reject", approved.revision, "changed mind");
    expect(undone).toMatchObject({ state: "rejected", reason: "changed mind" });
  });

  it("refuses a stale revision and an unknown id on act", async () => {
    const api = createFakeDecisionLoopApi();
    const created = await api.createProposal("news", { type: "noul" });
    expect(await refusal(api.actProposal(created.id, "approve", "0".repeat(64)))).toMatchObject({
      code: "not_applicable",
    });
    expect(await refusal(api.actProposal(UNKNOWN_ID, "approve", created.revision))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("records createProposal and actProposal calls in order with their arguments", async () => {
    const api = createFakeDecisionLoopApi();
    const created = await api.createProposal("news", { type: "noul" });
    await api.actProposal(created.id, "reject", created.revision, "not needed");
    expect(api.requests).toEqual([
      { method: "createProposal", args: ["news", { type: "noul" }] },
      { method: "actProposal", args: [created.id, "reject", created.revision, "not needed"] },
    ]);
  });
});

describe("createFakeDecisionLoopApi: change proposals (actChange)", () => {
  /** The example's one change item, seeded open (unacted): attention true, no stamps, draft held
   * ready for whenever it is approved (the fake's own simplification -- see fake.ts's actChange). */
  function seedOpenChange() {
    return createFakeDecisionLoopApi({
      change_act: {
        change: {
          ...CHANGE,
          state: "open",
          attention: true,
          approved_at: null,
          rejected_at: null,
          reason: null,
        },
        draft: EXAMPLES.change_act.draft,
      },
    });
  }

  it("approves an open change: stamps approved_at, clears attention, answers the draft", async () => {
    const api = seedOpenChange();
    const result = await api.actChange(CHANGE.id, "approve", CHANGE.revision);
    expect(result.change).toMatchObject({ state: "approved", attention: false });
    expect(result.change.approved_at).toMatch(STAMP);
    expect(result.draft).toBe(EXAMPLES.change_act.draft);
  });

  it("re-approving an approved change changes nothing and answers the draft again", async () => {
    const api = createFakeDecisionLoopApi(); // the seed's own change_act.change is already approved
    const before = await api.actChange(CHANGE.id, "approve", CHANGE.revision);
    expect(before).toEqual(EXAMPLES.change_act);
    expect(await api.actChange(CHANGE.id, "approve", CHANGE.revision)).toEqual(before);
  });

  it("rejects an open change with the stripped reason and no draft; acting on it again refuses", async () => {
    const api = seedOpenChange();
    const result = await api.actChange(CHANGE.id, "reject", CHANGE.revision, "  not needed  ");
    expect(result.change).toMatchObject({
      state: "rejected",
      attention: false,
      reason: "not needed",
    });
    expect(result.draft).toBeNull();
    expect(await refusal(api.actChange(CHANGE.id, "reject", CHANGE.revision))).toMatchObject({
      code: "not_applicable",
    });
  });

  it("refuses rejecting an approved change (changes have no undo) and a stale revision", async () => {
    const api = createFakeDecisionLoopApi(); // already approved
    expect(await refusal(api.actChange(CHANGE.id, "reject", CHANGE.revision))).toMatchObject({
      code: "not_applicable",
    });
    expect(await refusal(api.actChange(CHANGE.id, "approve", "0".repeat(64)))).toMatchObject({
      code: "not_applicable",
    });
  });

  it("refuses an unknown change id", async () => {
    const api = createFakeDecisionLoopApi();
    expect(await refusal(api.actChange(UNKNOWN_ID, "approve", CHANGE.revision))).toMatchObject({
      code: "unknown_item",
      status: 404,
    });
  });

  it("records actChange calls in order with their arguments", async () => {
    const api = seedOpenChange();
    await api.actChange(CHANGE.id, "reject", CHANGE.revision, "not needed");
    expect(api.requests).toEqual([
      { method: "actChange", args: [CHANGE.id, "reject", CHANGE.revision, "not needed"] },
    ]);
  });
});

describe("createFakeDecisionLoopApi: data routes", () => {
  it("refuses data queries the wire parser rejects", async () => {
    const api = createFakeDecisionLoopApi();
    const query = {
      kind: "decisions",
      ids: null,
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: "2026-10-22T00:00:00.000Z",
      before: null,
    };
    const invalid = [
      { ...query, kind: "decisions", state: "open" },
      { ...query, kind: "decisions", outcome: "success" },
      { ...query, kind: "suggestions", shown: "held" },
      { ...query, kind: "suggestions", answer: "useful" },
      { ...query, kind: "suggestions", outcome: "success" },
      { ...query, kind: "calls", outcome: null, topic: "inbox" },
      { ...query, kind: "decisions", before: { at: query.until, id: EXAMPLES.data_decision.id } },
      { ...query, kind: "decisions", ids: [EXAMPLES.data_decision.id], topic: "inbox" },
      { ...query, kind: "decisions", ids: [EXAMPLES.data_decision.id], outcome: "success" },
      { ...query, kind: "calls", ids: null, outcome: "unknown" },
      { ...query, kind: "decisions", topic: "unknown" },
      { ...query, kind: "decisions", until: null, topic: "inbox" },
      { ...query, kind: "decisions", topic: null },
      { ...query, kind: "decisions", since: query.until },
      { ...query, kind: "decisions", since: "2026-10-22T00:00:00Z" },
      { ...query, kind: "decisions", since: "2026-13-01T00:00:00Z" },
      { ...query, kind: "calls", topic: null, since: "2026-10-21T00:00:00Z" },
      { ...query, kind: "calls", topic: null, until: "2026-10-23T00:00:00Z" },
      { ...query, kind: "decisions", since: "0000-01-01T00:00:00.000Z" },
    ];

    for (const value of invalid) {
      const dataQuery = value as unknown as DataQuery;
      await expect(refusal(api.deleteData(dataQuery))).resolves.toMatchObject({
        code: "invalid_request",
      });
    }
    for (const value of [
      invalid[0],
      invalid[1],
      invalid[2],
      invalid[3],
      invalid[4],
      invalid[5],
      invalid[7],
      invalid[8],
      invalid[9],
      invalid[10],
      invalid[13],
      invalid[14],
      invalid[15],
      invalid[16],
      invalid[17],
    ]) {
      await expect(refusal(api.dataRecords(value as unknown as DataQuery))).resolves.toMatchObject({
        code: "invalid_request",
      });
    }
    await expect(
      api.dataRecords({
        ...query,
        since: "2026-10-22T00:00:00Z",
        until: "2026-10-22T00:00:00.001Z",
      } as DataQuery),
    ).resolves.toEqual(EXAMPLES.data_decisions);
    await expect(
      api.dataRecords({
        ...query,
        since: "0001-01-01T00:00:00Z",
        until: "0001-01-02T00:00:00Z",
      } as DataQuery),
    ).resolves.toEqual(EXAMPLES.data_decisions);
    const callId = EXAMPLES.data_calls.rows[0]?.id ?? "";
    const badIds = [
      { ...query, kind: "decisions", ids: ["not-a-uuid"], until: null },
      { ...query, kind: "suggestions", ids: [EXAMPLES.data_decision.id], until: null },
      {
        ...query,
        kind: "calls",
        ids: [callId],
        outcome: "answered",
        topic: null,
        until: null,
      },
    ];
    for (const value of badIds) {
      await expect(refusal(api.deleteData(value as unknown as DataQuery))).resolves.toMatchObject({
        code: "invalid_request",
      });
    }
  });

  it("answers data views, validates deletes, and removes deleted calls from call()", async () => {
    const api = createFakeDecisionLoopApi();
    expect(await api.dataSummary()).toEqual(EXAMPLES.data_summary);
    expect(
      await api.dataRecords({
        kind: "decisions",
        ids: null,
        topic: null,
        shown: null,
        answer: null,
        state: null,
        outcome: null,
        since: null,
        until: null,
        before: null,
      }),
    ).toEqual(EXAMPLES.data_decisions);
    expect(await api.dataRecord("decisions", EXAMPLES.data_decision.id)).toMatchObject({
      id: EXAMPLES.data_decision.id,
    });
    const call = EXAMPLES.data_calls.rows[0];
    expect(call).toBeDefined();
    const query: DataQuery = {
      kind: "calls",
      ids: [call?.id ?? ""],
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: null,
      before: null,
    };
    await expect(api.deleteData(query)).resolves.toEqual({ deleted: 1, kept: 0 });
    await expect(refusal(api.call(call?.id ?? ""))).resolves.toMatchObject({
      code: "unknown_item",
    });
    await expect(refusal(api.deleteData({ ...query, ids: [] }))).resolves.toMatchObject({
      code: "invalid_request",
    });
    await expect(
      refusal(
        api.deleteData({
          ...query,
          ids: null,
          outcome: null,
          until: "2026-10-22T00:00:00.000Z",
        } as unknown as DataQuery),
      ),
    ).resolves.toMatchObject({ code: "invalid_request" });
    await expect(
      refusal(api.deleteData({ ...query, ids: [call?.id ?? "", call?.id ?? ""] })),
    ).resolves.toMatchObject({ code: "invalid_request" });
    await expect(
      refusal(api.deleteData({ ...query, ids: Array.from({ length: 51 }, () => call?.id ?? "") })),
    ).resolves.toMatchObject({ code: "invalid_request" });
    const row = api.data.data_decisions.rows.find((candidate) => candidate.kept === null);
    if (!row) throw new Error("expected deletable decision example");
    const before = api.data.data_decisions.total;
    const filter = {
      kind: "decisions",
      ids: null,
      topic: row.topic,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: "2026-10-22T00:00:00.000Z",
      before: null,
    } as const;
    const result = await api.deleteData(filter);
    expect(result.deleted).toBeGreaterThan(0);
    expect(api.data.data_decisions.total).toBeLessThan(before);
    await expect(refusal(api.deleteData({ ...filter, ids: [row.id] }))).resolves.toMatchObject({
      code: "invalid_request",
    });
  });

  it("normalizes second-precision delete bounds before comparing row stamps", async () => {
    const api = createFakeDecisionLoopApi();
    const row = api.data.data_decisions.rows.find((candidate) => candidate.kept === null);
    if (!row) throw new Error("expected deletable decision example");
    const second = row.at.slice(0, 19);
    const result = await api.deleteData({
      kind: "decisions",
      ids: null,
      topic: row.topic,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: `${second}Z`,
      until: `${new Date(Date.parse(`${second}Z`) + 1000).toISOString()}`,
      before: null,
    });
    expect(result.deleted).toBeGreaterThan(0);
    expect(api.data.data_decisions.rows.some((candidate) => candidate.id === row.id)).toBe(false);

    const untilApi = createFakeDecisionLoopApi();
    const untilRow = untilApi.data.data_decisions.rows.find((candidate) => candidate.kept === null);
    if (!untilRow) throw new Error("expected deletable decision example");
    untilRow.at = "2026-10-22T00:00:00.000Z";
    for (const candidate of untilApi.data.data_decisions.rows) {
      if (candidate.id !== untilRow.id) candidate.at = "2026-10-22T00:00:02.000Z";
    }
    const before = await untilApi.deleteData({
      kind: "decisions",
      ids: null,
      topic: untilRow.topic,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: "2026-10-21T23:59:59.000Z",
      until: "2026-10-22T00:00:00Z",
      before: null,
    });
    expect(before.deleted).toBe(0);
    expect(
      untilApi.data.data_decisions.rows.some((candidate) => candidate.id === untilRow.id),
    ).toBe(true);
  });
});
