/**
 * The Decision page's "doing" rows: what VIKI is
 * allowed to do on its own and how its decisions have gone lately. Each export is one full row built from the row kit (`DecisionRow`, `Choice` in
 * `../DecisionRow`) and the `dloop-` styles in `../DecisionLoopPage.css`; every row defaults
 * closed, matching Messaging's independently-disclosed sections — `open`/`onToggle` let
 * the Decision page control that instead, falling back to this same per-row state and default
 * when a row is mounted alone, as every test below does (`useRowOpen`, `../DecisionRow`).
 *
 * `Choice` takes `disabled`, but jsdom still fires a disabled radio's `onChange` on a
 * synthetic `fireEvent.click` (it does not replay a real browser's click-suppression for a
 * disabled control), so the plan brake choice below guards the write in `onChange` too,
 * as defence in depth, even though the radios themselves grey out.
 */

import { useId, useState } from "react";
import type {
  DecisionLoopApi,
  FeedbackValue,
  HeldBackRow,
  MissionView,
  PageView,
} from "../../../clients/decisionLoop/types";
import { t } from "../../../i18n/t";
import { runElapsed } from "../../../state/runGroups";
import { formatT } from "../../StatusBar/PlanLimitMeter";
import { Choice, type ChoiceOption, DecisionRow, Switch, useRowOpen } from "../DecisionRow";
import { candidateLabel, RULES_ONLY_MODEL_STATES } from "../wording";
import type { DataFocus } from "./DataRows";
import { type Grant, GrantLine } from "./GrantLine";

/** Shared by every Decision-page row file: the settings document's effective
 * values, whether they can be changed right now, how to change one, the Decision API, and how to
 * ask the page to re-read itself after a change that the page view (not the settings view) must
 * reflect. */
export interface DecisionRowsProps {
  page: PageView;
  values: Readonly<Record<string, unknown>>;
  writable: boolean;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  api: DecisionLoopApi;
  onRefresh: () => void;
  /** Controls this row's disclosure from the page; left undefined, the row keeps its own
   * open/closed state, defaulting closed. */
  open?: boolean;
  onToggle?: () => void;
}

const FEEDBACK_WORDS: Readonly<Record<FeedbackValue, string>> = {
  useful: "Useful",
  wrong: "Not useful",
  later: "Not now",
  mute: "Stopped",
  missed: "Should have told me",
  right_to_hold: "Right to hold",
};

const COUNT_ORDER: readonly (keyof PageView["counts"])[] = [
  "shown",
  "useful",
  "wrong",
  "later",
  "mute",
  "missed",
  "right_to_hold",
];

const COUNT_WORDS: Readonly<Record<keyof PageView["counts"], string>> = {
  shown: "shown",
  useful: "useful",
  wrong: "not useful",
  later: "not now",
  mute: "stopped",
  missed: "should have told me",
  right_to_hold: "right to hold",
};

/** `n` with thousands separators, pinned to en-US so a test's locale never moves the digits. */
function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

function grantOf(value: unknown, fallback: Grant): Grant {
  return value === "off" || value === "ask" || value === "allowed" ? value : fallback;
}

const BRAKE_OPTIONS: readonly ChoiceOption<string>[] = [
  { value: "off", label: "Off" },
  { value: "50", label: "50%" },
  { value: "80", label: "80%" },
  { value: "95", label: "95%" },
];

function brakeOf(value: unknown): string {
  return BRAKE_OPTIONS.some((option) => option.value === value) ? (value as string) : "80";
}

/** The notes a grant line shows: each candidate that is not `false`, in order. */
function notesOf(...candidates: readonly (string | false)[]): string[] {
  return candidates.filter((note): note is string => note !== false);
}

function limitOf(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 10
    ? value
    : fallback;
}

export interface DoRowProps extends DecisionRowsProps {
  /** Opens the given chat; every running mission's "Open" calls this with its own session id. */
  onOpenChat: (sessionId: string) => void;
  /** Injected for tests; defaults to the real clock. */
  nowMs?: number;
}

/** "What VIKI may do": one legend, the chats VIKI started (each stoppable from this row), a grant
 * line with its daily limit for each kind of work, and the plan brake with the fixed line no grant
 * lifts. */
export function DoRow({
  page,
  values,
  writable,
  patch,
  api,
  onRefresh,
  onOpenChat,
  nowMs = Date.now(),
  open: openProp,
  onToggle: onToggleProp,
}: DoRowProps) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const uid = useId();
  const disabled = !writable || busy;
  const { running, today, limits, team, look, second_look, project_plan, inbox, checks } =
    page.missions;
  const runningCount = running.length + team.running + (checks?.running ?? 0);

  async function savePatch(changes: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await patch(changes);
    } catch {
      setError("Could not save that change. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function saveGrant(key: string): (grant: Grant) => void {
    return (grant) => void savePatch({ [key]: grant });
  }

  function saveLimit(key: string, value: number): void {
    void savePatch({ [key]: value });
  }

  async function stop(missionId: string): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.stopMission(missionId);
      onRefresh();
    } catch {
      setError("Could not stop that chat. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function stopAll(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.stopAllMissions();
      onRefresh();
    } catch {
      setError("Could not stop those chats. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const prepare = grantOf(values["decision.grant.prepare"], "allowed");
  const run = grantOf(values["decision.grant.run"], "ask");
  const draftsLimit = limitOf(values["decision.limit.drafts"], 5);
  const chatsLimit = limitOf(values["decision.limit.chats"], 3);
  const changesLimit = limitOf(values["decision.limit.changes"], 3);
  const teamGrant = grantOf(values["decision.grant.team"], "ask");
  const teamLimit = limitOf(values["decision.limit.team"], 4);
  const lookGrant = grantOf(values["decision.grant.look"], "allowed");
  const lookLimit = limitOf(values["decision.limit.look"], 3);
  const secondLookGrant = grantOf(values["decision.grant.second_look"], "allowed");
  const secondLookLimit = limitOf(values["decision.limit.second_look"], 3);
  const projectPlanGrant = grantOf(values["decision.grant.project_plan"], "allowed");
  const projectPlanLimit = limitOf(values["decision.limit.project_plan"], 3);
  const messageDraftLimit = limitOf(values["decision.limit.message_draft"], 3);
  const inboxGrant = grantOf(values["decision.grant.inbox_triage"], "allowed");
  const inboxLimit = limitOf(values["decision.limit.inbox_triage"], 3);
  const checkGrant = grantOf(values["decision.grant.check"], "ask");
  const checkLimit = limitOf(values["decision.limit.check"], 3);
  const handoffs = values["decision.team.handoffs"] === true;
  const brake = brakeOf(values["decision.plan_brake"]);
  const holding =
    page.missions.hold !== null &&
    (prepare === "allowed" ||
      run === "allowed" ||
      teamGrant === "allowed" ||
      lookGrant === "allowed" ||
      secondLookGrant === "allowed" ||
      projectPlanGrant === "allowed" ||
      inboxGrant === "allowed");
  const status =
    runningCount > 0
      ? `${runningCount} running`
      : prepare === "off" &&
          run === "off" &&
          teamGrant === "off" &&
          lookGrant === "off" &&
          secondLookGrant === "off" &&
          projectPlanGrant === "off" &&
          inboxGrant === "off" &&
          (checks === null || checkGrant === "off")
        ? "Off"
        : holding
          ? "Holding"
          : "On";

  const rulesOnly = RULES_ONLY_MODEL_STATES.has(page.model.state);
  const noTasks = values["decision.watch.tasks"] === false;
  const noVaultChanges = values["decision.watch.vault_changes"] === false;
  const taskNotes = notesOf(
    rulesOnly && t("decision_page.do.needs_model_tasks"),
    noTasks && t("decision_page.do.dep.tasks"),
  );
  // Which watch switch stops the upkeep proposals, one note only: Self-improvement gates them all,
  // then Vault changes (note pairs) and Tasks due (ticked tasks) each gate their own part.
  const upkeepWatchNote =
    values["decision.watch.improve"] === false
      ? t("decision_page.do.dep.improve")
      : noVaultChanges && noTasks
        ? t("decision_page.do.dep.upkeep")
        : noVaultChanges
          ? t("decision_page.do.dep.upkeep_links")
          : noTasks
            ? t("decision_page.do.dep.upkeep_tasks")
            : false;

  return (
    <DecisionRow
      id="do"
      name="What VIKI may do"
      description={t("decision_page.do.description")}
      status={status}
      control={null}
      open={open}
      onToggle={toggle}
    >
      <dl className="dloop-legend">
        <dt>Off</dt>
        <dd>{t("decision_page.do.legend.off")}</dd>
        <dt>Ask first</dt>
        <dd>{t("decision_page.do.legend.ask")}</dd>
        <dt>Allowed</dt>
        <dd>{t("decision_page.do.legend.allowed")}</dd>
      </dl>
      <p className="dloop-hint">{t("decision_page.do.legend.drafts")}</p>
      {runningCount > 0 ? (
        <div className="dloop-subgroup">
          <h4 className="dloop-subhead">Running chats</h4>
          {running.length > 0 ? (
            <ul>
              {running.map((mission: MissionView) => (
                <li key={mission.mission_id}>
                  <span>{`${mission.title} — running ${runElapsed(
                    Math.max(0, nowMs - Date.parse(mission.started_at)),
                  )}`}</span>
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={disabled}
                    onClick={() => onOpenChat(mission.session_id)}
                  >
                    Open
                  </button>
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={disabled}
                    onClick={() => void stop(mission.mission_id)}
                  >
                    Stop
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {team.running > 0 ? (
            <p>{`Bot runs VIKI started, still running: ${team.running}`}</p>
          ) : null}
          {checks !== null && checks.running > 0 ? (
            <p>{`Saved-check runs VIKI started, still running: ${checks.running}`}</p>
          ) : null}
          {runningCount >= 2 ? (
            <div className="dloop-button-row">
              <button
                type="button"
                className="btn alt sm"
                disabled={disabled}
                onClick={() => void stopAll()}
              >
                Stop all
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="dloop-subgroup">
        <h4 className="dloop-subhead">{t("decision_page.do.review")}</h4>
        <p className="dloop-hint">{t("decision_page.do.review_hint")}</p>
        <GrantLine
          label="Prepare drafts"
          hint={t("decision_page.hint.prepare")}
          grant={prepare}
          onGrant={saveGrant("decision.grant.prepare")}
          limits={[
            {
              key: "decision.limit.drafts",
              label: "Daily limit: drafts",
              value: draftsLimit,
              today: `Today: ${today.prepare} of ${limits.prepare} drafts`,
            },
            {
              key: "decision.limit.message_draft",
              label: "Daily limit: message drafts",
              value: messageDraftLimit,
              today: `Today: ${page.message_drafts.today} of ${page.message_drafts.limit} message drafts`,
            },
          ]}
          onLimit={saveLimit}
          notes={taskNotes}
          more={
            <p className="dloop-hint">
              Under Prepare drafts, VIKI also offers a message for the chat you have open in a
              vault: a handoff when its context is 70% full or more, or literature notes for the web
              pages your last reply fetched. A draft goes into your message box only when you select
              it, under Ask first and Allowed alike.
            </p>
          }
          disabled={disabled}
        />
        <GrantLine
          label="Look on my behalf"
          hint={t("decision_page.hint.look")}
          grant={lookGrant}
          onGrant={saveGrant("decision.grant.look")}
          limits={[
            {
              key: "decision.limit.look",
              label: "Daily limit: looks VIKI starts",
              value: lookLimit,
              today: `Today: ${look.today} of ${look.limit} looks`,
            },
          ]}
          onLimit={saveLimit}
          more={
            <p className="dloop-hint">
              When a trusted code folder's uncommitted changes have not changed for a day or more,
              VIKI starts a chat there in plan mode to look them over; it waits for your approval
              before it changes anything. Ask first offers Take a look in VIKI's launcher instead.
            </p>
          }
          disabled={disabled}
        />
        <GrantLine
          label="Take a second look at my work"
          hint={t("decision_page.hint.second_look")}
          grant={secondLookGrant}
          onGrant={saveGrant("decision.grant.second_look")}
          limits={[
            {
              key: "decision.limit.second_look",
              label: "Daily limit: second looks VIKI starts",
              value: secondLookLimit,
              today: `Today: ${second_look.today} of ${second_look.limit} second looks`,
            },
          ]}
          onLimit={saveLimit}
          more={
            <p className="dloop-hint">
              When a long task or work you corrected finishes in the chat you have open and edited
              notes, VIKI starts a chat in plan mode to look the work over and present one flaw,
              better approach or missing piece as a plan; it waits for your approval before it
              changes anything. Ask first offers Take a second look in VIKI's launcher instead.
            </p>
          }
          disabled={disabled}
        />
        <GrantLine
          label="Plan next steps for my projects"
          hint={t("decision_page.hint.project_plan")}
          grant={projectPlanGrant}
          onGrant={saveGrant("decision.grant.project_plan")}
          limits={[
            {
              key: "decision.limit.project_plan",
              label: "Daily limit: project plans VIKI starts",
              value: projectPlanLimit,
              today: `Today: ${project_plan.today} of ${project_plan.limit} project plans`,
            },
          ]}
          onLimit={saveLimit}
          notes={notesOf(noTasks && t("decision_page.do.dep.overdue"))}
          more={
            <p className="dloop-hint">
              When nothing in an active project has changed for two weeks, or a project's tasks are
              three days or more overdue, VIKI starts a chat in plan mode: for the project it
              proposes three next actions, and for the tasks it proposes ticking the done ones and
              new dates for the rest; it waits for your approval before it changes anything. Ask
              first offers Plan next steps or Check these tasks in VIKI's launcher instead.
            </p>
          }
          disabled={disabled}
        />
        <GrantLine
          label="Suggest destinations for my inbox"
          hint={t("decision_page.hint.inbox")}
          grant={inboxGrant}
          onGrant={saveGrant("decision.grant.inbox_triage")}
          limits={[
            {
              key: "decision.limit.inbox_triage",
              label: "Daily limit: inbox suggestion items",
              value: inboxLimit,
              today: `Today: ${inbox.today} of ${inbox.limit} inbox suggestion items`,
            },
          ]}
          onLimit={saveLimit}
          notes={notesOf(
            values["decision.watch.inbox"] === false && t("decision_page.do.dep.inbox"),
          )}
          more={
            <p className="dloop-hint">
              When your inbox has more than three captures, or one older than three days, and some
              have no suggested destination, VIKI asks Claude Code once, with read-only tools, where
              each could go, ten minutes after the newest one changed, so suggestions wait in Triage
              inbox. It sends your captures' opening text to Anthropic, as Ask VIKI to suggest does,
              and denies any permission that run asks for. Ask first offers Suggest destinations in
              VIKI's launcher instead.
            </p>
          }
          disabled={disabled}
        />
        <GrantLine
          label={t("decision_page.upkeep.label")}
          hint={t("decision_page.upkeep.hint")}
          limits={[
            {
              key: "decision.limit.changes",
              label: "Daily limit: vault changes",
              value: changesLimit,
              today: `Today: ${page.changes.today} of ${page.changes.limit} vault changes`,
            },
          ]}
          onLimit={saveLimit}
          notes={notesOf(rulesOnly && t("decision_page.do.needs_model_links"), upkeepWatchNote)}
          disabled={disabled}
        />
      </div>
      <div className="dloop-subgroup">
        <h4 className="dloop-subhead">{t("decision_page.do.changes")}</h4>
        <p className="dloop-hint">{t("decision_page.do.changes_hint")}</p>
        <GrantLine
          label="Start chats to do work"
          hint={t("decision_page.hint.run")}
          grant={run}
          onGrant={saveGrant("decision.grant.run")}
          limits={[
            {
              key: "decision.limit.chats",
              label: "Daily limit: chats",
              value: chatsLimit,
              today: `Today: ${today.run} of ${limits.run} chats`,
            },
          ]}
          onLimit={saveLimit}
          notes={taskNotes}
          disabled={disabled}
        />
        <GrantLine
          label="Start Bots' work"
          hint={t("decision_page.hint.team")}
          grant={teamGrant}
          onGrant={saveGrant("decision.grant.team")}
          limits={[
            {
              key: "decision.limit.team",
              label: "Daily limit: Bot runs VIKI starts",
              value: teamLimit,
              today: `Today: ${team.today} of ${team.limit} Bot runs`,
            },
          ]}
          onLimit={saveLimit}
          more={
            <p className="dloop-hint">
              When one of your todo cards waits for a seat, VIKI starts that seat's one enabled,
              tested routine. Ask first offers Start in VIKI's launcher instead.
            </p>
          }
          disabled={disabled}
        >
          <div className="dloop-switch-row">
            <span id={`${uid}-handoffs`}>Let seats hand work to each other</span>
            <Switch
              checked={handoffs}
              labelledBy={`${uid}-handoffs`}
              disabled={disabled}
              onChange={(checked) => void savePatch({ "decision.team.handoffs": checked })}
            />
          </div>
          <p className="dloop-hint">{t("decision_page.handoffs.hint")}</p>
        </GrantLine>
        {checks !== null ? (
          <GrantLine
            label="Run my saved checks"
            hint={t("decision_page.hint.check")}
            grant={checkGrant}
            onGrant={saveGrant("decision.grant.check")}
            limits={[
              {
                key: "decision.limit.check",
                label: "Daily limit: new commits VIKI checks",
                value: checkLimit,
                today: `Today: ${checks.today} of ${checks.limit} new commits`,
              },
            ]}
            onLimit={saveLimit}
            more={
              <p className="dloop-hint">
                When a trusted code folder stays for ten minutes at a new commit that its saved
                checks have not run on, a pulled commit included, VIKI runs the saved checks you
                have run in that folder before, as Run in its Checks tab does, and reports whether
                they passed. Ask first offers Run checks in VIKI's launcher instead. Each check is
                your own command: it runs with your account's rights and can change files and use
                the network.
              </p>
            }
            disabled={disabled}
          />
        ) : null}
      </div>
      <div className="dloop-subgroup">
        <h4 className="dloop-subhead">{t("decision_page.do.safety")}</h4>
        <Choice
          label="Pause automatic starts at Claude plan usage"
          options={BRAKE_OPTIONS}
          value={brake}
          disabled={disabled}
          onChange={(value) => {
            // Choice greys out its own radios via `disabled` above, but jsdom still fires a
            // disabled radio's onChange on a synthetic click, so this guard stays as defence in
            // depth (GrantLine has the same guard).
            if (disabled) return;
            void savePatch({ "decision.plan_brake": value });
          }}
        />
        <p className="dloop-hint">{t("decision_page.brake.hint")}</p>
        {holding && page.missions.hold !== null ? (
          <p className="dloop-hint">
            {page.missions.hold.until === null
              ? "Holding VIKI's starts until Claude Code reports your plan use again."
              : `Holding VIKI's starts until ${formatT(Date.parse(page.missions.hold.until), nowMs)}.`}
          </p>
        ) : null}
        <p className="dloop-hint">
          VIKI starts a Bot only through its own tested routine, under Start Bots' work. Apart from
          the notice of a Bot whose delivery you turned on, it does not message other chats or
          people or send to your phone.
        </p>
      </div>
      {error !== null ? (
        <p role="alert" className="dloop-error">
          {error}
        </p>
      ) : null}
    </DecisionRow>
  );
}

/** "How VIKI is doing": the 30-day feedback counts and links to browse them, the
 * weekly check's sample of what was held back last week (feedback-able unless already labelled),
 * and a way to report something VIKI never raised at all. Each part says whether it needs the
 * user, and the status chip counts the weekly check's unanswered rows. */
export interface HowRowProps extends DecisionRowsProps {
  onOpenData: (target: DataFocus["target"]) => void;
}

export function HowRow({
  page,
  writable,
  api,
  onRefresh,
  onOpenData,
  open: openProp,
  onToggle: onToggleProp,
}: HowRowProps) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reported, setReported] = useState(false);
  const disabled = !writable || busy;

  async function sendFeedback(episodeId: string, value: "right_to_hold" | "missed"): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.feedback({
        item_key: null,
        episode_id: episodeId,
        value,
        reason: null,
        source: "page",
      });
      onRefresh();
    } catch {
      setError("Could not save your feedback. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function reportMiss(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.report("unwatched", "page");
      setReported(true);
    } catch {
      setError("Could not send that report. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const counts = COUNT_ORDER.map(
    (key) => `${formatNumber(page.counts[key])} ${COUNT_WORDS[key]}`,
  ).join(" · ");
  // The weekly check's rows still waiting for an answer: the one thing this row asks for.
  const toCheck = page.held_back.filter((row) => row.feedback === null).length;

  return (
    <DecisionRow
      id="how"
      name="How VIKI is doing"
      description="A record of what VIKI showed you and what it held back. Once a week, tell VIKI whether holding things back was right; your answers help tune when it speaks up."
      status={toCheck > 0 ? `${formatNumber(toCheck)} to check` : "Nothing to do now"}
      control={null}
      open={open}
      onToggle={toggle}
    >
      <p className="dloop-hint">
        Only two parts here need you: the Weekly check, and Report a missing suggestion when VIKI
        missed something. The rest is a record to read.
      </p>

      <h3>Last 30 days</h3>
      <p className="dloop-hint">
        Read only. How many suggestions VIKI showed you, and how you answered them.
      </p>
      <p>{counts}</p>
      <div className="dloop-button-row">
        <button type="button" className="btn txt sm" onClick={() => onOpenData("answers")}>
          {t("decision_page.data.see_answers")}
        </button>
        <button type="button" className="btn txt sm" onClick={() => onOpenData("decisions")}>
          {t("decision_page.data.see_all")}
        </button>
      </div>

      <h3>Weekly check</h3>
      <p className="dloop-hint">
        Needs you, once a week. Up to 10 things VIKI kept quiet about last week, picked at random.
        Choose Right to hold if staying quiet was right, or Should have told me if you wanted to see
        it. While Learning is on, it uses your answers to tune when and how loudly VIKI speaks up.
      </p>
      {page.held_back.length === 0 ? (
        <p>Nothing to check from last week. No action needed.</p>
      ) : toCheck === 0 ? (
        <p>All answered. No action needed until next week.</p>
      ) : null}
      {page.held_back.length === 0 ? null : (
        <ul>
          {page.held_back.map((row: HeldBackRow) => (
            <li key={row.episode_id}>
              <span>
                {row.why === ""
                  ? candidateLabel(row.candidate)
                  : `${candidateLabel(row.candidate)} — ${row.why}`}
              </span>{" "}
              {row.feedback === null ? (
                <>
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={disabled}
                    onClick={() => void sendFeedback(row.episode_id, "right_to_hold")}
                  >
                    Right to hold
                  </button>
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={disabled}
                    onClick={() => void sendFeedback(row.episode_id, "missed")}
                  >
                    Should have told me
                  </button>
                </>
              ) : (
                <span className="chip">{`Your answer: ${FEEDBACK_WORDS[row.feedback]}`}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Something VIKI missed</h3>
      <p className="dloop-hint">
        Optional, any time. If VIKI never raised something you needed, report it. It counts as one
        should have told me in the 30-day numbers; it does not record what was missed.
      </p>
      {reported ? (
        <p role="status">Recorded.</p>
      ) : (
        <button
          type="button"
          className="btn alt sm"
          disabled={disabled}
          onClick={() => void reportMiss()}
        >
          Report a missing suggestion
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
