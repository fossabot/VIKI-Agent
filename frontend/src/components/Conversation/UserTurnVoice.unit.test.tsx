/**
 * A message hands-free sent shows as it was heard, with a small voice tag in place of the
 * spoken-answer request it carries; any other message renders as typed.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { SPOKEN_ANSWER_REQUEST, withSpokenAnswerRequest } from "../../voice/spokenAnswer";
import { UserTurn } from "./Conversation";

afterEach(cleanup);

const AT = "2026-10-05T09:00:00Z";
const NOW = Date.parse(AT);

test("a voice message shows the words heard and a voice tag, not the request", () => {
  const { container } = render(
    <UserTurn
      user={{ text: withSpokenAnswerRequest("buy milk"), command: "", at: AT }}
      nowMs={NOW}
    />,
  );
  const bubble = container.querySelector(".user-turn") as HTMLElement;
  expect(bubble.textContent).toContain("buy milk");
  expect(bubble.textContent).not.toContain(SPOKEN_ANSWER_REQUEST);
  expect(container.querySelector(".voice-chip")?.textContent).toContain("voice");
  expect(screen.getByText("Sent by voice")).not.toBeNull();
});

test("a message without the request is unchanged: one text node and no tag", () => {
  const text = `buy milk ${SPOKEN_ANSWER_REQUEST}`;
  const { container } = render(<UserTurn user={{ text, command: "", at: AT }} nowMs={NOW} />);
  const bubble = container.querySelector(".user-turn") as HTMLElement;
  expect(bubble.childNodes).toHaveLength(1);
  expect(bubble.firstChild?.nodeValue).toBe(text);
  expect(container.querySelector(".voice-chip")).toBeNull();
});
