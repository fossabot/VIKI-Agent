/**
 * Acceptance (frozen): the notice rows. A denial is the app
 * telling the user it refused to act in their vault, so it belongs where they are already reading —
 * in the run of tool rows it interrupted — and it may never be silently dropped.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { NoticeRow, noticeLine } from "../../components/Conversation/Blocks";
import { Conversation } from "../../components/Conversation/Conversation";
import type { NoticeBlock } from "../../state/view";
import { type Item, toolDone, toolStarted, turnStarted, userMessage, view } from "../m2b/fakes";

afterEach(cleanup);

const NOW = Date.parse("2026-09-12T10:00:00.000Z");

const block = (over: Partial<NoticeBlock> = {}): NoticeBlock => ({
  kind: "notice",
  id: "n1",
  tone: "denied",
  subject: "WebFetch",
  detail: "Fetching from an unfamiliar domain needs approval",
  ...over,
});

const denied = (tool: string, reason: string): Item => ({
  type: "permission.denied",
  data: { tool, reason },
});

const compacted: Item = { type: "compacted", data: { trigger: "auto", pre_tokens: 182_000 } };

function show(b: NoticeBlock) {
  const { container } = render(<NoticeRow block={b} />);
  return container.querySelector(".notice-row") as HTMLElement;
}

test("a denial says the app refused, names the tool, and carries the CLI's reason", () => {
  expect(noticeLine(block())).toEqual({
    mark: "⊘",
    head: "auto mode blocked WebFetch",
    tail: "Fetching from an unfamiliar domain needs approval",
  });
  const row = show(block());
  expect(row.textContent).toContain("⊘");
  expect(row.textContent).toContain("auto mode blocked WebFetch");
  expect(row.textContent).toContain("· Fetching from an unfamiliar domain needs approval");
});

test("a hook block names the hook without claiming what it did", () => {
  const b = block({
    tone: "hook",
    subject: "PreToolUse:Bash",
    detail: "guard: _raw/ is create-only",
  });
  const line = noticeLine(b);
  expect(line.mark).toBe("⊘");
  expect(line.head).toBe("hook PreToolUse:Bash");
  // The reducer keeps no `outcome`, and a hook can error as well as block. See Blocks.tsx.
  expect(line.head).not.toContain("blocked");
  expect(line.tail).toBe("guard: _raw/ is create-only");
});

test("a compaction is a different mark and reads from its trigger, not its detail", () => {
  const b = block({ tone: "compacted", subject: "auto", detail: "" });
  expect(noticeLine(b)).toEqual({ mark: "↻", head: "context compacted", tail: "auto" });
  expect(show(b).textContent).not.toContain("⊘");
});

test("an in-turn error names its code", () => {
  const b = block({ tone: "error", subject: "frame_error", detail: "a frame was too large" });
  expect(noticeLine(b)).toEqual({
    mark: "⊘",
    head: "error frame_error",
    tail: "a frame was too large",
  });
});

test("a notice with no tail shows no dangling separator", () => {
  const b = block({ tone: "compacted", subject: "", detail: "" });
  expect(show(b).textContent).toBe("↻context compacted");
});

test("a reason from the CLI is text, whatever it contains", () => {
  const row = show(block({ detail: '<img src=x onerror="window.__pwned=1">' }));
  expect(row.querySelector("img")).toBeNull();
  expect(row.textContent).toContain('<img src=x onerror="window.__pwned=1">');
});

test("a denial between two tool calls sits inside the same panel, in arrival order", () => {
  const v = view([
    userMessage("Read these"),
    turnStarted,
    toolStarted("t1", "Read", { head: "Read", rest: "40-sources/a.md" }),
    toolDone("t1"),
    denied("WebFetch", "unfamiliar domain"),
    toolStarted("t2", "Read", { head: "Read", rest: "40-sources/b.md" }),
    toolDone("t2"),
  ]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={() => {}} />);
  const panels = container.querySelectorAll(".tools");
  expect(panels).toHaveLength(1);
  const panel = panels[0] as HTMLElement;
  expect(panel.querySelector(".notice-row")).not.toBeNull();
  expect(panel.querySelectorAll(".tool-row")).toHaveLength(2);
  const texts = [...panel.querySelectorAll(".tool-row, .notice-row")].map((n) => n.textContent);
  expect(texts[0]).toContain("40-sources/a.md");
  expect(texts[1]).toContain("auto mode blocked WebFetch");
  expect(texts[2]).toContain("40-sources/b.md");
});

test("a notice standing alone still gets a panel rather than floating", () => {
  const v = view([userMessage("Sum it up"), turnStarted, compacted]);
  const { container } = render(<Conversation view={v} nowMs={NOW} onOpenUrl={() => {}} />);
  const panel = container.querySelector(".tools") as HTMLElement;
  expect(panel).not.toBeNull();
  expect(panel.querySelector(".notice-row")?.textContent).toContain("context compacted");
});
