import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { StatusBar } from "./StatusBar";

afterEach(cleanup);

test("the status bar's declared height folds in the hairline (border-box, jsdom applies no CSS)", () => {
  const css = readCss(import.meta.url, "StatusBar.css");
  const rule = css.match(/\.statusbar\s*\{[^}]*\}/)?.[0];
  expect(rule).toMatch(/height:\s*27px/);
});

test("renders nothing but the region wrapper when every field is absent", () => {
  const { container } = render(<StatusBar />);
  const bar = container.querySelector('[data-region="statusbar"]');
  expect(bar).not.toBeNull();
  expect(bar?.textContent).toBe("");
});

test("shows only the fields it is given", () => {
  const { container } = render(<StatusBar vaultCounts={{ knowledge: 1, sources: 2, inbox: 0 }} />);
  const bar = container.querySelector('[data-region="statusbar"]');
  expect(bar?.textContent).toContain("1 knowledge");
  expect(bar?.textContent).not.toContain("last commit");
});

test("renders the context bar width proportionally", () => {
  const { container } = render(<StatusBar contextPercent={50} />);
  const fill = container.querySelector(".statusbar-context-fill") as HTMLElement;
  expect(fill.style.width).toBe("32px");
});

test("below 800px the bar wraps instead of clipping, and keeps 27px as its minimum", () => {
  const narrow = narrowBlock(readCss(import.meta.url, "StatusBar.css"));
  expect(narrow).toMatch(/flex-wrap:\s*wrap/);
  expect(narrow).toMatch(/height:\s*auto/);
  expect(narrow).toMatch(/min-height:\s*27px/);
  // Nothing in the S block hides a field; the fields the bar is given are the fields it shows.
  expect(narrow).not.toMatch(/display:\s*none/);
});

test("the context field carries a title explaining the estimate", () => {
  const { container } = render(<StatusBar contextPercent={50} />);
  const context = container.querySelector(".statusbar-context");
  expect(context?.getAttribute("title")).toBe("Estimated from the last reply's token usage");
});

test("the plan-limit slot renders right after the notice, before the context fallback", () => {
  const { container } = render(
    <StatusBar notice="offline · retrying" planLimit={<span data-region="limit">limit</span>} />,
  );
  const spans = [...container.querySelectorAll("span")];
  expect(spans.map((node) => node.textContent)).toEqual(["offline · retrying", "limit"]);
});

test("a given contextMeter replaces the static context span", () => {
  const { container } = render(
    <StatusBar contextPercent={50} contextMeter={<span data-region="meter">custom</span>} />,
  );
  expect(container.querySelector('[data-region="meter"]')?.textContent).toBe("custom");
  expect(container.querySelector(".statusbar-context")).toBeNull();
});

test("an absent contextMeter still falls back to the static context span", () => {
  const { container } = render(<StatusBar contextPercent={50} />);
  expect(container.querySelector(".statusbar-context")).not.toBeNull();
});

test("the limit span pushes right and cancels the fallback span's own margin", () => {
  const css = readCss(import.meta.url, "StatusBar.css");
  const limitRule = css.match(/\.statusbar-limit\s*\{[^}]*\}/)?.[0];
  expect(limitRule).toMatch(/margin-left:\s*auto/);
  const adjacentRule = css.match(/\.statusbar-limit \+ \.statusbar-context\s*\{[^}]*\}/)?.[0];
  expect(adjacentRule).toMatch(/margin-left:\s*0/);
});

test("the limit span is colored for its warning and rejected statuses", () => {
  const css = readCss(import.meta.url, "StatusBar.css");
  const warningRule = css.match(
    /\.statusbar-limit\[data-status="allowed_warning"\]\s*\{[^}]*\}/,
  )?.[0];
  expect(warningRule).toMatch(/color:\s*var\(--or\)/);
  const rejectedRule = css.match(/\.statusbar-limit\[data-status="rejected"\]\s*\{[^}]*\}/)?.[0];
  expect(rejectedRule).toMatch(/color:\s*var\(--err\)/);
});

test("the git slot renders first, before every other field", () => {
  const { container } = render(
    <StatusBar git={<span data-region="git">chip</span>} notice="offline" />,
  );
  const bar = container.querySelector('[data-region="statusbar"]');
  expect(bar?.firstElementChild?.getAttribute("data-region")).toBe("git");
});

test("the git chip is contained at every width, with a 20px label row (moved from TopBar.test.tsx)", () => {
  const css = readCss(import.meta.url, "StatusBar.css");
  expect(ruleIn(css, ".git-chip")).toMatch(/min-width:\s*0/);
  expect(ruleIn(css, ".git-chip")).toMatch(/overflow:\s*hidden/);
  expect(ruleIn(css, ".git-chip")).toMatch(/height:\s*20px/);
  expect(css).toMatch(/\.git-chip span:not\(\.git-dot\)\s*\{[^}]*text-overflow:\s*ellipsis/);
});
