/** Hosts help topics, search, the walkthrough and draft hand-off. Ask VIKI prepares an unsent draft from the visible help excerpt and leaves the user to review it. */
import {
  type ChangeEvent,
  type MouseEvent,
  type ReactElement,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  askText,
  HELP_GROUPS,
  HELP_TOPICS,
  loadWhatsNew as loadWhatsNewDefault,
  searchHelp,
  TOUR_REGIONS,
  tourSteps,
} from "../../help/helpTopics";
import { Markdown } from "../Conversation/Markdown";
import { useModalFocus } from "../Dialog/useModalFocus";
import {
  closeHelp,
  type HelpTarget,
  openHelp,
  useHelpHost,
  useHelpShortcut,
  useHelpTarget,
} from "./helpStore";
import { Tour, type TourStep } from "./Tour";
import "./Help.css";

export interface HelpCenterProps {
  onOpenUrl: (url: string) => void;
  /** `origin` is this dialog's own panel element, the same "pass the modal that's about to
   * close" shape as the decision inbox's `openVaultNote(ref, origin)` — the caller needs it to
   * ask `vaultNavigation.canLeave(origin)`, which otherwise refuses to leave a vault note while
   * this dialog is still open. */
  onOpenDiagnostics: (origin?: HTMLElement) => void;
  /** Places `text` as an unsent draft; true on success, false when the draft cannot be saved.
   * Omitted entirely, "Ask VIKI" does not render — asking makes no sense with nowhere
   * for the draft to land. `origin` is this dialog's own panel element; see `onOpenDiagnostics`. */
  onAsk?: (text: string, origin?: HTMLElement) => boolean;
  /** Defaults to helpTopics.ts's own `loadWhatsNew`; a test seam, not a real alternative. */
  loadWhatsNew?: () => Promise<{ default: string }>;
}

const GETTING_STARTED_BODY = HELP_TOPICS.find((t) => t.id === "getting-started")?.body ?? "";

/** `tourSteps` carries no region; zipped onto `TOUR_REGIONS` by position. `noUncheckedIndexedAccess`
 * types `TOUR_REGIONS[index]` as possibly undefined (it cannot see both arrays are fixed at 7), so
 * the key is added only once narrowed — `exactOptionalPropertyTypes` forbids assigning `region:
 * undefined` outright, and omitting the key is what an absent region is supposed to mean anyway. */
const TOUR_STEPS: TourStep[] = tourSteps(GETTING_STARTED_BODY).map((step, index) => {
  const region = TOUR_REGIONS[index];
  return region === undefined ? step : { ...step, region };
});

export function HelpCenter({
  onOpenUrl,
  onOpenDiagnostics,
  onAsk,
  loadWhatsNew,
}: HelpCenterProps): ReactElement | null {
  useHelpHost();
  useHelpShortcut();
  const target = useHelpTarget();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  // One stable ref object, so useModalFocus (below) never sees initialFocusRef change identity —
  // only swapping which DOM node it points to would make that effect tear down and refocus.
  const entryRef = useRef<HTMLElement | null>(null);
  // undefined: not fetched yet or not on this page. null: unavailable or empty. A string is ready
  // to render.
  const [whatsNewChunk, setWhatsNewChunk] = useState<string | null | undefined>(undefined);
  const [askBlocked, setAskBlocked] = useState(false);

  // Refreshed every render, before useModalFocus's own layout effect (declared right after, so it
  // runs after this one) reads entryRef.current to decide where to put focus.
  useLayoutEffect(() => {
    entryRef.current = target?.kind === "search" ? searchRef.current : closeRef.current;
  });

  // Inactive while there is nothing to trap (closed) and while Tour is what's showing — Tour
  // mounts its own useModalFocus on its own dialog, and this hook's dialogRef is unattached in
  // both cases anyway (the backdrop below only renders in the `return` past both), so this is
  // belt-and-suspenders clarity more than a behaviour change.
  useModalFocus({
    active: target !== null && target.kind !== "tour",
    dialogRef,
    initialFocusRef: entryRef,
    onDismiss: closeHelp,
  });

  useEffect(() => {
    if (target?.kind !== "whats-new") return;
    let cancelled = false;
    setWhatsNewChunk(undefined);
    const load = loadWhatsNew ?? loadWhatsNewDefault;
    load().then(
      (mod) => {
        if (!cancelled) setWhatsNewChunk(mod.default.trim() || null);
      },
      () => {
        if (!cancelled) setWhatsNewChunk(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [target, loadWhatsNew]);

  // A blocked-draft notice belongs to the attempt that produced it; moving on to any other page
  // (even a re-open of the same one — `target` is a fresh object each `openHelp` call) drops it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: target is an intentional re-run key, not read in the body.
  useEffect(() => {
    setAskBlocked(false);
  }, [target]);

  if (!target) return null;

  if (target.kind === "tour") {
    return <Tour steps={TOUR_STEPS} onClose={closeHelp} />;
  }

  function handleBackdropMouseDown(event: MouseEvent<HTMLDivElement>): void {
    if (event.target === event.currentTarget) closeHelp();
  }

  function handleAsk(title: string, excerpt: string, question: string): void {
    if (!onAsk) return;
    const ok = onAsk(askText({ title, excerpt, question }), dialogRef.current ?? undefined);
    if (ok) closeHelp();
    else setAskBlocked(true);
  }

  // Takes the narrowed target as a parameter rather than closing over the outer `target`: type
  // narrowing on a variable does not cross into a nested function's body (TS treats it as
  // callable at any time), but it does apply to the argument at this call site, right after the
  // tour check above.
  function renderPage(target: Exclude<HelpTarget, { kind: "tour" }>): ReactElement | null {
    if (target.kind === "topic") {
      const topic = HELP_TOPICS.find((t) => t.id === target.id);
      if (!topic) return null; // unreachable: target.id is a HelpTopicId, HELP_TOPICS has every one
      const hasActions = topic.action !== undefined || onAsk !== undefined;
      return (
        <>
          <Markdown text={topic.body} onOpenUrl={onOpenUrl} />
          {topic.related.length > 0 ? (
            <section className="help-related" aria-labelledby="help-related-title">
              <h3 id="help-related-title">Related topics</h3>
              {topic.related.map((id) => {
                const relatedTopic = HELP_TOPICS.find((t) => t.id === id);
                if (!relatedTopic) return null; // unreachable: helpTopics' own tests pin every related id as real
                return (
                  <button
                    key={id}
                    type="button"
                    className="help-hit"
                    onClick={() => openHelp({ kind: "topic", id })}
                  >
                    <span>{relatedTopic.title}</span>
                    <span>{relatedTopic.lead}</span>
                  </button>
                );
              })}
            </section>
          ) : null}
          {hasActions ? (
            <div className="help-actions">
              {topic.action === "diagnostics" ? (
                <button
                  type="button"
                  className="btn alt sm"
                  onClick={() => {
                    onOpenDiagnostics(dialogRef.current ?? undefined);
                    closeHelp();
                  }}
                >
                  {"Open Settings → Diagnostics"}
                </button>
              ) : null}
              {topic.action === "tour" ? (
                <button
                  type="button"
                  className="btn alt sm"
                  onClick={() => openHelp({ kind: "tour" })}
                >
                  Start the tour
                </button>
              ) : null}
              {onAsk ? (
                <button
                  type="button"
                  className="btn txt sm"
                  onClick={() => handleAsk(topic.title, topic.body, "")}
                >
                  Ask VIKI
                </button>
              ) : null}
            </div>
          ) : null}
          {askBlocked ? (
            <p className="help-status jp">
              This window has no room for another draft. Close one, then try again.
            </p>
          ) : null}
        </>
      );
    }

    if (target.kind === "search") {
      const hits = searchHelp(target.query);
      const firstHit = hits[0];
      const firstHitTopic = firstHit ? HELP_TOPICS.find((t) => t.id === firstHit.id) : undefined;
      return (
        <>
          {hits.length > 0 ? (
            hits.map((hit) => (
              <button
                key={hit.id}
                type="button"
                className="help-hit"
                onClick={() => openHelp({ kind: "topic", id: hit.id })}
              >
                <span>{hit.title}</span>
                <span>{hit.snippet}</span>
              </button>
            ))
          ) : target.query.trim() !== "" ? (
            <p className="help-status jp">
              {`No help page matches “${target.query}”. Try another word, or ask VIKI.`}
            </p>
          ) : (
            <p className="help-status jp">
              {onAsk
                ? "Type a word above to search every help page, or select Ask VIKI to start a chat draft you can finish and send."
                : "Type a word above to search every help page."}
            </p>
          )}
          {onAsk ? (
            <div className="help-actions">
              <button
                type="button"
                className="btn txt sm"
                onClick={() =>
                  handleAsk(firstHitTopic?.title ?? "", firstHitTopic?.body ?? "", target.query)
                }
              >
                Ask VIKI
              </button>
            </div>
          ) : null}
          {askBlocked ? (
            <p className="help-status jp">
              This window has no room for another draft. Close one, then try again.
            </p>
          ) : null}
        </>
      );
    }

    // target.kind === "whats-new"
    if (whatsNewChunk === undefined) return null;
    if (whatsNewChunk === null) {
      return <p className="help-status jp">What's new isn't available in this build.</p>;
    }
    return <Markdown text={whatsNewChunk} onOpenUrl={onOpenUrl} />;
  }

  return (
    // A dismiss-on-click-outside overlay, not a control — the real dialog is .help-panel below,
    // which already carries role="dialog". Static by design (SearchPalette's own backdrop is the
    // same shape).
    // biome-ignore lint/a11y/noStaticElementInteractions: see above
    <div className="help-backdrop" onMouseDown={handleBackdropMouseDown}>
      <div
        ref={dialogRef}
        className="help-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-title"
        tabIndex={-1}
      >
        <nav className="help-nav">
          <div className="help-nav-top">
            <div className="help-head">
              <h2 id="help-title" className="jp">
                Help
              </h2>
              <button
                type="button"
                className="btn txt sm"
                ref={closeRef}
                aria-label="Close help"
                onClick={closeHelp}
              >
                <span aria-hidden="true">{"✕"}</span>
              </button>
            </div>
            <input
              type="text"
              className="help-search"
              placeholder="Search help"
              aria-label="Search help"
              maxLength={100}
              ref={searchRef}
              value={target.kind === "search" ? target.query : ""}
              onChange={(event: ChangeEvent<HTMLInputElement>) =>
                openHelp({ kind: "search", query: event.target.value })
              }
            />
          </div>
          {HELP_GROUPS.map((group, index) => (
            // A labelled group of nav buttons, not form fields — fieldset/legend (biome's
            // suggestion for role="group") is for form controls; a div is right here.
            // biome-ignore lint/a11y/useSemanticElements: see above
            <div
              key={group}
              className="help-group"
              role="group"
              aria-labelledby={`help-group-title-${index}`}
            >
              <h3 id={`help-group-title-${index}`} className="help-group-title">
                {group}
              </h3>
              {HELP_TOPICS.filter((topic) => topic.group === group).map((topic) => (
                <button
                  key={topic.id}
                  type="button"
                  className="help-hit"
                  aria-current={
                    target.kind === "topic" && target.id === topic.id ? "page" : undefined
                  }
                  onClick={() => openHelp({ kind: "topic", id: topic.id })}
                >
                  {topic.title}
                </button>
              ))}
              {group === "Reference" ? (
                <button
                  type="button"
                  className="help-hit"
                  aria-current={target.kind === "whats-new" ? "page" : undefined}
                  onClick={() => openHelp({ kind: "whats-new" })}
                >
                  What's new
                </button>
              ) : null}
            </div>
          ))}
        </nav>
        <div className="help-page">{renderPage(target)}</div>
      </div>
    </div>
  );
}
