import { cleanup, fireEvent, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, expect, test } from "vitest";
import { Markdown } from "../Conversation/Markdown";
import { linkWikilinks } from "./wikilinks";

afterEach(cleanup);

const always = () => true;
const never = () => false;

test("a plain name becomes a link with the name as both label and target", () => {
  expect(linkWikilinks("[[Foo]]", always)).toBe("[Foo](viki-note:Foo)");
});

test("an alias is used as the label, with the name as the target", () => {
  const seen: string[] = [];
  const linkable = (target: string) => {
    seen.push(target);
    return true;
  };
  expect(linkWikilinks("[[Foo|Bar]]", linkable)).toBe("[Bar](viki-note:Foo)");
  expect(seen).toEqual(["Foo"]);
});

test("a heading is dropped from both the label and the target", () => {
  expect(linkWikilinks("[[Foo#Section]]", always)).toBe("[Foo](viki-note:Foo)");
});

test("a heading with an alias keeps the alias as the label and the name as the target", () => {
  expect(linkWikilinks("[[Foo#Section|Bar]]", always)).toBe("[Bar](viki-note:Foo)");
});

test("angle brackets around a name are kept in the label but stripped from the target", () => {
  expect(linkWikilinks("[[<Foo>]]", always)).toBe("[<Foo>](viki-note:Foo)");
});

test("an embed (the [[ preceded by !) is left untouched", () => {
  expect(linkWikilinks("![[Foo]]", always)).toBe("![[Foo]]");
});

test("a wikilink inside a fenced code block is left untouched; ones outside still link", () => {
  const text = "[[Before]]\n```\n[[Inside]]\n```\n[[After]]";
  const seen: string[] = [];
  const linkable = (target: string) => {
    seen.push(target);
    return true;
  };
  expect(linkWikilinks(text, linkable)).toBe(
    "[Before](viki-note:Before)\n```\n[[Inside]]\n```\n[After](viki-note:After)",
  );
  expect(seen).toEqual(["Before", "After"]);
});

test("a wikilink inside a tilde-fenced code block is left untouched", () => {
  const text = "~~~\n[[Inside]]\n~~~\n[[After]]";
  expect(linkWikilinks(text, always)).toBe("~~~\n[[Inside]]\n~~~\n[After](viki-note:After)");
});

test("a wikilink inside an inline code span is left untouched", () => {
  const text = "before `[[Inside]]` and [[After]]";
  expect(linkWikilinks(text, always)).toBe("before `[[Inside]]` and [After](viki-note:After)");
});

test("an unlinkable target renders as its plain label, with no link", () => {
  expect(linkWikilinks("[[Foo]]", never)).toBe("Foo");
});

test("an unlinkable label escapes the markdown markers it could otherwise open", () => {
  expect(linkWikilinks("[[*A_B`C<D]]", never)).toBe("\\*A\\_B\\`C\\<D");
});

test("a literal backslash in the name is escaped too, so the output has no stray escape", () => {
  expect(linkWikilinks("[[A\\B]]", never)).toBe("A\\\\B");
});

test("two links on one line resolve independently", () => {
  const seen: string[] = [];
  const linkable = (target: string) => {
    seen.push(target);
    return target === "A";
  };
  expect(linkWikilinks("[[A]] and [[B]]", linkable)).toBe("[A](viki-note:A) and B");
  expect(seen).toEqual(["A", "B"]);
});

test("text with no wikilink is returned unchanged", () => {
  const text = "just some **markdown** text, no links here";
  expect(linkWikilinks(text, always)).toBe(text);
});

// A `|alias` match that starts in prose can run into a fence (LINK_TEXT allows newlines).
// Checking only the match's start against the excluded ranges would rewrite this match (start
// on line 1, end inside the fenced block on line 3): escaping the alias would escape the
// fence's own backticks, the fence would stop being one, and the now-live anchor inside it
// would point at https://evil.example/.
test("a match that starts in prose and runs into a fenced code block stays untouched, source byte-identical", () => {
  const source = [
    "Note text [[x|see",
    "```js",
    'const s = "]]"; // [click](https://evil.example/)',
    "```",
    "",
  ].join("\n");
  const rewritten = linkWikilinks(source, always);
  expect(rewritten).toBe(source);
  const { container } = render(
    createElement(Markdown, { text: rewritten, onOpenUrl: () => {}, onOpenNoteLink: () => {} }),
  );
  const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
  expect(hrefs).toEqual([]);
});

// encodeURIComponent leaves `(` and `)` as-is, so a target with either would land raw in the
// markdown destination — a `)` could close the destination early (wrong target, stray text) and
// a lone `(` would show up as raw markdown. Markdown.tsx's decodeNoteTarget reverses %28/%29 along
// with the rest of the target, since it is plain decodeURIComponent.
test("parentheses in a wikilink target are percent-encoded so the link opens the right note and the label still shows", () => {
  const rewritten = linkWikilinks("See [[Plan B) draft]] and [[Plan (C]].", always);
  expect(rewritten).toBe(
    "See [Plan B) draft](viki-note:Plan%20B%29%20draft) and [Plan (C](viki-note:Plan%20%28C).",
  );
  const opened: string[] = [];
  const { container } = render(
    createElement(Markdown, {
      text: rewritten,
      onOpenUrl: () => {},
      onOpenNoteLink: (target: string) => opened.push(target),
    }),
  );
  const buttons = Array.from(container.querySelectorAll("button.md-note-link"));
  expect(buttons.map((button) => button.textContent)).toEqual(["Plan B) draft", "Plan (C"]);
  for (const button of buttons) fireEvent.click(button);
  expect(opened).toEqual(["Plan B) draft", "Plan (C"]);
});

// Checking every match against every excluded range would be O(matches x ranges);
// codeSpansOnLine's own closer search would have the same O(runs^2) shape. 100,000 code spans x
// 100,000 wikilinks in one call would take over 30s that way; both are linear, so this
// finishes in well under vitest's default 5 s test timeout.
test("linkWikilinks stays linear for a 1 MB input of repeated inline code and wikilinks", () => {
  const n = 100_000;
  const text = "`a` [[b]] ".repeat(n); // 1,000,000 chars
  const rewritten = linkWikilinks(text, always);
  expect(rewritten.split("viki-note:b").length - 1).toBe(n);
});

// A `[[` escaped like any other markdown character (an odd run of backslashes right before
// it) is not a wikilink, and a match that itself spans a newline — a blank line or a setext `---`
// inside an alias — breaks the markdown the rewrite relies on. Both left untouched, source
// byte-identical, so the reader shows the written source rather than the rewrite's own syntax.
test("an escaped opener (odd backslash run before [[) is left untouched", () => {
  const source = "See \\[[x]] now.";
  expect(linkWikilinks(source, always)).toBe(source);
});

test("an alias spanning a blank line is left untouched", () => {
  const source = "See [[x|first\n\nsecond]] now.";
  expect(linkWikilinks(source, always)).toBe(source);
});

test("an alias spanning a setext --- line is left untouched", () => {
  const source = "See [[x|first\n---\nsecond]] now.";
  expect(linkWikilinks(source, always)).toBe(source);
});
