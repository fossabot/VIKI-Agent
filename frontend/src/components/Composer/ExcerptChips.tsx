/** The vault excerpts waiting to go with the next message. Each is checked against the note as it
 * is now, one request at a time (the excerpt route admits two at once, so a parallel burst of
 * chips would refuse each other with 429). Nothing is refreshed unless the user clicks Refresh: a
 * silent refresh would change what is about to be sent. Every server string is a text node. */
import { type ReactElement, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { ExcerptApi, VaultExcerpt, VaultExcerptPart } from "../../state/excerptApi";
import "./ExcerptChips.css";

type ChipState = "checking" | "fresh" | "stale" | "missing" | "unknown";
interface Check {
  state: Exclude<ChipState, "checking">;
  result?: VaultExcerpt;
}

export interface ExcerptChipsProps {
  parts: readonly VaultExcerptPart[];
  api: ExcerptApi;
  workspaceId: string;
  onRemove: (index: number) => void;
  /** The user clicked Refresh: `fresh` is the part the route returned for the same lines. */
  onRefresh: (index: number, fresh: VaultExcerptPart) => void;
  /** Reports whether every chip is `fresh`, before the browser paints a change. */
  onFreshChange?: (allFresh: boolean) => void;
}

const MISSING_CODES = new Set(["not_found", "forbidden", "invalid_range"]);

/** How many excerpts one turn carries: the service's `turn_parts.EXCERPTS_PER_TURN`, which
 * `durableApi`'s own `MAX_PARTS` reads a saved draft against. */
export const EXCERPTS_PER_TURN = 8;

/** The characters all the blocks of one turn carry together: the service's
 * `turn_parts.EXCERPT_BLOCK_CHARS`, which `vault_excerpts.check_block_budget` refuses a turn over
 * at dispatch. Every chip's own size below is read off the rendered block the route returned, so
 * the sum here is in the budget's own unit — characters, not the UTF-8 bytes of `block_bytes`. */
export const EXCERPT_BLOCK_CHARS = 12_000;

const BUDGET = EXCERPT_BLOCK_CHARS.toLocaleString("en-US");

const partKey = (part: VaultExcerptPart): string =>
  `${part.vault_id}\0${part.ref}\0${part.start_line}\0${part.end_line}\0${part.sha256}`;

const sameSpan = (a: VaultExcerptPart, b: VaultExcerptPart): boolean =>
  a.ref === b.ref && a.start_line === b.start_line && a.end_line === b.end_line;

/** What the next turn holds once a part is attached. The service refuses both a ninth part and a
 * second part over the same lines (`TurnInput.__post_init__`), and a refused save leaves the user
 * a retry that can only fail again, so an attach either rule would reject changes nothing here.
 * The third rule, `EXCERPT_BLOCK_CHARS`, is held by the chips instead: a part carries no block
 * size, so the total is known only once each chip has read its note. */
export function withExcerpt(
  parts: readonly VaultExcerptPart[],
  part: VaultExcerptPart,
): readonly VaultExcerptPart[] {
  if (parts.length >= EXCERPTS_PER_TURN) return parts;
  if (parts.some((held) => sameSpan(held, part))) return parts;
  return [...parts, part];
}

function failure(error: unknown): Check {
  return {
    state: error instanceof Error && MISSING_CODES.has(error.message) ? "missing" : "unknown",
  };
}

function compare(part: VaultExcerptPart, result: VaultExcerpt): Check {
  return { state: result.part.sha256 === part.sha256 ? "fresh" : "stale", result };
}

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

function relativeModified(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((then - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.trunc(seconds / size), unit);
  }
  return rtf.format(seconds, "second");
}

export function ExcerptChips({
  parts,
  api,
  workspaceId,
  onRemove,
  onRefresh,
  onFreshChange,
}: ExcerptChipsProps): ReactElement {
  const [checks, setChecks] = useState<Record<string, Check>>({});
  const [refreshing, setRefreshing] = useState(false);
  const refreshRef = useRef<{
    part: VaultExcerptPart;
    api: ExcerptApi;
    workspaceId: string;
    controller: AbortController;
  } | null>(null);
  const labelId = useId();

  useLayoutEffect(
    () => () => {
      refreshRef.current?.controller.abort();
      refreshRef.current = null;
    },
    [],
  );

  // A removed or replaced part no longer owns the pending refresh.
  useLayoutEffect(() => {
    const pending = refreshRef.current;
    if (
      pending &&
      (!parts.includes(pending.part) || pending.api !== api || pending.workspaceId !== workspaceId)
    ) {
      pending.controller.abort();
      refreshRef.current = null;
      setRefreshing(false);
    }
  }, [parts, api, workspaceId]);

  // The first chip without an answer is the only one in flight: its answer is what moves the
  // next one up, so two requests can never overlap.
  const next = parts.find((part) => checks[partKey(part)] === undefined);
  const nextKey = next ? partKey(next) : null;
  // biome-ignore lint/correctness/useExhaustiveDependencies: `next` is named by `nextKey`; a rerun with the same key must not issue a second request.
  useEffect(() => {
    if (!next || nextKey === null) return;
    const controller = new AbortController();
    api
      .excerpt(workspaceId, next.ref, next.start_line, next.end_line, controller.signal)
      .then(
        (result) => compare(next, result),
        (error: unknown) => failure(error),
      )
      .then((check) => {
        if (controller.signal.aborted) return;
        setChecks((previous) => ({ ...previous, [nextKey]: check }));
      });
    return () => controller.abort();
  }, [api, workspaceId, nextKey]);

  // A part that leaves and comes back is checked again: an old answer is not evidence about the
  // note as it is now.
  useEffect(() => {
    const live = new Set(parts.map(partKey));
    setChecks((previous) => {
      const kept = Object.entries(previous).filter(([key]) => live.has(key));
      return kept.length === Object.keys(previous).length ? previous : Object.fromEntries(kept);
    });
  }, [parts]);

  const states = parts.map((part): ChipState => checks[partKey(part)]?.state ?? "checking");
  const allFresh = states.every((state) => state === "fresh");
  // Each answered chip's own block, in characters. A partial sum only grows, so it is already
  // decisive about the budget below.
  const total = parts.reduce(
    (sum, part) => sum + (checks[partKey(part)]?.result?.block.length ?? 0),
    0,
  );
  const overBudget = total > EXCERPT_BLOCK_CHARS;
  // The dispatch refuses `excerpts_too_large` over the same budget, after accepting the send and
  // clearing the draft: holding Send here keeps that turn from being written at all.
  const sendable = allFresh && !overBudget;
  useLayoutEffect(() => {
    onFreshChange?.(sendable);
  }, [sendable, onFreshChange]);

  const busy = refreshing || states.includes("checking");

  async function refresh(index: number, part: VaultExcerptPart): Promise<void> {
    const pending = { part, api, workspaceId, controller: new AbortController() };
    refreshRef.current = pending;
    setRefreshing(true);
    try {
      const result = await api.excerpt(
        workspaceId,
        part.ref,
        part.start_line,
        part.end_line,
        pending.controller.signal,
      );
      if (refreshRef.current !== pending) return;
      // The new part is already known fresh: seeding it saves the check a second request.
      setChecks((previous) => ({
        ...previous,
        [partKey(result.part)]: { state: "fresh", result },
      }));
      onRefresh(index, result.part);
    } catch (error) {
      if (refreshRef.current === pending) {
        setChecks((previous) => ({ ...previous, [partKey(part)]: failure(error) }));
      }
    } finally {
      if (refreshRef.current === pending) {
        refreshRef.current = null;
        setRefreshing(false);
      }
    }
  }

  return (
    <section className="excerpt-chips jp" aria-label="Excerpts to send">
      <ul className="excerpt-chip-list">
        {parts.map((part, index) => {
          const check = checks[partKey(part)];
          const state = states[index];
          const id = `${labelId}-${index}`;
          return (
            <li
              // biome-ignore lint/suspicious/noArrayIndexKey: two chips can hold the same lines; position is their identity.
              key={index}
              className="excerpt-chip"
              data-state={state}
            >
              <span className="excerpt-chip-label" id={id}>
                {`${part.ref} (lines ${part.start_line}–${part.end_line})`}
              </span>
              {state === "stale" && (
                <button
                  type="button"
                  className="excerpt-chip-action"
                  aria-describedby={id}
                  disabled={busy}
                  onClick={() => void refresh(index, part)}
                >
                  Refresh
                </button>
              )}
              <button
                type="button"
                className="excerpt-chip-action"
                aria-describedby={id}
                onClick={() => onRemove(index)}
              >
                Remove
              </button>
              <span className="excerpt-chip-meta">
                {state === "checking" && "Checking…"}
                {state === "fresh" &&
                  check?.result &&
                  `${check.result.block.length.toLocaleString("en-US")} characters · To be sent to Claude`}
                {state === "stale" &&
                  "The note has changed. Refresh or remove this excerpt before sending."}
                {state === "missing" && "This note cannot be read. Remove the excerpt."}
                {state === "unknown" && "Could not check this excerpt."}
              </span>
              {state === "fresh" && check?.result ? (
                <details className="excerpt-chip-details">
                  <summary
                    aria-label={`Excerpt details for ${part.ref}, lines ${part.start_line}–${part.end_line}`}
                  >
                    Details
                  </summary>
                  <dl className="excerpt-chip-diagnostics">
                    <div>
                      <dt>Modified</dt>
                      <dd>{relativeModified(check.result.modified_at)}</dd>
                    </div>
                    <div>
                      <dt>SHA-256</dt>
                      <dd>
                        <code>{part.sha256}</code>
                      </dd>
                    </div>
                  </dl>
                </details>
              ) : null}
            </li>
          );
        })}
      </ul>
      {parts.length >= EXCERPTS_PER_TURN ? (
        <div className="excerpt-chips-footer">
          {`Each message can include at most ${EXCERPTS_PER_TURN} excerpts. Remove one to add another.`}
        </div>
      ) : null}
      {overBudget ? (
        <div className="excerpt-chips-footer">
          {`Excerpts exceed the per-message limit of ${BUDGET} characters. Remove an excerpt before sending.`}
        </div>
      ) : null}
      <div className="excerpt-chips-footer">{`App-added content: ${allFresh ? total.toLocaleString("en-US") : "—"} characters in total (limit: ${BUDGET} characters)`}</div>
      <div className="excerpt-chips-footer">
        Content Claude Code loads automatically, such as CLAUDE.md, is not shown here.
      </div>
    </section>
  );
}
