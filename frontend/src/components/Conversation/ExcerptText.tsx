/**
 * A sent turn's text with the vault excerpts the app prepended shown as citations.
 *
 * A turn with no complete excerpt block is one text segment, rendered as the bare string: the same
 * DOM text node a plain turn has always had, with no wrapper around it. An excerpt body is vault
 * text, so it is only ever a text node inside `<pre>`: no HTML, no markdown, no link built from it.
 * "Compare with the current note" reads the note again, once per click, and compares digests; it
 * writes and navigates nothing.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import { type ExcerptSegment, splitExcerpts } from "../../context/excerptBlocks";
import type { ExcerptApi } from "../../state/excerptApi";
import "./ExcerptText.css";

type Answer = "same" | "changed" | "unreadable";

const ANSWERS: Record<Answer, string> = {
  same: "Same as the current note",
  changed: "The note has changed",
  unreadable: "Can't read the note",
};

export interface ExcerptTextProps {
  text: string;
  api: ExcerptApi;
  workspaceId: string;
  /** Opens the cited note in the vault browser; the button is absent without it. */
  onOpenVault?: ((ref: string, start: number) => void) | undefined;
}

interface CitationProps {
  api: ExcerptApi;
  workspaceId: string;
  segment: Extract<ExcerptSegment, { kind: "excerpt" }>;
  onOpenVault?: ((ref: string, start: number) => void) | undefined;
}

function Citation({ api, workspaceId, segment, onOpenVault }: CitationProps): ReactElement {
  const { ref, start, end, hash12, body } = segment;
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [comparing, setComparing] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function compare(): Promise<void> {
    const mine = new AbortController();
    controller.current = mine;
    setComparing(true);
    let result: Answer;
    try {
      const note = await api.excerpt(workspaceId, ref, start, end, mine.signal);
      result = note.part.sha256.slice(0, 12) === hash12 ? "same" : "changed";
    } catch {
      result = "unreadable";
    }
    if (mine.signal.aborted) return;
    setAnswer(result);
    setComparing(false);
  }

  return (
    <details className="excerpt-cite" data-ref={ref}>
      <summary>{`Excerpt: ${ref} (lines ${start}–${end})`}</summary>
      <pre className="excerpt-body">{body}</pre>
      <div className="excerpt-actions">
        <span className="excerpt-digest mono">{`sha256 ${hash12}`}</span>
        {onOpenVault ? (
          <button type="button" onClick={() => onOpenVault(ref, start)}>
            Open note
          </button>
        ) : null}
        <button type="button" disabled={comparing} onClick={() => void compare()}>
          Compare with the current note
        </button>
      </div>
      <p className="excerpt-answer" role="status">
        {answer ? ANSWERS[answer] : ""}
      </p>
    </details>
  );
}

export function ExcerptText({
  text,
  api,
  workspaceId,
  onOpenVault,
}: ExcerptTextProps): ReactElement {
  return (
    <>
      {splitExcerpts(text).map((segment, index) =>
        segment.kind === "text" ? (
          segment.text
        ) : (
          <Citation
            // A turn's segments never reorder, so the position is the identity.
            // biome-ignore lint/suspicious/noArrayIndexKey: see above
            key={index}
            api={api}
            workspaceId={workspaceId}
            segment={segment}
            onOpenVault={onOpenVault}
          />
        ),
      )}
    </>
  );
}
