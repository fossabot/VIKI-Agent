/**
 * Hands-free's short spoken answer: the request an automatic Send
 * carries, recognised again in the saved text, and the 1–3 plain sentences spoken from a reply.
 */
import { describe, expect, test } from "vitest";
import { parseMarkdown } from "../components/Conversation/markdownTree";
import {
  MAX_SPOKEN_CHARS,
  SPOKEN_ANSWER_REQUEST,
  spokenAnswer,
  withoutSpokenAnswerRequest,
  withSpokenAnswerRequest,
} from "./spokenAnswer";

const spoken = (...blocks: string[]) => spokenAnswer(blocks.map(parseMarkdown))?.text;

describe("the request", () => {
  test("follows the words heard after a blank line and comes off again exactly", () => {
    const sent = withSpokenAnswerRequest("buy milk");
    expect(SPOKEN_ANSWER_REQUEST.startsWith("[voice] ")).toBe(true);
    expect(sent).toBe(`buy milk\n\n${SPOKEN_ANSWER_REQUEST}`);
    expect(withoutSpokenAnswerRequest(sent)).toBe("buy milk");
  });

  test("is recognised only as the whole tail after a blank line", () => {
    expect(withoutSpokenAnswerRequest("buy milk")).toBeNull();
    expect(withoutSpokenAnswerRequest(`buy milk ${SPOKEN_ANSWER_REQUEST}`)).toBeNull();
    expect(withoutSpokenAnswerRequest(`${withSpokenAnswerRequest("a")} and more`)).toBeNull();
    expect(withoutSpokenAnswerRequest(`buy milk\n\n${SPOKEN_ANSWER_REQUEST.slice(1)}`)).toBeNull();
  });

  const block = (ref: string) =>
    `[vault excerpt] ${ref}, lines 1-2, sha256 abcdef012345\n\`\`\`\ntext\n\`\`\``;

  test("is also recognised before the vault excerpt blocks an automatic send appends (session.py joined_text), the blocks kept", () => {
    const sent = `${withSpokenAnswerRequest("buy milk")}\n${block("a.md")}`;
    expect(withoutSpokenAnswerRequest(sent)).toBe(`buy milk\n${block("a.md")}`);
  });

  test("keeps every block when more than one is attached", () => {
    const sent = `${withSpokenAnswerRequest("buy milk")}\n${block("a.md")}\n${block("b.md")}`;
    expect(withoutSpokenAnswerRequest(sent)).toBe(`buy milk\n${block("a.md")}\n${block("b.md")}`);
  });

  test("is not stripped when the tail is followed by ordinary text, not excerpt blocks", () => {
    const sent = `${withSpokenAnswerRequest("buy milk")}\nmore words, no excerpt here`;
    expect(withoutSpokenAnswerRequest(sent)).toBeNull();
  });
});

describe("the spoken answer", () => {
  test("is the reply's first paragraph, not the detail after it", () => {
    expect(spoken("Your meeting is at three.\n\nDetails:\n\n- room 4\n- bring the deck")).toBe(
      "Your meeting is at three.",
    );
  });

  test("skips a leading heading, code block, table and list", () => {
    const reply = [
      "# Status",
      "```\nnpm test\n```",
      "| a | b |\n|---|---|\n| 1 | 2 |",
      "- first\n- second",
      "The build is green.",
      "More detail here.",
    ].join("\n\n");
    expect(spoken(reply)).toBe("The build is green.");
  });

  test("is found in a later block when the first has no paragraph", () => {
    expect(spoken("## Heading only", "It is done.\n\nThe rest.")).toBe("It is done.");
  });

  test("drops inline Markdown, links' addresses, bare URLs and emoji, on one line", () => {
    const reply =
      "**Bold** and _soft_ with `code`, a [link](https://example.com/a) and\n" +
      "https://example.com/b plus 😀 emoji 👍🏽 🇯🇵 too.";
    expect(spoken(reply)).toBe("Bold and soft with code, a link and plus emoji too.");
  });

  test("stops at three sentences", () => {
    expect(spoken("One is here. Two is here. Three is here. Four is here.")).toBe(
      "One is here. Two is here. Three is here.",
    );
  });

  test("stops at a sentence boundary before the length cap", () => {
    const long = `Word ${"word ".repeat(29)}end.`;
    const reply = `${long} ${long} ${long}`;
    const answer = spoken(reply) ?? "";
    expect(answer).toBe(`${long} ${long}`);
    expect(answer.length).toBeLessThanOrEqual(MAX_SPOKEN_CHARS);
  });

  test("cuts a single over-long sentence at a space within the cap", () => {
    const answer = spoken(`${"word ".repeat(100)}end.`) ?? "";
    expect(answer.length).toBeLessThanOrEqual(MAX_SPOKEN_CHARS);
    expect(answer.length).toBeGreaterThan(MAX_SPOKEN_CHARS - 6);
    expect(answer.endsWith("word")).toBe(true);
  });

  test("handles Japanese sentence marks", () => {
    expect(spoken("今日は晴れです。明日は雨です！本当ですか？はい、本当です。")).toBe(
      "今日は晴れです。 明日は雨です！ 本当ですか？",
    );
  });

  test("falls back to the reply's first sentences when the first paragraph says nothing", () => {
    expect(spoken("https://example.com/only\n\nThe rest is here. And more.")).toBe(
      "The rest is here. And more.",
    );
    expect(spoken("- one\n- two\n- three\n- four")).toBe("one two three");
  });

  test("is empty with nothing to say, and null for a reply that rendered plain", () => {
    expect(spoken("```\ncode only\n```")).toBe("");
    expect(spokenAnswer([{ kind: "plain", reason: "error" }])).toBeNull();
  });
});
