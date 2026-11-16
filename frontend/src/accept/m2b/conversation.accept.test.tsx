/**
 * Acceptance (frozen): the conversation column — the user's bubble, VIKI's reply, the
 * order of the rows inside it, and following the live edge.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Conversation, UserTurn } from "../../components/Conversation/Conversation";
import { ConversationHeader } from "../../components/Conversation/ConversationHeader";
import {
  AT,
  subagentStarted,
  textDelta,
  textDone,
  thinkingDone,
  toolDone,
  toolStarted,
  turnDone,
  turnStarted,
  userMessage,
  view,
} from "./fakes";

afterEach(cleanup);

const NOW = Date.parse("2026-09-12T10:00:00.000Z");

test("the owner's prompt is a bubble with the time, and it is never markdown", () => {
  const { container } = render(
    <UserTurn
      user={{ text: "- milk\n- eggs **not bold**", command: "", at: AT }}
      nowMs={NOW}
      userLabel="Alex"
    />,
  );
  expect(container.querySelector("strong")).toBeNull();
  expect(container.querySelector(".user-turn")?.textContent).toContain("- milk");
  expect(container.querySelector(".user-at")?.textContent).toMatch(/^Alex · \d\d:\d\d$/);
});

test("a slash command shows its chip before the text", () => {
  const { container } = render(
    <UserTurn user={{ text: "yesterday", command: "/capture", at: AT }} nowMs={NOW} />,
  );
  expect(container.querySelector(".cmd-chip")?.textContent).toBe("/capture");
  expect(container.querySelector(".user-turn")?.textContent).toContain("yesterday");
});

/* AMENDMENT (2026-10-20). Dropped the reply's `.reply-presence` label: this test's title no
   longer calls the label out, and its one assertion of it is removed. The next test, which
   existed only to pin that label across a thinking block and a finished turn, is retired
   outright. */

test("a reply shows VIKI and its rows in the order they arrived", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    thinkingDone("mapping the mechanisms", 6000),
    toolStarted("t1", "Read", { head: "Read", rest: "40-sources/Linter.md" }),
    toolDone("t1"),
    subagentStarted("a1", "researcher", "opus", "web sweep"),
    textDelta("In short"),
  ]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".reply-name")?.textContent).toBe("VIKI");
  expect(container.querySelector(".face-presence")).not.toBeNull(); // breathing while it writes
  const kinds = [...container.querySelectorAll(".thought, .tools, .routing, .md")].map(
    (el) => el.className.split(" ")[0],
  );
  expect(kinds).toEqual(["thought", "tools", "routing", "md"]);
});

test("a cancelled turn says so", () => {
  const v = view([userMessage("hi"), turnStarted, textDone("half"), turnDone("cancelled")]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".reply-cancelled")?.textContent).toBe("Stopped");
});

test("the column follows the live edge until the owner scrolls away from it", () => {
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const { container, rerender } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  const list = container.querySelector(".conversation-list") as HTMLElement;
  // jsdom reports zero heights, so the column drives scrollTop through these three numbers.
  Object.defineProperty(list, "scrollHeight", { value: 1000, configurable: true });
  Object.defineProperty(list, "clientHeight", { value: 400, configurable: true });
  list.scrollTop = 600; // at the bottom
  rerender(<Conversation view={view([textDelta(" two")], v)} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(list.scrollTop).toBe(600);

  list.scrollTop = 100; // the user scrolled up: following stops
  list.dispatchEvent(new Event("scroll"));
  rerender(<Conversation view={view([textDelta(" three")], v)} nowMs={NOW} onOpenUrl={vi.fn()} />);
  expect(list.scrollTop).toBe(100);
});

/* AMENDMENT (2026-10-20). Dropped the reply's `.reply-presence` label: `waitingLabel` is gone
   with it, so a card open on the user no longer changes what the reply names. The test this
   introduced — "while a card waits, the reply says so instead of claiming to be writing" —
   is retired outright; the pill test below is unaffected. */

/* Added 2026-09-13 (no assertion changed): the jump-to-latest pill, which this contract had
   never pinned because it did not exist when this file was written. */

test("scrolling away from the live edge offers the pill back to it", () => {
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} />);
  const list = container.querySelector(".conversation-list") as HTMLElement;
  Object.defineProperty(list, "scrollHeight", { value: 1000, configurable: true });
  Object.defineProperty(list, "clientHeight", { value: 400, configurable: true });
  // At the bottom there is nothing to offer.
  list.scrollTop = 600;
  fireEvent.scroll(list);
  expect(container.querySelector(".jump-pill")).toBeNull();

  list.scrollTop = 100;
  fireEvent.scroll(list);
  const pill = container.querySelector(".jump-pill") as HTMLElement;
  expect(pill.textContent).toContain("Jump to latest");

  fireEvent.click(pill);
  expect(list.scrollTop).toBe(600);
  expect(container.querySelector(".jump-pill")).toBeNull();
});

test("scrolling to the top asks for the page before it", () => {
  const onLoadOlder = vi.fn();
  const v = view([userMessage("hi"), turnStarted, textDelta("one")]);
  const { container } = render(
    <Conversation view={v} nowMs={NOW} onOpenUrl={vi.fn()} onLoadOlder={onLoadOlder} />,
  );
  const list = container.querySelector(".conversation-list") as HTMLElement;
  Object.defineProperty(list, "scrollHeight", { value: 4000, configurable: true });
  Object.defineProperty(list, "clientHeight", { value: 400, configurable: true });
  list.scrollTop = 2000;
  fireEvent.scroll(list);
  expect(onLoadOlder).not.toHaveBeenCalled();

  list.scrollTop = 0;
  fireEvent.scroll(list);
  expect(onLoadOlder).toHaveBeenCalled();
});

test("the header names the session and shows no live chip or participants", () => {
  const { container } = render(
    <ConversationHeader title="Plan a weekly review of my notes in VIKI" />,
  );
  expect(container.querySelector(".conversation-title")?.textContent).toBe(
    "Plan a weekly review of my notes in VIKI",
  );
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector(".participants")).toBeNull();
});

test("a session that is not live has no chip", () => {
  const { container } = render(<ConversationHeader title="Vault backup plan" />);
  expect(container.querySelector(".chip")).toBeNull();
});
