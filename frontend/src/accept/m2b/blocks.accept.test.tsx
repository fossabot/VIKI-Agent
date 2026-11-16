/**
 * Acceptance (frozen): the thought row, the tool block and the routing row — the three
 * rows that make VIKI's work visible in the design.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { RoutingRow, ThoughtRow, ToolRows } from "../../components/Conversation/Blocks";
import type { RoutingBlock, ThinkingBlock, ToolBlock } from "../../state/view";
import {
  historyView,
  subagentDone,
  subagentStarted,
  textDone,
  thinkingDelta,
  thinkingDone,
  toolDone,
  toolStarted,
  turnStarted,
  userMessage,
  view,
} from "./fakes";

afterEach(cleanup);

function blockOf<T>(v: ReturnType<typeof view>, kind: string): T {
  const found = v.turns.at(-1)?.blocks.find((b) => b.kind === kind);
  if (!found) throw new Error(`no ${kind} block`);
  return found as T;
}

test("a thinking block still streaming shows its latest text on one line", () => {
  const v = view([userMessage("hi"), turnStarted, thinkingDelta("Reading the linter source note")]);
  const { container } = render(<ThoughtRow block={blockOf<ThinkingBlock>(v, "thinking")} />);
  const row = container.querySelector(".thought") as HTMLElement;
  expect(row.textContent).toContain("Thought");
  expect(row.textContent).toContain("Reading the linter source note");
  // The one-line truncation is CSS on this element (jsdom applies no stylesheet, so the class
  // is what the test can pin; Conversation.css carries the ellipsis rule).
  expect(row.querySelector(".thought-summary")).not.toBeNull();
});

test("a finished thinking block with no text reads Thought · 6s, and a history one has no duration", () => {
  const live = view([userMessage("hi"), turnStarted, thinkingDone("", 6000)]);
  expect(
    render(<ThoughtRow block={blockOf<ThinkingBlock>(live, "thinking")} />).container.textContent,
  ).toBe("Thought · 6s");
  cleanup();
  const old = historyView([userMessage("hi"), thinkingDone("", 6000)]);
  const row = old.turns.at(-1)?.blocks.find((b) => b.kind === "thinking") as ThinkingBlock;
  expect(row.durationMs).toBeNull();
  expect(render(<ThoughtRow block={row} />).container.textContent).toBe("Thought");
});

test("clicking a thought row opens its full text", () => {
  const v = view([userMessage("hi"), turnStarted, thinkingDone("line one\nline two", 3000)]);
  const { container } = render(<ThoughtRow block={blockOf<ThinkingBlock>(v, "thinking")} />);
  expect(container.querySelector(".thought-full")).toBeNull();
  fireEvent.click(container.querySelector(".thought") as HTMLElement);
  expect((container.querySelector(".thought-full") as HTMLElement).textContent).toBe(
    "line one\nline two",
  );
});

test("tool rows show head, rest and tail in the mock's three tones", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    toolStarted("t1", "Grep", { head: "rg", rest: "-il linter 40-sources", tail: "→ 7 files" }),
    toolDone("t1"),
  ]);
  const { container } = render(<ToolRows block={blockOf<ToolBlock>(v, "tools")} />);
  const row = container.querySelector(".tool-row") as HTMLElement;
  expect(row.querySelector(".tool-head")?.textContent).toBe("rg");
  expect(row.querySelector(".tool-rest")?.textContent).toBe("-il linter 40-sources");
  expect(row.querySelector(".tool-tail")?.textContent).toBe("→ 7 files");
});

test("a failed tool row is marked, and its preview opens on click", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    toolStarted("t1", "Bash", { head: "git", rest: "push" }),
    toolDone("t1", { isError: true, summary: "rejected", preview: "! [rejected] main -> main" }),
  ]);
  const { container } = render(<ToolRows block={blockOf<ToolBlock>(v, "tools")} />);
  const row = container.querySelector(".tool-row") as HTMLElement;
  expect(row.classList.contains("tool-row--error")).toBe(true);
  expect(container.querySelector(".tool-preview")).toBeNull();
  fireEvent.click(row);
  expect((container.querySelector(".tool-preview") as HTMLElement).textContent).toContain(
    "! [rejected] main -> main",
  );
});

test("a long tool block shows the last three rows while running, then +N more", () => {
  const items = [userMessage("hi"), turnStarted];
  for (let i = 1; i <= 5; i++) {
    items.push(toolStarted(`t${i}`, "Read", { head: "Read", rest: `note-${i}.md` }));
    if (i < 5) items.push(toolDone(`t${i}`));
  }
  const v = view(items);
  const { container } = render(<ToolRows block={blockOf<ToolBlock>(v, "tools")} />);
  const visible = [...container.querySelectorAll(".tool-row")].map((r) => r.textContent ?? "");
  expect(visible).toHaveLength(3);
  expect(visible[2]).toContain("note-5.md");
  expect(container.querySelector(".tool-more")?.textContent).toBe("+2 more");
  fireEvent.click(container.querySelector(".tool-more") as HTMLElement);
  expect(container.querySelectorAll(".tool-row")).toHaveLength(5);
  expect(container.querySelector(".tool-more")).toBeNull();
});

test("a finished tool block collapses to one line, and three rows need no line at all", () => {
  const items = [userMessage("hi"), turnStarted];
  for (let i = 1; i <= 5; i++) {
    items.push(toolStarted(`t${i}`, "Read", { head: "Read", rest: `note-${i}.md` }));
    items.push(toolDone(`t${i}`));
  }
  const done = view(items);
  const { container } = render(<ToolRows block={blockOf<ToolBlock>(done, "tools")} />);
  expect(container.querySelectorAll(".tool-row")).toHaveLength(0);
  expect(container.querySelector(".tool-more")?.textContent).toBe("+5 more");
  fireEvent.click(container.querySelector(".tool-more") as HTMLElement);
  expect(container.querySelectorAll(".tool-row")).toHaveLength(5);
  cleanup();

  const short = view([
    userMessage("hi"),
    turnStarted,
    toolStarted("t1", "Read", { head: "Read", rest: "a.md" }),
    toolDone("t1"),
    toolStarted("t2", "Read", { head: "Read", rest: "b.md" }),
  ]);
  const two = render(<ToolRows block={blockOf<ToolBlock>(short, "tools")} />);
  expect(two.container.querySelectorAll(".tool-row")).toHaveLength(2);
  expect(two.container.querySelector(".tool-more")).toBeNull();
});

test("a routing row carries the agent, the model, its reason and its own tools", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    subagentStarted("a1", "researcher", "opus", "Web横断＋一次資料確認"),
    toolStarted("t1", "WebSearch", { head: "WebSearch", rest: "markdown linting" }, "a1"),
    toolDone("t1"),
    textDone("done"),
  ]);
  const { container } = render(<RoutingRow block={blockOf<RoutingBlock>(v, "routing")} />);
  expect(container.querySelector(".routing-text")?.textContent).toBe(
    "▸ routing: researcher → opus",
  );
  /* AMENDMENT (2026-10-09). The reason is
     VIKI's own words, model data, and stays as written; the full-width brackets around it were
     the app's chrome, which the English-only interface now renders
     as ASCII brackets. */
  expect(container.querySelector(".routing-reason")?.textContent).toBe("(Web横断＋一次資料確認)");
  expect(container.querySelector(".face")?.getAttribute("data-shape")).toBe("circle");
  expect(container.querySelector(".tool-row")?.textContent).toContain("markdown linting");
});

test("a finished routing row drops the presence dot; a failed one says so", () => {
  const base = [userMessage("hi"), turnStarted, subagentStarted("a1", "librarian", "sonnet")];
  const ok = view([...base, subagentDone("a1", "completed")]);
  const { container } = render(<RoutingRow block={blockOf<RoutingBlock>(ok, "routing")} />);
  expect(container.querySelector(".face-presence")).toBeNull();
  cleanup();
  const bad = view([...base, subagentDone("a1", "failed")]);
  expect(
    render(<RoutingRow block={blockOf<RoutingBlock>(bad, "routing")} />).container.textContent,
  ).toContain("· failed");
});

test("tool output is text, whatever it contains", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    toolStarted("t1", "Bash", { head: "cat", rest: '<img src=x onerror="window.__pwned=1">' }),
    toolDone("t1", { preview: "<script>window.__pwned=1</script>" }),
  ]);
  const { container } = render(<ToolRows block={blockOf<ToolBlock>(v, "tools")} />);
  fireEvent.click(container.querySelector(".tool-row") as HTMLElement);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1">');
  expect(container.textContent).toContain("<script>window.__pwned=1</script>");
});
