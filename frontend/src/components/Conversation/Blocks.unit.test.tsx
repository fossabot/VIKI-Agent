/**
 * Unit tests pinning behaviour that blocks.accept.test.tsx leaves implicit: NoticeRow (no
 * frozen test covers it directly), the exact boundary of the tool-block collapsing rule at
 * exactly three rows once finished, and RoutingRow's `.routing-meta`/`.routing-summary`
 * line — the live/background/done word, its tool/token/time counts, and the model's summary.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { toolDone, toolStarted, turnStarted, userMessage, view } from "../../accept/m2b/fakes";
import type {
  NoticeBlock,
  RoutingBlock,
  ThinkingBlock,
  ToolBlock,
  ToolRowView,
} from "../../state/view";
import { NoticeRow, RoutingRow, ThoughtRow, ToolRows } from "./Blocks";

afterEach(cleanup);

test("an opened thought's full text is a sibling of the button, so it can be selected", () => {
  const block: ThinkingBlock = {
    kind: "thinking",
    id: "k1",
    text: "First, read CLAUDE.md.",
    done: true,
    durationMs: 6000,
  };
  const { container } = render(<ThoughtRow block={block} />);
  const button = container.querySelector(".thought") as HTMLElement;
  expect(button.getAttribute("aria-expanded")).toBe("false");

  fireEvent.click(button);
  const full = container.querySelector(".thought-full") as HTMLElement;
  expect(full.textContent).toBe("First, read CLAUDE.md.");
  // Inside the button, every drag over this text would be a click on the toggle.
  expect(button.contains(full)).toBe(false);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(full.id).toBe(button.getAttribute("aria-controls"));
  expect(full.id).not.toBe("");
});

/* The frozen notices.accept.test.tsx owns the notice's wording and markup. What is tested
   here is what that test does not say: the two structural facts the row depends on. */

test("the mark column is decorative: a screen reader hears the sentence, not the glyph", () => {
  const block: NoticeBlock = {
    kind: "notice",
    id: "hook-1",
    tone: "hook",
    subject: "pre-commit",
    detail: "vault write blocked outside a session",
  };
  const { container } = render(<NoticeRow block={block} />);
  const mark = container.querySelector(".notice-mark") as HTMLElement;
  expect(mark.getAttribute("aria-hidden")).toBe("true");
  expect(mark.textContent).toBe("⊘");
});

test("the head and its tail share one span, so a long reason wraps as one sentence", () => {
  const block: NoticeBlock = {
    kind: "notice",
    id: "d-1",
    tone: "denied",
    subject: "Bash",
    detail: "auto mode does not allow this command",
  };
  const { container } = render(<NoticeRow block={block} />);
  const text = container.querySelector(".notice-text") as HTMLElement;
  expect(text.textContent).toBe("auto mode blocked Bash · auto mode does not allow this command");
  expect(text.querySelector(".notice-tail")).not.toBeNull();
});

test("a compacted notice with no tail shows no dangling separator", () => {
  const block: NoticeBlock = {
    kind: "notice",
    id: "c-1",
    tone: "compacted",
    subject: "",
    detail: "",
  };
  const { container } = render(<NoticeRow block={block} />);
  expect(container.querySelector(".notice-tail")).toBeNull();
  expect(container.querySelector(".notice-text")?.textContent).toBe("context compacted");
});

function toolBlockOf(v: ReturnType<typeof view>): ToolBlock {
  const found = v.turns.at(-1)?.blocks.find((b) => b.kind === "tools");
  if (!found) throw new Error("no tools block");
  return found as ToolBlock;
}

test("exactly three rows, all done, still show every row: three is not 'more than three'", () => {
  const v = view([
    userMessage("hi"),
    turnStarted,
    toolStarted("t1", "Read", { head: "Read", rest: "a.md" }),
    toolDone("t1"),
    toolStarted("t2", "Read", { head: "Read", rest: "b.md" }),
    toolDone("t2"),
    toolStarted("t3", "Read", { head: "Read", rest: "c.md" }),
    toolDone("t3"),
  ]);
  const { container } = render(<ToolRows block={toolBlockOf(v)} />);
  expect(container.querySelectorAll(".tool-row")).toHaveLength(3);
  expect(container.querySelector(".tool-more")).toBeNull();
});

/* RoutingRow's `.routing-meta`/`.routing-summary` — built as plain objects rather than
   through fakes.ts/view(), which pins only the shape the frozen accept tests use (no
   toolUses/totalTokens/durationMs/summary on subagentDone) and must not change. */

function routingBlock(overrides: Partial<RoutingBlock> = {}): RoutingBlock {
  return {
    kind: "routing",
    id: "a1",
    agent: "researcher",
    model: "opus",
    description: "Find daily note template",
    reason: "",
    status: "running",
    tools: [],
    ...overrides,
  };
}

function toolRow(id: string): ToolRowView {
  return {
    id,
    tool: "Read",
    row: { head: "Read", rest: "note.md", tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
  };
}

test("a running routing row's meta line shows the description and Running, with its live tool count", () => {
  const block = routingBlock({ tools: [toolRow("x1")] });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Running · 1 tool use",
  );
  expect(container.querySelector(".routing-summary")).toBeNull();
});

test("still running after its own turn ends, the row reads Running in background", () => {
  const block = routingBlock({ tools: [toolRow("x1"), toolRow("x2")] });
  const { container } = render(<RoutingRow block={block} background={true} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Running in background · 2 tool uses",
  );
});

test("a finished row reads Done with the CLI's tool count, tokens and time", () => {
  const block = routingBlock({
    status: "completed",
    toolUses: 2,
    totalTokens: 19700,
    durationMs: 9400,
  });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 2 tool uses · 19.7k tokens · 9.4s",
  );
});

test("a finished row with no CLI numbers counts its nested rows instead, with no tokens or time", () => {
  const block = routingBlock({ status: "completed", tools: [toolRow("x1"), toolRow("x2")] });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 2 tool uses",
  );
});

// A row rebuilt from history carries neither `toolUses` (the history path closes a block
// with {block_id, status} only) nor any nested row (a subagent's tools live in a separate
// transcript, never replayed into this one) — it must not fall back to `tools.length` and claim
// "0 tool uses" for a run that may have used several.
test("a row rebuilt from history has no toolUses and no nested rows: it reads Done alone, never a false zero", () => {
  const block = routingBlock({ status: "completed", tools: [] });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done",
  );
});

test("a running row with no tool yet shows no tool count either", () => {
  const block = routingBlock({ status: "running", tools: [] });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Running",
  );
});

test("tokens under 1000 show plain, and a minute or more shows minutes and whole seconds", () => {
  const block = routingBlock({
    status: "completed",
    toolUses: 1,
    totalTokens: 500,
    durationMs: 125_000,
  });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 1 tool use · 500 tokens · 2m 5s",
  );
});

// `elapsed()` must round the millisecond count exactly once. Rounding the sub-second part and
// the whole-second part separately would let each one round up to a false "60.0s" or "1m 60s" at
// a minute boundary. 59_949 and 119_499 ms sit just below a boundary and must read unchanged;
// 59_950 and 119_500 ms sit at the boundary that two-step rounding would overshoot.
test("59_949 ms stays 59.9s: the tenths place must not round up past the boundary", () => {
  const block = routingBlock({ status: "completed", toolUses: 1, durationMs: 59_949 });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 1 tool use · 59.9s",
  );
});

test("59_950 ms rounds to a full minute, never a false 60.0s", () => {
  const block = routingBlock({ status: "completed", toolUses: 1, durationMs: 59_950 });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 1 tool use · 1m 0s",
  );
});

test("119_499 ms stays 1m 59s: the whole-second part must not round up past the boundary", () => {
  const block = routingBlock({ status: "completed", toolUses: 1, durationMs: 119_499 });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 1 tool use · 1m 59s",
  );
});

test("119_500 ms rounds to the next minute, never a false 1m 60s", () => {
  const block = routingBlock({ status: "completed", toolUses: 1, durationMs: 119_500 });
  const { container } = render(<RoutingRow block={block} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — Done · 1 tool use · 2m 0s",
  );
});

test("a failed or stopped row adds no state word, just the counts", () => {
  const failed = routingBlock({ status: "failed", toolUses: 2 });
  const { container } = render(<RoutingRow block={failed} />);
  expect(container.querySelector(".routing-meta")?.textContent).toBe(
    "Find daily note template — 2 tool uses",
  );
  cleanup();
  const stopped = routingBlock({ status: "stopped", tools: [toolRow("x1")] });
  expect(
    render(<RoutingRow block={stopped} />).container.querySelector(".routing-meta")?.textContent,
  ).toBe("Find daily note template — 1 tool use");
});

/* --- a block-level jump's own row -------------------------------------------------------------- */

test("a routing row's target outlines the row, keeps it out of tab order, and expands its own tool rows fully", () => {
  const block = routingBlock({
    status: "completed",
    tools: [toolRow("x1"), toolRow("x2"), toolRow("x3"), toolRow("x4"), toolRow("x5")],
  });
  const { container } = render(<RoutingRow block={block} target={true} />);
  const row = container.querySelector(".routing") as HTMLElement;
  expect(row.className).toBe("routing routing-target");
  expect(row.getAttribute("tabindex")).toBe("-1");
  expect(row.getAttribute("data-routing-id")).toBe(block.id);
  // All five done rows show, never "+5 more" — the user jumped here to see this call.
  expect(container.querySelectorAll(".tool-row")).toHaveLength(5);
  expect(container.querySelector(".tool-more")).toBeNull();
});

test("without target the row reads as before, but always carries its own id for the jump to find", () => {
  const block = routingBlock({
    status: "completed",
    tools: [toolRow("x1"), toolRow("x2"), toolRow("x3"), toolRow("x4"), toolRow("x5")],
  });
  const { container } = render(<RoutingRow block={block} />);
  const row = container.querySelector(".routing") as HTMLElement;
  expect(row.className).toBe("routing");
  expect(row.hasAttribute("tabindex")).toBe(false);
  expect(row.getAttribute("data-routing-id")).toBe(block.id);
  // The pre-existing collapsing rule is untouched: five done rows still fold to "+5 more".
  expect(container.querySelectorAll(".tool-row")).toHaveLength(0);
  expect(container.querySelector(".tool-more")?.textContent).toBe("+5 more");
});

test("the summary renders as plain text, with the full string on title for a hover", () => {
  const hostile = "<script>window.__pwned=1</script> **bold** text";
  const block = routingBlock({ status: "completed", toolUses: 0, summary: hostile });
  const { container } = render(<RoutingRow block={block} />);
  const el = container.querySelector(".routing-summary") as HTMLElement;
  expect(el.textContent).toBe(hostile);
  expect(el.getAttribute("title")).toBe(hostile);
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("strong")).toBeNull();
});

test("a folded tool group exposes its call and error counts before revealing rows", () => {
  const block: ToolBlock = {
    kind: "tools",
    id: "group",
    rows: [
      { ...toolRow("failed"), isError: true, preview: "The command returned an error." },
      toolRow("second"),
      toolRow("third"),
      toolRow("fourth"),
      toolRow("fifth"),
    ],
  };
  const { container, getByRole } = render(<ToolRows block={block} />);
  expect(container.querySelectorAll(".tool-row")).toHaveLength(0);
  expect(container.querySelector(".tool-group-summary")?.textContent).toContain("5 tool calls");
  expect(container.querySelector(".tool-error-count")?.textContent).toBe("1 error");
  expect(container.querySelector(".tool-more")?.textContent).toBe("+5 more");
  fireEvent.click(getByRole("button", { name: "Show 5 more tool calls" }));
  expect(container.querySelectorAll(".tool-row")).toHaveLength(5);
  expect(container.querySelector(".tool-more")).toBeNull();
  expect(container.querySelector(".tool-row--error .tool-error-label")?.textContent).toBe("Error");
});

test("tool preview controls keep their target when earlier folded rows are revealed", () => {
  const block: ToolBlock = {
    kind: "tools",
    id: "group",
    rows: [
      toolRow("first"),
      toolRow("second"),
      { ...toolRow("third row/id"), preview: "Selected output" },
      toolRow("fourth"),
      { ...toolRow("fifth"), done: false },
    ],
  };
  const { container, getByRole } = render(<ToolRows block={block} />);
  const row = container.querySelector("button.tool-row") as HTMLButtonElement;
  expect(row.getAttribute("aria-expanded")).toBe("false");
  expect(row.hasAttribute("aria-controls")).toBe(false);
  fireEvent.click(row);
  const preview = container.querySelector(".tool-preview") as HTMLElement;
  const id = preview.id;
  expect(id).not.toBe("");
  expect(id).not.toMatch(/\s/);
  expect(row.getAttribute("aria-controls")).toBe(id);
  expect(row.getAttribute("aria-expanded")).toBe("true");
  expect(row.contains(preview)).toBe(false);

  fireEvent.click(getByRole("button", { name: "Show 2 more tool calls" }));
  expect(container.querySelector(".tool-preview")?.id).toBe(id);
  expect(container.querySelectorAll("button[aria-expanded=true]")).toHaveLength(1);
  fireEvent.click(row);
  expect(container.querySelector(".tool-preview")).toBeNull();
  expect(row.getAttribute("aria-expanded")).toBe("false");
});

test("separate tool groups give repeated provider row IDs separate disclosure targets", () => {
  const block: ToolBlock = { kind: "tools", id: "same", rows: [toolRow("same")] };
  const { container } = render(
    <>
      <ToolRows block={block} />
      <ToolRows block={block} />
    </>,
  );
  for (const button of container.querySelectorAll("button.tool-row")) fireEvent.click(button);
  const ids = [...container.querySelectorAll(".tool-preview")].map((preview) => preview.id);
  expect(ids).toHaveLength(2);
  expect(new Set(ids).size).toBe(2);
});

test("a routing summary has a focused disclosure outside its selectable text", () => {
  const summary = "First finding.\nSecond finding.\nA longer explanation follows.";
  const { container, getByRole } = render(
    <RoutingRow block={routingBlock({ status: "completed", summary })} />,
  );
  const button = getByRole("button", { name: "Expand summary from researcher" });
  const text = container.querySelector(".routing-summary") as HTMLElement;
  expect(text.textContent).toBe(summary);
  expect(button.getAttribute("aria-controls")).toBe(text.id);
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(button.contains(text)).toBe(false);
  button.focus();
  fireEvent.click(button);
  expect(document.activeElement).toBe(button);
  expect(text.classList.contains("routing-summary--open")).toBe(true);
  expect(getByRole("button", { name: "Collapse summary from researcher" })).toBe(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(button);
  expect(text.classList.contains("routing-summary--open")).toBe(false);
  expect(text.textContent).toBe(summary);
});
