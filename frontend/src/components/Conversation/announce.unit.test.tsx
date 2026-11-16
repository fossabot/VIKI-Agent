/**
 * Unit tests for Conversation's turn-completion aria-live region, extending the frozen
 * `m4s/announce.accept.test.tsx` (which pins polite/once/silent-on-history) to the case
 * that file does not cover: a turn finishing the SAME way as the one before it, back
 * to back, with no settle in between — the ordinary case for a chatty session, and the one a
 * naive `setState` would swallow because the string does not change. See Conversation.tsx's own
 * header comment ("Turn-completion aria-live") for the mechanism: a counter (`n`) keys the
 * region's child span, so React remounts a fresh DOM node on every qualifying turn even when the
 * text it carries is byte-identical to what was already there.
 */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { emptyView, type SessionView, type Turn } from "../../state/view";
import { Conversation } from "./Conversation";

const NOW = Date.UTC(2026, 8, 16, 8, 0, 0);

// Mirrors Conversation.tsx's own private ANNOUNCEMENT_SETTLE_MS, which the module does not
// export, so it is restated here with the same value; if the two ever disagree, this file's own
// tests are the ones that catch it.
const ANNOUNCEMENT_SETTLE_MS = 4000;

function turn(id: string, status: Turn["status"]): Turn {
  return {
    id,
    user: { text: "Count them", command: "", at: "2026-09-16T08:00:00.000Z" },
    blocks: [{ kind: "text", id: `${id}-b`, text: "There are 132.", done: status !== "running" }],
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function multiView(...turns: Turn[]): SessionView {
  return { ...emptyView("s1"), turns };
}

function region(container: HTMLElement): HTMLElement {
  const found = container.querySelector('[role="status"]');
  if (!(found instanceof HTMLElement)) throw new Error("no live region in the conversation");
  return found;
}

function announcedSpan(container: HTMLElement): Element | null {
  return region(container).querySelector("span");
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("a second turn finishing the same way as the first is still announced, in a fresh DOM node", () => {
  const { container, rerender } = render(
    <Conversation view={multiView(turn("t1", "running"))} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  rerender(
    <Conversation view={multiView(turn("t1", "success"))} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  expect(region(container).textContent).toBe("Reply finished");
  const firstSpan = announcedSpan(container);

  // t2 is introduced running (establishing the baseline the effect compares against) and then,
  // in a later render, finishes the same way t1 did — no timer advance in between, so if the
  // region were keyed on text alone this second "Reply finished" would never mutate the DOM.
  rerender(
    <Conversation
      view={multiView(turn("t1", "success"), turn("t2", "running"))}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
    />,
  );
  rerender(
    <Conversation
      view={multiView(turn("t1", "success"), turn("t2", "success"))}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
    />,
  );
  expect(region(container).textContent).toBe("Reply finished");
  expect(announcedSpan(container)).not.toBe(firstSpan);
});

test("the announcement clears itself after ANNOUNCEMENT_SETTLE_MS, and a later identical outcome announces again", () => {
  const { container, rerender } = render(
    <Conversation view={multiView(turn("t1", "running"))} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  rerender(
    <Conversation view={multiView(turn("t1", "success"))} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  expect(region(container).textContent).toBe("Reply finished");

  act(() => void vi.advanceTimersByTime(ANNOUNCEMENT_SETTLE_MS));
  expect(region(container).textContent).toBe("");

  // A third identical outcome, reached after the region has gone quiet, announces again rather
  // than staying silent because "nothing changed since the last non-empty value" — the region's
  // current text ("") already differs from "Reply finished", so this also guards against a
  // regression that keys only off the previous NON-EMPTY announcement.
  rerender(
    <Conversation
      view={multiView(turn("t1", "success"), turn("t2", "running"))}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
    />,
  );
  rerender(
    <Conversation
      view={multiView(turn("t1", "success"), turn("t2", "success"))}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
    />,
  );
  expect(region(container).textContent).toBe("Reply finished");
});

test("a turn this component never saw running is never announced", () => {
  const { container, rerender } = render(
    <Conversation view={multiView(turn("t1", "running"))} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  // t2 arrives already "cancelled" in the very commit that finishes t1 — reachable whenever a
  // turn starts and ends before this component renders it in between (its turn.started and
  // turn.done coalesced into one batch), which is ordinary under a resumed or fast-moving
  // session. previousStatusesRef never recorded t2 as "running" (it had no prior render at all),
  // so the effect's `was === "running"` guard skips it and only t1's outcome — the one turn this
  // instance actually watched go from running to done — reaches the region.
  rerender(
    <Conversation
      view={multiView(turn("t1", "success"), turn("t2", "cancelled"))}
      nowMs={NOW}
      onOpenUrl={vi.fn()}
    />,
  );
  expect(region(container).textContent).toBe("Reply finished");
});
