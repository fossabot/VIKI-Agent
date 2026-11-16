import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CodeDiff, CodeFile } from "../../clients/code/codeApi";
import { DiffView } from "./DiffView";
import { FileView } from "./FileView";

afterEach(cleanup);

function file(over: Partial<CodeFile> = {}): CodeFile {
  return {
    path: "a.py",
    size: 12,
    sha256: "x",
    encoding: "utf-8",
    newline: "crlf",
    text: "one\r\ntwo\r\nthree\r\n",
    reason: null,
    ...over,
  };
}

function setup(over: Partial<CodeFile> = {}, canQuote = true) {
  const onQuote = vi.fn();
  const onOpen = vi.fn();
  const view = render(
    <FileView file={file(over)} canQuote={canQuote} onQuote={onQuote} onOpen={onOpen} />,
  );
  return { onQuote, onOpen, view };
}

const line = (n: number) => screen.getByText(String(n));

test("header, three numbered lines, no phantom last line", () => {
  const { view } = setup();
  expect(screen.getByText("utf-8").className).toContain("mono");
  expect(screen.getByText("CRLF")).toBeTruthy();
  expect(screen.getByText("12 bytes")).toBeTruthy();
  expect(view.container.querySelectorAll(".code-line").length).toBe(3);
});

test("click selects, shift-click extends, quote and open use it", () => {
  const { onQuote, onOpen } = setup();
  fireEvent.click(line(3));
  fireEvent.click(line(1), { shiftKey: true });
  fireEvent.click(screen.getByText("Quote in chat"));
  expect(onQuote).toHaveBeenCalledWith("a.py", 1, 3);
  fireEvent.click(screen.getByText("Open in VS Code"));
  expect(onOpen).toHaveBeenCalledWith(1);
});

test("open without a selection passes null; quote is disabled with its hint", () => {
  const { onQuote, onOpen } = setup();
  fireEvent.click(screen.getByText("Open in VS Code"));
  expect(onOpen).toHaveBeenCalledWith(null);
  const quote = screen.getByText("Quote in chat");
  expect(quote.getAttribute("aria-disabled")).toBe("true");
  expect(quote.getAttribute("title")).toBe("Select the lines to quote first");
  fireEvent.click(quote);
  expect(onQuote).not.toHaveBeenCalled();
});

test("cannot-quote hint wins and blocks the click", () => {
  const { onQuote } = setup({}, false);
  fireEvent.click(line(2));
  const quote = screen.getByText("Quote in chat");
  expect(quote.getAttribute("title")).toBe("Only files in the active code folder can be quoted");
  fireEvent.click(quote);
  expect(onQuote).not.toHaveBeenCalled();
});

test("a different file clears the selection", () => {
  const onQuote = vi.fn();
  const props = { canQuote: true, onQuote, onOpen: vi.fn() };
  const { rerender } = render(<FileView file={file()} {...props} />);
  fireEvent.click(line(2));
  rerender(<FileView file={file({ path: "b.py" })} {...props} />);
  fireEvent.click(screen.getByText("Quote in chat"));
  expect(onQuote).not.toHaveBeenCalled();
});

test("a reason shows words and no text", () => {
  const { view } = setup({ text: null, reason: "too_large", newline: "mixed" });
  expect(screen.getByText("This file is over 1 MiB, so it is not shown.")).toBeTruthy();
  expect(screen.getByText("Mixed")).toBeTruthy();
  expect(view.container.querySelector(".code-lines")).toBeNull();
});

test("a script in a file renders as text", () => {
  const { view } = setup({ text: "<script>alert(1)</script>" });
  expect(view.container.querySelector("script")).toBeNull();
  expect(view.container.querySelector(".code-text")?.textContent).toBe("<script>alert(1)</script>");
});

test("diff classes by line kind", () => {
  const diff: CodeDiff = {
    path: "a.py",
    reason: null,
    text: "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n ctx\n",
  };
  const { container } = render(<DiffView diff={diff} />);
  const classes = [...container.querySelectorAll("pre")].map((p) => p.className);
  expect(classes).toEqual([
    "diff-meta",
    "diff-meta",
    "diff-meta",
    "diff-hunk",
    "diff-del",
    "diff-add",
    "diff-ctx",
  ]);
});

test("hunk lines are classified by first character, headers only before the first hunk", () => {
  const diff: CodeDiff = {
    path: "a.sql",
    reason: null,
    text: [
      "diff --git a/x b/x",
      "--- a/x",
      "+++ b/x",
      "@@ -1,2 +1,2 @@",
      "--- removed comment",
      "+++ added",
      " ctx",
      "\\ No newline at end of file",
      "diff --git a/y b/y",
      "--- a/y",
    ].join("\n"),
  };
  const { container } = render(<DiffView diff={diff} />);
  expect([...container.querySelectorAll("pre")].map((p) => p.className)).toEqual([
    "diff-meta",
    "diff-meta",
    "diff-meta",
    "diff-hunk",
    "diff-del",
    "diff-add",
    "diff-ctx",
    "diff-meta",
    "diff-meta",
    "diff-meta",
  ]);
});

test("the line area is one tab stop; arrows move and extend the selection", () => {
  const { view, onQuote } = setup();
  const area = view.container.querySelector(".code-lines") as HTMLElement;
  expect(view.container.querySelectorAll("[tabindex='0']").length).toBe(1);
  expect(area.getAttribute("tabindex")).toBe("0");
  fireEvent.keyDown(area, { key: "ArrowDown" });
  fireEvent.keyDown(area, { key: "ArrowDown" });
  const selected = () =>
    [...view.container.querySelectorAll(".code-line-selected")].map((r) => r.textContent);
  expect(selected()).toEqual(["2two"]);
  expect(area.getAttribute("aria-activedescendant")).toBe(
    view.container.querySelectorAll(".code-line")[1]?.id,
  );
  fireEvent.keyDown(area, { key: "ArrowDown", shiftKey: true });
  expect(selected()).toEqual(["2two", "3three"]);
  fireEvent.keyDown(area, { key: "Home" });
  expect(selected()).toEqual(["1one"]);
  fireEvent.keyDown(area, { key: "End", shiftKey: true });
  fireEvent.click(screen.getByText("Quote in chat"));
  expect(onQuote).toHaveBeenCalledWith("a.py", 1, 3);
});

test("diff reason in words; script in a diff renders as text", () => {
  const { container, rerender } = render(
    <DiffView diff={{ path: "a", text: null, reason: "no_diff" }} />,
  );
  expect(screen.getByText("No changes against HEAD.")).toBeTruthy();
  rerender(<DiffView diff={{ path: "a", text: "+<script>x</script>", reason: null }} />);
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("pre")?.textContent).toBe("+<script>x</script>");
});
