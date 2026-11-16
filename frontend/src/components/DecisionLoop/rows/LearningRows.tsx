/**
 * The Decision page's Learning row: placed between How VIKI is doing and Data, closed by default.
 * It composes the read-only parts (`rows/LearningParts.tsx`) around this row's own controls: the
 * on/off switch, a hidden settings key (`decision.learning`) read from `values` and saved through
 * `patch` exactly as `DoRow` reads and saves its own (`rows/DoingRows.tsx`'s `DecisionRowsProps`
 * and save pattern). The settings document updates `values` after `patch`; refreshing the page
 * also updates its learning status. Reset what VIKI learned asks the engine to forget its snapshot
 * (an `api.preference` call, not a settings write) and shares `SetupRows.tsx`'s `ConfirmLine`
 * rather than re-implementing the same confirm markup. `DecisionLoopPage.tsx` composes this row
 * like every other one; this file owns only its own fields, controls and copy.
 */
import { type ReactNode, useState } from "react";
import {
  FEEDBACK_REASONS,
  type FindingKind,
  type LearningInboxTriage,
  type LearningMessageDraft,
  type LearningProjectPlan,
  type LearningSavedCheck,
  type LearningSecondLookTrigger,
  type LearningView,
  type PreferenceBody,
} from "../../../clients/decisionLoop/types";
import { DecisionRow, Switch, useRowOpen } from "../DecisionRow";
import { REASON_LABELS } from "../wording";
import type { DecisionRowsProps } from "./DoingRows";
import {
  AllNumbers,
  CalibrationPart,
  calendarDate,
  QuestionsPart,
  WhatChanged,
} from "./LearningParts";
import { ConfirmLine } from "./SetupRows";

/** `n` with thousands separators, pinned to en-US so a test's locale never moves the digits
 * (matches `DoingRows.tsx` and `LearningParts.tsx`'s own `formatNumber`). */
function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

/** "label"/"labels" for the count it follows (matches `Launcher.tsx`'s own `itemsNeedYou` and
 * `SetupRows.tsx`'s own `heldBackWords`, each singular only at exactly 1). */
function labelWord(n: number): string {
  return n === 1 ? "feedback result" : "feedback results";
}

/** The three status words: off, below the label floor (honest that T and C are already moving;
 * only the topic and question weights wait for the floor), or learning. */
function learningStatus(learning: LearningView): string {
  if (!learning.enabled) return "Off";
  if (learning.labels < learning.min_labels) {
    return `Learning (${formatNumber(learning.labels)} of ${formatNumber(
      learning.min_labels,
    )} ${labelWord(learning.min_labels)})`;
  }
  return `Learning from ${formatNumber(learning.labels)} ${labelWord(learning.labels)}`;
}

/** `decision.learning`'s own default (contracts.py's `DecisionSettings.learning: bool = True`). */
function readLearningSetting(values: Readonly<Record<string, unknown>>): boolean {
  const raw = values["decision.learning"];
  return typeof raw === "boolean" ? raw : true;
}

const TRIGGER_LABELS = {
  long_task_look: "After a long task",
  correction_look: "After a correction",
} as const;

const PLAN_LABELS = {
  stalled_project: "For a stalled project",
  overdue_tasks: "For overdue tasks",
} as const;

const DRAFT_LABELS = {
  context_handoff: "For a nearly full chat",
  source_note: "For fetched pages",
} as const;

/** The words after each count of a yield line; a draft is used, not approved. */
interface YieldWords {
  made: string;
  accepted: string;
  rejected: string;
  /** Absent: the line has no fourth count. */
  nothing?: string;
  paused: string;
}

const PLAN_WORDS: YieldWords = {
  made: "started",
  accepted: "approved",
  rejected: "turned down",
  nothing: "found nothing",
  paused: "approved",
};

const DRAFT_WORDS: YieldWords = {
  made: "offered",
  accepted: "used",
  rejected: "turned down",
  nothing: "closed unused",
  paused: "used",
};

const CHECK_WORDS: YieldWords = {
  made: "offered",
  accepted: "used",
  rejected: "turned down",
  paused: "used",
};

const TRIAGE_WORDS: YieldWords = {
  made: "offered",
  accepted: "used",
  rejected: "turned down",
  nothing: "found nothing",
  paused: "used",
};

const KIND_PLURALS: Readonly<Record<FindingKind, string>> = {
  flaw: "Flaws",
  approach: "Better approaches",
  missing: "Missing pieces",
};

export interface LearningRowProps extends DecisionRowsProps {
  /** Injected for tests; defaults to the real clock (matches `DoRow`'s own `nowMs`). */
  nowMs?: number;
}

/** "Learning": what VIKI has learned from the user's answers, in bounds that never move a
 * limit, how well it matches the user, and Reset. */
export function LearningRow({
  page,
  values,
  writable,
  patch,
  api,
  onRefresh,
  nowMs = Date.now(),
  open: openProp,
  onToggle: onToggleProp,
}: LearningRowProps) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNumbers, setShowNumbers] = useState(false);
  const [resetConfirming, setResetConfirming] = useState(false);
  const disabled = !writable || busy;
  const learning = page.learning;

  async function savePatch(changes: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await patch(changes);
      onRefresh();
    } catch {
      setError("Couldn't save that change.");
    } finally {
      setBusy(false);
    }
  }

  async function handleReset(): Promise<void> {
    if (!writable) return;
    setBusy(true);
    setError(null);
    try {
      await api.preference({ name: "reset_learning" });
      setResetConfirming(false);
      onRefresh();
    } catch {
      setError("Couldn't save that change.");
    } finally {
      setBusy(false);
    }
  }

  // Like handleReset: a failed save shows the same line and leaves the page as it was.
  async function handlePreference(body: PreferenceBody): Promise<void> {
    if (!writable) return;
    setBusy(true);
    setError(null);
    try {
      await api.preference(body);
      onRefresh();
    } catch {
      setError("Couldn't save that change.");
    } finally {
      setBusy(false);
    }
  }

  // One type's yield, shared by the second looks, the project plans and the message drafts:
  // counts (worded per kind), Not useful reasons and, while paused, Try again under a name of
  // its own.
  function yieldBlock(
    entry:
      | LearningSecondLookTrigger
      | LearningProjectPlan
      | LearningMessageDraft
      | LearningSavedCheck
      | LearningInboxTriage,
    label: string,
    words: YieldWords = PLAN_WORDS,
  ): ReactNode {
    const reasons = FEEDBACK_REASONS.filter((reason) => entry.reasons[reason] > 0);
    return (
      <div key={entry.candidate}>
        <p>{`${label}: ${entry.made} ${words.made}, ${entry.accepted} ${words.accepted}, ${entry.rejected} ${words.rejected}${
          words.nothing === undefined ? "" : `, ${entry.nothing} ${words.nothing}`
        }`}</p>
        {reasons.length > 0 ? (
          <p className="dloop-hint">{`Not useful: ${reasons
            .map((reason) => `${REASON_LABELS[reason]} ${entry.reasons[reason]}`)
            .join(", ")}`}</p>
        ) : null}
        {entry.paused ? (
          <>
            <p className="dloop-hint">{`Paused: fewer than 1 in 4 ${words.paused}.`}</p>
            <button
              type="button"
              className="btn alt sm"
              disabled={disabled}
              aria-label={`Try again: ${label}`}
              onClick={() =>
                void handlePreference({ name: "try_again", candidate: entry.candidate })
              }
            >
              Try again
            </button>
          </>
        ) : null}
      </div>
    );
  }

  return (
    <DecisionRow
      id="learning"
      name="Learning"
      description="Use your feedback to adjust which suggestions appear and when."
      status={learningStatus(learning)}
      open={open}
      onToggle={toggle}
      control={
        <Switch
          checked={readLearningSetting(values)}
          labelledBy="learning-heading"
          disabled={disabled}
          onChange={(next) => void savePatch({ "decision.learning": next })}
        />
      }
    >
      <p>
        VIKI learns from Useful, Not useful, Stop these, opening or acting on an item, and your
        weekly check answers. It adjusts when and how prominently suggestions appear. Your quiet
        hours, nudges per hour, alerts, muted topics and choices about what VIKI may do stay as you
        set them.
      </p>
      <p>{`Learning which questions and topics matter to you requires feedback on at least ${formatNumber(
        learning.min_labels,
      )} Decision model decisions, including at least 3 positive and 3 negative results. Until then, VIKI adjusts only timing and how prominently suggestions appear.`}</p>
      {learning.since !== null ? (
        <p className="dloop-hint">{`Learning since ${calendarDate(learning.since)}`}</p>
      ) : null}
      <div className="dloop-feedback-guide">
        <h3>How to give feedback</h3>
        <dl>
          <div>
            <dt>Useful</dt>
            <dd>The suggestion helped you.</dd>
          </div>
          <div>
            <dt>Not useful</dt>
            <dd>Tell VIKI why: the topic, timing or information was wrong.</dd>
          </div>
          <div>
            <dt>Stop these</dt>
            <dd>Stop suggestions about that topic. You can unmute it in How VIKI may reach you.</dd>
          </div>
        </dl>
      </div>
      <details className="dloop-details">
        <summary>Learning details and statistics</summary>
        <p className="dloop-hint">
          A feedback result records whether a decision was useful. It can come from your feedback
          buttons, weekly check answers, or opening or acting on an item. The statistics below show
          how these results affect suggestions.
        </p>
        <WhatChanged learning={learning} nowMs={nowMs} />
        <button
          type="button"
          className="btn txt sm"
          aria-expanded={showNumbers}
          aria-controls="learning-numbers"
          onClick={() => setShowNumbers((was) => !was)}
        >
          {showNumbers ? "Hide all numbers" : "Show all numbers"}
        </button>
        {showNumbers ? <AllNumbers learning={learning} /> : null}
        <h3>How well it matches you</h3>
        <CalibrationPart calibration={learning.calibration} />
        <h3>Each question</h3>
        <QuestionsPart questions={learning.questions} />
        <h3>Second looks</h3>
        <p>
          VIKI continues each kind of second look while you approve at least 1 in 4, after 10 have
          been answered. A look that finds nothing does not count. Keep planning counts as turned
          down only if that chat then ends without an approval. VIKI stops showing a kind of finding
          after you turn it down 5 times in a row. Your permission setting, daily limit and quiet
          hours stay as you set them.
        </p>
        {learning.second_looks.triggers.map((trigger) =>
          yieldBlock(trigger, TRIGGER_LABELS[trigger.candidate]),
        )}
        {learning.second_looks.kinds
          .filter((entry) => entry.rejected_in_a_row > 0)
          .map((entry) => (
            <div key={entry.kind}>
              <p>{`${KIND_PLURALS[entry.kind]}: turned down ${entry.rejected_in_a_row} in a row${
                entry.paused ? " - suggestions paused" : ""
              }`}</p>
              {entry.paused ? (
                <button
                  type="button"
                  className="btn alt sm"
                  disabled={disabled}
                  aria-label={`Show these again: ${KIND_PLURALS[entry.kind]}`}
                  onClick={() => void handlePreference({ name: "raise_again", kind: entry.kind })}
                >
                  Show these again
                </button>
              ) : null}
            </div>
          ))}
        <h3>Project plans</h3>
        <p>
          VIKI keeps making a kind of project plan only while you approve at least 1 in 4, counted
          once 10 are answered, and a plan that finds nothing to propose counts as not approved.
          Keep planning counts as turned down only if that chat then ends without an approval. Your
          permission setting, daily limit and quiet hours stay as you set them.
        </p>
        {learning.project_plans.map((plan) => yieldBlock(plan, PLAN_LABELS[plan.candidate]))}
        <h3>Message drafts</h3>
        <p>
          VIKI keeps offering a kind of message draft only while you use at least 1 in 4, counted
          once 10 are used, turned down or closed unused; selecting a draft's button counts as using
          it. Your permission setting, daily limit and quiet hours stay as you set them.
        </p>
        {learning.message_drafts.map((draft) =>
          yieldBlock(draft, DRAFT_LABELS[draft.candidate], DRAFT_WORDS),
        )}
        <h3>Inbox suggestions</h3>
        <p>
          VIKI keeps running inbox suggestions only while you use at least 1 in 4, counted once 10
          are used, turned down or found nothing; selecting Triage inbox or Suggest destinations on
          the item, or Useful, counts as using one, and an item you leave unanswered counts toward
          neither. Your permission setting, daily limit and quiet hours stay as you set them.
        </p>
        {learning.inbox_triage.map((entry) => yieldBlock(entry, "Before you triage", TRIAGE_WORDS))}
        {page.missions.checks !== null ? (
          <>
            <h3>Saved checks</h3>
            <p>
              VIKI keeps offering or running your saved checks on new commits only while you use at
              least 1 in 4 of them, counted once 10 are used or turned down; selecting Run checks or
              Useful counts as using one, and an item you leave unanswered counts toward neither.
              Your permission setting, daily limit and quiet hours stay as you set them.
            </p>
            {learning.saved_checks.map((check) =>
              yieldBlock(check, "On a new commit", CHECK_WORDS),
            )}
          </>
        ) : null}
      </details>
      {resetConfirming ? (
        <ConfirmLine
          text="Reset VIKI's learned values to their starting values? Your recorded decisions and feedback stay."
          confirmLabel="Reset"
          busy={busy}
          onConfirm={() => void handleReset()}
          onCancel={() => setResetConfirming(false)}
        />
      ) : (
        <button
          type="button"
          className="btn txt sm"
          disabled={disabled}
          onClick={() => setResetConfirming(true)}
        >
          Reset what VIKI learned
        </button>
      )}
      {error !== null ? (
        <p role="alert" className="dloop-error">
          {error}
        </p>
      ) : null}
    </DecisionRow>
  );
}
