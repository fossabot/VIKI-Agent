/**
 * DecisionTab: the Decision tab inside Context &
 * loadout (ContextView's `decision` slot). A picker over the last 20 calls to the Decision model
 * (`api.calls()`), then the exact state sent to whichever one is selected (`api.call(id)`) — the
 * two ContextBar bars, the version/latency/cost facts, the token split, the questions and
 * their answers (or, for a failed call, the plain failure in words), the playbook version, and
 * "What was sent" itself with a Copy button.
 *
 * `enabled` gates everything: while off, this makes no request at all, matching the
 * Decision page's own switch one level up — the
 * caller already knows that flag from the Page view, so it is a prop rather than re-read from
 * `api.calls()`'s own (redundant) `enabled` field.
 *
 * Both fetches follow ContextView.tsx's own idiom one file up: an AbortController per request,
 * aborted on unmount or on the next request, and a `signal.aborted` guard on both the resolve and
 * the reject branch so a stale answer (the user picked a different call before the first one
 * answered) is dropped rather than painted.
 *
 * `requestedCall` (the launcher's "See why," which names the call its answers came from) is
 * applied once the calls list carries it — on this load, or a later one while already loaded —
 * else the newest call keeps being the default, exactly as when the prop is absent (the Decision
 * page's own "See the last state sent"). Its `request` counter, not `callId` alone, drives the
 * second effect below, so a second request for the same call still re-selects it even if the
 * user moved the picker since. The first load's own pick happens inline in the fetch below
 * (through a ref, so that effect need not depend on `requestedCall`), in the same commit that
 * reveals the picker, so it never flashes the newest call first and then corrects to the
 * requested one a render later.
 */
import { Fragment, type ReactElement, type ReactNode, useEffect, useRef, useState } from "react";
import type {
  CallDetailView,
  CallSummary,
  CallsView,
  DecisionLoopApi,
  ModelErrorCode,
  VersionSetting,
} from "../../clients/decisionLoop/types";
import { CopyButton } from "../Conversation/Copy";
import { Loading } from "../Loading/Loading";
import { ContextBar } from "./ContextBar";
import "./DecisionTab.css";

/** Only the two routes this tab reads — the same narrowing `usePresence.ts` uses for its one
 * route, so this component cannot be handed, or accidentally call, anything else. */
export type DecisionTabApi = Pick<DecisionLoopApi, "calls" | "call">;

/** A specific call to open (the launcher's "See why"), applied by `DecisionTab` once the calls list
 * carries it. `request` changes on every ask, including a repeat naming the same `callId`, so a
 * second "See why" on the same item still re-selects it even after the user moved the picker —
 * the same `{ id, request }` idiom Conversation.tsx's own `jumpTo` prop uses. */
export interface RequestedCall {
  callId: string | null;
  request: number;
}

export interface DecisionTabProps {
  api: DecisionTabApi;
  /** The chat open in this window now, or null. A listed call whose own `session_id` matches
   * this exactly is marked "this chat"; two nulls are never a match. */
  openChatId: string | null;
  /** Mirrors the Decision page's own "Proactive VIKI" switch. */
  enabled: boolean;
  /** The launcher's "See why", naming the call its answers came from. Absent (the default)
   * the newest call is selected once the list loads. */
  requestedCall?: RequestedCall;
}

const VERSION_SETTING_WORDS: Readonly<Record<VersionSetting, string>> = {
  pinned: "pinned",
  latest: "latest stable",
  preview: "preview",
};

/** The plain words shown instead of answers when a call's `outcome` is "failed". */
const FAILURE_WORDS: Readonly<Record<ModelErrorCode, string>> = {
  no_key: "No key was saved.",
  key_rejected: "The Decision model rejected the key.",
  refused: "The Decision model refused the request.",
  too_large: "The request was too large for the Decision model.",
  rate_limited: "The Decision model asked VIKI to slow down.",
  overloaded: "The Decision model is busy. Try again later.",
  unreachable: "The Decision model could not be reached.",
  invalid_reply: "The Decision model's reply could not be read.",
  failed: "The call failed.",
};

function formatTokens(n: number): string {
  return n.toLocaleString("en-US");
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Local "HH:MM" for the tab's stamps. */
function localClock(stamp: string): string {
  const at = new Date(stamp);
  return `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
}

function sharePercent(used: number, max: number): number {
  return max > 0 ? Math.round((used / max) * 100) : 0;
}

/** "{label}: {used} of {max} tokens ({pct}%)", with an optional literal suffix — the text both
 * drawn on screen and handed to ContextBar as its one accessible name. */
function tokenBarText(label: string, used: number, max: number, suffix = ""): string {
  return `${label}: ${formatTokens(used)} of ${formatTokens(max)} tokens (${sharePercent(used, max)}%)${suffix}`;
}

/** Up to 5 decimals, trailing zeros (and a bare trailing point) trimmed off; a positive amount
 * that would round away to nothing at that precision reads "< $0.00001" rather than "$0". */
function formatCost(cost: number): string {
  if (cost > 0 && cost < 0.00001) return "< $0.00001";
  const fixed = cost.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
  return `$${fixed}`;
}

/** The picker's one option text: "14:02 · Water the seedlings · 3,912 tokens", plus " · this
 * chat" when this call ran for the chat open right now. */
function pickerOptionText(call: CallSummary, openChatId: string | null): string {
  const thisChat = openChatId !== null && call.session_id === openChatId;
  const base = `${localClock(call.at)} · ${call.title} · ${formatTokens(call.tokens)} tokens`;
  return thisChat ? `${base} · this chat` : base;
}

/** The call to select from a freshly loaded or already-loaded list: `requested`'s `callId` when
 * it names one of `calls`, else the newest (`calls[0]`, the default) — the same rule
 * whether `requested` is absent, names a call since dropped from the last 20, or never named one
 * at all (`callId: null`, the Decision page's own "See the last state sent"). */
function pickSelection(
  calls: readonly CallSummary[],
  requested: RequestedCall | undefined,
): string | null {
  if (requested !== undefined && requested.callId !== null) {
    if (calls.some((call) => call.call_id === requested.callId)) return requested.callId;
  }
  return calls[0]?.call_id ?? null;
}

function failureText(detail: CallDetailView): string {
  return detail.failure !== null ? FAILURE_WORDS[detail.failure] : FAILURE_WORDS.failed;
}

/** The generic "a fetch failed" shell: ContextView.tsx's own "Could not read the context." /
 * Try again shape, reused here for both this tab's fetches. */
function FailureNotice({ text, onRetry }: { text: string; onRetry: () => void }): ReactElement {
  return (
    <>
      <p>{text}</p>
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </>
  );
}

function DetailBody({ detail }: { detail: CallDetailView }): ReactElement {
  const requestText = tokenBarText(
    "Request",
    detail.request.used,
    detail.request.max,
    " · measured",
  );
  const questionBarText = tokenBarText(
    "Largest question",
    detail.largest_question.used,
    detail.largest_question.max,
  );
  const facts =
    `Version ${detail.version}, ${VERSION_SETTING_WORDS[detail.version_setting]} · ` +
    `answered in ${formatTokens(detail.latency_ms)} ms · ${formatCost(detail.cost_usd)} · ` +
    `Long context: ${detail.long_context ? "on" : "off"}`;

  return (
    <>
      <ContextBar
        size="large"
        used={[{ label: "Request", tokens: detail.request.used }]}
        max={detail.request.max}
        label={requestText}
      />
      <p>{requestText}</p>
      <ContextBar
        size="large"
        used={[{ label: "Largest question", tokens: detail.largest_question.used }]}
        max={detail.largest_question.max}
        label={questionBarText}
      />
      <p>{questionBarText}</p>
      <p>{facts}</p>
      <p className="lbl">split estimated</p>
      <ul className="context-decision-split">
        {detail.split.map((part) => (
          <li key={part.part}>
            {part.part} · {formatTokens(part.tokens)} tokens
          </li>
        ))}
      </ul>
      {detail.outcome === "failed" ? (
        <p>{failureText(detail)}</p>
      ) : (
        <dl className="context-decision-questions">
          {detail.questions.map((question) => (
            <Fragment key={question.name}>
              <dt>{question.label}</dt>
              <dd>{question.answer}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      <p>Question playbook {detail.playbook_version}</p>
      <h3>What was sent</h3>
      <pre className="mono context-decision-pre">{detail.state_text}</pre>
      <CopyButton getText={() => detail.state_text} label="Copy what was sent" />
      {detail.held_back > 0 ? (
        <p>{`${detail.held_back} items held back by your never-send list`}</p>
      ) : null}
    </>
  );
}

type CallsLoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; view: CallsView };

type DetailLoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; detail: CallDetailView };

export function DecisionTab({
  api,
  openChatId,
  enabled,
  requestedCall,
}: DecisionTabProps): ReactElement {
  const [callsState, setCallsState] = useState<CallsLoadState>({ status: "loading" });
  const [callsRetry, setCallsRetry] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailLoadState | null>(null);
  const [detailRetry, setDetailRetry] = useState(0);
  // Read fresh inside the fetch below (never a dependency of it: a later request naming a
  // different call must not force a brand-new fetch of a list already loaded).
  const requestedCallRef = useRef(requestedCall);
  requestedCallRef.current = requestedCall;

  // biome-ignore lint/correctness/useExhaustiveDependencies(callsRetry): the Try again button's reload trigger, not read in the body.
  useEffect(() => {
    if (!enabled) return undefined;
    setCallsState({ status: "loading" });
    const controller = new AbortController();
    api.calls(controller.signal).then(
      (view) => {
        if (controller.signal.aborted) return;
        setCallsState({ status: "ready", view });
        // Picked in the same commit the list itself arrives in, so the picker never shows the
        // newest call for a render and only then corrects to a requested one.
        setSelectedId(pickSelection(view.calls, requestedCallRef.current));
      },
      () => {
        if (controller.signal.aborted) return;
        setCallsState({ status: "error" });
      },
    );
    return () => controller.abort();
  }, [api, enabled, callsRetry]);

  // A fresh request named a call while the list was already loaded (a second "See why" without
  // leaving this tab): re-picks against that list. A load's own pick happens above, inline with
  // the list itself, so this effect only ever has to handle the "already loaded" half.
  // biome-ignore lint/correctness/useExhaustiveDependencies: must fire only when requestedCall itself changes; callsState is read fresh through the closure below, not listed as a trigger.
  useEffect(() => {
    if (requestedCall === undefined || callsState.status !== "ready") return;
    setSelectedId(pickSelection(callsState.view.calls, requestedCall));
  }, [requestedCall]);

  // biome-ignore lint/correctness/useExhaustiveDependencies(detailRetry): the Try again button's reload trigger, not read in the body.
  useEffect(() => {
    if (selectedId === null) {
      setDetailState(null);
      return undefined;
    }
    setDetailState({ status: "loading" });
    const controller = new AbortController();
    api.call(selectedId, controller.signal).then(
      (detail) => {
        if (controller.signal.aborted) return;
        setDetailState({ status: "ready", detail });
      },
      () => {
        if (controller.signal.aborted) return;
        setDetailState({ status: "error" });
      },
    );
    return () => controller.abort();
  }, [api, selectedId, detailRetry]);

  let body: ReactNode;
  if (!enabled) {
    body = <p>Decision is off</p>;
  } else if (callsState.status === "loading") {
    body = <Loading>Loading your decisions…</Loading>;
  } else if (callsState.status === "error") {
    body = (
      <FailureNotice
        text="Could not load your decisions."
        onRetry={() => setCallsRetry((value) => value + 1)}
      />
    );
  } else if (callsState.view.calls.length === 0) {
    body = (
      <p>{callsState.view.latest === "rules" ? "Rules only: no model call" : "No decisions yet"}</p>
    );
  } else {
    const { calls } = callsState.view;
    body = (
      <>
        <label className="context-decision-field">
          <span className="lbl">Call</span>
          <select value={selectedId ?? ""} onChange={(event) => setSelectedId(event.target.value)}>
            {calls.map((call) => (
              <option key={call.call_id} value={call.call_id}>
                {pickerOptionText(call, openChatId)}
              </option>
            ))}
          </select>
        </label>
        {detailState === null || detailState.status === "loading" ? (
          <Loading>Loading this call…</Loading>
        ) : detailState.status === "error" ? (
          <FailureNotice
            text="Could not load this call."
            onRetry={() => setDetailRetry((value) => value + 1)}
          />
        ) : (
          <DetailBody detail={detailState.detail} />
        )}
      </>
    );
  }

  return (
    <div className="context-decision">
      <h2>Decision model context window</h2>
      {body}
    </div>
  );
}
