/**
 * useAnswerHold: for a moment after an inline answer's buttons appear or move, a pointer press on
 * them is swallowed.
 *
 * An inline answer is sent the moment it is clicked, so the click must reach the request the user
 * aimed at. Whatever changes the layout around the buttons (a request answered or arriving, a
 * proposal row or the notice footer leaving a scrolled list, a line wrapping, the slot above
 * growing, a scroll) can put another request's Allow once where the pointer already rests, and the
 * second click of a double-click, or a click already on its way, lands there. The causes are not
 * listed anywhere: the hook watches the box of the buttons themselves, so it lives with them
 * (RequestActions) and holds wherever they are mounted.
 *
 * The box is looked at after every render of its owner, on a scroll of anything it sits in, on a
 * window resize, and again when a press arrives, which catches a move nothing else reported. The
 * hold ends only once the box has stayed put for the whole wait. A keyboard activation is never
 * held: focus stays with its button wherever the button goes.
 */
import {
  type MouseEvent,
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

/** How long the buttons ignore the pointer after they last appeared or moved: long enough to
 * cover the second click of a double-click, the delay browsers put on their own permission
 * prompts. */
export const ANSWER_SETTLE_MS = 500;

export interface AnswerHold {
  /** For the element that wraps the buttons: the box that is watched. */
  ref: RefObject<HTMLDivElement | null>;
  /** True while the pointer is held off, so the buttons can show it. */
  settling: boolean;
  /** For that element's `onMouseDownCapture` and `onClickCapture`. The press is stopped as well as
   * the click, so a held press does not leave focus on a button the user did not aim at. */
  onPointer: (event: MouseEvent) => void;
}

/** `identity` names the request the buttons answer: buttons that stay in place but come to
 * answer another request have changed under the pointer just the same. */
export function useAnswerHold(identity: string): AnswerHold {
  const ref = useRef<HTMLDivElement>(null);
  // Which request the buttons answered, and where they were, when last looked at. Empty until
  // the first look.
  const seen = useRef("");
  // Read by the press handler: a ref is current at once, where state waits for a render. Buttons
  // that have just mounted start held.
  const held = useRef(true);
  // The press now in progress began while held, so the click that completes it is held too,
  // however long the button stays down.
  const pressHeld = useRef(false);
  const [settling, setSettling] = useState(true);
  // Counts the holds; each new one restarts the wait below.
  const [holds, setHolds] = useState(0);

  const moved = useCallback((): boolean => {
    const box = ref.current?.getBoundingClientRect();
    const now = box ? `${identity}@${Math.round(box.left)},${Math.round(box.top)}` : "";
    if (now === seen.current) return false;
    seen.current = now;
    return true;
  }, [identity]);

  const hold = useCallback((): void => {
    held.current = true;
    setSettling(true);
    setHolds((count) => count + 1);
  }, []);

  // No dependency list: a render that moved the buttons holds them in that same commit, before
  // the next input event can arrive.
  useLayoutEffect(() => {
    const first = seen.current === "";
    // The first look only records where the buttons are: the hold they mount with is running.
    if (moved() && !first) hold();
  });

  useEffect(() => {
    const onShift = (event: Event): void => {
      const from = event.target;
      // A scroller the buttons are not in cannot have moved them; a resize names the window.
      if (from instanceof Node && (!ref.current || !from.contains(ref.current))) return;
      if (moved()) hold();
    };
    // Scroll events do not bubble; capturing at the document sees every scroller's.
    document.addEventListener("scroll", onShift, true);
    window.addEventListener("resize", onShift);
    return () => {
      document.removeEventListener("scroll", onShift, true);
      window.removeEventListener("resize", onShift);
    };
  }, [moved, hold]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `holds` is what restarts the wait
  useEffect(() => {
    // The render this release causes looks at the box again (the effect above), so buttons that
    // are still moving then, a drawer sliding in, go straight back into a hold.
    const id = setTimeout(() => {
      held.current = false;
      setSettling(false);
    }, ANSWER_SETTLE_MS);
    return () => clearTimeout(id);
  }, [holds]);

  const onPointer = useCallback(
    (event: MouseEvent): void => {
      // `detail` counts the presses of a pointer and is 0 for a keyboard activation.
      if (event.detail === 0) return;
      if (moved()) hold();
      const press = event.type === "mousedown";
      const stop = held.current || (!press && pressHeld.current);
      pressHeld.current = press && stop;
      if (!stop) return;
      event.preventDefault();
      event.stopPropagation();
    },
    [moved, hold],
  );

  return { ref, settling, onPointer };
}
