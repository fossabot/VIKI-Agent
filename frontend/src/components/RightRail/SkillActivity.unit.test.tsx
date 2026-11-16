import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { Envelope } from "../../protocol";
import { applyEnvelope } from "../../state/sessionReducer";
import {
  emptyView,
  type RoutingBlock,
  type SessionView,
  type ToolRowView,
  type Turn,
} from "../../state/view";
import { observedSkillCalls, SkillActivity } from "./SkillActivity";

afterEach(cleanup);

function tool(id: string, rest: string, extra: Partial<ToolRowView> = {}): ToolRowView {
  return {
    id,
    tool: "Skill",
    row: { head: "Skill", rest, tail: "" },
    done: false,
    isError: false,
    summary: "",
    preview: "",
    ...extra,
  };
}

function turn(id: string, blocks: Turn["blocks"], extra: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks,
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
    ...extra,
  };
}

function routing(tools: ToolRowView[], status: RoutingBlock["status"] = "running"): RoutingBlock {
  return {
    kind: "routing",
    id: "route",
    agent: "helper",
    model: "",
    description: "",
    reason: "",
    status,
    tools,
  };
}

function view(turns: Turn[], extra: Partial<SessionView> = {}): SessionView {
  return { ...emptyView("chat"), synced: true, turns, ...extra };
}

test("latest distinct Skill rows include nested calls and stop after three labels", () => {
  const selected = view([
    turn("old", [{ kind: "tools", id: "a0", rows: [tool("a0", " Alpha ", { done: true })] }], {
      status: "success",
    }),
    turn("new", [
      { kind: "tools", id: "b0", rows: [tool("b0", "Beta", { done: true })] },
      routing([tool("a1", "Alpha", { done: true, isError: true }), tool("c0", "Gamma")]),
      {
        kind: "tools",
        id: "ignored",
        rows: [
          tool("ignored", "Beta"),
          tool("d0", "Delta"),
          tool("read", "Other", { tool: "Read" }),
        ],
      },
    ]),
  ]);
  expect(observedSkillCalls(selected)).toEqual([
    { id: "d0", label: "Delta", status: "calling" },
    { id: "ignored", label: "Beta", status: "calling" },
    { id: "c0", label: "Gamma", status: "calling" },
  ]);
});

test("latest duplicate retains failed result across top-level and nested rows", () => {
  const selected = view([
    turn("one", [
      { kind: "tools", id: "earlier", rows: [tool("earlier", "Alpha")] },
      routing([tool("latest", " Alpha ", { done: true, isError: true })]),
    ]),
  ]);
  expect(observedSkillCalls(selected)).toEqual([
    { id: "latest", label: "Alpha", status: "failed" },
  ]);
  const { container } = render(<SkillActivity view={selected} />);
  expect(screen.getByText("Recent Skill calls · loaded chat")).toBeTruthy();
  expect(screen.getByText("Failed")).toBeTruthy();
  expect(container.querySelector(".skill-activity-call--calling")).toBeNull();
});

test.each([
  { isError: false, text: "Invoked" },
  { isError: true, text: "Failed" },
])("a matching result changes Calling to $text", ({ isError, text }) => {
  const started = view([
    turn("one", [{ kind: "tools", id: "skill", rows: [tool("skill", "Capture")] }]),
  ]);
  const { container, rerender } = render(<SkillActivity view={started} />);
  expect(screen.getByText("Calling")).toBeTruthy();
  expect(container.querySelectorAll(".skill-activity-call--calling")).toHaveLength(1);

  const ended = view([
    turn("one", [
      { kind: "tools", id: "skill", rows: [tool("skill", "Capture", { done: true, isError })] },
    ]),
  ]);
  rerender(<SkillActivity view={ended} />);
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.queryByText("Calling")).toBeNull();
  expect(container.querySelector(".skill-activity-call--calling")).toBeNull();
});

test("reverse walk reaches older loaded turns when fewer than three names are newer", () => {
  const selected = view([
    turn("old", [{ kind: "tools", id: "old", rows: [tool("old", "First", { done: true })] }], {
      status: "success",
    }),
    turn("new", [{ kind: "tools", id: "new", rows: [tool("new", "Second")] }]),
  ]);
  expect(observedSkillCalls(selected)).toEqual([
    { id: "new", label: "Second", status: "calling" },
    { id: "old", label: "First", status: "invoked" },
  ]);
});

test("actual event order wins when a nested Skill appends behind a later top-level call", () => {
  const item = (seq: number, type: string, data: Record<string, unknown>): Envelope =>
    ({
      v: 2,
      generation: "gen",
      request_id: "",
      session_id: "chat",
      seq,
      type,
      data,
    }) as unknown as Envelope;
  const started = (id: string, label: string, parent_id = "") => ({
    block_id: id,
    tool: "Skill",
    row: { head: "Skill", rest: label, tail: "" },
    parent_id,
  });
  const route = {
    block_id: "route",
    agent: "helper",
    model: "",
    description: "",
    reason: "",
  };
  let selected = emptyView("chat");
  for (const envelope of [
    item(1, "user.message", { text: "one", command: "", at: "now" }),
    item(2, "turn.started", { turn_id: "one" }),
    item(3, "subagent.started", route),
    item(4, "tool.started", started("nested-old", "Alpha", "route")),
    item(5, "tool.started", started("top", "Beta")),
    item(6, "tool.started", started("nested-new", "Alpha", "route")),
  ])
    selected = applyEnvelope(selected, envelope);
  expect(observedSkillCalls(selected).map((call) => call.id)).toEqual(["nested-new", "top"]);

  for (const envelope of [
    item(7, "turn.done", { status: "success", usage: null, context_percent: null, duration_ms: 1 }),
    item(8, "turn.started", { turn_id: "two" }),
    item(9, "tool.started", started("later-top", "Gamma")),
    item(10, "tool.started", started("old-route-latest", "Alpha", "route")),
    item(11, "tool.started", started("newest", "Delta")),
  ])
    selected = applyEnvelope(selected, envelope);
  expect(observedSkillCalls(selected).map((call) => call.id)).toEqual([
    "newest",
    "old-route-latest",
    "later-top",
  ]);
});

test("unfinished calls glow only while the selected live turn and routing block run", () => {
  const main = tool("main", "Main");
  const nested = tool("nested", "Nested");
  const running = view([
    turn("one", [{ kind: "tools", id: "main", rows: [main] }, routing([nested])]),
  ]);
  const { container, rerender } = render(<SkillActivity view={running} />);
  expect(within(screen.getByRole("list")).getAllByText("Calling")).toHaveLength(2);
  expect(container.querySelectorAll(".skill-activity-call--calling")).toHaveLength(2);

  const routeEnded = view([
    turn("one", [{ kind: "tools", id: "main", rows: [main] }, routing([nested], "completed")]),
  ]);
  rerender(<SkillActivity view={routeEnded} />);
  expect(observedSkillCalls(routeEnded)).toEqual([
    { id: "nested", label: "Nested", status: "unknown" },
    { id: "main", label: "Main", status: "calling" },
  ]);
  expect(container.querySelectorAll(".skill-activity-call--calling")).toHaveLength(1);

  const completed = view([
    turn("one", [{ kind: "tools", id: "main", rows: [main] }, routing([nested])], {
      status: "success",
    }),
  ]);
  rerender(<SkillActivity view={completed} />);
  expect(screen.getAllByText("Result unknown")).toHaveLength(2);
  expect(container.querySelector(".skill-activity-call--calling")).toBeNull();
});

test.each([
  { reason: "disconnect", changes: { synced: false } },
  { reason: "close", changes: { closed: true } },
  { reason: "block", changes: { blocked: {} as NonNullable<SessionView["blocked"]> } },
  { reason: "truncation", changes: { truncated: true } },
  {
    reason: "terminal error",
    changes: { error: { code: "crashed" as const, message: "ended", recoverable: false } },
  },
])("$reason makes an unfinished call's result unknown", ({ changes }) => {
  const selected = view(
    [turn("one", [{ kind: "tools", id: "a", rows: [tool("a", "Alpha")] }])],
    changes,
  );
  expect(observedSkillCalls(selected)).toEqual([{ id: "a", label: "Alpha", status: "unknown" }]);
  const { container } = render(<SkillActivity view={selected} />);
  expect(screen.getByText("Result unknown")).toBeTruthy();
  expect(container.querySelector(".skill-activity-call--calling")).toBeNull();
});

test("history and completed results do not gain a live glow", () => {
  const selected = view([
    turn(
      "old",
      [
        {
          kind: "tools",
          id: "h",
          rows: [tool("h", "History"), tool("ok", "Done", { done: true })],
        },
      ],
      { history: true },
    ),
  ]);
  expect(observedSkillCalls(selected)).toEqual([
    { id: "ok", label: "Done", status: "invoked" },
    { id: "h", label: "History", status: "unknown" },
  ]);
});

test("empty and hostile labels stay plain text with full accessible title", () => {
  const hostile = '<img src=x onerror="evil()">';
  const selected = view([
    turn("one", [
      {
        kind: "tools",
        id: "a",
        rows: [tool("a", "   ", { done: true }), tool("b", hostile, { done: true })],
      },
    ]),
  ]);
  expect(observedSkillCalls(selected).map((call) => call.label)).toEqual([hostile, "Skill call"]);
  const { container } = render(<SkillActivity view={selected} />);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(hostile).getAttribute("title")).toBe(hostile);
  expect(screen.getByText("Skill call")).toBeTruthy();
});

test("no selected chat or Skill row renders no activity strip", () => {
  expect(observedSkillCalls(null)).toEqual([]);
  const selected = view([
    turn("one", [{ kind: "tools", id: "read", rows: [tool("read", "a skill", { tool: "Read" })] }]),
  ]);
  expect(observedSkillCalls(selected)).toEqual([]);
  const { container, rerender } = render(<SkillActivity view={undefined} />);
  expect(container.firstChild).toBeNull();
  rerender(<SkillActivity view={selected} />);
  expect(container.firstChild).toBeNull();
});
