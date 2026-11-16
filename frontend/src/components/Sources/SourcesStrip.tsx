/** Shows sources used by a finished reply and offers a draft hand-off for saving it. The caller determines where the draft opens and supplies the current sources. */
import { type ReactElement, useEffect, useId, useMemo, useRef, useState } from "react";
import { turnSources } from "../../state/sources";
import type { Turn } from "../../state/view";
import { SourceChip } from "./SourceChip";
import { replyWords } from "./saveAsNote";
import { useSourcesEnv } from "./sourcesEnv";
import "./Sources.css";

export interface SourcesStripProps {
  turn: Turn;
  sessionId: string;
}

/** Chips shown before the rest fold into "+N more". */
const MAX_CHIPS = 8;
const NO_ROOM_MESSAGE = "No room for another draft in this window. Close a draft, then try again.";
const UNAVAILABLE_MESSAGE = "Couldn't prepare a note from this reply.";

export function SourcesStrip({ turn, sessionId }: SourcesStripProps): ReactElement | null {
  const env = useSourcesEnv();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const sourcesId = useId();
  const mounted = useRef(true);
  const vault = env ? env.inVault(sessionId) : false;
  const sources = useMemo(() => turnSources(turn, vault), [turn, vault]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: a different reply resets this local disclosure, even though the effect does not read its identity.
  useEffect(() => {
    setExpanded(false);
  }, [sessionId, turn.id]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  if (!env || turn.status === "running" || turn.blocks.length === 0) return null;

  // Arrow expression, not a function declaration: the latter is hoisted and so, as far as the
  // type checker is concerned, callable above the `if (!env)` guard, which would widen `env` back
  // to nullable inside it (same reasoning as SourceChip.tsx's own handlers).
  const handleSave = async (): Promise<void> => {
    if (busy || !env.saveAsNote) return;
    setBusy(true);
    setMessage(null);
    try {
      const outcome = await env.saveAsNote(sessionId, turn);
      if (!mounted.current) return;
      if (outcome === "no_room") setMessage(NO_ROOM_MESSAGE);
      else if (outcome === "unavailable") setMessage(UNAVAILABLE_MESSAGE);
    } catch {
      if (mounted.current) setMessage(UNAVAILABLE_MESSAGE);
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const hasNote = sources.some((source) => source.kind === "note");
  const showBadge = vault && !hasNote;
  const showSave = env.saveAsNote !== undefined && replyWords(turn) !== "";
  if (sources.length === 0 && !showBadge && !showSave) return null;

  const shown = expanded ? sources : sources.slice(0, MAX_CHIPS);
  const more = sources.length - MAX_CHIPS;

  return (
    // biome-ignore lint/a11y/useSemanticElements: the source list and independent actions form a group, not a form fieldset.
    <div className="reply-sources" role="group" aria-label="Read for this reply">
      {sources.length > 0 ? (
        <>
          <div className="sources-header">
            <span className="sources-label">Read</span>
            <span className="sources-count">{`${sources.length} ${sources.length === 1 ? "source" : "sources"}`}</span>
            {more > 0 ? (
              <button
                type="button"
                className="sources-more"
                aria-label={expanded ? "Show fewer sources" : `Show all ${sources.length} sources`}
                aria-expanded={expanded}
                aria-controls={sourcesId}
                onClick={() => setExpanded((value) => !value)}
              >
                {expanded ? "Show fewer" : `+${more} more`}
              </button>
            ) : null}
          </div>
          <ul className="reply-source-list" id={sourcesId}>
            {shown.map((source) => (
              <li className="reply-source" key={JSON.stringify([env.workspaceId, source.key])}>
                <SourceChip source={source} sessionId={sessionId} />
                {source.kind === "note" ? (
                  <code className="source-strip-ref">{source.ref}</code>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {showBadge ? (
        <span className="sources-badge" title="VIKI read no vault note for this reply.">
          No vault sources
        </span>
      ) : null}
      {showSave ? (
        <button
          type="button"
          className="sources-save"
          aria-busy={busy}
          onClick={() => void handleSave()}
        >
          {busy ? "Preparing…" : "Save as note"}
        </button>
      ) : null}
      {message !== null ? (
        <span role="status" className="sources-message">
          {message}
        </span>
      ) : null}
    </div>
  );
}
