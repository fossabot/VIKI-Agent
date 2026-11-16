/**
 * Wire shapes of the Decision routes under /api/v2/decision/, key for key in snake_case.
 * examples.json holds one complete example of each response view; fake.ts serves them to UI
 * tests.
 *
 * Stamps are 24-character UTC text ("2026-10-21T14:02:00.000Z"), ids canonical UUIDs and item
 * keys 64 lower-case hex characters. Versions are display numbers ("1.14.0"), never a model id.
 * A member typed `X | null` may be null; examples.json lists those members in `_nullable`.
 */

import type { ChangeProposalItem, QuestionProposalItem } from "../../state/decisionsApi";

export const CANDIDATES = [
  "approvals",
  "tasks_due",
  "agent_runs",
  "inbox",
  "uncommitted",
  "routine",
  "mission_report",
  "card_needs_you",
  "card_stale",
  "seat_waiting",
  "seat_working",
  "seat_start",
  "seat_report",
  "vault_recovery",
  "memory_drift",
  "bridge_stopped",
  "chat_waiting",
  "send_uncertain",
  "mcp_context",
  "code_look",
  "long_task_look",
  "correction_look",
  "stalled_project",
  "overdue_tasks",
  "context_handoff",
  "source_note",
  "commit_checks",
  "inbox_triage",
  "skill_offer",
  "skill_suggest",
  "learn_source",
  "retro_due",
  "question_tune",
  "note_link",
  "note_superseded",
  "tasks_ticked",
] as const;
export type DecisionCandidate = (typeof CANDIDATES)[number];
export const TOPICS = [...CANDIDATES, "unwatched"] as const;
export type DecisionTopic = (typeof TOPICS)[number];
export const KINDS = ["inform", "ask", "suggest", "ready", "report"] as const;
export type DecisionKind = (typeof KINDS)[number];
export const LEVELS = ["silent", "list", "badge", "nudge", "alert"] as const;
export type DecisionLevel = (typeof LEVELS)[number];
export const POLICIES = ["rules", "model"] as const;
export type DecisionPolicy = (typeof POLICIES)[number];
export const SOURCES = ["brief", "rail", "review", "launcher", "page"] as const;
export type DecisionSource = (typeof SOURCES)[number];
export const FEEDBACK_SHOWN = ["useful", "wrong", "later", "mute"] as const;
export const FEEDBACK_HELD = ["missed", "right_to_hold"] as const;
export type FeedbackValue = (typeof FEEDBACK_SHOWN)[number] | (typeof FEEDBACK_HELD)[number];
export const FEEDBACK_REASONS = [
  "out_of_date",
  "already_knew",
  "wrong_topic",
  "bad_timing",
] as const;
export type FeedbackReason = (typeof FEEDBACK_REASONS)[number];
export const OBSERVE_EVENTS = ["shown", "opened", "acted"] as const;
export type ObserveEvent = (typeof OBSERVE_EVENTS)[number];
export const ACTION_TYPES = [
  "open_chat",
  "open_note",
  "open_approvals",
  "open_inbox",
  "open_routine",
  "open_runs",
  "open_context",
  "open_vault",
  "open_messaging",
  "open_code",
  "open_triage",
  "start_prepare",
  "start_run",
  "stop_mission",
  "start_seat",
  "stop_seat",
  "run_checks",
  "stop_checks",
  "suggest_inbox",
  "draft_chat",
  "draft_new",
] as const;
export type ActionType = (typeof ACTION_TYPES)[number];
export const LAUNCHER_STATES = ["off", "paused", "on"] as const;
export type LauncherState = (typeof LAUNCHER_STATES)[number];
export const LAUNCHER_LEVELS = ["idle", "list", "badge", "question", "nudge", "alert"] as const;
export type LauncherLevel = (typeof LAUNCHER_LEVELS)[number];
export const MODEL_STATES = [
  "ready",
  "rules",
  "no_key",
  "key_rejected",
  "capped",
  "unreachable",
] as const;
export type ModelState = (typeof MODEL_STATES)[number];
/** Why a call to the Decision model failed: the page's last error, a failed call, a failed test. */
export const MODEL_ERROR_CODES = [
  "no_key",
  "key_rejected",
  "refused",
  "too_large",
  "rate_limited",
  "overloaded",
  "unreachable",
  "invalid_reply",
  "failed",
] as const;
export type ModelErrorCode = (typeof MODEL_ERROR_CODES)[number];
export const KEY_STATES = ["saved", "missing"] as const;
export type KeyState = (typeof KEY_STATES)[number];
export const VERSION_SETTINGS = ["pinned", "latest", "preview"] as const;
export type VersionSetting = (typeof VERSION_SETTINGS)[number];
/** `decision.shadow`'s own values: "off", or a version setting to shadow instead of the live one. */
export type ShadowSetting = "off" | VersionSetting;
export const OUTCOMES = ["answered", "failed"] as const;
export type CallOutcome = (typeof OUTCOMES)[number];
export const ITEM_STATES = ["open", "acted", "dismissed", "stale", "done"] as const;
export type ItemState = (typeof ITEM_STATES)[number];
export const DATA_KINDS = ["decisions", "suggestions", "calls"] as const;
export type DataKind = (typeof DATA_KINDS)[number];
export const DATA_SHOWN = ["held", "listed", "attention", "report"] as const;
export type DataShown = (typeof DATA_SHOWN)[number];
export type DataAnswer = FeedbackValue | "none";
export const MISSION_WORK = ["prepare", "run"] as const;
export type MissionWork = (typeof MISSION_WORK)[number];
export const MISSION_STATES = [
  "starting",
  "running",
  "done",
  "failed",
  "stopped",
  "unknown",
] as const;
export type MissionState = (typeof MISSION_STATES)[number];
export const GIT_STATES = ["known", "unknown", "not_repository"] as const;
export type GitState = (typeof GIT_STATES)[number];

/** What an item's button does. Only the members its type uses are present. */
export interface ItemAction {
  type: ActionType;
  label: string;
  primary: boolean;
  session_id?: string;
  workspace_id?: string;
  ref?: string;
  routine_id?: string;
  call_id?: string;
  mission_id?: string;
  /** draft_chat, draft_new: the draft's own text, 1..4,000 UTF-16 units ("\n"/"\t" allowed). */
  text?: string;
}

/** An evidence chip: a note (`workspace_id` and `ref`), a chat (`session_id`) or a plain label. */
export interface ItemEvidence {
  label: string;
  workspace_id?: string;
  ref?: string;
  session_id?: string;
}

export interface ItemView {
  item_key: string;
  episode_id: string;
  candidate: DecisionCandidate;
  kind: DecisionKind;
  level: DecisionLevel;
  what: string;
  why: string;
  evidence: ItemEvidence[];
  actions: ItemAction[];
  created_at: string;
  mission_id: string | null;
}

export interface LauncherView {
  state: LauncherState;
  level: LauncherLevel;
  /** The badge's number: open items at badge level or louder. */
  count: number;
  /** Counters that grow by one each time a nudge (`nudge`) or a new list item (`glow`) is
   * committed; the launcher plays its cue once per increase. */
  nudge: number;
  glow: number;
  /** VIKI's chats running now. */
  working: number;
  /** Open items, the panel's "2 open". */
  open: number;
  checked_at: string | null;
  paused_until: string | null;
  focus_until: string | null;
  model: ModelState;
  items: ItemView[];
  /** Chats VIKI started in the last 30 days (at most 50). */
  started: string[];
  /** The weekly check: due while some of last week's sample is unlabelled and the week is not
   * skipped. */
  review: { due: boolean; count: number };
  /** VIKI's chats whose plan waits. */
  plans_waiting: number;
}

/** The page's own counts a presence report carries. */
export interface PageFacts {
  native_waiting: number;
  runs: { working: number; review: number };
  git: { state: GitState; dirty: number | null };
  routine: { routine_id: string; next_run: string; paused: boolean } | null;
  /** The client's own "make this a skill" hint, surfaced to the detectors; null while there is
   * none (off, no vault workspace, or no hint for this chat). */
  skill: {
    trigger: "correction" | "repeated_request" | "long_session";
    sessions: string[];
    titles: string[];
  } | null;
  /** The open vault chat's facts for a skill suggestion (`pageFacts.ts::suggestFacts`): matched
   * on the local server, never sent on; null while there is none. */
  suggest: { session: string; title: string; used: string[] } | null;
  /** The open vault chat's last finished turn when it is long or was corrected and edited
   * notes (`skillTriggers.ts::secondLookHint`); null while there is none. */
  second_look: {
    session: string;
    turn: string;
    trigger: "correction" | "long_session";
    title: string;
    refs: string[];
  } | null;
  /** The open vault chat's context share, a whole percent from 0 to 100; null while there is
   * none. The server owns the threshold. */
  context: { session: string; percent: number } | null;
  /** The open vault chat's last reply that fetched web pages, and whether it read a vault
   * note; null while there is none. */
  sources: { session: string; turn: string; notes: boolean } | null;
}

/** POST presence. `chat_title` is at most 120 characters and `note.ref` at most 512, neither
 * with control characters. */
export interface PresenceBody {
  window: string;
  focused: boolean;
  typing: boolean;
  chat: string | null;
  chat_title: string | null;
  note: { workspace_id: string; ref: string } | null;
  launcher: boolean;
  page: PageFacts | null;
}

/** POST feedback on a shown item (`item_key`) or a held-back episode (`episode_id`): exactly one
 * of the two is set. */
export type FeedbackBody = {
  value: FeedbackValue;
  reason: FeedbackReason | null;
  source: DecisionSource;
} & ({ item_key: string; episode_id: null } | { item_key: null; episode_id: string });

export type PreferenceBody =
  | { name: "unmute"; candidate: DecisionCandidate }
  | {
      name: "try_again";
      candidate:
        | "long_task_look"
        | "correction_look"
        | "stalled_project"
        | "overdue_tasks"
        | "context_handoff"
        | "source_note"
        | "commit_checks"
        | "inbox_triage";
    }
  | { name: "raise_again"; kind: FindingKind }
  | { name: "review_seen" }
  | { name: "reset_learning" };

export interface MissionView {
  mission_id: string;
  session_id: string;
  work: MissionWork;
  state: MissionState;
  title: string;
  started_at: string;
  finished_at: string | null;
  reason: string;
}

/** When the newest plan reading says the brake's hold on VIKI's own starts ends; null when it names none. */
export interface PlanHold {
  until: string | null;
}

/** The team seats' starts today against the limit setting, and the decision runs open now. */
export interface PageTeam {
  today: number;
  limit: number;
  running: number;
}

/** Look chats started today against the limit setting. */
export interface PageLook {
  today: number;
  limit: number;
}

/** What a second look's plan can name; the Learning row lists them in this order. */
export const FINDING_KINDS = ["flaw", "approach", "missing"] as const;
export type FindingKind = (typeof FINDING_KINDS)[number];

export interface PageChecks {
  today: number;
  limit: number;
  running: number;
}

export interface PageMissions {
  running: MissionView[];
  today: Record<MissionWork, number>;
  limits: Record<MissionWork, number>;
  hold: PlanHold | null;
  team: PageTeam;
  look: PageLook;
  /** Second looks started today against the limit setting. */
  second_look: PageLook;
  project_plan: PageLook;
  inbox: PageLook;
  /** New commits checked today against the limit, and runs VIKI started that are still going;
   * null while the server sends no count. */
  checks: PageChecks | null;
}

/** Held back in the last 7 days (at most 50). */
export interface HeldBackRow {
  episode_id: string;
  created_at: string;
  candidate: DecisionCandidate;
  level: DecisionLevel;
  reason: string;
  why: string;
  feedback: FeedbackValue | null;
}

/** The latest decisions (at most 20). */
export interface RecentRow {
  episode_id: string;
  created_at: string;
  candidate: DecisionCandidate;
  level: DecisionLevel;
  policy: DecisionPolicy;
  reason: string;
  why: string;
  feedback: FeedbackValue | null;
}

/** One learned weight (the five `START_WEIGHTS` features, by name): never the bias term, which
 * sits on `LearningView.bias` instead. */
export interface LearningWeight {
  name: string;
  label: string;
  start: number;
  now: number;
}

/** One candidate's own fitted topic offset, added to `p` alongside the named weights. */
export interface LearningTopic {
  candidate: DecisionCandidate;
  start: number;
  now: number;
  support: number;
}

/** One level's seen-in-time (`reach`, over "list"|"badge"|"nudge"|"alert") or attention cost
 * (`costs`, over "badge"|"nudge"|"alert") number. */
export interface LearningLevelNumber {
  level: DecisionLevel;
  start: number;
  now: number;
  support: number;
}

export interface LearningCalibrationBin {
  low: number;
  high: number;
  n: number;
  mean_p: number | null;
  rate: number | null;
}

export interface LearningCalibration {
  n: number;
  brier: number | null;
  bins: LearningCalibrationBin[];
}

/** One of the seven noul playbook questions' own useful rates, split by its yes/no answer. */
export interface LearningQuestionRate {
  name: string;
  label: string;
  yes_n: number;
  yes_rate: number | null;
  no_n: number;
  no_rate: number | null;
}

export interface LearningShadowStats {
  setting: ShadowSetting;
  decisions: number;
  agree: number;
  kind_agree: number;
  labels: number;
  brier: number | null;
  live_brier: number | null;
}

/** One second-look type's yield: looks made, how they ended, the Not useful reasons and whether
 * the type is paused. */
export interface LearningSecondLookTrigger {
  candidate: "long_task_look" | "correction_look";
  made: number;
  accepted: number;
  rejected: number;
  nothing: number;
  reasons: Record<FeedbackReason, number>;
  paused: boolean;
}

/** One finding kind's turned-down looks in a row and whether the kind is paused. */
export interface LearningSecondLookKind {
  kind: FindingKind;
  rejected_in_a_row: number;
  paused: boolean;
}

export interface LearningSecondLooks {
  triggers: LearningSecondLookTrigger[];
  kinds: LearningSecondLookKind[];
}

/** One project-plan type's yield, counted as a second-look type's is. */
export interface LearningProjectPlan {
  candidate: "stalled_project" | "overdue_tasks";
  made: number;
  accepted: number;
  rejected: number;
  nothing: number;
  reasons: Record<FeedbackReason, number>;
  paused: boolean;
}

/** One message-draft type's yield, counted as a second-look type's is. */
export interface LearningMessageDraft {
  candidate: "context_handoff" | "source_note";
  made: number;
  accepted: number;
  rejected: number;
  nothing: number;
  reasons: Record<FeedbackReason, number>;
  paused: boolean;
}

/** One saved-check type's yield, counted as a second-look type's is. */
export interface LearningSavedCheck {
  candidate: "commit_checks";
  made: number;
  accepted: number;
  rejected: number;
  nothing: number;
  reasons: Record<FeedbackReason, number>;
  paused: boolean;
}

/** One inbox-suggestion type's yield, counted as a saved check's is. */
export interface LearningInboxTriage {
  candidate: "inbox_triage";
  made: number;
  accepted: number;
  rejected: number;
  nothing: number;
  reasons: Record<FeedbackReason, number>;
  paused: boolean;
}

/** The Learning row's own numbers: the fitted snapshot
 * alongside each number's starting value, or every `now` at its `start` while learning is off or
 * unfitted. */
export interface LearningView {
  enabled: boolean;
  labels: number;
  min_labels: number;
  fitted_at: string | null;
  since: string | null;
  attention_price: number;
  bias: { start: number; now: number };
  weights: LearningWeight[];
  topics: LearningTopic[];
  reach: LearningLevelNumber[];
  costs: LearningLevelNumber[];
  calibration: LearningCalibration;
  questions: LearningQuestionRate[];
  shadow: LearningShadowStats;
  second_looks: LearningSecondLooks;
  project_plans: LearningProjectPlan[];
  message_drafts: LearningMessageDraft[];
  saved_checks: LearningSavedCheck[];
  inbox_triage: LearningInboxTriage[];
}

export interface PageView {
  enabled: boolean;
  /** The stored setting: a stamp, or "" when not paused. */
  paused_until: string;
  /** The stored setting: a stamp, or "" when Focus is off. */
  focus_until: string;
  status: { open: number; checked_at: string | null };
  model: {
    key: KeyState;
    version: { setting: VersionSetting; display: string };
    state: ModelState;
    last_error: ModelErrorCode | null;
    last_test: { at: string; latency_ms: number } | null;
  };
  today: {
    decisions: number;
    model_calls: number;
    tokens: number;
    cost_usd: number;
    cap_usd: number;
    capped: boolean;
    shadow_calls: number;
  };
  privacy: {
    last_call: { call_id: string; at: string; tokens: number } | null;
    held_back_today: number;
  };
  muted: DecisionCandidate[];
  held_back: HeldBackRow[];
  recent: RecentRow[];
  /** The last 30 days. */
  counts: Record<"shown" | FeedbackValue, number>;
  missions: PageMissions;
  /** Vault-change proposals (note_link, note_superseded, tasks_ticked) filed today across every
   * workspace, and the day's own limit (`decision.limit.changes`); DoRow's "Today: N of M vault
   * changes". */
  changes: { today: number; limit: number };
  /** New message drafts raised today across workspaces against `decision.limit.message_draft`. */
  message_drafts: { today: number; limit: number };
  data: { episodes: number; oldest: string | null; max: number; retention_days: number };
  learning: LearningView;
}

/** One model call; the client marks "this chat" when `session_id` is the open chat. */
export interface CallSummary {
  call_id: string;
  at: string;
  title: string;
  candidate: DecisionCandidate;
  tokens: number;
  session_id: string | null;
}

export interface CallsView {
  enabled: boolean;
  /** How the latest decision was made, or null before the first. */
  latest: DecisionPolicy | null;
  /** The last 20 model calls. */
  calls: CallSummary[];
}

export interface CallDetailView {
  call_id: string;
  at: string;
  title: string;
  candidate: DecisionCandidate;
  session_id: string | null;
  version: string;
  version_setting: VersionSetting;
  latency_ms: number;
  cost_usd: number;
  long_context: false;
  request: { used: number; max: number };
  largest_question: { used: number; max: number };
  split: { part: string; tokens: number }[];
  questions: { name: string; label: string; answer: string }[];
  playbook_version: string;
  state_text: string;
  questions_text: string;
  /** Evidence the never-send list kept out of this call. */
  held_back: number;
  outcome: CallOutcome;
  failure: ModelErrorCode | null;
}

export interface TestResultView {
  ok: boolean;
  latency_ms: number | null;
  version: string | null;
  error: ModelErrorCode | null;
}

/** One wording's usefulness over a trial's labelled decisions, split by its Yes and No answers;
 * a rate is null without a labelled decision on that side. */
export interface TrialRate {
  yes_n: number;
  yes_rate: number | null;
  no_n: number;
  no_rate: number | null;
}

/** GET proposals/{id}: one question proposal for its inspector.
 * `proposal` is its Approvals inbox item; `current_text` and `proposed_text` are
 * the wording in use and the proposed one as indented JSON, 0..4,000 UTF-16 units ("\n"/"\t"
 * allowed); `tried` compares the two over the decisions the proposed wording was tried on:
 * `agree` counts those whose two answers fell on the same side, `labels` those the user
 * labelled. */
export interface QuestionProposalView {
  proposal: QuestionProposalItem;
  label: string;
  current_text: string;
  proposed_text: string;
  tried: { decisions: number; agree: number; labels: number; live: TrialRate; trial: TrialRate };
}

/** POST changes/{id}: the result of approving, rejecting or redrafting a vault-change proposal
 * `change` is the acted proposal's
 * Approvals inbox item; `draft` is the chat message that approval answers, or null after a
 * rejection. */
export interface ChangeActResult {
  change: ChangeProposalItem;
  draft: string | null;
}

/** Every refusal's body. */
export interface ErrorBody {
  error: { code: string; message: string };
}

export interface DataCursor {
  at: string;
  id: string;
}

type DataQueryBase = {
  ids: string[] | null;
  since: string | null;
  until: string | null;
  before: DataCursor | null;
};

export type DataQuery =
  | (DataQueryBase & {
      kind: "decisions";
      topic: DecisionTopic | null;
      shown: DataShown | null;
      answer: DataAnswer | null;
      state: null;
      outcome: null;
    })
  | (DataQueryBase & {
      kind: "suggestions";
      topic: DecisionCandidate | null;
      shown: null;
      answer: null;
      state: ItemState | null;
      outcome: null;
    })
  | (DataQueryBase & {
      kind: "calls";
      topic: null;
      shown: null;
      answer: null;
      state: null;
      outcome: CallOutcome | null;
      since: null;
      until: null;
      before: null;
    });

export interface DataDay {
  day: string;
  held: number;
  listed: number;
  attention: number;
  reports: number;
  useful: number;
  wrong: number;
  later: number;
  mute: number;
  right_to_hold: number;
  missed: number;
}

export interface DataTopic {
  topic: DecisionTopic;
  held: number;
  shown: number;
  reports: number;
  useful: number;
  wrong: number;
}

export interface DataSummaryView {
  as_of: string;
  today: string;
  decisions: {
    decisions: number;
    reports: number;
    oldest: string | null;
    newest: string | null;
    max: number;
    retention_days: number;
  };
  suggestions: {
    open: number;
    closed: number;
    oldest: string | null;
    newest: string | null;
    max_open: number;
    closed_days: number;
  };
  calls: {
    count: number;
    oldest: string | null;
    newest: string | null;
    max: number;
  };
  chats: { count: number; running: number; oldest: string | null; newest: string | null };
  proposals: {
    wordings: number;
    changes: number;
    oldest: string | null;
    newest: string | null;
    max_wordings: number;
    max_changes: number;
  };
  preferences: { muted: number; learned: boolean; updated: string | null };
  key: { saved: boolean; updated: string | null };
  days: DataDay[];
  topics: DataTopic[];
}

export interface DataDecisionRow {
  id: string;
  at: string;
  entry: "decision" | "report";
  topic: DecisionTopic;
  level: DecisionLevel | null;
  policy: DecisionPolicy;
  answer: FeedbackValue | null;
  kept: "open_suggestion" | null;
}

export interface DataSuggestionRow {
  id: string;
  at: string;
  created_at: string;
  title: string;
  topic: DecisionCandidate;
  state: ItemState;
  level: DecisionLevel;
  kept: "open" | "chat" | null;
}

export interface DataCallRow {
  id: string;
  at: string;
  title: string;
  topic: DecisionCandidate | null;
  outcome: CallOutcome;
  tokens: number;
  cost_usd: number;
}

export type DataRecordsView =
  | {
      kind: "decisions";
      total: number;
      deletable: number;
      next: DataCursor | null;
      rows: DataDecisionRow[];
    }
  | {
      kind: "suggestions";
      total: number;
      deletable: number;
      next: DataCursor | null;
      rows: DataSuggestionRow[];
    }
  | { kind: "calls"; total: number; deletable: number; next: null; rows: DataCallRow[] };

export interface DataDecisionView {
  kind: "decisions";
  id: string;
  at: string;
  entry: "decision" | "report";
  topic: DecisionTopic;
  level: DecisionLevel | null;
  policy: DecisionPolicy;
  why: string;
  evidence: ItemEvidence[];
  answer: FeedbackValue | null;
  answer_reason: string | null;
  answered_at: string | null;
  shown: number;
  opened_at: string | null;
  acted_at: string | null;
  tokens: number | null;
  cost_usd: number | null;
  kept: "open_suggestion" | null;
}

export interface DataSuggestionView {
  kind: "suggestions";
  id: string;
  title: string;
  topic: DecisionCandidate;
  state: ItemState;
  level: DecisionLevel;
  created_at: string;
  updated_at: string;
  shown_at: string | null;
  opened_at: string | null;
  what: string;
  why: string;
  evidence: ItemEvidence[];
  kept: "open" | "chat" | null;
}

export type DataRecordView = DataDecisionView | DataSuggestionView;
export interface DataDeleted {
  deleted: number;
  kept: number;
}

/** examples.json. */
export interface DecisionLoopExamples {
  /** Member paths (dotted, `[]` for each array element) whose value may also be null. */
  _nullable: string[];
  launcher: LauncherView;
  page: PageView;
  calls: CallsView;
  call_detail: CallDetailView;
  mission: MissionView;
  test_result: TestResultView;
  question_proposal: QuestionProposalView;
  change_act: ChangeActResult;
  error: ErrorBody;
  data_summary: DataSummaryView;
  data_decisions: Extract<DataRecordsView, { kind: "decisions" }>;
  data_suggestions: Extract<DataRecordsView, { kind: "suggestions" }>;
  data_calls: Extract<DataRecordsView, { kind: "calls" }>;
  data_decision: DataDecisionView;
  data_suggestion: DataSuggestionView;
  data_deleted: DataDeleted;
}

/** A refusal: the server's `code` and HTTP status, or a code with status 0 when no response
 * carried one. */
export class DecisionLoopApiError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(code);
  }
}

/** The routes under /api/v2/decision/, one method each. Reads take an optional abort signal. */
export interface DecisionLoopApi {
  /** GET launcher. */
  launcher(signal?: AbortSignal): Promise<LauncherView>;
  /** POST launcher/open: re-checks the open items, then answers the launcher view. */
  openLauncher(signal?: AbortSignal): Promise<LauncherView>;
  /** POST presence; 204. */
  presence(body: PresenceBody): Promise<void>;
  /** POST feedback; the launcher view after it. */
  feedback(body: FeedbackBody): Promise<LauncherView>;
  /** POST observe, at most 50 keys (the launcher's most items); 204. */
  observe(event: ObserveEvent, itemKeys: string[]): Promise<void>;
  /** POST reports; the page view after it. */
  report(topic: DecisionTopic, source: DecisionSource): Promise<PageView>;
  /** GET page. */
  page(signal?: AbortSignal): Promise<PageView>;
  /** POST preferences; the page view after it. */
  preference(body: PreferenceBody): Promise<PageView>;
  /** DELETE episodes: the scope's episodes, items and calls; how many episodes went. */
  deleteEpisodes(): Promise<{ deleted: number }>;
  /** POST key; the page view after it, which says only whether a key is saved. */
  saveKey(key: string): Promise<PageView>;
  /** DELETE key; the page view after it. */
  removeKey(): Promise<PageView>;
  /** POST test: one small question to the Decision model with the saved key. */
  test(): Promise<TestResultView>;
  /** GET calls. */
  calls(signal?: AbortSignal): Promise<CallsView>;
  /** GET calls/{call_id}. */
  call(callId: string, signal?: AbortSignal): Promise<CallDetailView>;
  /** GET missions: VIKI's running chats, today's starts and the daily limits. */
  missions(signal?: AbortSignal): Promise<PageMissions>;
  /** POST missions: start a chat for an open item. */
  startMission(itemKey: string, work: MissionWork): Promise<MissionView>;
  /** POST missions/{mission_id}/stop. */
  stopMission(missionId: string): Promise<MissionView>;
  /** POST missions/stop: stops every running mission; the missions after it. */
  stopAllMissions(): Promise<PageMissions>;
  /** POST seats/{routine_id}/start: start that routine's seat; the missions after it. */
  startSeat(routineId: string): Promise<PageMissions>;
  /** POST seats/{routine_id}/stop. */
  stopSeat(routineId: string): Promise<PageMissions>;
  /** POST checks/{workspace_id}/run: run that folder's saved checks; the missions after it. */
  runChecks(workspaceId: string): Promise<PageMissions>;
  /** POST checks/{workspace_id}/stop: stop the checks Decision started there. */
  stopChecks(workspaceId: string): Promise<PageMissions>;
  /** POST inbox/suggest: start the suggestion run for an inbox item; the missions after it. */
  suggestInbox(itemKey: string): Promise<PageMissions>;
  /** POST proposals: a new question-wording proposal, sealed and open. */
  createProposal(name: string, question: Record<string, unknown>): Promise<QuestionProposalItem>;
  /** GET proposals/{id}: that proposal's own inspector view. */
  proposal(id: string, signal?: AbortSignal): Promise<QuestionProposalView>;
  /** POST proposals/{id}: approve or reject an open proposal, or undo (reject) an approved one;
   * `reason` only with reject. */
  actProposal(
    id: string,
    action: "approve" | "reject",
    revision: string,
    reason?: string,
  ): Promise<QuestionProposalItem>;
  /** POST changes/{id}: approve (opens a draft) or reject (optional reason) an open vault-change
   * proposal, or draft an approved one again; `reason` only with reject. */
  actChange(
    id: string,
    action: "approve" | "reject",
    revision: string,
    reason?: string,
  ): Promise<ChangeActResult>;
  /** GET data summary. */
  dataSummary(signal?: AbortSignal): Promise<DataSummaryView>;
  /** POST data/records. */
  dataRecords(query: DataQuery, signal?: AbortSignal): Promise<DataRecordsView>;
  /** POST data/record. */
  dataRecord(
    kind: "decisions" | "suggestions",
    id: string,
    signal?: AbortSignal,
  ): Promise<DataRecordView>;
  /** POST data/delete. */
  deleteData(query: DataQuery): Promise<DataDeleted>;
}
