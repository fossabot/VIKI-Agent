/**
 * What every surface that takes an answer shares for text this app did not write (a command, a
 * path, a plan, a hook's reason): the text itself with its hidden characters written out and
 * marked (visibleText.ts), the line that says they were, and the line that says a scrolling box
 * holds more than it shows. The chat's cards and the right rail's inline answers draw the same
 * three, so the same request reads the same wherever it is answered.
 */
import { type ReactElement, type RefObject, useLayoutEffect, useState } from "react";
import { t } from "../../i18n/t";
import { visibleParts } from "./visibleText";
import "./DecisionText.css";

/** `text` as text nodes, each hidden character's code inside a `<mark>`: a code this app wrote
 * stands out from the same letters typed into the command, which stay unmarked. Text with nothing
 * hidden renders as the one text node it always was. */
export function DecisionText({ text }: { text: string }): ReactElement {
  return (
    <>
      {visibleParts(text).map((part, index) =>
        part.code ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: the pieces of one string have no other identity
          <mark key={index} className="hidden-code">
            {part.text}
          </mark>
        ) : (
          part.text
        ),
      )}
    </>
  );
}

/** Whether the box in `ref` holds more than it shows, kept current as its width or its content
 * changes: a scrollbar alone is too quiet a sign beside a button that answers in one click.
 * jsdom lays nothing out, so there this is false unless a test supplies the two heights. */
export function useOverflowing(ref: RefObject<HTMLElement | null>): boolean {
  const [overflowing, setOverflowing] = useState(false);
  useLayoutEffect(() => {
    const box = ref.current;
    if (!box) return;
    // One pixel of slack: both heights are rounded, each its own way.
    const measure = (): void => setOverflowing(box.scrollHeight > box.clientHeight + 1);
    measure();
    const resized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    resized?.observe(box);
    // A plan's Markdown arrives after the card has rendered, without the card rendering again.
    const changed = new MutationObserver(measure);
    changed.observe(box, { childList: true, subtree: true, characterData: true });
    return () => {
      resized?.disconnect();
      changed.disconnect();
    };
  }, [ref]);
  return overflowing;
}

export interface DecisionNotesProps {
  /** Names the lines for the control they describe (`aria-describedby`). */
  id: string;
  /** The text above had hidden characters, now written out as codes. */
  hidden: boolean;
  /** The box above scrolls: part of its text is out of view. */
  overflowing: boolean;
}

/** The lines under a request's text; nothing at all when neither applies. */
export function DecisionNotes({
  id,
  hidden,
  overflowing,
}: DecisionNotesProps): ReactElement | null {
  if (!hidden && !overflowing) return null;
  return (
    <div id={id} className="decision-notes">
      {hidden ? <p>{t("common.decision.hidden_characters")}</p> : null}
      {overflowing ? <p>{t("common.decision.out_of_view")}</p> : null}
    </div>
  );
}
