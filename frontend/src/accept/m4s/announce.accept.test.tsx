/**
 * Acceptance (frozen): aria-live on turn completion.
 *
 * The one moment in this app a screen-reader user cannot discover by looking is the moment the
 * reply stops growing. Everything else is either their own action or a thing they can go and
 * read; the end of a turn is a change with no event they can feel. So it is announced, politely,
 * once.
 *
 * Two rules do all the work, and both are about NOT announcing:
 * - The region is empty until a turn that was running IN THIS WINDOW finishes. Opening a session
 *   with history, or a reload replaying a finished turn, must announce nothing: every one of
 *   those turns is already over, and reading them all out is noise that would teach the user to
 *   ignore the region.
 * - polite, never assertive: the turn ending is worth knowing and never worth interrupting what
 *   they are reading or typing.
 *
 * The wording sits on a screen-reader-only element, so it has no visual design to check
 * it against. "Stopped"
 * is deliberately the same sentence the cancelled reply already shows (Conversation.tsx), so the
 * two channels do not describe one event two ways.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Conversation } from "../../components/Conversation/Conversation";
import { emptyView, type SessionView, type Turn } from "../../state/view";

afterEach(cleanup);

const NOW = Date.UTC(2026, 8, 13, 8, 0, 0);

function view(status: Turn["status"], history = false): SessionView {
  return {
    ...emptyView("s1"),
    turns: [
      {
        id: "t1",
        user: { text: "Count them", command: "", at: "2026-09-13T08:00:00.000Z" },
        blocks: [{ kind: "text", id: "b1", text: "There are 132.", done: status !== "running" }],
        status,
        usage: null,
        durationMs: 0,
        history,
      },
    ],
  };
}

function region(container: HTMLElement): HTMLElement {
  const found = container.querySelector('[role="status"]');
  if (!(found instanceof HTMLElement)) throw new Error("no live region in the conversation");
  return found;
}

test("the region is polite, and silent while the turn is still running", () => {
  const { container } = render(
    <Conversation view={view("running")} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  const live = region(container);
  expect(live.getAttribute("aria-live")).toBe("polite");
  expect(live.className).toContain("sr-only"); // announced, never drawn
  expect(live.textContent).toBe("");
});

test("a turn that finishes while he is watching is announced once", () => {
  const { container, rerender } = render(
    <Conversation view={view("running")} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  rerender(<Conversation view={view("success")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(region(container).textContent).toBe("Reply finished");
});

test("stopping and failing say which one happened", () => {
  const stopped = render(<Conversation view={view("running")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  stopped.rerender(<Conversation view={view("cancelled")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  // The same sentence the cancelled reply itself shows: one event, one wording.
  expect(region(stopped.container).textContent).toBe("Stopped");
  cleanup();

  const failed = render(<Conversation view={view("running")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  failed.rerender(<Conversation view={view("error")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(region(failed.container).textContent).toBe("Reply failed");
});

test("a session opened onto turns that are already over announces nothing", () => {
  // A reload, or clicking a row in the rail: every turn arrives finished. None of them just
  // happened, so none of them is news.
  const { container } = render(
    <Conversation view={view("success", true)} nowMs={NOW} onOpenUrl={vi.fn()} />,
  );
  expect(region(container).textContent).toBe("");

  const live = render(<Conversation view={view("success")} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(region(live.container).textContent).toBe("");
});
