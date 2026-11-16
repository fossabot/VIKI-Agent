import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { ContextReport } from "../../clients/conversations/contextApi";
import { ContextItems } from "./ContextItems";

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
    skills: { total: 0, included: 0, tokens: 0, items: [] },
    agents: [],
    reasons: {},
    hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
    ...overrides,
  };
}

it("each section is a labelled region with its own heading", () => {
  render(<ContextItems report={report()} />);
  for (const name of [
    "Breakdown",
    "Connectors (MCP servers)",
    "Skills",
    "Memory files",
    "Custom agents",
  ]) {
    expect(screen.getByRole("region", { name })).toBeTruthy();
    expect(screen.getByRole("heading", { name, level: 2 })).toBeTruthy();
  }
});

it("breakdown keeps CLI order (not token order) and shows each used row's share of max_tokens", () => {
  render(
    <ContextItems
      report={report({
        max_tokens: 1000,
        categories: [
          { name: "System prompt", tokens: 100, kind: "used" },
          { name: "Messages", tokens: 400, kind: "used" },
          { name: "MCP tools", tokens: 50, kind: "deferred" },
          { name: "Held back", tokens: 200, kind: "buffer" },
          { name: "Unspent", tokens: 250, kind: "free" },
        ],
      })}
    />,
  );
  const rows = screen.getAllByRole("row").slice(1); // drop the header row
  const cells = rows.map((row) =>
    Array.from(row.querySelectorAll("td")).map((td) => td.textContent),
  );
  expect(cells).toEqual([
    ["System prompt", "100", "10%"],
    ["Messages", "400", "40%"],
    ["MCP tools", "50", "loaded only when used"],
    ["Held back", "200", "kept free for auto-compact"],
    ["Unspent", "250", "free"],
  ]);
});

it("breakdown falls back to an em dash for a used row's share when max_tokens is null", () => {
  render(
    <ContextItems
      report={report({
        max_tokens: null,
        categories: [{ name: "System prompt", tokens: 100, kind: "used" }],
      })}
    />,
  );
  expect(screen.getByText("—")).toBeTruthy();
});

it("connectors: tools/loaded/tokens per server, sorted by loaded_tokens descending", () => {
  render(
    <ContextItems
      report={report({
        mcp_servers: [
          { server: "alpha", tools: 3, loaded: 1, loaded_tokens: 50 },
          { server: "beta", tools: 5, loaded: 2, loaded_tokens: 900 },
        ],
      })}
    />,
  );
  const names = screen.getAllByText(/^(alpha|beta)$/).map((el) => el.textContent);
  expect(names).toEqual(["beta", "alpha"]);
  expect(screen.getByText("5 tools · 2 loaded · 900 tokens")).toBeTruthy();
  expect(screen.getByText("3 tools · 1 loaded · 50 tokens")).toBeTruthy();
});

it("connectors: 0 loaded replaces the stats with the deferred note", () => {
  render(
    <ContextItems
      report={report({
        mcp_servers: [{ server: "gamma", tools: 4, loaded: 0, loaded_tokens: 0 }],
      })}
    />,
  );
  expect(screen.getByText("names only; counted in MCP tools (deferred)")).toBeTruthy();
  expect(screen.queryByText(/tools ·/)).toBeNull();
});

it("skills: the summary line, plugin vs source items, sorted by tokens descending", () => {
  render(
    <ContextItems
      report={report({
        skills: {
          total: 5,
          included: 2,
          tokens: 300,
          items: [
            { name: "small-skill", source: "project", plugin: null, tokens: 100 },
            { name: "big-skill", source: "user", plugin: "example-plugin", tokens: 200 },
          ],
        },
      })}
    />,
  );
  expect(screen.getByText("2 of 5 listed · 300 tokens")).toBeTruthy();
  const items = screen.getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "big-skill (plugin example-plugin) · 200 tokens",
    "small-skill (project) · 100 tokens",
  ]);
});

it("skills: the summary line omits tokens when the total cost is null", () => {
  render(
    <ContextItems
      report={report({ skills: { total: 1, included: 0, tokens: null, items: [] } })}
    />,
  );
  expect(screen.getByText("0 of 1 listed")).toBeTruthy();
});

it("memory files: name, type and tokens, sorted by tokens descending", () => {
  render(
    <ContextItems
      report={report({
        memory_files: [
          { name: "lessons.md", type: "project", tokens: 50 },
          { name: "CLAUDE.md", type: "project", tokens: 500 },
        ],
      })}
    />,
  );
  const items = screen.getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "CLAUDE.md · project · 500 tokens",
    "lessons.md · project · 50 tokens",
  ]);
});

it("memory files: a rerender with a new highest-token CLAUDE.md shows four fresh rows with no React key warning", () => {
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { rerender } = render(
    <ContextItems
      report={report({
        memory_files: [
          { name: "AGENTS.md", type: "project", tokens: 300 },
          { name: "CLAUDE.md", type: "project", tokens: 200 },
          { name: "CLAUDE.md", type: "user", tokens: 100 },
        ],
      })}
    />,
  );
  expect(errorSpy).not.toHaveBeenCalled();

  // A refresh finds a nested CLAUDE.md — same name, same "project" type as the existing one —
  // whose tokens now sort it first.
  rerender(
    <ContextItems
      report={report({
        memory_files: [
          { name: "CLAUDE.md", type: "project", tokens: 400 },
          { name: "AGENTS.md", type: "project", tokens: 300 },
          { name: "CLAUDE.md", type: "project", tokens: 200 },
          { name: "CLAUDE.md", type: "user", tokens: 100 },
        ],
      })}
    />,
  );
  expect(errorSpy).not.toHaveBeenCalled();

  const items = screen.getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "CLAUDE.md · project · 400 tokens",
    "AGENTS.md · project · 300 tokens",
    "CLAUDE.md · project · 200 tokens",
    "CLAUDE.md · user · 100 tokens",
  ]);

  errorSpy.mockRestore();
});

it("custom agents: type, source and tokens, sorted by tokens descending, or None when empty", () => {
  const { unmount } = render(
    <ContextItems
      report={report({
        agents: [
          { type: "Explore", source: "built-in", tokens: 40 },
          { type: "code-reviewer", source: "project", tokens: 900 },
        ],
      })}
    />,
  );
  const items = screen.getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "code-reviewer · project · 900 tokens",
    "Explore · built-in · 40 tokens",
  ]);
  unmount();

  render(<ContextItems report={report({ agents: [] })} />);
  expect(
    within(screen.getByRole("region", { name: "Custom agents" })).getByText("None"),
  ).toBeTruthy();
});

it("sorting is stable: equal-tokens items keep their report order", () => {
  render(
    <ContextItems
      report={report({
        agents: [
          { type: "first", source: "built-in", tokens: 10 },
          { type: "second", source: "built-in", tokens: 10 },
        ],
      })}
    />,
  );
  const items = screen.getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "first · built-in · 10 tokens",
    "second · built-in · 10 tokens",
  ]);
});

it("each reason reads its own exact sentence for its null part", () => {
  render(
    <ContextItems
      report={report({
        categories: null,
        mcp_servers: null,
        skills: null,
        memory_files: null,
        agents: null,
        reasons: {
          categories: "not_observed",
          mcp_servers: "absent",
          skills: "unrecognized",
          memory_files: "timeout",
          agents: "unavailable",
        },
      })}
    />,
  );
  expect(screen.getByText("No reply has ended yet in this Claude Code process.")).toBeTruthy();
  expect(screen.getByText("Claude Code did not report this.")).toBeTruthy();
  expect(
    screen.getByText("Claude Code reported this in a shape this app does not read."),
  ).toBeTruthy();
  expect(screen.getByText("The request timed out.")).toBeTruthy();
  expect(screen.getByText("This is unavailable.")).toBeTruthy();
});

it("hidden > 0 adds the filtered-or-capped note for that part", () => {
  render(
    <ContextItems
      report={report({
        categories: [{ name: "System prompt", tokens: 10, kind: "used" }],
        hidden: { categories: 2, memory_files: 0, mcp_servers: 0, skills: 0, agents: 3 },
      })}
    />,
  );
  expect(
    screen.getByText(
      "2 more not shown: their names look like secrets to the app's filter, or the list is past its cap.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "3 more not shown: their names look like secrets to the app's filter, or the list is past its cap.",
    ),
  ).toBeTruthy();
});
