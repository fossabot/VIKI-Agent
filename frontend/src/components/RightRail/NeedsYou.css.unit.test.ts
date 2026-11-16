import { expect, test } from "vitest";
import { readCss } from "../../testing/css";

const css = readCss(import.meta.url, "NeedsYou.css");

const rules = [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap(
  ([, selectors = "", declarations = ""], order) =>
    selectors.trimStart().startsWith("@")
      ? []
      : selectors
          .split(",")
          .map((selector) => ({ selector: selector.trim(), declarations, order })),
);

function specificity(selector: string): number {
  return (selector.match(/[.#:][\w-]+/g) ?? []).length;
}

function matches(selector: string, hovered: boolean, focused = false): boolean {
  if (selector.includes(":hover") && !hovered) return false;
  if (selector.includes(":focus-visible") && !focused) return false;
  const parts = selector
    .replace(/:[\w-]+/g, "")
    .trim()
    .split(/\s+/);
  const ancestors = [
    new Set(["needs-you-band", "needs-you-band--urgent"]),
    new Set(["rail-section-head"]),
    new Set(["need-open"]),
  ];
  let ancestor = ancestors.length - 1;
  for (const part of parts.reverse()) {
    const classes = [...part.matchAll(/\.([\w-]+)/g)].map(([, name]) => name ?? "");
    while (ancestor >= 0) {
      const candidate = ancestors[ancestor];
      if (candidate && classes.every((name) => candidate.has(name))) break;
      ancestor--;
    }
    if (ancestor < 0) return false;
    ancestor--;
  }
  return true;
}

function winningValue(
  property: "color" | "outline",
  hovered = false,
  focused = false,
): string | undefined {
  let winner: { color: string; specificity: number; order: number } | undefined;
  for (const rule of rules) {
    if (!matches(rule.selector, hovered, focused)) continue;
    const value = rule.declarations
      .match(new RegExp(`(?:^|;)\\s*${property}:\\s*([^;]+);?`))?.[1]
      ?.trim();
    if (!value) continue;
    const next = { color: value, specificity: specificity(rule.selector), order: rule.order };
    if (
      !winner ||
      next.specificity > winner.specificity ||
      (next.specificity === winner.specificity && next.order > winner.order)
    ) {
      winner = next;
    }
  }
  return winner?.color;
}

test("urgent and proposal-only bands have distinct count styling", () => {
  expect(css).toMatch(/\.needs-you-band--urgent[^{}]*\{[^}]*--or/);
  expect(css).toMatch(/\.needs-you-band--urgent \.lbl[^{}]*\{[^}]*--on-or/);
  expect(css).toMatch(/\.needs-you-band--urgent[^{}]*\.attention-count/);
  expect(css).toMatch(/\.needs-you-band--subdued[^{}]*\.rail-section-count--dim/);
});

test("urgent Open Approvals keeps on-or as the winning normal and hover color", () => {
  expect(winningValue("color")).toBe("var(--on-or)");
  expect(winningValue("color", true)).toBe("var(--on-or)");
});

test("forced-colors focus keeps Highlight as the winning outline", () => {
  expect(winningValue("outline", false, true)).toBe("2px solid Highlight");
});

test("later rises pulse two rings, with reduced-motion and forced-colors fallbacks", () => {
  expect(css).toMatch(/\.needs-you-pulse::before/);
  expect(css).toMatch(/\.needs-you-pulse::after/);
  expect(css).toMatch(/@keyframes needs-you-ring/);
  expect(css).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.needs-you-pulse/);
  expect(css).toMatch(/html\[data-motion="reduce"\]\s+\.needs-you-pulse/);
  expect(css).toMatch(/@media\s*\(forced-colors:\s*active\)[\s\S]*?\.needs-you-band/);
});
