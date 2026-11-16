/**
 * Unit tests pinning behaviour that markdown.accept.test.tsx leaves implicit: the exact shape
 * of the fallback text for a refused link (the frozen test only checks `.toContain`), and the
 * two link rules that have to agree with the server's own open.url check.
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { Markdown } from "./Markdown";
import {
  type MarkdownParser,
  MarkdownParserContext,
  SETTLED_DEADLINE_MS,
  STREAMING_DEADLINE_MS,
} from "./markdownParser";
import { type ParseResult, parseMarkdown } from "./markdownTree";

afterEach(cleanup);

test("the parse deadlines are 250 ms while streaming and 2 s once settled", () => {
  expect(STREAMING_DEADLINE_MS).toBe(250);
  expect(SETTLED_DEADLINE_MS).toBe(2000);
});

test("a refused link keeps its label, followed by the raw URL in parentheses", () => {
  const { container } = render(
    <Markdown text="[read more](javascript:alert(1))" onOpenUrl={vi.fn()} />,
  );
  expect(container.querySelector("a")).toBeNull();
  expect(container.textContent).toBe("read more (javascript:alert(1))");
});

test("a bare autolink with no label still shows the refused URL", () => {
  const { container } = render(<Markdown text="<javascript:alert(1)>" onOpenUrl={vi.fn()} />);
  expect(container.querySelector("a")).toBeNull();
  expect(container.textContent).toContain("javascript:alert(1)");
});

test("an uppercase scheme is a real link, because the server opens one", () => {
  // This side always lowercased before checking; open.url did not, so `HTTPS://x` was drawn as
  // a live link and then silently dropped on click. The server now folds the same way.
  const { container } = render(<Markdown text="[x](HTTPS://example.com)" onOpenUrl={vi.fn()} />);
  expect(container.querySelector("a")?.getAttribute("href")).toBe("HTTPS://example.com");
});

test("a destination containing a space is cut by the parser, not smuggled through whole", () => {
  // CommonMark ends an unbracketed destination at the first space, so the href this component
  // ever sees is already whitespace-free — which is why UNSAFE_URL_CHARS is belt and braces
  // rather than the load-bearing half of the agreement with open.url. Pinned so a future parser
  // or plugin that starts passing the whole string through is noticed here first.
  const { container } = render(<Markdown text="[x](https://exa mple.com)" onOpenUrl={vi.fn()} />);
  expect(container.querySelector("a")?.getAttribute("href")).toBe("https://exa");
});

test("with onOpenNoteLink, a viki-note: link renders a note link as a button, and its click decodes the target", () => {
  const onOpenNoteLink = vi.fn();
  const { container } = render(
    <Markdown text="[a b](viki-note:x%20y)" onOpenUrl={vi.fn()} onOpenNoteLink={onOpenNoteLink} />,
  );
  expect(container.querySelector("a")).toBeNull();
  const button = container.querySelector("button.md-note-link");
  expect(button?.textContent).toBe("a b");
  fireEvent.click(button as HTMLButtonElement);
  expect(onOpenNoteLink).toHaveBeenCalledWith("x y");
});

test("a malformed viki-note: href renders as text instead of a button", () => {
  const onOpenNoteLink = vi.fn();
  const { container } = render(
    <Markdown text="[a](viki-note:%E0%A4)" onOpenUrl={vi.fn()} onOpenNoteLink={onOpenNoteLink} />,
  );
  expect(container.querySelector("a")).toBeNull();
  expect(container.querySelector("button")).toBeNull();
  expect(container.textContent).toBe("a (viki-note:%E0%A4)");
  expect(onOpenNoteLink).not.toHaveBeenCalled();
});

test("without onOpenNoteLink, a viki-note: link renders like any other unsafe scheme", () => {
  const { container } = render(<Markdown text="[a](viki-note:x)" onOpenUrl={vi.fn()} />);
  expect(container.querySelector("a")).toBeNull();
  expect(container.querySelector("button")).toBeNull();
  expect(container.textContent).toBe("a (viki-note:x)");
});

test("an https:// link is still a real anchor when onOpenNoteLink is given", () => {
  const { container } = render(
    <Markdown text="[docs](https://example.com)" onOpenUrl={vi.fn()} onOpenNoteLink={vi.fn()} />,
  );
  expect(container.querySelector("button")).toBeNull();
  expect(container.querySelector("a")?.getAttribute("href")).toBe("https://example.com");
});

test("ordinary markdown still renders as markdown", () => {
  const { container } = render(<Markdown text="**bold** text" onOpenUrl={vi.fn()} />);
  expect(container.querySelector("strong")?.textContent).toBe("bold");
});

test("a very deeply nested list renders the plain-text fallback instead of throwing, and the rest of the tree stays mounted", () => {
  // The parse refuses the depth before any DOM exists: the recursive stages overflow the call
  // stack on input like this, and Edge's layout crashes on a far shallower rendered list.
  const text = `${"- ".repeat(1000)}x`;
  const { container } = render(
    <div>
      <p data-testid="sibling">sibling</p>
      <Markdown text={text} onOpenUrl={vi.fn()} />
    </div>,
  );
  expect(container.querySelector("[data-testid='sibling']")?.textContent).toBe("sibling");
  expect(container.querySelector("pre")?.textContent).toBe(text);
  expect(container.querySelector("pre")?.className).toBe("md-plain");
  expect(container.querySelector("pre")?.getAttribute("data-reason")).toBe("depth");
});

test("text above the size cap renders as plain text without being parsed", () => {
  const text = `${"*".repeat(300 * 1024)}\n`;
  const { container } = render(<Markdown text={text} onOpenUrl={vi.fn()} />);
  expect(container.querySelector("em")).toBeNull();
  expect(container.querySelector("pre")?.textContent).toBe(text);
  expect(container.querySelector("pre")?.className).toBe("md-plain");
});

test("fifty list levels already exceed the depth bound and render plain", () => {
  const text = `${"- ".repeat(50)}a`;
  const { container } = render(<Markdown text={text} onOpenUrl={vi.fn()} />);
  expect(container.querySelector("ul")).toBeNull();
  expect(container.querySelector("pre.md-plain")?.textContent).toBe(text);
});

interface Asked {
  owner: object;
  text: string;
  deadlineMs: number;
  onResult: (result: ParseResult) => void;
}

function fakeParser() {
  const cache = new Map<string, ParseResult>();
  const asked: Asked[] = [];
  const parser: MarkdownParser = {
    cached: (text) => cache.get(text),
    request: (owner, text, deadlineMs, onResult) => {
      asked.push({ owner, text, deadlineMs, onResult });
    },
    cancel: vi.fn(),
  };
  const answer = (index: number, result?: ParseResult) => {
    const job = asked[index] as Asked;
    act(() => job.onResult(result ?? parseMarkdown(job.text)));
  };
  const wrap = (node: ReactElement) => (
    <MarkdownParserContext.Provider value={parser}>{node}</MarkdownParserContext.Provider>
  );
  return { parser, cache, asked, answer, wrap };
}

const TIMEOUT: ParseResult = { kind: "plain", reason: "timeout" };
const onOpenUrl = vi.fn();

test("with a parser, the text shows plain and busy until the worker answers, then formatted", () => {
  const { asked, answer, wrap } = fakeParser();
  const { container } = render(wrap(<Markdown text="**bold** text" onOpenUrl={onOpenUrl} />));
  expect(container.querySelector("strong")).toBeNull();
  expect(container.querySelector("pre.md-plain")?.textContent).toBe("**bold** text");
  expect(container.querySelector("pre.md-plain")?.hasAttribute("data-reason")).toBe(false);
  expect(container.querySelector(".md")?.getAttribute("aria-busy")).toBe("true");
  expect(asked.map((job) => [job.text, job.deadlineMs])).toEqual([
    ["**bold** text", SETTLED_DEADLINE_MS],
  ]);

  answer(0);
  expect(container.querySelector("strong")?.textContent).toBe("bold");
  expect(container.querySelector("pre.md-plain")).toBeNull();
  expect(container.querySelector("[aria-busy]")).toBeNull();
});

test("a cached text formats in the first render", () => {
  const { cache, wrap } = fakeParser();
  cache.set("**bold** text", parseMarkdown("**bold** text"));
  const { container } = render(wrap(<Markdown text="**bold** text" onOpenUrl={onOpenUrl} />));
  expect(container.querySelector("strong")?.textContent).toBe("bold");
});

test("while streaming, the last tree stays up until the newer text is parsed", () => {
  const { asked, answer, wrap } = fakeParser();
  const { container, rerender } = render(
    wrap(<Markdown text="**a**" streaming onOpenUrl={onOpenUrl} />),
  );
  answer(0);
  rerender(wrap(<Markdown text="**a** and **b**" streaming onOpenUrl={onOpenUrl} />));
  expect([...container.querySelectorAll("strong")].map((node) => node.textContent)).toEqual(["a"]);
  expect(container.querySelector("[aria-busy]")).toBeNull();
  expect(container.querySelector(".caret")).not.toBeNull();
  expect(asked.map((job) => job.deadlineMs)).toEqual([
    STREAMING_DEADLINE_MS,
    STREAMING_DEADLINE_MS,
  ]);

  answer(1);
  expect([...container.querySelectorAll("strong")].map((node) => node.textContent)).toEqual([
    "a",
    "b",
  ]);
});

test("an older answer that lands after a newer one never replaces it", () => {
  const { answer, wrap } = fakeParser();
  const { container, rerender } = render(
    wrap(<Markdown text="**a**" streaming onOpenUrl={onOpenUrl} />),
  );
  rerender(wrap(<Markdown text="**a** and **b**" streaming onOpenUrl={onOpenUrl} />));
  answer(1);
  answer(0);
  expect(container.querySelectorAll("strong")).toHaveLength(2);
});

test("an answer for a text that was replaced, not extended, is not shown", () => {
  const { answer, wrap } = fakeParser();
  const { container, rerender } = render(
    wrap(<Markdown text="**old note**" onOpenUrl={onOpenUrl} />),
  );
  rerender(wrap(<Markdown text="new note" onOpenUrl={onOpenUrl} />));
  answer(0);
  expect(container.querySelector("strong")).toBeNull();
  expect(container.querySelector("pre.md-plain")?.textContent).toBe("new note");
  expect(container.querySelector(".md")?.getAttribute("aria-busy")).toBe("true");
});

test("a streaming timeout keeps the reply plain without asking again, until it settles", () => {
  const { asked, answer, wrap } = fakeParser();
  const { container, rerender } = render(
    wrap(<Markdown text="**a**" streaming onOpenUrl={onOpenUrl} />),
  );
  answer(0, TIMEOUT);
  expect(container.querySelector("pre.md-plain")?.textContent).toBe("**a**");
  expect(container.querySelector("pre.md-plain")?.getAttribute("data-reason")).toBe("timeout");

  rerender(wrap(<Markdown text="**a** more" streaming onOpenUrl={onOpenUrl} />));
  expect(container.querySelector("pre.md-plain")?.textContent).toBe("**a** more");
  expect(container.querySelector("[aria-busy]")).toBeNull();
  expect(asked).toHaveLength(1);

  rerender(wrap(<Markdown text="**a** more" onOpenUrl={onOpenUrl} />));
  expect(asked.map((job) => [job.text, job.deadlineMs])).toEqual([
    ["**a**", STREAMING_DEADLINE_MS],
    ["**a** more", SETTLED_DEADLINE_MS],
  ]);
  answer(1);
  expect(container.querySelector("strong")?.textContent).toBe("a");
});

test("unmounting cancels the instance's own jobs", () => {
  const { parser, asked, wrap } = fakeParser();
  const { unmount } = render(wrap(<Markdown text="**a**" onOpenUrl={onOpenUrl} />));
  unmount();
  expect(parser.cancel).toHaveBeenCalledWith(asked[0]?.owner);
});

test("a tree that throws in render falls back to plain text, and the fallback clears once a later render gets ordinary text", () => {
  // VaultBrowser reuses one Markdown instance across notes (no key on it, since keying by text
  // would remount the parser on every streamed token). Without a props-driven reset, one note
  // that threw would leave every later note rendered as plain text too.
  const { cache, wrap } = fakeParser();
  // A text node whose value is an object: React refuses it as a child and throws in render.
  const broken = {
    kind: "tree",
    tree: { type: "root", children: [{ type: "text", value: {} }] },
    nodes: 2,
  } as ParseResult;
  cache.set("broken", broken);
  cache.set("**bold** text", parseMarkdown("**bold** text"));
  // React reports the error the boundary caught; the test only needs the fallback.
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { container, rerender } = render(wrap(<Markdown text="broken" onOpenUrl={onOpenUrl} />));
  expect(container.querySelector("pre.md-plain")?.textContent).toBe("broken");

  rerender(wrap(<Markdown text="**bold** text" onOpenUrl={onOpenUrl} />));
  spy.mockRestore();
  expect(container.querySelector("pre")).toBeNull();
  expect(container.querySelector("strong")?.textContent).toBe("bold");
});

test("a parse answer that formats the text after its commit announces itself, once", () => {
  const { cache, answer, wrap } = fakeParser();
  const formats = vi.fn();
  const { container, rerender } = render(
    wrap(<Markdown text="**a**" streaming onOpenUrl={onOpenUrl} />),
  );
  container.addEventListener("markdownformat", formats);
  answer(0);
  expect(formats).toHaveBeenCalledTimes(1);

  // A new text renders in its own commit, where the conversation already re-measures: no event,
  // whether it keeps the last tree up or is a cache hit.
  rerender(wrap(<Markdown text="**a** b" streaming onOpenUrl={onOpenUrl} />));
  cache.set("**a** b c", parseMarkdown("**a** b c"));
  rerender(wrap(<Markdown text="**a** b c" streaming onOpenUrl={onOpenUrl} />));
  expect(container.querySelector("strong")?.textContent).toBe("a");
  expect(formats).toHaveBeenCalledTimes(1);
});
