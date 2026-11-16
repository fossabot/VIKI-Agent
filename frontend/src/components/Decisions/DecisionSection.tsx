/**
 * The inbox's proposal section: vault note, skill, question-wording and vault-change proposals
 * awaiting review, grouped by what needs attention now, what has already been handed off, and
 * what is recorded history. It reads the list itself and records nothing beyond each row's own
 * approve/edit/reject/later action (a question or change proposal offers only approve and
 * reject; a question's reject relabels Undo once its wording is in use, a change's approve
 * relabels Draft again once it is approved), routed through the decision queue's undo window. A
 * change proposal never offers Review details: the app composes no note text to inspect, only
 * the fixed draft its own Approve/Draft again opens.
 */
import {
  type ReactElement,
  type MouseEvent as ReactMouseEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { Key } from "../../i18n";
import { t } from "../../i18n/t";
import {
  CHANGE_STATUS_LABELS,
  type ChangeProposalItem,
  type DecisionsApi,
  type QuestionProposalItem,
  type VaultProposalItem,
} from "../../state/decisionsApi";
import { SKILL_STATE_WORDS, type SkillProposalItem } from "../../state/skillProposalsApi";
import { Loading } from "../Loading/Loading";
import {
  type ApprovalAction,
  type ApprovalActionSpec,
  approvalActions,
  QUESTION_STATUS_LABELS,
} from "./approvalKinds";
import { focusNextRow, useDecisionQueue } from "./decisionQueue";
import { ReasonDialog } from "./ReasonDialog";
import "./DecisionSection.css";

export type ProposalRow =
  | VaultProposalItem
  | SkillProposalItem
  | QuestionProposalItem
  | ChangeProposalItem;

/** The open `ReasonDialog`'s own context: which row and action spec it is for, and the row
 * element `focusNextRow` moves on from once confirm closes it. */
interface ReasonState {
  row: ProposalRow;
  spec: ApprovalActionSpec;
  rowEl: HTMLElement | null;
}

export interface DecisionSectionProps {
  api: DecisionsApi;
  /** A new value refetches the list. */
  refreshKey: number;
  onInspect: (item: ProposalRow) => void;
  /** The window's active workspace, for `approvalKinds`' vault "active" gate. Absent reads as no
   * active workspace: every vault row's actions go disabled with the "other vault" hint. */
  activeWorkspaceId?: string | null;
  /** Runs a row's decided action once the queue's undo window expires, or once a reason is
   * confirmed. Absent, a row's approve/edit/reject/later buttons still render (so a keyboard
   * dispatch elsewhere still finds them structurally) but enqueue a no-op send. */
  onAct?: (row: ProposalRow, action: ApprovalAction, note: string) => Promise<void>;
}

// A row sits in the first group that claims it.
const GROUPS: readonly { key: string; title: string; has: (item: ProposalRow) => boolean }[] = [
  { key: "attention", title: "Needs review", has: (item) => item.attention },
  { key: "handed", title: "Draft handoffs", has: (item) => item.state === "handed" },
  { key: "recorded", title: "History", has: () => true },
];

const STATUS_LABELS: Readonly<Record<string, string>> = {
  handed: "Handoff recorded",
  rejected: "Rejected",
  stale: "Changed since",
  source_missing: "Source not found",
  open: "Proposed",
  applied: "File matches the proposal",
  unknown: "State unknown",
  // Unique to a question proposal, so neither collides with a vault or skill state above.
  approved: "In use",
  replaced: "Replaced",
};

// The Status filter's own label for one row. A question or change proposal never reads through
// STATUS_LABELS: a question's "open" would otherwise read back as "Proposed", not "Being tried",
// and a change's "approved" as "In use" (a question's own word), not "Draft made".
function optionLabel(item: ProposalRow): string {
  if (item.kind === "question_proposal") return QUESTION_STATUS_LABELS[item.state];
  if (item.kind === "change_proposal") return CHANGE_STATUS_LABELS[item.state];
  return STATUS_LABELS[item.state] ?? item.state;
}

function nextStep(item: ProposalRow): string {
  if (item.kind === "question_proposal") {
    if (item.state === "open")
      return "Being tried in the shadow. Approve starts using this wording; Reject keeps the current one.";
    if (item.state === "approved")
      return "In use for new decisions. Undo goes back to the wording before this change.";
    if (item.state === "replaced")
      return "A newer wording is now in use instead. Kept here for history.";
    return "Your rejection is recorded. The wording was not changed by that decision.";
  }
  if (item.kind === "change_proposal") {
    if (item.state === "open")
      return "Approve opens a chat draft asking VIKI to make this change; nothing in the vault changes until you send it.";
    if (item.state === "approved")
      return "A draft was made. Draft again opens it once more. The app never changes the vault itself; only a chat you send can.";
    return "Your rejection is recorded. The vault was not changed by that decision.";
  }
  if (item.state === "stale") return "This item changed. Review details before deciding again.";
  if (item.state === "source_missing")
    return "The source could not be found. Review details before taking action.";
  if (item.state === "unknown")
    return "The status could not be confirmed. Review details and the source.";
  if (item.state === "handed")
    return "Handoff recorded. Check the conversation for the draft and its result.";
  if (item.state === "applied")
    return item.kind === "skill_proposal"
      ? "The current skill file matches the proposal. Review details to compare the source."
      : "The current note matches the proposal. Review details to compare the source.";
  if (item.state === "rejected")
    return "Your rejection is recorded. The skill file was not changed by that decision.";
  return item.kind === "skill_proposal"
    ? "Approve creates a chat draft asking VIKI to apply this skill. Review and send it in the conversation."
    : "Approve creates a chat draft asking VIKI to apply this note change. Review and send it in the conversation.";
}

const sourceOf = (item: ProposalRow) =>
  item.kind === "skill_proposal"
    ? item.relative_id
    : item.kind === "question_proposal"
      ? "Decision"
      : item.ref;

export function DecisionSection({
  api,
  refreshKey,
  onInspect,
  activeWorkspaceId = null,
  onAct,
}: DecisionSectionProps): ReactElement {
  const [items, setItems] = useState<readonly ProposalRow[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  // Whether any read has ever succeeded: the stale-response line below is only meaningful once
  // there is a prior successful list to say it is showing.
  const [everLoaded, setEverLoaded] = useState(false);
  const [sourceFilter, setSourceFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [reload, setReload] = useState(0);
  const [reason, setReason] = useState<ReasonState | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyId = useId();
  const queue = useDecisionQueue();
  const pendingFocusRef = useRef<HTMLElement | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const sourceRef = useRef<HTMLSelectElement>(null);
  const refreshFocusRef = useRef<{ element: HTMLElement; row: HTMLElement } | null>(null);

  // A successful read can move a row between groups, replacing its focused DOM node.
  // Restore that identity only if the commit removed focus and the user has not moved it.
  useLayoutEffect(() => {
    const previous = refreshFocusRef.current;
    refreshFocusRef.current = null;
    if (!previous || previous.element.isConnected || document.activeElement !== document.body)
      return;
    const replacement = [
      ...(sectionRef.current?.querySelectorAll<HTMLElement>("[data-approval-row]") ?? []),
    ].find(
      (row) =>
        row.dataset.id === previous.row.dataset.id &&
        row.dataset.kind === previous.row.dataset.kind,
    );
    (replacement ?? sourceRef.current)?.focus();
  });

  // A confirm closes ReasonDialog (unmounting its own focus trap) before this runs, so the move
  // never races the trap's own focus-restore.
  useEffect(() => {
    if (reason !== null) return;
    const target = pendingFocusRef.current;
    if (!target) return;
    pendingFocusRef.current = null;
    focusNextRow(target);
  }, [reason]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey and reload are trigger-only; nothing in the body reads them
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api
      .decisions(controller.signal)
      .then((inbox) => {
        if (controller.signal.aborted) return;
        const focused = document.activeElement;
        const row = focused?.closest<HTMLElement>("[data-approval-row]");
        refreshFocusRef.current =
          focused instanceof HTMLElement && row && sectionRef.current?.contains(row)
            ? { element: focused, row }
            : null;
        setItems(inbox.items);
        setLoadFailed(false);
        setLoading(false);
        setEverLoaded(true);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setLoadFailed(true);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, refreshKey, reload]);

  // Section row: approve/later send right away; a vault or skill edit opens the
  // inspector (it changes the note itself, not a queued decision); any reject first asks why,
  // through ReasonDialog.
  function handleAction(
    row: ProposalRow,
    spec: ApprovalActionSpec,
    event: ReactMouseEvent<HTMLButtonElement>,
  ): void {
    if (spec.action === "approve" || spec.action === "later") {
      queue.enqueue(row.id, `${spec.label} · ${row.title}`, () =>
        onAct ? onAct(row, spec.action, "") : Promise.resolve(),
      );
      return;
    }
    if (
      spec.action === "edit" &&
      (row.kind === "vault_proposal" || row.kind === "skill_proposal")
    ) {
      onInspect(row);
      return;
    }
    const rowEl = event.currentTarget.closest<HTMLElement>("[data-approval-row]");
    setReason({ row, spec, rowEl });
  }

  function confirmReason(note: string): void {
    if (!reason) return;
    const { row, spec, rowEl } = reason;
    queue.enqueue(row.id, `${spec.label} · ${row.title}`, () =>
      onAct ? onAct(row, spec.action, note) : Promise.resolve(),
    );
    pendingFocusRef.current = rowEl;
    setReason(null);
  }

  const sources = [...new Set(items.map(sourceOf))];
  // Raw states collide only where one state means different things by kind: "open" is
  // "Proposed" for a vault or skill proposal but "Being tried" for a question proposal (see
  // QUESTION_STATUS_LABELS, imported above from approvalKinds). Split the Status filter's
  // options there, one per caption, so each kind gets its own option; every other state still
  // keys by its own raw value, unchanged, and a state that happens to share one caption across
  // kinds (e.g. "rejected") still merges into a single option.
  const captionsByState = new Map<string, Set<string>>();
  for (const item of items) {
    const captions = captionsByState.get(item.state) ?? new Set<string>();
    captions.add(optionLabel(item));
    captionsByState.set(item.state, captions);
  }
  const filterKey = (item: ProposalRow): string =>
    (captionsByState.get(item.state)?.size ?? 0) > 1 ? optionLabel(item) : item.state;
  const stateOptions = [
    ...new Map(items.map((item) => [filterKey(item), optionLabel(item)] as const)),
  ];
  const matches = (item: ProposalRow) =>
    (!sourceFilter || sourceOf(item) === sourceFilter) &&
    (!stateFilter || filterKey(item) === stateFilter);
  const shown = items.filter(matches);
  const groups = GROUPS.map((group, index) => ({
    ...group,
    rows: items.filter((item) => GROUPS.findIndex((other) => other.has(item)) === index),
  })).filter((group) => group.rows.length > 0);

  return (
    <section
      ref={sectionRef}
      className="decisions decision-section"
      aria-label={t("decisions.section")}
    >
      {loadFailed ? <p className="decisions-notice jp">{t("decisions.load_failed")}</p> : null}
      {loading ? <Loading announce={false}>Reading proposals…</Loading> : null}
      {sourceFilter || stateFilter ? (
        <p>
          {shown.length} of {items.length} returned proposals match these filters.
        </p>
      ) : null}
      {(loading || loadFailed) && everLoaded ? (
        <p>Displayed proposals are from the last successful response.</p>
      ) : null}
      {!loading && !loadFailed && items.length === 0 ? (
        <p>No proposals were returned in this response.</p>
      ) : null}
      {loadFailed ? (
        <button
          type="button"
          className="btn alt sm"
          disabled={loading}
          onClick={() => setReload((count) => count + 1)}
        >
          Retry
        </button>
      ) : null}
      <fieldset className="decision-filters">
        <legend>Filter proposals</legend>
        <label>
          Source
          <select
            ref={sourceRef}
            value={sourceFilter}
            onChange={(event) => setSourceFilter(event.target.value)}
          >
            <option value="">All returned sources</option>
            {sourceFilter && !sources.includes(sourceFilter) ? (
              <option value={sourceFilter}>{sourceFilter} (not returned)</option>
            ) : null}
            {sources.map((source) => (
              <option key={source} value={source}>
                {source}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
            <option value="">All statuses</option>
            {stateFilter && !stateOptions.some(([key]) => key === stateFilter) ? (
              <option value={stateFilter}>
                {STATUS_LABELS[stateFilter] ?? stateFilter} (not returned)
              </option>
            ) : null}
            {stateOptions.map(([key, caption]) => (
              <option key={key} value={key}>
                {caption}
              </option>
            ))}
          </select>
        </label>
      </fieldset>
      {groups.map(({ key, title, rows }) => (
        <div key={key} className="decisions-group" data-group={key}>
          <h3 className="decisions-group-title jp">
            {key === "recorded" ? (
              <button
                type="button"
                className="decision-history-toggle"
                aria-expanded={historyOpen}
                aria-controls={historyId}
                onClick={() => setHistoryOpen((open) => !open)}
              >
                History ({rows.length} returned)
              </button>
            ) : (
              title
            )}
          </h3>
          {key !== "recorded" || historyOpen ? (
            <p className="decision-group-count">
              {rows.length} returned proposals; {rows.filter(matches).length} shown.
            </p>
          ) : null}
          <div className="decision-group-rows" id={key === "recorded" ? historyId : undefined}>
            {key !== "recorded" || historyOpen
              ? rows.filter(matches).map((item) => {
                  const actions = approvalActions(item, { activeWorkspaceId });
                  return (
                    <div
                      key={item.id}
                      className="decision-row"
                      data-kind={item.kind}
                      data-id={item.id}
                      data-state={item.state}
                      data-approval-row
                      tabIndex={-1}
                    >
                      <div className="decision-row-heading">
                        <span className="decision-row-kind">
                          {item.kind === "skill_proposal"
                            ? "Skill proposal"
                            : item.kind === "question_proposal"
                              ? "Question proposal"
                              : item.kind === "change_proposal"
                                ? item.change === "note_link"
                                  ? "Link proposal"
                                  : "Upkeep proposal"
                                : "Note proposal"}
                        </span>
                        <span className="state-chip jp" data-state={item.state}>
                          {item.kind === "skill_proposal"
                            ? SKILL_STATE_WORDS[item.state]
                            : item.kind === "question_proposal"
                              ? QUESTION_STATUS_LABELS[item.state]
                              : item.kind === "change_proposal"
                                ? CHANGE_STATUS_LABELS[item.state]
                                : item.state === "handed"
                                  ? "Handoff recorded"
                                  : t(`decisions.chip.${item.state}` as Key)}
                        </span>
                      </div>
                      <div className="decision-row-text">
                        <div className="decision-row-title jp">{item.title}</div>
                        <p className="decision-row-next">{nextStep(item)}</p>
                        {item.origin.workspace_id !== activeWorkspaceId ? (
                          <p className="decision-row-next">
                            This proposal is for another vault. Switch to its vault to use these
                            actions.
                          </p>
                        ) : null}
                        <div className="decision-row-context">
                          <span>Source</span>
                          <span className="decision-row-source mono">{sourceOf(item)}</span>
                        </div>
                      </div>
                      {actions.some((spec) => spec.enabled && spec.action !== "approve") ? (
                        <ul className="decision-row-action-help" aria-label="Action details">
                          {actions
                            .filter((spec) => spec.enabled && spec.action !== "approve")
                            .map((spec) => (
                              <li key={spec.action}>
                                {spec.label}: {spec.hint}
                              </li>
                            ))}
                        </ul>
                      ) : null}
                      <div className="decision-row-actions">
                        {actions.map((spec) => (
                          <button
                            key={spec.action}
                            type="button"
                            className="btn alt sm"
                            data-approval-action={spec.action}
                            title={spec.hint}
                            disabled={!spec.enabled || queue.isPending(item.id)}
                            onClick={(event) => handleAction(item, spec, event)}
                          >
                            {spec.label}
                          </button>
                        ))}
                        {item.kind === "change_proposal" ? null : (
                          <button
                            type="button"
                            className="btn alt sm"
                            data-approval-open
                            aria-label={`Review details ${item.title} · ${sourceOf(item)}`}
                            disabled={queue.isPending(item.id)}
                            onClick={() => onInspect(item)}
                          >
                            Review details
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })
              : null}
          </div>
        </div>
      ))}
      {reason ? (
        <ReasonDialog
          title={reason.row.title}
          noteLabel={
            reason.spec.note === "edit" ? "What to change (optional)" : "Reason (optional)"
          }
          confirmLabel={reason.spec.note === "edit" ? "Approve with edits" : reason.spec.label}
          onConfirm={confirmReason}
          onCancel={() => setReason(null)}
        />
      ) : null}
    </section>
  );
}
