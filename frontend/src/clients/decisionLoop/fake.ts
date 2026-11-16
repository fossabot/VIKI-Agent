/**
 * In-memory Decision API for UI tests. It never touches the network and answers every call at
 * once.
 *
 * `createFakeDecisionLoopApi(seed?)` answers from `data`: the examples.json views, with each
 * document `seed` names in their place. It records every call in `requests`, applies the
 * mutations a test looks for (feedback takes an item off the launcher or labels an episode,
 * saveKey and removeKey move the key, deleteEpisodes empties the history, missions start and
 * stop, preference forgets what was learned or marks the weekly check seen, data routes answer and
 * delete their seeded views, createProposal opens
 * one and actProposal approves or rejects it, actChange approves, redrafts or rejects the one
 * change item under `data.change_act`) and lets a test
 * script one failure per method with `fail(method, code)`. `data` starts as a deep copy and
 * every answer is one too, so neither the shared examples nor the fake's state change through an
 * object a test holds.
 *
 * examples.json is the wire contract both sides test against: one complete example per response
 * view, every optional member present and every array non-empty somewhere. Its `_nullable` lists
 * the member paths (dotted, `[]` for each array element) whose value may also be null; their
 * examples are non-null so the JSON type shows, except where the example's own state rules it
 * out: an "on" launcher's `paused_until`, a running mission's `finished_at`, an answered call's
 * `failure` and a passed test's `error`. fake.unit.test.ts pins both lists.
 */
import type { ChangeProposalItem, QuestionProposalItem } from "../../state/decisionsApi";
import examples from "./examples.json";
import {
  CANDIDATES,
  DATA_KINDS,
  DATA_SHOWN,
  type DataQuery,
  type DecisionLevel,
  type DecisionLoopApi,
  DecisionLoopApiError,
  type DecisionLoopExamples,
  FEEDBACK_HELD,
  FEEDBACK_SHOWN,
  ITEM_STATES,
  type MissionView,
  type ModelState,
  OUTCOMES,
  type QuestionProposalView,
  TOPICS,
  type TrialRate,
} from "./types";

/** The HTTP status of each refusal code. */
const STATUS: Readonly<Record<string, number>> = {
  invalid_request: 400,
  unknown_item: 404,
  not_applicable: 409,
  limit: 409,
  too_large: 413,
  invalid_representation: 415,
  busy: 429,
  unavailable: 503,
};
/** The levels the launcher's badge counts. */
const LOUD: readonly DecisionLevel[] = ["badge", "nudge", "alert"];
/** When the fake stops a mission. */
const STOPPED_AT = "2026-10-21T14:05:00.000Z";
/** `preference({name:"reset_learning"})`'s restart stamp: the design's "learn_since" = now. */
const RESET_LEARNING_SINCE = "2026-10-22T00:00:00.000Z";
/** `reset_learning`'s λ: the design's start value (`arbiter.py::START_LAMBDA`), what a fresh
 * scope paces from. */
const RESET_LAMBDA = 0.3;
/** `createProposal`'s stamp for a freshly opened proposal. */
const PROPOSAL_CREATED_AT = "2026-10-22T09:00:00.000Z";
/** `actProposal`'s stamp for an approve or reject. */
const PROPOSAL_ACTED_AT = "2026-10-22T09:05:00.000Z";
/** `actChange`'s stamp for an approve or reject. */
const CHANGE_ACTED_AT = "2026-10-22T09:10:00.000Z";
/** A fresh proposal's own `tried`: no decision has used it yet. */
const EMPTY_TRIAL_RATE: TrialRate = { yes_n: 0, yes_rate: null, no_n: 0, no_rate: null };

const DATA_QUERY_KEYS = [
  "kind",
  "ids",
  "topic",
  "shown",
  "answer",
  "state",
  "outcome",
  "since",
  "until",
  "before",
] as const;

const EXAMPLES = examples as unknown as DecisionLoopExamples;

/** A fresh deep copy of examples.json. */
export function decisionLoopExamples(): DecisionLoopExamples {
  return structuredClone(EXAMPLES);
}

/** The documents the fake answers from. */
export type FakeDecisionLoopData = Omit<DecisionLoopExamples, "_nullable" | "error">;

export interface FakeDecisionLoopRequest {
  method: keyof DecisionLoopApi;
  args: unknown[];
}

export interface FakeDecisionLoopApi extends DecisionLoopApi {
  /** The documents every call answers from; a test may edit them between calls. */
  data: FakeDecisionLoopData;
  /** Every call in order, with a copy of its arguments; abort signals are left out. */
  requests: FakeDecisionLoopRequest[];
  /** The next call of `method` is recorded, then rejects with `DecisionLoopApiError(code,
   * status)` and changes nothing; later calls answer again. `status` defaults to the code's
   * own status in `STATUS`, else 0. */
  fail(method: keyof DecisionLoopApi, code: string, status?: number): void;
}

export function createFakeDecisionLoopApi(
  seed: Partial<FakeDecisionLoopData> = {},
): FakeDecisionLoopApi {
  const { _nullable: _paths, error: _error, ...views } = decisionLoopExamples();
  const data: FakeDecisionLoopData = { ...views, ...structuredClone(seed) };
  const requests: FakeDecisionLoopRequest[] = [];
  const failures = new Map<keyof DecisionLoopApi, DecisionLoopApiError>();

  function record(method: keyof DecisionLoopApi, ...args: unknown[]): void {
    requests.push({ method, args: structuredClone(args) });
    const scripted = failures.get(method);
    if (scripted !== undefined) {
      failures.delete(method);
      throw scripted;
    }
  }
  function refuse(code: string): never {
    throw new DecisionLoopApiError(code, STATUS[code] ?? 0);
  }
  function validChoice(value: unknown, choices: readonly string[]): boolean {
    return typeof value === "string" && choices.includes(value);
  }
  function normalizeStamp(value: string): string {
    return value.length === 20 ? `${value.slice(0, -1)}.000Z` : value;
  }
  function validStamp(value: unknown): value is string {
    if (
      typeof value !== "string" ||
      value.startsWith("0000-") ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)
    )
      return false;
    const normalized = normalizeStamp(value);
    const timestamp = new Date(normalized);
    return Number.isFinite(timestamp.getTime()) && timestamp.toISOString() === normalized;
  }
  /** The wire's `parse_data_query` exact-key, kind, filter, cursor and delete checks. */
  function validDataQuery(query: DataQuery, deleting: boolean): boolean {
    if (
      query === null ||
      typeof query !== "object" ||
      Object.keys(query).length !== DATA_QUERY_KEYS.length ||
      DATA_QUERY_KEYS.some((key) => !Object.hasOwn(query, key)) ||
      !validChoice(query.kind, DATA_KINDS)
    )
      return false;
    const { kind } = query;
    const decision = kind === "decisions";
    const suggestion = kind === "suggestions";
    const calls = kind === "calls";
    if (
      (!decision && !suggestion && query.topic !== null) ||
      (decision && query.topic !== null && !validChoice(query.topic, TOPICS)) ||
      (suggestion && query.topic !== null && !validChoice(query.topic, CANDIDATES)) ||
      (!decision && query.shown !== null) ||
      (decision && query.shown !== null && !validChoice(query.shown, DATA_SHOWN)) ||
      (!decision && query.answer !== null) ||
      (decision &&
        query.answer !== null &&
        !validChoice(query.answer, [...FEEDBACK_SHOWN, ...FEEDBACK_HELD, "none"])) ||
      (!suggestion && query.state !== null) ||
      (suggestion && query.state !== null && !validChoice(query.state, ITEM_STATES)) ||
      (!calls && query.outcome !== null) ||
      (calls && query.outcome !== null && !validChoice(query.outcome, OUTCOMES)) ||
      (query.since !== null && !validStamp(query.since)) ||
      (query.until !== null && !validStamp(query.until)) ||
      (calls && (query.since !== null || query.until !== null)) ||
      (query.since !== null &&
        query.until !== null &&
        normalizeStamp(query.since) >= normalizeStamp(query.until)) ||
      (!deleting && query.ids !== null)
    )
      return false;
    if (query.before !== null) {
      if (
        deleting ||
        calls ||
        typeof query.before !== "object" ||
        Object.keys(query.before).length !== 2 ||
        !Object.hasOwn(query.before, "at") ||
        !Object.hasOwn(query.before, "id") ||
        !validStamp(query.before.at) ||
        (suggestion
          ? typeof query.before.id !== "string" || !/^[0-9a-f]{64}$/.test(query.before.id)
          : typeof query.before.id !== "string" ||
            !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(query.before.id))
      )
        return false;
    }
    if (!deleting) return true;
    if (query.before !== null) return false;
    if (query.ids !== null) {
      const validId = (id: unknown) =>
        typeof id === "string" &&
        (suggestion
          ? /^[0-9a-f]{64}$/.test(id)
          : /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id));
      return (
        Array.isArray(query.ids) &&
        query.ids.length >= 1 &&
        query.ids.length <= 50 &&
        query.ids.every(validId) &&
        new Set(query.ids).size === query.ids.length &&
        query.until === null &&
        query.topic === null &&
        query.shown === null &&
        query.answer === null &&
        query.state === null &&
        query.outcome === null &&
        query.since === null
      );
    }
    return (
      !calls &&
      query.until !== null &&
      (query.topic !== null ||
        query.shown !== null ||
        query.answer !== null ||
        query.state !== null ||
        query.since !== null)
    );
  }
  /** The launcher's open and badge counts, and the page's open count, follow its items. */
  function recount(): void {
    const launcher = data.launcher;
    launcher.open = launcher.items.length;
    launcher.count = launcher.items.filter((item) => LOUD.includes(item.level)).length;
    if (launcher.open === 0) launcher.level = "idle";
    data.page.status.open = launcher.open;
  }
  function setModel(state: ModelState): void {
    data.page.model.state = state;
    data.launcher.model = state;
  }
  /** Forgets the fitted snapshot (and whatever was computed from the rows that taught it): every
   * learned number reverts to its own start value with every support count (`topics`, `reach`,
   * `costs`; `weights` carries none) at 0, `labels` 0 and `fitted_at` null, matching
   * `wire.py::learning_view`'s own fallback with no snapshot (`learned` None); calibration,
   * question rates and the shadow comparison empty too, since they are read over the same rows
   * (`service.py::_learning`, `calibration.py`). Shared by `resetLearning` (which also restarts
   * `since`/λ) and `deleteEpisodes` (which leaves them: delete-all keeps λ and never sets a
   * `learn_since`). */
  function forgetLearnedSnapshot(): void {
    const learning = data.page.learning;
    learning.labels = 0;
    learning.fitted_at = null;
    learning.bias.now = learning.bias.start;
    for (const weight of learning.weights) weight.now = weight.start;
    for (const topic of learning.topics) {
      topic.now = topic.start;
      topic.support = 0;
    }
    for (const row of learning.reach) {
      row.now = row.start;
      row.support = 0;
    }
    for (const row of learning.costs) {
      row.now = row.start;
      row.support = 0;
    }
    learning.calibration = {
      n: 0,
      brier: null,
      bins: learning.calibration.bins.map((bin) => ({ ...bin, n: 0, mean_p: null, rate: null })),
    };
    for (const question of learning.questions) {
      question.yes_n = 0;
      question.yes_rate = null;
      question.no_n = 0;
      question.no_rate = null;
    }
    learning.shadow = {
      ...learning.shadow,
      decisions: 0,
      agree: 0,
      kind_agree: 0,
      labels: 0,
      brier: null,
      live_brier: null,
    };
  }
  /** `reset_learning`: forgets the fitted snapshot and restarts from now, as the engine's Reset
   * does — beside `forgetLearnedSnapshot`, λ returns to its own start value and `since`
   * becomes the restart stamp; decisions and labels stay. */
  function resetLearning(): void {
    forgetLearnedSnapshot();
    data.page.learning.since = RESET_LEARNING_SINCE;
    data.page.learning.attention_price = RESET_LAMBDA;
  }

  return {
    data,
    requests,
    fail(method, code, status = STATUS[code] ?? 0) {
      failures.set(method, new DecisionLoopApiError(code, status));
    },
    async launcher() {
      record("launcher");
      return structuredClone(data.launcher);
    },
    async openLauncher() {
      record("openLauncher");
      return structuredClone(data.launcher);
    },
    async presence(body) {
      record("presence", body);
    },
    async feedback(body) {
      record("feedback", body);
      if (body.item_key !== null) {
        const at = data.launcher.items.findIndex((item) => item.item_key === body.item_key);
        if (at < 0) refuse("unknown_item");
        data.launcher.items.splice(at, 1);
        recount();
      } else {
        const rows = [...data.page.held_back, ...data.page.recent].filter(
          (row) => row.episode_id === body.episode_id,
        );
        if (rows.length === 0) refuse("unknown_item");
        // Labelling one of the weekly check's own rows (`page.held_back`, the launcher's audit
        // sample) drops it from the launcher's review count too, due going false at 0
        // (stats.py::review).
        const checked = data.page.held_back.find((row) => row.episode_id === body.episode_id);
        if (checked !== undefined && checked.feedback === null) {
          const review = data.launcher.review;
          review.count = Math.max(0, review.count - 1);
          if (review.count === 0) review.due = false;
        }
        for (const row of rows) row.feedback = body.value;
      }
      return structuredClone(data.launcher);
    },
    async observe(event, itemKeys) {
      record("observe", event, itemKeys);
    },
    async report(topic, source) {
      record("report", topic, source);
      return structuredClone(data.page);
    },
    async page() {
      record("page");
      return structuredClone(data.page);
    },
    async preference(body) {
      record("preference", body);
      if (body.name === "unmute") {
        data.page.muted = data.page.muted.filter((candidate) => candidate !== body.candidate);
      } else if (body.name === "reset_learning") {
        resetLearning();
      } else if (body.name === "review_seen") {
        data.launcher.review.due = false;
      }
      return structuredClone(data.page);
    },
    async deleteEpisodes() {
      record("deleteEpisodes");
      const { launcher, page, calls } = data;
      const deleted = page.data.episodes;
      launcher.items = [];
      recount();
      page.held_back = [];
      page.recent = [];
      page.counts = {
        shown: 0,
        useful: 0,
        wrong: 0,
        later: 0,
        mute: 0,
        missed: 0,
        right_to_hold: 0,
      };
      page.privacy.last_call = null;
      page.data.episodes = 0;
      page.data.oldest = null;
      forgetLearnedSnapshot();
      calls.calls = [];
      calls.latest = null;
      return { deleted };
    },
    async saveKey(key) {
      record("saveKey", key);
      data.page.model.key = "saved";
      // A new key ends a rejected key's pause.
      if (data.page.model.state === "no_key" || data.page.model.state === "key_rejected") {
        setModel("ready");
      }
      return structuredClone(data.page);
    },
    async removeKey() {
      record("removeKey");
      data.page.model.key = "missing";
      setModel("no_key");
      return structuredClone(data.page);
    },
    async test() {
      record("test");
      return structuredClone(data.test_result);
    },
    async calls() {
      record("calls");
      return structuredClone(data.calls);
    },
    async call(callId) {
      record("call", callId);
      // A listed call answers with `call_detail` under that call's own id, time, title and chat.
      const summary = data.calls.calls.find((listed) => listed.call_id === callId);
      if (summary === undefined) return refuse("unknown_item");
      const { call_id, at, title, candidate, session_id } = summary;
      return { ...structuredClone(data.call_detail), call_id, at, title, candidate, session_id };
    },
    async dataSummary() {
      record("dataSummary");
      return structuredClone(data.data_summary);
    },
    async dataRecords(query) {
      record("dataRecords", query);
      if (!validDataQuery(query, false)) refuse("invalid_request");
      const view = data[`data_${query.kind}`] as
        | DecisionLoopExamples["data_decisions"]
        | DecisionLoopExamples["data_suggestions"]
        | DecisionLoopExamples["data_calls"];
      return structuredClone(view);
    },
    async dataRecord(kind, id) {
      record("dataRecord", kind, id);
      const view = structuredClone(
        kind === "decisions" ? data.data_decision : data.data_suggestion,
      );
      return { ...view, id };
    },
    async deleteData(query) {
      record("deleteData", query);
      if (!validDataQuery(query, true)) refuse("invalid_request");
      const view = data[`data_${query.kind}`] as {
        total: number;
        deletable: number;
        rows: {
          id: string;
          at: string;
          topic?: string;
          entry?: string;
          level?: string | null;
          answer?: string | null;
          state?: string;
          kept?: string | null;
        }[];
      };
      const since = query.since === null ? null : normalizeStamp(query.since);
      const until = query.until === null ? null : normalizeStamp(query.until);
      const matches = (row: (typeof view.rows)[number]) =>
        query.ids !== null
          ? query.ids.includes(row.id)
          : (query.topic === null || row.topic === query.topic) &&
            (since === null || row.at >= since) &&
            (until === null || row.at < until) &&
            (query.kind === "decisions"
              ? (query.shown === null ||
                  (query.shown === "report"
                    ? row.entry === "report"
                    : query.shown === "held"
                      ? row.entry === "decision" && row.level === "silent"
                      : query.shown === "listed"
                        ? row.level === "list"
                        : ["badge", "nudge", "alert"].includes(row.level ?? ""))) &&
                (query.answer === null ||
                  (query.answer === "none" ? row.answer === null : row.answer === query.answer))
              : query.state === null || row.state === query.state);
      const selected = view.rows.filter((row) => matches(row));
      const deletable = selected.filter((row) => row.kept === null || query.kind === "calls");
      const removed = new Set(deletable.map((row) => row.id));
      view.rows = view.rows.filter((row) => !removed.has(row.id));
      view.total = Math.max(0, view.total - removed.size);
      view.deletable = Math.max(0, view.deletable - removed.size);
      if (query.kind === "calls")
        data.calls.calls = data.calls.calls.filter((row) => !removed.has(row.call_id));
      return { deleted: removed.size, kept: selected.length - removed.size };
    },
    async missions() {
      record("missions");
      return structuredClone(data.page.missions);
    },
    async startMission(itemKey, work) {
      record("startMission", itemKey, work);
      const item = data.launcher.items.find((open) => open.item_key === itemKey);
      if (item === undefined) return refuse("unknown_item");
      // `mission` lends its ids and start time, so a test knows the chat that opens.
      const mission: MissionView = {
        ...data.mission,
        work,
        state: "running",
        title: item.what,
        finished_at: null,
        reason: "",
      };
      const missions = data.page.missions;
      missions.running = [
        ...missions.running.filter((running) => running.mission_id !== mission.mission_id),
        mission,
      ];
      missions.today[work] += 1;
      data.launcher.working = missions.running.length;
      if (!data.launcher.started.includes(mission.session_id)) {
        data.launcher.started.unshift(mission.session_id);
      }
      return structuredClone(mission);
    },
    async stopMission(missionId) {
      record("stopMission", missionId);
      const missions = data.page.missions;
      const mission = missions.running.find((running) => running.mission_id === missionId);
      if (mission === undefined) return refuse("unknown_item");
      missions.running = missions.running.filter((running) => running !== mission);
      data.launcher.working = missions.running.length;
      const stopped: MissionView = {
        ...mission,
        state: "stopped",
        finished_at: STOPPED_AT,
        reason: "owner",
      };
      return stopped;
    },
    async stopAllMissions() {
      record("stopAllMissions");
      data.page.missions.running = [];
      data.launcher.working = 0;
      return structuredClone(data.page.missions);
    },
    async startSeat(routineId) {
      record("startSeat", routineId);
      return structuredClone(data.page.missions);
    },
    async stopSeat(routineId) {
      record("stopSeat", routineId);
      return structuredClone(data.page.missions);
    },
    async runChecks(workspaceId) {
      record("runChecks", workspaceId);
      return structuredClone(data.page.missions);
    },
    async stopChecks(workspaceId) {
      record("stopChecks", workspaceId);
      return structuredClone(data.page.missions);
    },
    async suggestInbox(itemKey) {
      record("suggestInbox", itemKey);
      return structuredClone(data.page.missions);
    },
    async proposal(id) {
      record("proposal", id);
      if (data.question_proposal.proposal.id !== id) return refuse("unknown_item");
      return structuredClone(data.question_proposal);
    },
    async createProposal(name, question) {
      record("createProposal", name, question);
      const was = data.question_proposal;
      const proposal: QuestionProposalItem = {
        ...was.proposal,
        state: "open",
        attention: true,
        name,
        created_at: PROPOSAL_CREATED_AT,
        updated_at: PROPOSAL_CREATED_AT,
        approved_at: null,
        rejected_at: null,
        reason: null,
      };
      const view: QuestionProposalView = {
        proposal,
        label: was.label,
        current_text: was.current_text,
        proposed_text: JSON.stringify(question, null, 2),
        tried: {
          decisions: 0,
          agree: 0,
          labels: 0,
          live: EMPTY_TRIAL_RATE,
          trial: EMPTY_TRIAL_RATE,
        },
      };
      data.question_proposal = view;
      return structuredClone(proposal);
    },
    async actProposal(id, action, revision, reason) {
      record("actProposal", id, action, revision, reason);
      const { proposal } = data.question_proposal;
      if (proposal.id !== id) return refuse("unknown_item");
      // Undo is a reject on an approved proposal ("Reject (open) or Undo (in use)");
      // approve only ever acts on an open one; either way a stale revision refuses too.
      const fromRight =
        proposal.state === "open" || (action === "reject" && proposal.state === "approved");
      if (proposal.revision !== revision || !fromRight) return refuse("not_applicable");
      const updated: QuestionProposalItem =
        action === "approve"
          ? {
              ...proposal,
              state: "approved",
              attention: false,
              approved_at: PROPOSAL_ACTED_AT,
              updated_at: PROPOSAL_ACTED_AT,
            }
          : {
              ...proposal,
              state: "rejected",
              attention: false,
              rejected_at: PROPOSAL_ACTED_AT,
              updated_at: PROPOSAL_ACTED_AT,
              reason: reason ?? null,
            };
      data.question_proposal = { ...data.question_proposal, proposal: updated };
      return structuredClone(updated);
    },
    async actChange(id, action, revision, reason) {
      record("actChange", id, action, revision, reason);
      const { change, draft } = data.change_act;
      if (change.id !== id) return refuse("unknown_item");
      if (change.revision !== revision) return refuse("not_applicable");
      // Approve on open opens the draft; approve on an already-approved
      // change writes nothing and answers the same draft again; reject on open stamps the stripped
      // reason and leaves no draft; every other action/state pairing (reject on approved -- changes
      // have no undo, unlike actProposal -- or on rejected) is not_applicable.
      if (action === "approve" && change.state === "open") {
        const updated: ChangeProposalItem = {
          ...change,
          state: "approved",
          attention: false,
          approved_at: CHANGE_ACTED_AT,
          updated_at: CHANGE_ACTED_AT,
        };
        data.change_act = { change: updated, draft };
        return structuredClone(data.change_act);
      }
      if (action === "approve" && change.state === "approved") {
        return structuredClone(data.change_act);
      }
      if (action === "reject" && change.state === "open") {
        const updated: ChangeProposalItem = {
          ...change,
          state: "rejected",
          attention: false,
          rejected_at: CHANGE_ACTED_AT,
          updated_at: CHANGE_ACTED_AT,
          reason: reason?.trim() ?? null,
        };
        data.change_act = { change: updated, draft: null };
        return structuredClone(data.change_act);
      }
      return refuse("not_applicable");
    },
  };
}
