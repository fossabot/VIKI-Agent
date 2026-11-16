import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ContextReport } from "../../clients/conversations/contextApi";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { ContextBar, contextTone } from "./ContextBar";
import { ContextItems } from "./ContextItems";
import { ContextMeter } from "./ContextMeter";

afterEach(cleanup);

function report(overrides: Partial<ContextReport> = {}): ContextReport {
  return {
    observed_at: "2026-10-09T00:00:00.000Z",
    total_tokens: 100,
    max_tokens: 1000,
    raw_max_tokens: 1000,
    percentage: 10,
    auto_compact: null,
    categories: [],
    memory_files: [],
    mcp_servers: [],
    skills: null,
    agents: [],
    reasons: {},
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    ...overrides,
  };
}

test("breakdown swatches and shares match the segmented bar, including unknown categories", () => {
  const categories = [
    "System prompt",
    "System tools",
    "MCP tools",
    "Memory files",
    "Skills",
    "Messages",
    "New category",
  ].map((name, index) => ({ name, tokens: (index + 1) * 10, kind: "used" as const }));
  const { container } = render(
    <>
      <ContextBar
        label="Used context"
        max={1000}
        used={categories.map((category) => ({ label: category.name, tokens: category.tokens }))}
      />
      <ContextItems report={report({ categories })} />
    </>,
  );
  const segments = [...container.querySelectorAll<HTMLElement>(".context-bar-segment")];
  const rows = screen.getAllByRole("row").slice(1);
  for (const [index, row] of rows.entries()) {
    const swatch = row.querySelector<HTMLElement>(".context-category-swatch");
    const fill = row.querySelector<HTMLElement>(".context-category-fill");
    expect(swatch?.style.background).toBe(segments[index]?.style.background);
    expect(fill?.style.background).toBe(segments[index]?.style.background);
    expect(parseFloat(fill?.style.width ?? "")).toBeCloseTo(index + 1);
    expect(within(row).getByText(`${index + 1}%`)).toBeTruthy();
    expect(within(row).getByText(String((index + 1) * 10))).toBeTruthy();
    expect(swatch?.getAttribute("aria-hidden")).toBe("true");
    expect(fill?.parentElement?.getAttribute("aria-hidden")).toBe("true");
  }
  expect(segments[6]?.style.background).toBe("var(--accent-slate)");
  expect(contextTone("constructor")).toBe("--accent-slate");
  expect(contextTone("__proto__")).toBe("--accent-slate");
});

test("non-used rows retain counts and notes without consumption fills or category colors", () => {
  const { container } = render(
    <ContextItems
      report={report({
        categories: [
          { name: "Messages", tokens: 1500, kind: "used" },
          { name: "MCP tools", tokens: 100, kind: "deferred" },
          { name: "Buffer", tokens: 200, kind: "buffer" },
          { name: "Free", tokens: 300, kind: "free" },
        ],
      })}
    />,
  );
  const rows = screen.getAllByRole("row").slice(1);
  expect(within(rows[0] as HTMLElement).getByText("150%")).toBeTruthy();
  expect(container.querySelector<HTMLElement>(".context-category-fill")?.style.width).toBe("100%");
  for (const row of rows.slice(1)) {
    expect(row.querySelector(".context-category-track")).toBeNull();
    expect(row.querySelector<HTMLElement>(".context-category-swatch")?.style.background).toBe("");
  }
  screen.getByText("loaded only when used");
  screen.getByText("kept free for auto-compact");
  screen.getByText("free");
  for (const count of ["100", "200", "300"]) screen.getByText(count);
});

test.each([null, 0])("an unknown or empty maximum (%s) draws no category share", (max) => {
  const { container } = render(
    <ContextItems
      report={report({
        max_tokens: max,
        categories: [{ name: "Messages", tokens: 100, kind: "used" }],
      })}
    />,
  );
  expect(container.querySelector(".context-category-track")).toBeNull();
  screen.getByText("—");
  screen.getByText("100");
});

test("the popover uses the same category tones and keeps partial totals unknown", async () => {
  const get = vi.fn().mockResolvedValue(
    report({
      total_tokens: null,
      max_tokens: null,
      categories: [
        { name: "Messages", tokens: 50, kind: "used" },
        { name: "MCP tools", tokens: 100, kind: "deferred" },
        { name: "New category", tokens: 10, kind: "used" },
      ],
    }),
  );
  render(<ContextMeter percent={5} sessionId="chat" api={{ get }} onOpenPage={vi.fn()} />);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "context 5%" }));
  });
  const dialog = screen.getByRole("dialog", { name: "Context" });
  within(dialog).getByText("— of — tokens");
  within(dialog).getByText("Messages 50 · New category 10");
  const swatches = [...dialog.querySelectorAll<HTMLElement>(".context-popover-swatch")];
  expect(swatches.map((swatch) => swatch.style.background)).toEqual([
    "var(--or)",
    "var(--accent-slate)",
  ]);
  expect(dialog.querySelector(".context-bar-segment")).toBeNull();
});

test("local category aliases resolve six distinct semantic colors in both themes", () => {
  const css = readCss(import.meta.url, "ContextBar.css");
  const tokens = readCss(import.meta.url, "../../styles/tokens.css");
  const aliases = [
    ...ruleIn(css, ".context-category-colors").matchAll(/--[\w-]+:\s*var\((--context-[\w-]+)\)/g),
  ].map((match) => match[1]);
  expect(aliases).toHaveLength(6);
  for (const selector of [
    ".context-category-colors",
    '[data-theme="light"] .context-category-colors',
  ]) {
    const palette = new Map(
      [...ruleIn(tokens, selector).matchAll(/(--context-[\w-]+):\s*(#[\da-f]{6})/g)].map(
        (match) => [match[1], match[2]],
      ),
    );
    expect(palette.size).toBe(6);
    for (const alias of aliases) expect(palette.has(alias)).toBe(true);
    expect(new Set(aliases.map((alias) => palette.get(alias))).size).toBe(6);
  }
});

test("non-used patterns and forced-color boundaries accompany the readable labels", () => {
  const css = readCss(import.meta.url, "ContextItems.css");
  expect(ruleIn(css, '.context-items [data-kind="buffer"] .context-category-swatch')).toContain(
    "repeating-linear-gradient",
  );
  expect(ruleIn(css, '.context-items [data-kind="deferred"] .context-category-swatch')).toContain(
    "dashed",
  );
  expect(ruleIn(css, '.context-items [data-kind="free"] .context-category-swatch')).toContain(
    "border-color",
  );
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".context-category-swatch")).toContain("CanvasText");
  expect(ruleIn(forced, ".context-category-track")).toContain("CanvasText");
  expect(ruleIn(forced, ".context-category-fill")).toContain("Highlight");
});
