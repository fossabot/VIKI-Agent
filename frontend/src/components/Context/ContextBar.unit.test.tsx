import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { ContextBar } from "./ContextBar";

afterEach(cleanup);

function segmentsOf(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll(".context-bar-segment"));
}

test("each used segment's width is its own share of max, capped at 100", () => {
  const { container } = render(
    <ContextBar
      label="context"
      max={100}
      used={[
        { label: "System prompt", tokens: 25 },
        { label: "Messages", tokens: 150 },
      ]}
    />,
  );
  const segments = segmentsOf(container);
  expect(segments).toHaveLength(2);
  expect(parseFloat(segments[0]?.style.width ?? "")).toBeCloseTo(25);
  expect(parseFloat(segments[1]?.style.width ?? "")).toBeCloseTo(100);
});

test("tone follows the label table, falling back to the slate accent for anything else", () => {
  const { container } = render(
    <ContextBar
      label="context"
      max={100}
      used={[
        { label: "System prompt", tokens: 10 },
        { label: "System tools", tokens: 10 },
        { label: "MCP tools", tokens: 10 },
        { label: "Memory files", tokens: 10 },
        { label: "Skills", tokens: 10 },
        { label: "Messages", tokens: 10 },
        { label: "Something new", tokens: 10 },
      ]}
    />,
  );
  const segments = segmentsOf(container);
  expect(segments.map((segment) => segment.style.background)).toEqual([
    "var(--accent-slate)",
    "var(--accent-blue)",
    "var(--accent-purple)",
    "var(--or-hi)",
    "var(--accent-green)",
    "var(--or)",
    "var(--accent-slate)",
  ]);
});

test("a zero-token segment is omitted, not drawn as an empty sliver", () => {
  const { container } = render(
    <ContextBar
      label="context"
      max={100}
      used={[
        { label: "System prompt", tokens: 0 },
        { label: "Messages", tokens: 40 },
      ]}
    />,
  );
  const segments = segmentsOf(container);
  expect(segments).toHaveLength(1);
  expect(parseFloat(segments[0]?.style.width ?? "")).toBeCloseTo(40);
});

test("the reserved buffer draws as its own block, sized as a share of max", () => {
  const { container } = render(<ContextBar label="context" max={200} used={[]} reserved={50} />);
  const reserved = container.querySelector<HTMLElement>(".context-bar-reserved");
  expect(reserved).not.toBeNull();
  expect(parseFloat(reserved?.style.width ?? "")).toBeCloseTo(25);
});

test("no reserved block when reserved is absent, null or zero", () => {
  const { container: withoutProp } = render(<ContextBar label="context" max={100} used={[]} />);
  expect(withoutProp.querySelector(".context-bar-reserved")).toBeNull();

  const { container: withNull } = render(
    <ContextBar label="context" max={100} used={[]} reserved={null} />,
  );
  expect(withNull.querySelector(".context-bar-reserved")).toBeNull();

  const { container: withZero } = render(
    <ContextBar label="context" max={100} used={[]} reserved={0} />,
  );
  expect(withZero.querySelector(".context-bar-reserved")).toBeNull();
});

test("the mark sits at its own share of max and stays out of the accessible tree", () => {
  const { container } = render(<ContextBar label="context" max={100} used={[]} mark={80} />);
  const mark = container.querySelector<HTMLElement>(".context-bar-mark");
  expect(mark).not.toBeNull();
  expect(parseFloat(mark?.style.left ?? "")).toBeCloseTo(80);
  expect(mark?.getAttribute("aria-hidden")).toBe("true");
});

test("no mark when mark is absent or null", () => {
  const { container } = render(<ContextBar label="context" max={100} used={[]} mark={null} />);
  expect(container.querySelector(".context-bar-mark")).toBeNull();
});

test("max <= 0 draws no segments, reserved block or mark, but keeps the labelled bar", () => {
  const { container } = render(
    <ContextBar
      label="context unavailable"
      max={0}
      used={[{ label: "Messages", tokens: 10 }]}
      reserved={5}
      mark={1}
    />,
  );
  expect(segmentsOf(container)).toHaveLength(0);
  expect(container.querySelector(".context-bar-reserved")).toBeNull();
  expect(container.querySelector(".context-bar-mark")).toBeNull();
  const root = container.querySelector(".context-bar");
  expect(root?.getAttribute("role")).toBe("img");
  expect(root?.getAttribute("aria-label")).toBe("context unavailable");
});

test("role, label and the size class (large by default, small when asked)", () => {
  const { container: large } = render(
    <ContextBar label="128,000 of 200,000 tokens" max={1} used={[]} />,
  );
  const largeRoot = large.querySelector(".context-bar");
  expect(largeRoot?.getAttribute("role")).toBe("img");
  expect(largeRoot?.getAttribute("aria-label")).toBe("128,000 of 200,000 tokens");
  expect(largeRoot?.classList.contains("context-bar-large")).toBe(true);

  const { container: small } = render(
    <ContextBar label="context" max={1} used={[]} size="small" />,
  );
  expect(small.querySelector(".context-bar")?.classList.contains("context-bar-small")).toBe(true);
});

const css = readCss(import.meta.url, "ContextBar.css");

test("the track is var(--track), sized 64x3 small and 12px-tall full-width large, like the status bar's own meter", () => {
  expect(ruleIn(css, ".context-bar")).toMatch(/background:\s*var\(--track\)/);
  expect(ruleIn(css, ".context-bar-small")).toMatch(/width:\s*64px/);
  expect(ruleIn(css, ".context-bar-small")).toMatch(/height:\s*3px/);
  expect(ruleIn(css, ".context-bar-large")).toMatch(/width:\s*100%/);
  expect(ruleIn(css, ".context-bar-large")).toMatch(/height:\s*12px/);
});

test("segment and reserved widths transition at 400ms, dropped under reduced motion", () => {
  expect(ruleIn(css, ".context-bar-segment")).toMatch(/transition:\s*width 400ms/);
  expect(ruleIn(css, ".context-bar-reserved")).toMatch(/transition:\s*width 400ms/);
  const reduced = narrowBlock(css, "@media (prefers-reduced-motion: reduce)");
  expect(ruleIn(reduced, ".context-bar-segment")).toMatch(/transition:\s*none/);
  expect(ruleIn(reduced, ".context-bar-reserved")).toMatch(/transition:\s*none/);
});

test("under forced colours, segments gain a CanvasText right border and the mark turns Highlight", () => {
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".context-bar-segment")).toMatch(/border-right:\s*1px solid CanvasText/);
  expect(ruleIn(forced, ".context-bar-mark")).toMatch(/background:\s*Highlight/);
});
