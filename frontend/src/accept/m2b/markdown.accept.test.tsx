/**
 * Acceptance (frozen): VIKI's reply text is markdown, and the threat model holds inside
 * it. Model output is the least trusted text in the app — it can quote a web page it just
 * fetched — so this file is where "rendered as text, never markup" is pinned.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Markdown } from "../../components/Conversation/Markdown";

afterEach(cleanup);

function show(text: string, streaming = false) {
  const onOpenUrl = vi.fn();
  const { container } = render(
    <Markdown text={text} streaming={streaming} onOpenUrl={onOpenUrl} />,
  );
  return { container, onOpenUrl };
}

test("raw HTML in a reply is text, not markup", () => {
  const { container } = show('before <img src="https://evil.example/p.png" onerror="x"> after');
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain('<img src="https://evil.example/p.png" onerror="x">');
});

test("a script tag in a reply never becomes an element", () => {
  const { container } = show("<script>window.__pwned = 1</script>");
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain("window.__pwned = 1");
});

test("http, https and mailto links are anchors; every other scheme stays text", () => {
  const { container } = show(
    "[a](https://example.com) [b](http://example.com) [c](mailto:x@example.com) " +
      "[d](javascript:alert(1)) [e](vscode://file/C:/x) [f](data:text/html,<b>)",
  );
  const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href"));
  expect(hrefs).toEqual(["https://example.com", "http://example.com", "mailto:x@example.com"]);
  // The refused ones are still readable: the user sees what the model wrote.
  expect(container.textContent).toContain("javascript:alert(1)");
  expect(container.textContent).toContain("vscode://file/C:/x");
});

/* Added 2026-09-13 (no assertion changed): the renderer and the server have
   to agree about what a link is, and this case is where they did not. The server's open.url
   wants the literal "http://"; the renderer used to accept any "http:" scheme, so this link was
   drawn and then silently dropped on click. */
test("a scheme with no authority is not a link, because the server would refuse to open it", () => {
  const { container } = show("[x](http:evil) [y](HTTPS://Example.com/A)");
  const anchors = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href"));
  // The upper-case one still links: the server lower-cases before its own check, so it opens.
  expect(anchors).toEqual(["HTTPS://Example.com/A"]);
  expect(container.textContent).toContain("http:evil");
});

test("a middle click opens through the server too, instead of loading in the app's window", () => {
  const { container, onOpenUrl } = show("[docs](https://example.com/x)");
  const anchor = container.querySelector("a") as HTMLAnchorElement;
  const event = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
  fireEvent(anchor, event);
  expect(onOpenUrl).toHaveBeenCalledWith("https://example.com/x");
  expect(event.defaultPrevented).toBe(true);
});

test("a link opens through the server instead of navigating the page", () => {
  const { container, onOpenUrl } = show("[docs](https://example.com/x)");
  const anchor = container.querySelector("a") as HTMLAnchorElement;
  expect(anchor.getAttribute("rel")).toBe("noreferrer noopener");
  const event = new MouseEvent("click", { bubbles: true, cancelable: true });
  fireEvent(anchor, event);
  expect(onOpenUrl).toHaveBeenCalledWith("https://example.com/x");
  expect(event.defaultPrevented).toBe(true);
});

test("an image in a reply renders as its alt text, so nothing is fetched", () => {
  const { container } = show("![a diagram](https://evil.example/p.png)");
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain("[a diagram]");
});

test("code keeps its text verbatim and is not highlighted", () => {
  const { container } = show("```py\nprint('<b>hi</b>')\n```\n\nand `x < y` inline");
  const code = container.querySelector(".md-code") as HTMLElement;
  expect(code.textContent).toContain("print('<b>hi</b>')");
  expect(code.querySelector("span")).toBeNull(); // no highlighter spans
  expect(container.querySelector(".md-inline-code")?.textContent).toBe("x < y");
});

test("GitHub markdown and Japanese emphasis both render", () => {
  const { container } = show("| a | b |\n| - | - |\n| 1 | 2 |\n\n- [x] done\n\n**牛乳**を買う");
  expect(container.querySelector("table")).not.toBeNull();
  expect(container.querySelector('input[type="checkbox"]')).not.toBeNull();
  expect(screen.getByText("牛乳").tagName).toBe("STRONG");
});

test("a streaming reply ends in the blinking caret; a finished one does not", () => {
  expect(show("one two", true).container.querySelector(".caret")).not.toBeNull();
  expect(show("one two", false).container.querySelector(".caret")).toBeNull();
});
