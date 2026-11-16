/**
 * Strict parsers for the Decision routes' response bodies. Each `parseX(value: unknown): X |
 * null` returns a value whose shape, enums, stamps and bounds all match the wire contract, or
 * `null` for anything else — the caller (`decisionLoopApi.ts`) treats `null` the same as a
 * malformed response. A value that parses is returned exactly as the server sent it: nothing
 * here rewrites, trims or escapes a string.
 *
 * `examples.json` (via `fake.ts`) holds one complete, valid example of every shape below;
 * `parse.unit.test.ts` proves each one parses and that a single damaged field anywhere in it
 * fails the whole parse.
 */

import {
  type ChangeProposalItem,
  isChangeProposalItem,
  isQuestionProposalItem,
  type QuestionProposalItem,
} from "../../state/decisionsApi";
import {
  ACTION_TYPES,
  type ActionType,
  CANDIDATES,
  type CallDetailView,
  type CallSummary,
  type CallsView,
  type ChangeActResult,
  DATA_KINDS,
  type DataCallRow,
  type DataDay,
  type DataDecisionRow,
  type DataDecisionView,
  type DataDeleted,
  type DataRecordsView,
  type DataSuggestionRow,
  type DataSuggestionView,
  type DataSummaryView,
  type DataTopic,
  type DecisionLevel,
  type ErrorBody,
  FEEDBACK_HELD,
  FEEDBACK_REASONS,
  FEEDBACK_SHOWN,
  FINDING_KINDS,
  type HeldBackRow,
  ITEM_STATES,
  type ItemAction,
  type ItemEvidence,
  type ItemView,
  KEY_STATES,
  KINDS,
  LAUNCHER_LEVELS,
  LAUNCHER_STATES,
  type LauncherView,
  LEVELS,
  type LearningCalibration,
  type LearningCalibrationBin,
  type LearningLevelNumber,
  type LearningQuestionRate,
  type LearningSecondLooks,
  type LearningShadowStats,
  type LearningTopic,
  type LearningView,
  type LearningWeight,
  MISSION_STATES,
  MISSION_WORK,
  type MissionView,
  MODEL_ERROR_CODES,
  MODEL_STATES,
  OUTCOMES,
  type PageMissions,
  type PageView,
  type PlanHold,
  POLICIES,
  type QuestionProposalView,
  type RecentRow,
  type ShadowSetting,
  type TestResultView,
  TOPICS,
  type TrialRate,
  VERSION_SETTINGS,
} from "./types";

// ---- bounds; a "not set by the wire contract" comment marks a defensive
// choice of ours where the contract leaves the ceiling unstated. ----
const MAX_WHAT = 300;
const MAX_WHY = 400;
const MAX_LABEL = 120;
/** A draft action's own `text` (draft_chat, draft_new). */
const MAX_DRAFT_TEXT = 4_000;
/** A question proposal's two wordings (`current_text`, `proposed_text`):
 * indented JSON, shown in a `<pre>` like `state_text`, so they keep `\n` and `\t` too. */
const MAX_PROPOSAL_TEXT = 4_000;
const MAX_STATE_TEXT = 20_000;
/** Not set by the wire contract. `questions_text` is the same kind of JSON-blob field as
 * `state_text`, so it keeps the same generous ceiling; the contract only names `state_text` as
 * shown in a `<pre>`, so this
 * field keeps the plain no-control-character rule rather than the `\n`/`\t` exception below. */
const MAX_QUESTIONS_TEXT = 20_000;
/** Not set by the wire contract: the generic ceiling for text it doesn't bound on its own (titles,
 * reason text, a playbook version, a split part name, a question row). Reuses `why`'s bound. */
const MAX_TEXT = 400;
/** The same ceiling as the presence request's `note.ref`. */
const MAX_REF = 512;
const MAX_ITEMS = 50;
const MAX_CALLS = 20;
const MAX_RECENT = 20;
const MAX_HELD = 50;
const MAX_STARTED = 50;
/** Not set by the wire contract: one item's evidence chips, kept finite. */
const MAX_EVIDENCE = 20;
/** An item cannot offer more actions than action types exist. */
const MAX_ACTIONS = ACTION_TYPES.length;
/** Not set by the wire contract: the same ceiling as `calls`/`recent`. */
const MAX_RUNNING = 20;
/** Not set by the wire contract: the state split's own part names are a short, fixed list. */
const MAX_SPLIT = 20;
/** Not set by the wire contract, kept finite. */
const MAX_QUESTIONS = 50;
/** The learning block's own fixed-size arrays: "weights[5]", "topics[7]", "reach[4]",
 * "costs[3]", "bins[5]" and "questions[7]". */
const MAX_LEARNING_WEIGHTS = 5;
const MAX_LEARNING_TOPICS = CANDIDATES.length;
const REACH_LEVELS: readonly DecisionLevel[] = ["list", "badge", "nudge", "alert"];
const COST_LEVELS: readonly DecisionLevel[] = ["badge", "nudge", "alert"];
const MAX_LEARNING_BINS = 5;
/** The calibration block's own 5 bin edges, keyed by `binKey` (rounded `low:high`), in any
 * order: `wire.py::_calibration_view`, calibration.py's bins and examples.json agree on
 * [0, 0.2), [0.2, 0.4), [0.4, 0.6), [0.6, 0.8), [0.8, 1]. */
const LEARNING_BIN_KEYS = ["0:0.2", "0.2:0.4", "0.4:0.6", "0.6:0.8", "0.8:1"] as const;
const MAX_LEARNING_QUESTIONS = 7;
const SHADOW_SETTINGS: readonly ShadowSetting[] = ["off", ...VERSION_SETTINGS];

const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ITEM_KEY = /^[0-9a-f]{64}$/;
/** `display_version(model)`'s own shape: the first `\d+(\.\d+){1,3}` match, or "". */
const DISPLAY_VERSION = /^\d+(\.\d+){1,3}$/;
/** The short machine code this app's other clients use for a server error (shellApi.ts). */
const ERROR_CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;

/** `code` is a control character this wire never carries as data (DEL counts as one too); `\t`
 * (9) and `\n` (10) are let through only when `allowNewlineTab`, for `state_text`. Checked by
 * code point, not a regex range, so nothing here needs a backslash escape sequence. */
function isControlCode(code: number, allowNewlineTab: boolean): boolean {
  if (code === 0x7f) return true;
  if (code >= 0x20) return false;
  if (allowNewlineTab && (code === 0x09 || code === 0x0a)) return false;
  return true;
}
function hasControlChar(value: string, allowNewlineTab: boolean): boolean {
  for (let i = 0; i < value.length; i++) {
    if (isControlCode(value.charCodeAt(i), allowNewlineTab)) return true;
  }
  return false;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
/** `value` is an object with exactly `keys` as its own keys: nothing extra, nothing missing. A
 * member that is nullable per types.ts / examples.json `_nullable` is still a required key here —
 * only its value may be `null`. */
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
/** `exact`, except that any of `retired` may also be present, whatever it holds: members an
 * earlier server sent that this client no longer reads. */
function exactBesides(
  value: unknown,
  keys: readonly string[],
  retired: readonly string[],
): value is Record<string, unknown> {
  return (
    object(value) &&
    keys.every((key) => key in value) &&
    Object.keys(value).every((key) => keys.includes(key) || retired.includes(key))
  );
}
function flag(value: unknown): value is boolean {
  return typeof value === "boolean";
}
/** A non-negative integer: every count in the wire. */
function count(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}
/** A finite, non-negative amount: `cost_usd`, `cap_usd`, and the learning block's own
 * non-negative numbers (weights, reach, costs, `attention_price`, calibration and rates). */
function cost(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}
function nullableCost(value: unknown): value is number | null {
  return value === null || cost(value);
}
/** A finite real number with no sign restriction: `learning.bias` and a topic's own fitted
 * offset (bias bounds [-3, 1], topic bounds [-1.5, 1.5]),
 * both of which the server may legitimately send negative. */
function real(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
function stamp(value: unknown): value is string {
  return typeof value === "string" && STAMP.test(value);
}
function nullableStamp(value: unknown): value is string | null {
  return value === null || stamp(value);
}
/** The page's own `paused_until`/`focus_until`: a stamp, or "" when unset. */
function stampOrBlank(value: unknown): value is string {
  return value === "" || stamp(value);
}
function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function nullableUuid(value: unknown): value is string | null {
  return value === null || uuid(value);
}
function itemKey(value: unknown): value is string {
  return typeof value === "string" && ITEM_KEY.test(value);
}
function displayVersion(value: unknown): value is string {
  return value === "" || (typeof value === "string" && DISPLAY_VERSION.test(value));
}
function nullableDisplayVersion(value: unknown): value is string | null {
  return value === null || displayVersion(value);
}
function oneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value);
}
function nullableOneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T | null {
  return value === null || oneOf(value, allowed);
}
/** Kept as data: no HTML handling, no trimming that would change meaning, bounded, and no
 * control character (every string but `state_text` refuses `\n`/`\t` too). */
function text(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max && !hasControlChar(value, false);
}
/** `state_text`: the Context tab renders it in a `<pre>`, so `\n` and `\t` pass through; every
 * other control character is still refused. */
function stateText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max && !hasControlChar(value, true);
}
/** A draft action's own `text`: 1..`MAX_DRAFT_TEXT` UTF-16 units, with `\n` and `\t` allowed (as
 * `stateText`) but, unlike every other string here, a required minimum length — an empty draft
 * defeats the point of "fills a chat's message box". */
function draftText(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= MAX_DRAFT_TEXT &&
    !hasControlChar(value, true)
  );
}
function boundedArray(value: unknown, max: number): value is unknown[] {
  return Array.isArray(value) && value.length <= max;
}
/** `items.length` equals `allowed.length`, and the keys `keyOf` derives from `items` form
 * exactly the `allowed` set: nothing extra, missing or duplicated. Pairs with `boundedArray`
 * (and each element's own shape check) on the Learning block's own arrays that must also cover a
 * known vocabulary once each -- `topics` (`CANDIDATES`), `reach` (`REACH_LEVELS`), `costs`
 * (`COST_LEVELS`) and `bins` (`LEARNING_BIN_KEYS`) -- which length alone lets a dropped, doubled
 * or substituted member slip through. */
function exactSet<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
  allowed: readonly string[],
): boolean {
  if (items.length !== allowed.length) return false;
  const keys = new Set(items.map(keyOf));
  return keys.size === allowed.length && allowed.every((key) => keys.has(key));
}
/** `items` has exactly `size` entries and no two share a `keyOf`. `weights` and `questions`
 * name design facts (`arbiter.FEATURES`, the seven noul `playbook.questions`) this client does
 * not pin as a vocabulary the way `CANDIDATES` or a level list is, so this checks shape --
 * distinct, the right count -- rather than `exactSet`'s membership in a known set. */
function exactlyDistinct<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
  size: number,
): boolean {
  return items.length === size && new Set(items.map(keyOf)).size === size;
}
/** An object with exactly `keys`, every value a non-negative integer: `counts`, and `today`/
 * `limits` keyed by `MISSION_WORK`. */
function countMap(value: unknown, keys: readonly string[]): boolean {
  if (!exact(value, keys)) return false;
  const row = value;
  return keys.every((key) => count(row[key]));
}

const FEEDBACK = [...FEEDBACK_SHOWN, ...FEEDBACK_HELD] as const;

// ---- ItemAction: "only the members its type uses are present" (types.ts) ----
const ACTION_EXTRAS: Readonly<Record<ActionType, readonly string[]>> = {
  open_chat: ["session_id"],
  open_note: ["workspace_id", "ref"],
  open_approvals: [],
  open_inbox: [],
  open_routine: ["routine_id"],
  open_runs: [],
  open_context: ["call_id"],
  open_vault: ["workspace_id"],
  open_messaging: [],
  open_code: ["workspace_id"],
  open_triage: [],
  start_prepare: [],
  start_run: [],
  stop_mission: ["mission_id"],
  start_seat: ["routine_id"],
  stop_seat: ["routine_id"],
  run_checks: ["workspace_id"],
  stop_checks: ["workspace_id"],
  suggest_inbox: [],
  draft_chat: ["session_id", "text"],
  draft_new: ["text"],
};
function itemAction(value: unknown): value is ItemAction {
  if (!object(value)) return false;
  const type = value.type;
  if (!oneOf(type, ACTION_TYPES)) return false;
  const extras = ACTION_EXTRAS[type];
  if (!exact(value, ["type", "label", "primary", ...extras])) return false;
  if (!text(value.label, MAX_LABEL) || !flag(value.primary)) return false;
  for (const key of extras) {
    let ok: boolean;
    if (key === "ref") ok = text(value.ref, MAX_REF);
    else if (key === "text") ok = draftText(value.text);
    else ok = uuid(value[key]);
    if (!ok) return false;
  }
  return true;
}

// ---- ItemEvidence: a note chip (workspace_id+ref), a chat chip (session_id), or a plain label ----
function itemEvidence(value: unknown): value is ItemEvidence {
  if (!object(value)) return false;
  const hasNote = "workspace_id" in value || "ref" in value;
  const hasChat = "session_id" in value;
  if (hasNote && hasChat) return false;
  const keys = hasNote
    ? ["label", "workspace_id", "ref"]
    : hasChat
      ? ["label", "session_id"]
      : ["label"];
  if (!exact(value, keys)) return false;
  if (!text(value.label, MAX_LABEL)) return false;
  if (hasNote && (!uuid(value.workspace_id) || !text(value.ref, MAX_REF))) return false;
  if (hasChat && !uuid(value.session_id)) return false;
  return true;
}

function itemView(value: unknown): value is ItemView {
  return (
    exact(value, [
      "item_key",
      "episode_id",
      "candidate",
      "kind",
      "level",
      "what",
      "why",
      "evidence",
      "actions",
      "created_at",
      "mission_id",
    ]) &&
    itemKey(value.item_key) &&
    uuid(value.episode_id) &&
    oneOf(value.candidate, CANDIDATES) &&
    oneOf(value.kind, KINDS) &&
    oneOf(value.level, LEVELS) &&
    text(value.what, MAX_WHAT) &&
    text(value.why, MAX_WHY) &&
    boundedArray(value.evidence, MAX_EVIDENCE) &&
    value.evidence.every(itemEvidence) &&
    boundedArray(value.actions, MAX_ACTIONS) &&
    value.actions.every(itemAction) &&
    stamp(value.created_at) &&
    nullableUuid(value.mission_id)
  );
}

function launcherReview(value: unknown): boolean {
  return exact(value, ["due", "count"]) && flag(value.due) && count(value.count);
}

function launcherView(value: unknown): value is LauncherView {
  return (
    exact(value, [
      "state",
      "level",
      "count",
      "nudge",
      "glow",
      "working",
      "open",
      "checked_at",
      "paused_until",
      "focus_until",
      "model",
      "items",
      "started",
      "review",
      "plans_waiting",
    ]) &&
    oneOf(value.state, LAUNCHER_STATES) &&
    oneOf(value.level, LAUNCHER_LEVELS) &&
    count(value.count) &&
    count(value.nudge) &&
    count(value.glow) &&
    count(value.working) &&
    count(value.open) &&
    nullableStamp(value.checked_at) &&
    nullableStamp(value.paused_until) &&
    nullableStamp(value.focus_until) &&
    oneOf(value.model, MODEL_STATES) &&
    boundedArray(value.items, MAX_ITEMS) &&
    value.items.every(itemView) &&
    boundedArray(value.started, MAX_STARTED) &&
    value.started.every(uuid) &&
    launcherReview(value.review) &&
    count(value.plans_waiting)
  );
}
/** GET `launcher`, POST `launcher/open`, POST `feedback`. */
export function parseLauncher(value: unknown): LauncherView | null {
  return launcherView(value) ? value : null;
}

function heldBackRow(value: unknown): value is HeldBackRow {
  return (
    exact(value, ["episode_id", "created_at", "candidate", "level", "reason", "why", "feedback"]) &&
    uuid(value.episode_id) &&
    stamp(value.created_at) &&
    oneOf(value.candidate, CANDIDATES) &&
    oneOf(value.level, LEVELS) &&
    text(value.reason, MAX_TEXT) &&
    text(value.why, MAX_WHY) &&
    nullableOneOf(value.feedback, FEEDBACK)
  );
}
function recentRow(value: unknown): value is RecentRow {
  return (
    exact(value, [
      "episode_id",
      "created_at",
      "candidate",
      "level",
      "policy",
      "reason",
      "why",
      "feedback",
    ]) &&
    uuid(value.episode_id) &&
    stamp(value.created_at) &&
    oneOf(value.candidate, CANDIDATES) &&
    oneOf(value.level, LEVELS) &&
    oneOf(value.policy, POLICIES) &&
    text(value.reason, MAX_TEXT) &&
    text(value.why, MAX_WHY) &&
    nullableOneOf(value.feedback, FEEDBACK)
  );
}

function missionView(value: unknown): value is MissionView {
  return (
    exact(value, [
      "mission_id",
      "session_id",
      "work",
      "state",
      "title",
      "started_at",
      "finished_at",
      "reason",
    ]) &&
    uuid(value.mission_id) &&
    uuid(value.session_id) &&
    oneOf(value.work, MISSION_WORK) &&
    oneOf(value.state, MISSION_STATES) &&
    text(value.title, MAX_TEXT) &&
    stamp(value.started_at) &&
    nullableStamp(value.finished_at) &&
    text(value.reason, MAX_TEXT)
  );
}
/** POST `missions`, POST `missions/{id}/stop`. */
export function parseMission(value: unknown): MissionView | null {
  return missionView(value) ? value : null;
}

function planHold(value: unknown): value is PlanHold | null {
  return value === null || (exact(value, ["until"]) && nullableStamp(value.until));
}

function pageMissions(value: unknown): value is PageMissions {
  return (
    exact(value, [
      "running",
      "today",
      "limits",
      "hold",
      "team",
      "look",
      "second_look",
      "project_plan",
      "inbox",
      "checks",
    ]) &&
    boundedArray(value.running, MAX_RUNNING) &&
    value.running.every(missionView) &&
    countMap(value.today, MISSION_WORK) &&
    countMap(value.limits, MISSION_WORK) &&
    planHold(value.hold) &&
    countMap(value.team, ["today", "limit", "running"]) &&
    countMap(value.look, ["today", "limit"]) &&
    countMap(value.second_look, ["today", "limit"]) &&
    countMap(value.project_plan, ["today", "limit"]) &&
    countMap(value.inbox, ["today", "limit"]) &&
    (value.checks === null || countMap(value.checks, ["today", "limit", "running"]))
  );
}
/** GET `missions`, POST `missions/stop`: the page's own `missions` block. */
export function parsePageMissions(value: unknown): PageMissions | null {
  return pageMissions(value) ? value : null;
}

function pageStatus(value: unknown): boolean {
  return (
    exact(value, ["open", "checked_at"]) && count(value.open) && nullableStamp(value.checked_at)
  );
}
function pageModelVersion(value: unknown): boolean {
  return (
    exact(value, ["setting", "display"]) &&
    oneOf(value.setting, VERSION_SETTINGS) &&
    displayVersion(value.display)
  );
}
function pageLastTest(value: unknown): boolean {
  return (
    value === null ||
    (exact(value, ["at", "latency_ms"]) && stamp(value.at) && count(value.latency_ms))
  );
}
function pageModel(value: unknown): boolean {
  return (
    exact(value, ["key", "version", "state", "last_error", "last_test"]) &&
    oneOf(value.key, KEY_STATES) &&
    pageModelVersion(value.version) &&
    oneOf(value.state, MODEL_STATES) &&
    nullableOneOf(value.last_error, MODEL_ERROR_CODES) &&
    pageLastTest(value.last_test)
  );
}
function pageToday(value: unknown): boolean {
  return (
    exact(value, [
      "decisions",
      "model_calls",
      "tokens",
      "cost_usd",
      "cap_usd",
      "capped",
      "shadow_calls",
    ]) &&
    count(value.decisions) &&
    count(value.model_calls) &&
    count(value.tokens) &&
    cost(value.cost_usd) &&
    cost(value.cap_usd) &&
    flag(value.capped) &&
    count(value.shadow_calls)
  );
}
function pageLastCall(value: unknown): boolean {
  return (
    value === null ||
    (exact(value, ["call_id", "at", "tokens"]) &&
      uuid(value.call_id) &&
      stamp(value.at) &&
      count(value.tokens))
  );
}
function pagePrivacy(value: unknown): boolean {
  return (
    exact(value, ["last_call", "held_back_today"]) &&
    pageLastCall(value.last_call) &&
    count(value.held_back_today)
  );
}
function pageChanges(value: unknown): boolean {
  return exact(value, ["today", "limit"]) && count(value.today) && count(value.limit);
}
function pageData(value: unknown): boolean {
  return (
    exact(value, ["episodes", "oldest", "max", "retention_days"]) &&
    count(value.episodes) &&
    nullableStamp(value.oldest) &&
    count(value.max) &&
    count(value.retention_days)
  );
}

// ---- learning ----

function learningStartNow(value: unknown): boolean {
  return exact(value, ["start", "now"]) && real(value.start) && real(value.now);
}
function learningWeight(value: unknown): value is LearningWeight {
  return (
    exact(value, ["name", "label", "start", "now"]) &&
    text(value.name, MAX_TEXT) &&
    text(value.label, MAX_LABEL) &&
    cost(value.start) &&
    cost(value.now)
  );
}
function learningTopic(value: unknown): value is LearningTopic {
  return (
    exact(value, ["candidate", "start", "now", "support"]) &&
    oneOf(value.candidate, CANDIDATES) &&
    real(value.start) &&
    real(value.now) &&
    count(value.support)
  );
}
function learningLevelNumber(
  value: unknown,
  allowed: readonly DecisionLevel[],
): value is LearningLevelNumber {
  return (
    exact(value, ["level", "start", "now", "support"]) &&
    oneOf(value.level, allowed) &&
    cost(value.start) &&
    cost(value.now) &&
    count(value.support)
  );
}
/** `reach`'s own element shape, over `REACH_LEVELS`: a named reference, like `learningWeight`,
 * so `.every` narrows the array for the `exactSet` check right after it. */
function learningReachRow(value: unknown): value is LearningLevelNumber {
  return learningLevelNumber(value, REACH_LEVELS);
}
/** `costs`' own element shape, over `COST_LEVELS` (see `learningReachRow`). */
function learningCostRow(value: unknown): value is LearningLevelNumber {
  return learningLevelNumber(value, COST_LEVELS);
}
/** What an earlier server also sent and this client no longer reads: the Learning block's
 * confidence-floor lift for items it took to be Japanese (`ja_floor`), and its calibration's
 * per-language scores (`en`, `ja`). */
const RETIRED_LEARNING = ["ja_floor"] as const;
const RETIRED_CALIBRATION = ["en", "ja"] as const;
function learningCalibrationBin(value: unknown): value is LearningCalibrationBin {
  return (
    exact(value, ["low", "high", "n", "mean_p", "rate"]) &&
    cost(value.low) &&
    cost(value.high) &&
    count(value.n) &&
    nullableCost(value.mean_p) &&
    nullableCost(value.rate)
  );
}
/** A bin edge rounded to 1 decimal place, string-keyed: wider than the server's own 3-place
 * rounding (`wire.py::_LEARN_PLACES`), so float noise around a real edge still matches, and a
 * genuinely wrong edge still misses every key in `LEARNING_BIN_KEYS`. */
function binKey(low: number, high: number): string {
  const round1 = (value: number): number => Math.round(value * 10) / 10;
  return `${round1(low)}:${round1(high)}`;
}
function learningCalibration(value: unknown): value is LearningCalibration {
  return (
    exactBesides(value, ["n", "brier", "bins"], RETIRED_CALIBRATION) &&
    count(value.n) &&
    nullableCost(value.brier) &&
    boundedArray(value.bins, MAX_LEARNING_BINS) &&
    value.bins.every(learningCalibrationBin) &&
    exactSet(value.bins, (bin) => binKey(bin.low, bin.high), LEARNING_BIN_KEYS)
  );
}
function learningQuestionRate(value: unknown): value is LearningQuestionRate {
  return (
    exact(value, ["name", "label", "yes_n", "yes_rate", "no_n", "no_rate"]) &&
    text(value.name, MAX_TEXT) &&
    text(value.label, MAX_LABEL) &&
    count(value.yes_n) &&
    nullableCost(value.yes_rate) &&
    count(value.no_n) &&
    nullableCost(value.no_rate)
  );
}
function learningShadowStats(value: unknown): value is LearningShadowStats {
  return (
    exact(value, [
      "setting",
      "decisions",
      "agree",
      "kind_agree",
      "labels",
      "brier",
      "live_brier",
    ]) &&
    oneOf(value.setting, SHADOW_SETTINGS) &&
    count(value.decisions) &&
    count(value.agree) &&
    count(value.kind_agree) &&
    count(value.labels) &&
    nullableCost(value.brier) &&
    nullableCost(value.live_brier)
  );
}
const SECOND_LOOK_TYPES = ["long_task_look", "correction_look"] as const;
const PROJECT_PLAN_TYPES = ["stalled_project", "overdue_tasks"] as const;
const MESSAGE_DRAFT_TYPES = ["context_handoff", "source_note"] as const;
const SAVED_CHECK_TYPES = ["commit_checks"] as const;
const INBOX_TRIAGE_TYPES = ["inbox_triage"] as const;
/** One yield row per type, in the server's order (`wire.py::_trigger_rows`). */
function triggerRows(rows: unknown, types: readonly string[]): boolean {
  return (
    Array.isArray(rows) &&
    rows.length === types.length &&
    rows.every(
      (row, index) =>
        exact(row, ["candidate", "made", "accepted", "rejected", "nothing", "reasons", "paused"]) &&
        row.candidate === types[index] &&
        count(row.made) &&
        count(row.accepted) &&
        count(row.rejected) &&
        count(row.nothing) &&
        countMap(row.reasons, FEEDBACK_REASONS) &&
        flag(row.paused),
    )
  );
}
/** `second_looks`: one row per second-look type, then one per finding kind, each in the
 * server's order (`wire.py::_second_looks_view`). */
function learningSecondLooks(value: unknown): value is LearningSecondLooks {
  if (!exact(value, ["triggers", "kinds"])) return false;
  const { triggers, kinds } = value;
  return (
    triggerRows(triggers, SECOND_LOOK_TYPES) &&
    Array.isArray(kinds) &&
    kinds.length === FINDING_KINDS.length &&
    kinds.every(
      (row, index) =>
        exact(row, ["kind", "rejected_in_a_row", "paused"]) &&
        row.kind === FINDING_KINDS[index] &&
        count(row.rejected_in_a_row) &&
        flag(row.paused),
    )
  );
}
function learningView(value: unknown): value is LearningView {
  return (
    exactBesides(
      value,
      [
        "enabled",
        "labels",
        "min_labels",
        "fitted_at",
        "since",
        "attention_price",
        "bias",
        "weights",
        "topics",
        "reach",
        "costs",
        "calibration",
        "questions",
        "shadow",
        "second_looks",
        "project_plans",
        "message_drafts",
        "saved_checks",
        "inbox_triage",
      ],
      RETIRED_LEARNING,
    ) &&
    flag(value.enabled) &&
    count(value.labels) &&
    count(value.min_labels) &&
    nullableStamp(value.fitted_at) &&
    nullableStamp(value.since) &&
    cost(value.attention_price) &&
    learningStartNow(value.bias) &&
    boundedArray(value.weights, MAX_LEARNING_WEIGHTS) &&
    value.weights.every(learningWeight) &&
    exactlyDistinct(value.weights, (weight) => weight.name, MAX_LEARNING_WEIGHTS) &&
    boundedArray(value.topics, MAX_LEARNING_TOPICS) &&
    value.topics.every(learningTopic) &&
    exactSet(value.topics, (topic) => topic.candidate, CANDIDATES) &&
    boundedArray(value.reach, REACH_LEVELS.length) &&
    value.reach.every(learningReachRow) &&
    exactSet(value.reach, (row) => row.level, REACH_LEVELS) &&
    boundedArray(value.costs, COST_LEVELS.length) &&
    value.costs.every(learningCostRow) &&
    exactSet(value.costs, (row) => row.level, COST_LEVELS) &&
    learningCalibration(value.calibration) &&
    boundedArray(value.questions, MAX_LEARNING_QUESTIONS) &&
    value.questions.every(learningQuestionRate) &&
    exactlyDistinct(value.questions, (question) => question.name, MAX_LEARNING_QUESTIONS) &&
    learningShadowStats(value.shadow) &&
    learningSecondLooks(value.second_looks) &&
    triggerRows(value.project_plans, PROJECT_PLAN_TYPES) &&
    triggerRows(value.message_drafts, MESSAGE_DRAFT_TYPES) &&
    triggerRows(value.saved_checks, SAVED_CHECK_TYPES) &&
    triggerRows(value.inbox_triage, INBOX_TRIAGE_TYPES)
  );
}

const COUNT_KEYS = ["shown", ...FEEDBACK] as const;

function pageView(value: unknown): value is PageView {
  return (
    exact(value, [
      "enabled",
      "paused_until",
      "focus_until",
      "status",
      "model",
      "today",
      "privacy",
      "muted",
      "held_back",
      "recent",
      "counts",
      "missions",
      "changes",
      "message_drafts",
      "data",
      "learning",
    ]) &&
    flag(value.enabled) &&
    stampOrBlank(value.paused_until) &&
    stampOrBlank(value.focus_until) &&
    pageStatus(value.status) &&
    pageModel(value.model) &&
    pageToday(value.today) &&
    pagePrivacy(value.privacy) &&
    boundedArray(value.muted, CANDIDATES.length) &&
    value.muted.every((candidate) => oneOf(candidate, CANDIDATES)) &&
    boundedArray(value.held_back, MAX_HELD) &&
    value.held_back.every(heldBackRow) &&
    boundedArray(value.recent, MAX_RECENT) &&
    value.recent.every(recentRow) &&
    countMap(value.counts, COUNT_KEYS) &&
    pageMissions(value.missions) &&
    pageChanges(value.changes) &&
    countMap(value.message_drafts, ["today", "limit"]) &&
    pageData(value.data) &&
    learningView(value.learning)
  );
}
/** POST `report`, POST `preferences`, POST `key`, DELETE `key`. */
export function parsePage(value: unknown): PageView | null {
  return pageView(value) ? value : null;
}

function callSummary(value: unknown): value is CallSummary {
  return (
    exact(value, ["call_id", "at", "title", "candidate", "tokens", "session_id"]) &&
    uuid(value.call_id) &&
    stamp(value.at) &&
    text(value.title, MAX_TEXT) &&
    oneOf(value.candidate, CANDIDATES) &&
    count(value.tokens) &&
    nullableUuid(value.session_id)
  );
}
function callsView(value: unknown): value is CallsView {
  return (
    exact(value, ["enabled", "latest", "calls"]) &&
    flag(value.enabled) &&
    nullableOneOf(value.latest, POLICIES) &&
    boundedArray(value.calls, MAX_CALLS) &&
    value.calls.every(callSummary)
  );
}
/** GET `calls`. */
export function parseCalls(value: unknown): CallsView | null {
  return callsView(value) ? value : null;
}

function usedMax(value: unknown): boolean {
  return exact(value, ["used", "max"]) && count(value.used) && count(value.max);
}
function splitPart(value: unknown): boolean {
  return exact(value, ["part", "tokens"]) && text(value.part, MAX_TEXT) && count(value.tokens);
}
function questionRow(value: unknown): boolean {
  return (
    exact(value, ["name", "label", "answer"]) &&
    text(value.name, MAX_TEXT) &&
    text(value.label, MAX_LABEL) &&
    text(value.answer, MAX_TEXT)
  );
}
function callDetailView(value: unknown): value is CallDetailView {
  return (
    exact(value, [
      "call_id",
      "at",
      "title",
      "candidate",
      "session_id",
      "version",
      "version_setting",
      "latency_ms",
      "cost_usd",
      "long_context",
      "request",
      "largest_question",
      "split",
      "questions",
      "playbook_version",
      "state_text",
      "questions_text",
      "held_back",
      "outcome",
      "failure",
    ]) &&
    uuid(value.call_id) &&
    stamp(value.at) &&
    text(value.title, MAX_TEXT) &&
    oneOf(value.candidate, CANDIDATES) &&
    nullableUuid(value.session_id) &&
    displayVersion(value.version) &&
    oneOf(value.version_setting, VERSION_SETTINGS) &&
    count(value.latency_ms) &&
    cost(value.cost_usd) &&
    value.long_context === false &&
    usedMax(value.request) &&
    usedMax(value.largest_question) &&
    boundedArray(value.split, MAX_SPLIT) &&
    value.split.every(splitPart) &&
    boundedArray(value.questions, MAX_QUESTIONS) &&
    value.questions.every(questionRow) &&
    text(value.playbook_version, MAX_TEXT) &&
    stateText(value.state_text, MAX_STATE_TEXT) &&
    text(value.questions_text, MAX_QUESTIONS_TEXT) &&
    count(value.held_back) &&
    oneOf(value.outcome, OUTCOMES) &&
    nullableOneOf(value.failure, MODEL_ERROR_CODES)
  );
}
/** GET `calls/{call_id}`. */
export function parseCallDetail(value: unknown): CallDetailView | null {
  return callDetailView(value) ? value : null;
}

function testResultView(value: unknown): value is TestResultView {
  return (
    exact(value, ["ok", "latency_ms", "version", "error"]) &&
    flag(value.ok) &&
    (value.latency_ms === null || count(value.latency_ms)) &&
    nullableDisplayVersion(value.version) &&
    nullableOneOf(value.error, MODEL_ERROR_CODES)
  );
}
/** POST `test`. */
export function parseTestResult(value: unknown): TestResultView | null {
  return testResultView(value) ? value : null;
}

// ---- a question proposal ----

const QUESTION_PROPOSAL_KEYS = [
  "kind",
  "id",
  "origin",
  "revision",
  "state",
  "attention",
  "title",
  "name",
  "created_at",
  "updated_at",
  "approved_at",
  "rejected_at",
  "reason",
] as const;
/** The Approvals inbox item `isQuestionProposalItem` takes (state/decisionsApi.ts), held to this
 * file's own rule as well: exactly its keys and its origin's, a canonical workspace id, and a
 * title and reason without control characters. */
function questionProposalItem(value: unknown): value is QuestionProposalItem {
  return (
    isQuestionProposalItem(value) &&
    exact(value, QUESTION_PROPOSAL_KEYS) &&
    exact(value.origin, ["workspace_id", "session_id"]) &&
    uuid(value.origin.workspace_id) &&
    text(value.title, MAX_TEXT) &&
    (value.reason === null || text(value.reason, MAX_TEXT))
  );
}
function trialRate(value: unknown): value is TrialRate {
  return (
    exact(value, ["yes_n", "yes_rate", "no_n", "no_rate"]) &&
    count(value.yes_n) &&
    nullableCost(value.yes_rate) &&
    count(value.no_n) &&
    nullableCost(value.no_rate)
  );
}
function proposalTried(value: unknown): boolean {
  return (
    exact(value, ["decisions", "agree", "labels", "live", "trial"]) &&
    count(value.decisions) &&
    count(value.agree) &&
    count(value.labels) &&
    trialRate(value.live) &&
    trialRate(value.trial)
  );
}
function questionProposalView(value: unknown): value is QuestionProposalView {
  return (
    exact(value, ["proposal", "label", "current_text", "proposed_text", "tried"]) &&
    questionProposalItem(value.proposal) &&
    text(value.label, MAX_LABEL) &&
    stateText(value.current_text, MAX_PROPOSAL_TEXT) &&
    stateText(value.proposed_text, MAX_PROPOSAL_TEXT) &&
    proposalTried(value.tried)
  );
}
/** GET `proposals/{id}`. */
export function parseQuestionProposal(value: unknown): QuestionProposalView | null {
  return questionProposalView(value) ? value : null;
}

function proposalResult(value: unknown): value is { proposal: QuestionProposalItem } {
  return exact(value, ["proposal"]) && questionProposalItem(value.proposal);
}
/** POST `proposals`, POST `proposals/{id}`: `{proposal}`, held to the same strict item check
 * (`questionProposalItem`) as the GET view's own `proposal` member -- not the looser
 * `isQuestionProposalItem` the Approvals inbox uses. */
export function parseProposalResult(value: unknown): QuestionProposalItem | null {
  return proposalResult(value) ? value.proposal : null;
}

// ---- a change proposal act ----

/** `ChangeActResult.change`: held to this file's own rule as well, past what the Approvals
 * inbox's own looser `isChangeProposalItem` lets through (unlike that check, this one is
 * already exact on its own keys, so this only tightens the three fields it leaves unbounded --
 * mirrors `questionProposalItem`). */
function changeProposalItem(value: unknown): value is ChangeProposalItem {
  return (
    isChangeProposalItem(value) &&
    exact(value.origin, ["workspace_id", "session_id"]) &&
    uuid(value.origin.workspace_id) &&
    text(value.title, MAX_TEXT) &&
    text(value.ref, MAX_REF) &&
    (value.reason === null || text(value.reason, MAX_TEXT))
  );
}
function changeActResult(value: unknown): value is ChangeActResult {
  return (
    exact(value, ["change", "draft"]) &&
    changeProposalItem(value.change) &&
    (value.draft === null || draftText(value.draft))
  );
}
/** POST `changes/{id}`. */
export function parseChangeActResult(value: unknown): ChangeActResult | null {
  return changeActResult(value) ? value : null;
}

function errorBody(value: unknown): value is ErrorBody {
  if (!exact(value, ["error"])) return false;
  const error = value.error;
  return (
    exact(error, ["code", "message"]) &&
    typeof error.code === "string" &&
    ERROR_CODE.test(error.code) &&
    text(error.message, MAX_TEXT)
  );
}
/** Every refusal's body (`api_json.failure`). */
export function parseError(value: unknown): ErrorBody | null {
  return errorBody(value) ? value : null;
}

/** DELETE `episodes`'s body; types.ts has no standalone name for it. */
export interface DeletedResult {
  deleted: number;
}
export function parseDeleted(value: unknown): DeletedResult | null {
  return exact(value, ["deleted"]) && count(value.deleted)
    ? (value as unknown as DeletedResult)
    : null;
}

function dataDay(value: unknown): value is DataDay {
  return (
    exact(value, [
      "day",
      "held",
      "listed",
      "attention",
      "reports",
      "useful",
      "wrong",
      "later",
      "mute",
      "right_to_hold",
      "missed",
    ]) &&
    typeof value.day === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(value.day) &&
    count(value.held) &&
    count(value.listed) &&
    count(value.attention) &&
    count(value.reports) &&
    count(value.useful) &&
    count(value.wrong) &&
    count(value.later) &&
    count(value.mute) &&
    count(value.right_to_hold) &&
    count(value.missed)
  );
}

function dataTopic(value: unknown): value is DataTopic {
  return (
    exact(value, ["topic", "held", "shown", "reports", "useful", "wrong"]) &&
    oneOf(value.topic, TOPICS) &&
    count(value.held) &&
    count(value.shown) &&
    count(value.reports) &&
    count(value.useful) &&
    count(value.wrong)
  );
}

function dataSummaryView(value: unknown): value is DataSummaryView {
  return (
    exact(value, [
      "as_of",
      "today",
      "decisions",
      "suggestions",
      "calls",
      "chats",
      "proposals",
      "preferences",
      "key",
      "days",
      "topics",
    ]) &&
    stamp(value.as_of) &&
    typeof value.today === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(value.today) &&
    exact(value.decisions, ["decisions", "reports", "oldest", "newest", "max", "retention_days"]) &&
    count(value.decisions.decisions) &&
    count(value.decisions.reports) &&
    nullableStamp(value.decisions.oldest) &&
    nullableStamp(value.decisions.newest) &&
    count(value.decisions.max) &&
    count(value.decisions.retention_days) &&
    exact(value.suggestions, ["open", "closed", "oldest", "newest", "max_open", "closed_days"]) &&
    count(value.suggestions.open) &&
    count(value.suggestions.closed) &&
    nullableStamp(value.suggestions.oldest) &&
    nullableStamp(value.suggestions.newest) &&
    count(value.suggestions.max_open) &&
    count(value.suggestions.closed_days) &&
    exact(value.calls, ["count", "oldest", "newest", "max"]) &&
    count(value.calls.count) &&
    nullableStamp(value.calls.oldest) &&
    nullableStamp(value.calls.newest) &&
    count(value.calls.max) &&
    exact(value.chats, ["count", "running", "oldest", "newest"]) &&
    count(value.chats.count) &&
    count(value.chats.running) &&
    nullableStamp(value.chats.oldest) &&
    nullableStamp(value.chats.newest) &&
    exact(value.proposals, [
      "wordings",
      "changes",
      "oldest",
      "newest",
      "max_wordings",
      "max_changes",
    ]) &&
    count(value.proposals.wordings) &&
    count(value.proposals.changes) &&
    nullableStamp(value.proposals.oldest) &&
    nullableStamp(value.proposals.newest) &&
    count(value.proposals.max_wordings) &&
    count(value.proposals.max_changes) &&
    exact(value.preferences, ["muted", "learned", "updated"]) &&
    count(value.preferences.muted) &&
    flag(value.preferences.learned) &&
    nullableStamp(value.preferences.updated) &&
    exact(value.key, ["saved", "updated"]) &&
    flag(value.key.saved) &&
    nullableStamp(value.key.updated) &&
    boundedArray(value.days, 366) &&
    value.days.every(dataDay) &&
    boundedArray(value.topics, TOPICS.length) &&
    value.topics.every(dataTopic)
  );
}

/** GET `data`: bounded decision history summary. */
export function parseDataSummary(value: unknown): DataSummaryView | null {
  return dataSummaryView(value) ? value : null;
}

function dataCursor(value: unknown, idIsKey: boolean): boolean {
  return (
    exact(value, ["at", "id"]) && stamp(value.at) && (idIsKey ? itemKey(value.id) : uuid(value.id))
  );
}

function dataDecisionRow(value: unknown): value is DataDecisionRow {
  return (
    exact(value, ["id", "at", "entry", "topic", "level", "policy", "answer", "kept"]) &&
    uuid(value.id) &&
    stamp(value.at) &&
    oneOf(value.entry, ["decision", "report"]) &&
    oneOf(value.topic, TOPICS) &&
    nullableOneOf(value.level, LEVELS) &&
    oneOf(value.policy, POLICIES) &&
    nullableOneOf(value.answer, [...FEEDBACK_SHOWN, ...FEEDBACK_HELD]) &&
    nullableOneOf(value.kept, ["open_suggestion"])
  );
}

function dataSuggestionRow(value: unknown): value is DataSuggestionRow {
  return (
    exact(value, ["id", "at", "created_at", "title", "topic", "state", "level", "kept"]) &&
    itemKey(value.id) &&
    stamp(value.at) &&
    stamp(value.created_at) &&
    text(value.title, MAX_TEXT) &&
    oneOf(value.topic, CANDIDATES) &&
    oneOf(value.state, ITEM_STATES) &&
    oneOf(value.level, LEVELS) &&
    nullableOneOf(value.kept, ["open", "chat"])
  );
}

function dataCallRow(value: unknown): value is DataCallRow {
  return (
    exact(value, ["id", "at", "title", "topic", "outcome", "tokens", "cost_usd"]) &&
    uuid(value.id) &&
    stamp(value.at) &&
    text(value.title, MAX_TEXT) &&
    nullableOneOf(value.topic, CANDIDATES) &&
    oneOf(value.outcome, OUTCOMES) &&
    count(value.tokens) &&
    cost(value.cost_usd)
  );
}

function dataRecordsView(value: unknown): value is DataRecordsView {
  if (!object(value) || !oneOf(value.kind, DATA_KINDS)) return false;
  const base = ["kind", "total", "deletable", "next", "rows"];
  if (!exact(value, base) || !count(value.total) || !count(value.deletable)) return false;
  if (value.kind === "calls") {
    return (
      value.next === null && boundedArray(value.rows, MAX_CALLS) && value.rows.every(dataCallRow)
    );
  }
  const idIsKey = value.kind === "suggestions";
  return (
    (value.next === null || dataCursor(value.next, idIsKey)) &&
    boundedArray(value.rows, MAX_ITEMS) &&
    value.rows.every(value.kind === "decisions" ? dataDecisionRow : dataSuggestionRow)
  );
}

/** POST `data/records`: one bounded page of records. */
export function parseDataRecords(value: unknown): DataRecordsView | null {
  return dataRecordsView(value) ? value : null;
}

function dataDecisionView(value: unknown): value is DataDecisionView {
  return (
    exact(value, [
      "kind",
      "id",
      "at",
      "entry",
      "topic",
      "level",
      "policy",
      "why",
      "evidence",
      "answer",
      "answer_reason",
      "answered_at",
      "shown",
      "opened_at",
      "acted_at",
      "tokens",
      "cost_usd",
      "kept",
    ]) &&
    value.kind === "decisions" &&
    uuid(value.id) &&
    stamp(value.at) &&
    oneOf(value.entry, ["decision", "report"]) &&
    oneOf(value.topic, TOPICS) &&
    nullableOneOf(value.level, LEVELS) &&
    oneOf(value.policy, POLICIES) &&
    text(value.why, MAX_WHY) &&
    boundedArray(value.evidence, MAX_EVIDENCE) &&
    value.evidence.every(itemEvidence) &&
    nullableOneOf(value.answer, [...FEEDBACK_SHOWN, ...FEEDBACK_HELD]) &&
    (value.answer_reason === null || text(value.answer_reason, MAX_TEXT)) &&
    nullableStamp(value.answered_at) &&
    count(value.shown) &&
    nullableStamp(value.opened_at) &&
    nullableStamp(value.acted_at) &&
    (value.tokens === null || count(value.tokens)) &&
    nullableCost(value.cost_usd) &&
    nullableOneOf(value.kept, ["open_suggestion"])
  );
}

function dataSuggestionView(value: unknown): value is DataSuggestionView {
  return (
    exact(value, [
      "kind",
      "id",
      "title",
      "topic",
      "state",
      "level",
      "created_at",
      "updated_at",
      "shown_at",
      "opened_at",
      "what",
      "why",
      "evidence",
      "kept",
    ]) &&
    value.kind === "suggestions" &&
    itemKey(value.id) &&
    text(value.title, MAX_TEXT) &&
    oneOf(value.topic, CANDIDATES) &&
    oneOf(value.state, ITEM_STATES) &&
    oneOf(value.level, LEVELS) &&
    stamp(value.created_at) &&
    stamp(value.updated_at) &&
    nullableStamp(value.shown_at) &&
    nullableStamp(value.opened_at) &&
    text(value.what, MAX_WHAT) &&
    text(value.why, MAX_WHY) &&
    boundedArray(value.evidence, MAX_EVIDENCE) &&
    value.evidence.every(itemEvidence) &&
    nullableOneOf(value.kept, ["open", "chat"])
  );
}

/** POST `data/record`: one stored decision or suggestion. */
export function parseDataRecord(value: unknown): DataDecisionView | DataSuggestionView | null {
  if (!object(value)) return null;
  if (value.kind === "decisions") return dataDecisionView(value) ? value : null;
  if (value.kind === "suggestions") return dataSuggestionView(value) ? value : null;
  return null;
}

/** POST `data/delete`: the number of records removed and retained. */
export function parseDataDeleted(value: unknown): DataDeleted | null {
  return exact(value, ["deleted", "kept"]) && count(value.deleted) && count(value.kept)
    ? (value as unknown as DataDeleted)
    : null;
}
