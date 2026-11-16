/**
 * NeedsYou: the right rail's single merged list of everything still waiting on the user —
 * native permission/question/plan cards and vault proposal rows — drawn from
 * `state/needsYou.ts`'s `needsYou()` merge (natives, then proposals; already ordered and
 * deduplicated there). This component only renders that merge and wires each row's click to the
 * callback App supplies; it holds no merge logic of its own.
 *
 * Hidden entirely when there is nothing to show: `count === 0`.
 *
 * A native row can carry its own actions: `actions` (the caller's own "Allow once"/"Deny" for a
 * `kind: "tool"` permission, via RequestActions — App gives one to every such card whose chat is
 * not on screen, a main-thread card included) and, for a row naming a specific run (`blockId`
 * and `agent` set) once `stops` is given, its own `RunStop`. `stops` is the caller's own
 * `useRunStops` (App builds one over all groups' running keys and shares it with AgentRuns too),
 * so a run Stopped from here keeps its phase if the row later lists there instead, and vice
 * versa. Either `actions` or a Stop present wraps the row's unchanged `button.need` in
 * `div.need-native` alongside it, the actions in a `div.need-answer` of their own; `waiting`
 * appends ` · <text>` to `.need-meta`. None of `actions`/`waiting`/`stops` given (or no node/text
 * for this row) leaves the row a plain `button.need`.
 *
 * An inline answer is sent the moment it is clicked, so a click must reach the request the user
 * aimed at. Any row, the footer or a wrapped line coming or going moves the buttons below it and
 * can put another request's Allow once where the pointer already rests; the buttons themselves
 * hold the pointer off for a moment after they move (useAnswerHold.ts, inside RequestActions),
 * so nothing here has to list what can move them.
 *
 * Ticks once a second, like AgentRuns' own tick, but only while a row could show a Stop
 * (`blockId` set) or `waiting` names text for one — `now` (never the possibly-stale `nowMs` prop
 * alone) reaches RunStop and both callbacks, so "waiting <n>s" and a stale "Stopping…" keep
 * advancing between App's own renders.
 *
 * A row leaving must not strand focus on the page, and must not hand it to a control that
 * answers or stops another request: a key already on its way down would act on a row the user
 * was not looking at. Every native and proposal row has a keyed wrapper whose layout-effect
 * cleanup moves focus to the next row's own `button.need`, which only opens it, then the previous
 * row's, then Open Approvals. If the last row leaves, a queued microtask preserves the existing
 * run-row or rail fallback after the whole section unmounts.
 *
 * Never renders `.card`, `.agent`, `.s`, `.s-on`, `.rt`, `.tile`, `.sess` or `.hit` — those belong
 * to Cards.tsx, AgentsSection.tsx, AgentRuns.tsx and VaultTiles.tsx's own frozen contracts: this
 * is still a summary row that routes to the real decision, except where a row decides in place —
 * an off-screen tool card's own inline Allow once / Deny.
 */
import {
  type ReactElement,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import type { NativeDecision } from "../../state/decisions";
import type { NeedRow, NeedsYou as NeedsYouData, ProposalNeed } from "../../state/needsYou";
import { runKey } from "../../state/runGroups";
import { visibleLine } from "../Cards/visibleText";
import { QUESTION_STATUS_LABELS } from "../Decisions/approvalKinds";
import { Icon } from "../Icon/Icon";
import { RunStop, type RunStops } from "./RunStop";
import "./NeedsYou.css";

type NativeRow = Extract<NeedRow, { kind: "native" }>;
type ProposalRow = Extract<NeedRow, { kind: "proposal" }>;
type NativeKind = "tool" | "plan" | "question";

function nativeKind(item: NativeDecision): NativeKind {
  if (item.kind === "question") return "question";
  return item.inline ? "tool" : "plan";
}

/** One clipped line naming the request, in the request's own words: hidden characters are written
 * as their codes and line breaks drawn (visibleText.ts), so the line reads as what was sent. */
function nativeTitle(item: NativeDecision): string {
  const kind = nativeKind(item);
  if (kind === "tool") return visibleLine(`${item.action} ${item.resource}`);
  if (kind === "plan") return `Plan · ${visibleLine(item.resource)}`;
  return visibleLine(item.resource);
}

function nativeStateWord(kind: NativeKind): string {
  if (kind === "tool") return "waiting for approval";
  if (kind === "plan") return "plan to approve";
  return "question";
}

/** `<ref|relative_id|"Decision">`: the field that names where a proposal came from differs by
 * kind; a question proposal always comes from Decision itself. */
function sourceOf(item: ProposalNeed): string {
  if (item.kind === "skill_proposal") return item.relative_id;
  if (item.kind === "question_proposal") return "Decision";
  return item.ref;
}

/** The chip word after the `·`. A question proposal shares Approvals' own
 * `QUESTION_STATUS_LABELS` (its "open" reads "Being tried", the same word DecisionSection shows,
 * not the generic "Proposed" the i18n chip catalog gives every other kind's "open"); a change
 * proposal always reads "Vault change" here regardless of state (Approvals' own
 * `CHANGE_STATUS_LABELS` carries the state word there instead); a vault or skill proposal keeps
 * that unchanged i18n chip word. */
function chipWord(item: ProposalNeed): string {
  if (item.kind === "question_proposal") return QUESTION_STATUS_LABELS[item.state];
  if (item.kind === "change_proposal") return "Vault change";
  return t(`decisions.chip.${item.state}` as Key);
}

const FOCUSABLE = "button, a[href], input, select, textarea, [tabindex]";

/** `el` itself when it is focusable, else the first focusable element inside it; `null` for
 * neither (no such sibling, or one with nothing focusable in it). */
function firstFocusable(el: Element | null): HTMLElement | null {
  if (el === null) return null;
  return el.matches(FOCUSABLE) ? (el as HTMLElement) : el.querySelector<HTMLElement>(FOCUSABLE);
}

/** Called while a keyed row is attached. Looks for the nearest other row in row order across
 * both groups, the rows after this one first, and focuses the button that opens it: never its
 * Allow once, Deny or Stop, which act at one key press (see the module doc). */
function focusNextRow(root: HTMLElement, runKeyOf: string | null): void {
  if (!root.contains(document.activeElement)) return;
  const body = root.closest(".rail-section-body");
  const rows = [...(body?.querySelectorAll<HTMLElement>("[data-need-row]") ?? [])];
  const index = rows.indexOf(root);
  for (const row of [...rows.slice(index + 1), ...rows.slice(0, index).reverse()]) {
    const opener = row.querySelector<HTMLElement>("button.need");
    if (opener) {
      opener.focus();
      return;
    }
  }
  root.closest(".rail-section")?.querySelector<HTMLElement>(".need-open")?.focus();
  const rail = root.closest('[data-region="right-rail"]');
  queueMicrotask(() => {
    if (document.activeElement && document.activeElement !== document.body) return;
    if (runKeyOf === null) {
      firstFocusable(rail)?.focus();
      return;
    }
    document.querySelector<HTMLElement>(`[data-run-key="${runKeyOf}"] button`)?.focus();
  });
}

/** Every keyed row uses this cleanup wrapper. Native action content keeps its nested
 * `div.need-native` contract; `runKeyOf` stays current for the last-row fallback. */
function NeedRowFocus({
  children,
  runKeyOf = null,
  kind,
}: {
  children: ReactNode;
  runKeyOf?: string | null;
  kind: "native" | "proposal";
}): ReactElement {
  const rootRef = useRef<HTMLDivElement>(null);
  const keyRef = useRef(runKeyOf);
  keyRef.current = runKeyOf;
  useLayoutEffect(() => {
    const root = rootRef.current;
    return () => {
      if (root) focusNextRow(root, keyRef.current);
    };
  }, []);
  return (
    <div className="needs-you-row" data-need-row={kind} ref={rootRef}>
      {children}
    </div>
  );
}

export interface NeedsYouProps {
  needs: NeedsYouData;
  selectedId: string | null;
  labelOf: (sessionId: string) => string;
  onOpenApprovals?: () => void;
  onOpenNative: (row: NativeRow) => void;
  onOpenProposal: (item: ProposalNeed) => void;
  /** The caller's own inline answer for a row, e.g. RequestActions for a `kind: "tool"`
   * permission naming a run. `now` is this component's own ticking clock (see the module doc),
   * not necessarily the caller's own, possibly-stale one. `undefined`/no node: the row gets no
   * actions. */
  actions?(row: NativeRow, now: number): ReactNode;
  /** Appended to `.need-meta` as ` · <text>`, e.g. `waiting <runElapsed>`. Same `now` as `actions`
   * above. `undefined`/null: no suffix. */
  waiting?(row: NativeRow, now: number): string | null;
  /** Given, a row naming a run (`blockId` set) gets its own `RunStop`, sharing the caller's one
   * `useRunStops` instead of this component owning a hook of its own — so a run Stopped from this
   * row keeps its phase if it later lists in AgentRuns instead, and vice versa. Keyed the same way
   * AgentRuns' own rows are (`runKey(sessionId, blockId)`). Omitted: no Stop anywhere here. */
  stops?: RunStops | undefined;
  nowMs?: number;
  footer?: ReactNode;
}

export function NeedsYou({
  needs,
  selectedId,
  labelOf,
  onOpenApprovals,
  onOpenNative,
  onOpenProposal,
  actions,
  waiting,
  stops,
  footer,
  nowMs = Date.now(),
}: NeedsYouProps): ReactElement | null {
  // Ticks once a second, like AgentRuns' own tick, but only while a row could show its own
  // Stop (`blockId` set) or names waiting text — otherwise a stale "waiting <n>s" or a stale
  // "Stopping…" would sit until App next renders, up to the rest store's 60 s poll.
  // Called before the early return below (Rules of Hooks): every render calls it, whether or not
  // there is anything to show.
  const active = needs.rows.some(
    (row) =>
      row.kind === "native" && (row.blockId !== null || (waiting?.(row, nowMs) ?? null) !== null),
  );
  const [tick, setTick] = useState<number>(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  const now = Math.max(nowMs, tick);
  const previousCount = useRef(needs.count);
  const [pulse, setPulse] = useState(0);
  useEffect(() => {
    if (needs.count > previousCount.current) setPulse((value) => value + 1);
    previousCount.current = needs.count;
  }, [needs.count]);

  if (needs.count === 0) return null;

  function chatLabel(sessionId: string): string {
    return sessionId === selectedId ? "this chat" : labelOf(sessionId);
  }

  function nativeMeta(row: NativeRow): string {
    const base = `${chatLabel(row.item.origin.session_id)} · ${nativeStateWord(nativeKind(row.item))}`;
    const named = row.agent ? `${base} · ${row.agent}` : base;
    const extra = waiting?.(row, now);
    return extra ? `${named} · ${extra}` : named;
  }

  function stopFor(row: NativeRow): ReactElement | null {
    if (!stops || row.blockId === null || row.agent === null) return null;
    const sessionId = row.item.origin.session_id;
    const blockId = row.blockId;
    const key = runKey(sessionId, blockId);
    return (
      <RunStop
        agent={row.agent}
        chat={chatLabel(sessionId)}
        phase={stops.phase(key)}
        nowMs={now}
        onAsk={() => stops.ask(key)}
        onCancel={() => stops.cancel(key)}
        onSend={() => stops.send(key, sessionId, blockId)}
      />
    );
  }

  const natives = needs.rows.filter((row): row is NativeRow => row.kind === "native");
  const proposals = needs.rows.filter((row): row is ProposalRow => row.kind === "proposal");
  const urgent = natives.length > 0;

  return (
    <section
      className="rail-section needs-you"
      aria-label={tf("shell.needs_you.region" as Key, { n: needs.count })}
    >
      <div
        className={`needs-you-band ${urgent ? "needs-you-band--urgent" : "needs-you-band--subdued"}`}
      >
        {pulse > 0 ? <span className="needs-you-pulse" key={pulse} aria-hidden="true" /> : null}
        <div className="rail-section-head">
          <Icon name="shield" />
          <span className="lbl">Needs you</span>
          <span
            className={`rail-section-count ${urgent ? "attention-count" : "rail-section-count--dim"}`}
          >
            {needs.count}
          </span>
          {onOpenApprovals ? (
            <button type="button" className="need-open" onClick={onOpenApprovals}>
              Open Approvals <span aria-hidden="true">›</span>
            </button>
          ) : null}
        </div>
      </div>
      <div className="rail-section-body">
        {natives.length > 0 ? (
          <div className="needs-you-native-group" data-need-group="native">
            {natives.map((row) => {
              const button = (
                <button
                  type="button"
                  className="need"
                  data-need="native"
                  onClick={() => onOpenNative(row)}
                >
                  <span className="need-title">{nativeTitle(row.item)}</span>
                  <span className="need-meta">{nativeMeta(row)}</span>
                </button>
              );
              const actionsNode = actions?.(row, now);
              const stopNode = stopFor(row);
              return (
                <NeedRowFocus
                  key={row.key}
                  kind="native"
                  runKeyOf={
                    row.blockId === null ? null : runKey(row.item.origin.session_id, row.blockId)
                  }
                >
                  {actionsNode || stopNode ? (
                    <div className="need-native">
                      {button}
                      {actionsNode ? <div className="need-answer">{actionsNode}</div> : null}
                      {stopNode}
                    </div>
                  ) : (
                    button
                  )}
                </NeedRowFocus>
              );
            })}
          </div>
        ) : null}
        {proposals.length > 0 ? (
          <div className="needs-you-proposal-group" data-need-group="proposal">
            <span className="lbl">{tf("shell.needs_you.proposals" as Key, {})}</span>
            {proposals.map((row) => (
              <NeedRowFocus key={row.key} kind="proposal">
                <button
                  type="button"
                  className="need"
                  data-need="proposal"
                  onClick={() => onOpenProposal(row.item)}
                >
                  <span className="need-title">{row.item.title}</span>
                  <span className="need-meta">{`${sourceOf(row.item)} · ${chipWord(row.item)}`}</span>
                </button>
              </NeedRowFocus>
            ))}
          </div>
        ) : null}
        {footer ? <div className="needs-you-footer">{footer}</div> : null}
      </div>
    </section>
  );
}
