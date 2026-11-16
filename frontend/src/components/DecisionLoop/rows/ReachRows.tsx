/**
 * The Decision page's "reach" rows: what VIKI watches and how it may reach the user — 2 of the
 * page's rows. Built from the row kit (`DecisionRow`, `Switch`, `Choice` in `../DecisionRow`) and
 * the `dloop-` classes in `../DecisionLoopPage.css`. Each row owns its own disclosure `open`
 * state, defaulting closed (ported from Messaging's own per-provider rows, as
 * `../DecisionRow.tsx`'s header says this kit does); `SetupRows.tsx` instead defaults its rows
 * open.
 *
 * `values` (the settings document's effective values; kept fresh by the caller's own settings
 * subscription once `patch` resolves, so every change below that goes through `patch` needs no
 * `onRefresh`) drives every control. Unmute (`api.preference`, not `patch`) always refreshes,
 * since `page.muted` is its only source.
 *
 * `Choice` takes `disabled`, but jsdom still fires a disabled radio's `onChange` on a synthetic
 * `fireEvent.click` (it does not replay a real browser's click-suppression for a disabled
 * control) — so the cadence choice below guards its own `onChange` too, as defence in depth, even
 * though the radios themselves grey out. The watch/alerts/in-context rows pair their label and
 * `Switch` under `../DecisionLoopPage.css`'s `.dloop-switch-row`.
 */
import { useState } from "react";
import type {
  DecisionCandidate,
  DecisionLoopApi,
  PageView,
} from "../../../clients/decisionLoop/types";
import type { Key } from "../../../i18n";
import { t, tf } from "../../../i18n/t";
import { Choice, type ChoiceOption, DecisionRow, Switch, useRowOpen } from "../DecisionRow";
import { candidateLabel, RULES_ONLY_MODEL_STATES } from "../wording";

/** Shared by every Decision-page row file: the page's own view, the settings
 * document's effective values, whether they can be changed right now, how to change one, the
 * Decision API, and how to ask the page to re-read itself after a change the page view (not the
 * settings view) must reflect. */
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

const SAVE_ERROR = "Could not save that change. Try again.";

/** The 48 half-hour marks a quiet-hours select offers, "00:00" through "23:30"
 * (`../../Routines/RoutineControls.tsx`'s own `QUIET_TIMES`, read for the pattern rather than
 * imported since this file owns no dependency on it). */
const QUIET_TIMES = Array.from({ length: 48 }, (_, i) => {
  const hour = String(Math.floor(i / 2)).padStart(2, "0");
  return `${hour}:${i % 2 ? "30" : "00"}`;
});

// ---------------------------------------------------------------------------
// What VIKI watches
// ---------------------------------------------------------------------------

type WatchGroup = "vault" | "app" | "improve";

interface WatchItem {
  key: string;
  id: string;
  label: string;
  group: WatchGroup;
  /** Plain text, or a catalog key resolved in render (never at module load). */
  description: string | { key: Key };
  /** The catalog key of a note on what else turning this off stops. */
  dep?: Key;
}

const WATCH_ITEMS: readonly WatchItem[] = [
  {
    key: "decision.watch.tasks",
    id: "watch-tasks",
    label: "Tasks due",
    group: "vault",
    description: "Due dates and overdue tasks in your vault.",
    dep: "decision_page.watch.dep.tasks",
  },
  {
    key: "decision.watch.inbox",
    id: "watch-inbox",
    label: "Inbox",
    group: "vault",
    description: "Captured notes waiting to be organized.",
    dep: "decision_page.watch.dep.inbox",
  },
  {
    key: "decision.watch.vault_changes",
    id: "watch-vault-changes",
    label: "Vault changes",
    group: "vault",
    description: "Uncommitted changes and suggestions to link or update notes.",
    dep: "decision_page.watch.dep.vault_changes",
  },
  {
    key: "decision.watch.kanban",
    id: "watch-kanban",
    label: "Kanban",
    group: "vault",
    description: "Cards that need attention or could move to a Bot.",
  },
  {
    key: "decision.watch.vault_health",
    id: "watch-vault-health",
    label: "Vault health",
    group: "vault",
    description: "Recovery issues and memories whose source notes changed.",
  },
  {
    key: "decision.watch.approvals",
    id: "watch-approvals",
    label: "Approvals",
    group: "app",
    description: "Chats waiting for your approval or an answer.",
  },
  {
    key: "decision.watch.chats",
    id: "watch-chats",
    label: "Chats and runs",
    group: "app",
    description: "Activity and problems in your chats and agent runs.",
  },
  {
    key: "decision.watch.routines",
    id: "watch-routines",
    label: "Routines",
    group: "app",
    description: "Upcoming scheduled work and Bot runs that need you.",
  },
  {
    key: "decision.watch.messaging",
    id: "watch-messaging",
    label: "Messaging",
    group: "app",
    description: "Messaging bridges that have stopped.",
  },
  {
    key: "decision.watch.improve",
    id: "watch-improve",
    label: "Self-improvement",
    group: "improve",
    description: { key: "decision_page.watch.improve" },
  },
];

const WATCH_GROUPS: ReadonlyArray<{ group: WatchGroup; title: Key }> = [
  { group: "vault", title: "decision_page.watch.group.vault" },
  { group: "app", title: "decision_page.watch.group.app" },
  { group: "improve", title: "decision_page.watch.group.improve" },
];

type CadenceValue = "change" | "5m" | "15m";

/** "What VIKI watches": ten switches, each `decision.watch.*` defaulting to on, so a
 * missing or non-boolean value in `values` reads as on. None of these keys are mirrored in
 * `page`, so a successful change needs no `onRefresh`. */
export function WatchRow({
  values,
  writable,
  patch,
  open: openProp,
  onToggle: onToggleProp,
}: DecisionRowsProps) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const disabled = !writable || busy;

  async function save(changes: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await patch(changes);
    } catch {
      setError(SAVE_ERROR);
    } finally {
      setBusy(false);
    }
  }

  const onCount = WATCH_ITEMS.filter((item) => values[item.key] !== false).length;
  const cadenceRaw = values["decision.cadence"];
  const cadence: CadenceValue = cadenceRaw === "5m" || cadenceRaw === "15m" ? cadenceRaw : "change";
  const cadenceOptions: readonly ChoiceOption<CadenceValue>[] = [
    { value: "change", label: t("decision_page.watch.cadence.change") },
    { value: "5m", label: "Every 5 minutes" },
    { value: "15m", label: "Every 15 minutes" },
  ];

  return (
    <DecisionRow
      id="watch"
      name="What VIKI watches"
      description={t("decision_page.watch.description")}
      status={`${onCount} of ${WATCH_ITEMS.length} on`}
      control={null}
      open={open}
      onToggle={toggle}
    >
      <Choice
        label="How often VIKI checks"
        options={cadenceOptions}
        value={cadence}
        disabled={disabled}
        describedBy="watch-cadence-hint"
        onChange={(value) => {
          // Choice (../DecisionRow) greys out its own radios via `disabled` above, but jsdom
          // still fires a disabled radio's onChange on a synthetic click (found), so this guard
          // against writing while disabled stays, as defence in depth.
          if (disabled) return;
          void save({ "decision.cadence": value });
        }}
      />
      <p className="dloop-hint" id="watch-cadence-hint">
        {t("decision_page.watch.cadence_hint")}
      </p>
      <p className="dloop-hint">{t("decision_page.watch.rules_note")}</p>
      {WATCH_GROUPS.map(({ group, title }) => (
        <div key={group} className="dloop-subgroup">
          <h4 className="dloop-subhead">{t(title)}</h4>
          {WATCH_ITEMS.filter((item) => item.group === group).map((item) => (
            <div key={item.key} className="dloop-switch-row">
              <div>
                <span id={item.id}>{item.label}</span>
                <p className="dloop-hint" id={`${item.id}-hint`}>
                  {typeof item.description === "string"
                    ? item.description
                    : t(item.description.key)}
                </p>
                {item.dep ? (
                  <p className="dloop-hint" id={`${item.id}-dep`}>
                    {t(item.dep)}
                  </p>
                ) : null}
              </div>
              <Switch
                checked={values[item.key] !== false}
                labelledBy={item.id}
                describedBy={item.dep ? `${item.id}-hint ${item.id}-dep` : `${item.id}-hint`}
                disabled={disabled}
                onChange={(checked) => void save({ [item.key]: checked })}
              />
            </div>
          ))}
        </div>
      ))}
      {error !== null ? (
        <p role="alert" className="dloop-error">
          {error}
        </p>
      ) : null}
    </DecisionRow>
  );
}

// ---------------------------------------------------------------------------
// How VIKI may reach you
// ---------------------------------------------------------------------------

const NUDGES_OPTIONS = [0, 1, 2] as const;
type NudgesOption = (typeof NUDGES_OPTIONS)[number];

/** "How VIKI may reach you": nudges per hour, alerts, in-context delivery, and the
 * muted candidates, each with its own Unmute. None of the settings here are mirrored in `page`,
 * so only Unmute (which changes `page.muted` through `api.preference`) refreshes. */
export function ReachRow({
  page,
  values,
  writable,
  patch,
  api,
  onRefresh,
  open: openProp,
  onToggle: onToggleProp,
}: DecisionRowsProps) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const disabled = !writable || busy;

  async function save(changes: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await patch(changes);
    } catch {
      setError(SAVE_ERROR);
    } finally {
      setBusy(false);
    }
  }

  async function unmute(candidate: DecisionCandidate): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.preference({ name: "unmute", candidate });
      onRefresh();
    } catch {
      setError("Could not unmute that. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const nudgesRaw = values["decision.nudges_per_hour"];
  const nudgesPerHour: NudgesOption =
    typeof nudgesRaw === "number" && (NUDGES_OPTIONS as readonly number[]).includes(nudgesRaw)
      ? (nudgesRaw as NudgesOption)
      : 2;
  const alertsOn = values["decision.alerts"] === true;
  const inContext = values["decision.in_context"] !== false;
  const nudgesWord =
    nudgesPerHour === 0
      ? t("decision_page.reach.nudges.none")
      : nudgesPerHour === 1
        ? t("decision_page.reach.nudges.one")
        : tf("decision_page.reach.nudges.many", { n: nudgesPerHour });

  const quietStartRaw = values["notifications.quiet_start"];
  const quietStart = typeof quietStartRaw === "string" ? quietStartRaw : null;
  const quietEndRaw = values["notifications.quiet_end"];
  const quietEnd = typeof quietEndRaw === "string" ? quietEndRaw : null;

  return (
    <DecisionRow
      id="reach"
      name="How VIKI may reach you"
      description={t("decision_page.reach.description")}
      status={`${nudgesWord} · ${alertsOn ? "Alerts on" : "Alerts off"}`}
      control={null}
      open={open}
      onToggle={toggle}
    >
      {RULES_ONLY_MODEL_STATES.has(page.model.state) ? (
        <p className="dloop-hint">{t("decision_page.reach.rules_only")}</p>
      ) : null}
      <dl className="dloop-legend">
        <dt>Listed</dt>
        <dd>{t("decision_page.reach.legend.list")}</dd>
        <dt>Badge</dt>
        <dd>{t("decision_page.reach.legend.badge")}</dd>
        <dt>Nudge</dt>
        <dd>{t("decision_page.reach.legend.nudge")}</dd>
        <dt>Alert</dt>
        <dd>{t("decision_page.reach.legend.alert")}</dd>
      </dl>
      <label className="dloop-field">
        <span className="lbl">Nudges per hour</span>
        <select
          value={String(nudgesPerHour)}
          disabled={disabled}
          aria-describedby="reach-nudges-hint"
          onChange={(event) =>
            void save({ "decision.nudges_per_hour": Number(event.target.value) })
          }
        >
          {NUDGES_OPTIONS.map((count) => (
            <option key={count} value={String(count)}>
              {count}
            </option>
          ))}
        </select>
      </label>
      <p className="dloop-hint" id="reach-nudges-hint">
        {t("decision_page.reach.nudges_hint")}
      </p>
      <div className="dloop-switch-row">
        <div>
          <span id="reach-alerts">Alerts</span>
          <p className="dloop-hint" id="reach-alerts-hint">
            {t("decision_page.reach.alerts_hint")}
          </p>
        </div>
        <Switch
          checked={alertsOn}
          labelledBy="reach-alerts"
          describedBy="reach-alerts-hint"
          disabled={disabled}
          onChange={(checked) => void save({ "decision.alerts": checked })}
        />
      </div>
      <div className="dloop-switch-row">
        <div>
          <span id="reach-in-context">Tell me when I open the related chat or note</span>
          <p className="dloop-hint" id="reach-in-context-hint">
            {t("decision_page.reach.in_context_hint")}
          </p>
        </div>
        <Switch
          checked={inContext}
          labelledBy="reach-in-context"
          describedBy="reach-in-context-hint"
          disabled={disabled}
          onChange={(checked) => void save({ "decision.in_context": checked })}
        />
      </div>
      <fieldset className="dloop-choice" aria-describedby="reach-quiet-hint">
        <legend className="dloop-choice-legend">Quiet hours</legend>
        <label className="dloop-field">
          <span className="lbl">From</span>
          <select
            value={quietStart ?? ""}
            disabled={disabled}
            onChange={(event) =>
              void save({ "notifications.quiet_start": event.target.value || null })
            }
          >
            <option value="">None</option>
            {QUIET_TIMES.map((time) => (
              <option key={time} value={time}>
                {time}
              </option>
            ))}
          </select>
        </label>
        <label className="dloop-field">
          <span className="lbl">To</span>
          <select
            value={quietEnd ?? ""}
            disabled={disabled}
            onChange={(event) =>
              void save({ "notifications.quiet_end": event.target.value || null })
            }
          >
            <option value="">None</option>
            {QUIET_TIMES.map((time) => (
              <option key={time} value={time}>
                {time}
              </option>
            ))}
          </select>
        </label>
      </fieldset>
      <p className="dloop-hint" id="reach-quiet-hint">
        {t("decision_page.reach.quiet_hint")}
      </p>
      <div>
        <p className="lbl">{t("decision_page.reach.stopped")}</p>
        {page.muted.length === 0 ? (
          <p>{t("decision_page.reach.stopped_none")}</p>
        ) : (
          <ul>
            {page.muted.map((candidate) => (
              <li key={candidate}>
                <span className="chip">{candidateLabel(candidate)}</span>
                <button
                  type="button"
                  className="btn txt sm"
                  disabled={disabled}
                  aria-label={`Unmute ${candidateLabel(candidate)}`}
                  onClick={() => void unmute(candidate)}
                >
                  Unmute
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {error !== null ? (
        <p role="alert" className="dloop-error">
          {error}
        </p>
      ) : null}
    </DecisionRow>
  );
}
