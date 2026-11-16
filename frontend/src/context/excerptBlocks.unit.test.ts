import { describe, expect, it } from "vitest";
import { splitExcerpts } from "./excerptBlocks";

const label = (ref = "a/b.md", start = 2, end = 3, hash = "0123456789ab") =>
  `[vault excerpt] ${ref}, lines ${start}-${end}, sha256 ${hash}`;
const block = (body: string, fence = "```", head = label()) =>
  `${head}\n${fence}\n${body}\n${fence}`;

describe("splitExcerpts", () => {
  it("returns plain text as one segment", () => {
    for (const text of ["", "hello\nworld", "```\ncode\n```", "[vault excerpt] only a line"])
      expect(splitExcerpts(text)).toEqual([{ kind: "text", text }]);
  });

  it("splits one excerpt with text after it", () => {
    expect(splitExcerpts(`${block("x\ny")}\nquestion`)).toEqual([
      { kind: "excerpt", ref: "a/b.md", start: 2, end: 3, hash12: "0123456789ab", body: "x\ny" },
      { kind: "text", text: "question" },
    ]);
  });

  it("splits two excerpts with text between them and before", () => {
    const text = `intro\n${block("one")}\nmiddle\n${block("two", "```", label("c.md", 5, 5, "ffffffffffff"))}`;
    expect(splitExcerpts(text)).toEqual([
      { kind: "text", text: "intro" },
      { kind: "excerpt", ref: "a/b.md", start: 2, end: 3, hash12: "0123456789ab", body: "one" },
      { kind: "text", text: "middle" },
      { kind: "excerpt", ref: "c.md", start: 5, end: 5, hash12: "ffffffffffff", body: "two" },
    ]);
  });

  it("takes a four-backtick fence and keeps a shorter fence line inside the body", () => {
    expect(splitExcerpts(block("a\n```\nb", "````"))).toEqual([
      {
        kind: "excerpt",
        ref: "a/b.md",
        start: 2,
        end: 3,
        hash12: "0123456789ab",
        body: "a\n```\nb",
      },
    ]);
  });

  it("keeps a block with no closing fence as text", () => {
    for (const text of [
      `${label()}\n\`\`\`\nbody\nmore`,
      `${label()}\n\`\`\`\nbody\n\`\`\`\``, // a longer line does not close a three-backtick fence
      `${label()}\n\`\`\`\``,
      `${label()}\n\`\`\``,
      `${label()}\n\`\`\`\n\`\`\``, // empty body
    ])
      expect(splitExcerpts(text)).toEqual([{ kind: "text", text }]);
  });

  it("keeps a label with a bad digest, span or fence as text", () => {
    for (const head of [
      label("a.md", 2, 3, "0123456789a"),
      label("a.md", 2, 3, "0123456789abc"),
      label("a.md", 2, 3, "0123456789AB"),
      label("a.md", 3, 2),
      label("a.md", 0, 2),
    ]) {
      const text = block("x", "```", head);
      expect(splitExcerpts(text)).toEqual([{ kind: "text", text }]);
    }
    const noFence = `${label()}\n\`\`\nx\n\`\`\``;
    expect(splitExcerpts(noFence)).toEqual([{ kind: "text", text: noFence }]);
  });

  it("splits a good block that follows an unterminated one", () => {
    const text = `${label()}\n\`\`\`\nlost\n${block("kept", "````")}`;
    expect(splitExcerpts(text).map((segment) => segment.kind)).toEqual(["text", "excerpt"]);
  });

  it("stays linear on many labels with no fence and on one huge label-like line", () => {
    const labels = `${label()}
not a fence
`.repeat(20_000);
    expect(splitExcerpts(labels)).toEqual([{ kind: "text", text: labels }]);
    const long = `[vault excerpt] ${"a, lines 1-1, ".repeat(50_000)}`;
    expect(splitExcerpts(long)).toEqual([{ kind: "text", text: long }]);
  });
});
