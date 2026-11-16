/**
 * Acceptance (frozen): the empty conversation and the read-only history line. The history
 * line is the one place the app admits what a transcript does not record, and it has to
 * admit it where the user is about to type.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { EmptyState, HistoryNote } from "../../components/Conversation/States";

afterEach(cleanup);

test("a new session is greeted, and told how to reach the skills", () => {
  const { container } = render(<EmptyState />);
  const empty = container.querySelector(".empty") as HTMLElement;
  expect(empty.textContent).toContain("New conversation");
  expect(empty.textContent).toContain("Talk to VIKI. Use / to call a skill.");
});

test("the empty state shows the brand lettering without the old default portrait", () => {
  const { container } = render(<EmptyState />);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".face")).toBeNull();
  expect(container.querySelector(".empty-wordmark")?.textContent).toBe("VIKIAGENT");
});

test("a history session says it will resume, and what the record is missing", () => {
  const { container } = render(<HistoryNote />);
  const note = container.querySelector(".history-note") as HTMLElement;
  expect(note.textContent).toContain(
    "This is a past conversation. Send a message to resume from here.",
  );
  expect(note.textContent).toContain("history · usage and thinking times are not recorded");
});
