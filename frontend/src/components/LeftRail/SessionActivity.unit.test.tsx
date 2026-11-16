import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionRow } from "../../protocol";
import { emptyView, type SessionView, type Turn } from "../../state/view";
import type { RowContext } from "./rowState";
import { SessionList } from "./SessionList";

afterEach(cleanup);
const row: SessionRow = {
  session_id: "s1",
  title: "Activity",
  first_prompt: "",
  entrypoint: "viki-agent",
  modified: new Date(2026, 9, 12, 12).toISOString(),
  cli_version: "",
  other: false,
};
const turn = (over: Partial<Turn> = {}): Turn => ({
  id: "t1",
  user: null,
  blocks: [],
  status: "success",
  usage: null,
  durationMs: 0,
  history: false,
  ...over,
});
function mount(view: SessionView | null, over: Partial<RowContext> = {}) {
  return render(
    <SessionList
      groups={[{ label: "Today", rows: [row] }]}
      other={[]}
      selectedId="s1"
      liveIds={new Set(["s1"])}
      nowMs={Date.now()}
      onOpen={vi.fn()}
      rowContext={{
        app: { sessions: view ? { s1: view } : {}, others: [], status: "open" },
        elsewhere: new Map(),
        ...over,
      }}
    />,
  );
}
test.each([false, true])(
  "running and thinking share the running perimeter (thinking %s)",
  (thinking) => {
    const blocks: Turn["blocks"] = thinking
      ? [{ kind: "thinking", id: "b1", text: "", done: false, durationMs: null }]
      : [];
    const { container } = mount({
      ...emptyView("s1"),
      turns: [turn({ status: "running", blocks })],
    });
    expect(container.querySelector(".sess-activity")?.getAttribute("data-activity")).toBe(
      "running",
    );
    expect(container.querySelector(".m")?.textContent).toBe("writing…");
    expect(container.querySelector(".sess-activity")?.getAttribute("aria-hidden")).toBe("true");
    expect(container.querySelector(".sess-selected")).not.toBeNull();
  },
);
test("an open decision gets the attention perimeter rather than running or done", () => {
  const { container } = mount({
    ...emptyView("s1"),
    turns: [turn()],
    cards: [
      {
        kind: "permission",
        nativeRequest: null,
        data: {
          tool_use_id: "p1",
          kind: "tool",
          tool: "Read",
          summary: "",
          question: "",
          reason: "",
          agent: "",
          plan: "",
        },
      },
    ],
  });
  expect(container.querySelector(".sess-activity")?.getAttribute("data-activity")).toBe(
    "needs-you",
  );
  expect(container.querySelector(".chip")?.textContent).toBe("Needs review");
});
test("only an observed successful latest turn gets a done perimeter", () => {
  const { container } = mount({ ...emptyView("s1"), turns: [turn()] });
  expect(container.querySelector(".sess-activity")?.getAttribute("data-activity")).toBe("done");
  expect(container.querySelector(".m")?.textContent).toBe("live · idle");
});
test.each([
  ["idle", {}],
  ["history", { turns: [turn({ history: true })] }],
  ["cancelled latest", { turns: [turn(), turn({ status: "cancelled" })] }],
  ["failed latest", { turns: [turn(), turn({ status: "error" })] }],
  ["closed", { turns: [turn()], closed: true }],
  ["error", { turns: [turn()], error: { code: "crashed", message: "", recoverable: false } }],
  [
    "blocked",
    {
      turns: [turn()],
      blocked: {
        reason: "live_elsewhere",
        holder: { session_id: "s1", entrypoint: "cli", title: "", pid: 1 },
      },
    },
  ],
  ["rate limit", { turns: [turn()], rateLimit: { status: "rejected", kind: "", resets_at: 0 } }],
  ["compacting", { turns: [turn()], compacting: true }],
] satisfies [string, Partial<SessionView>][])("%s does not claim completion", (_label, over) => {
  expect(
    mount({ ...emptyView("s1"), ...over }).container.querySelector(".sess-activity"),
  ).toBeNull();
});
test("an unheld or externally held session does not claim completion", () => {
  expect(mount(null).container.querySelector(".sess-activity")).toBeNull();
  cleanup();
  expect(
    mount(
      { ...emptyView("s1"), turns: [turn()] },
      { elsewhere: new Map([["s1", "cli"]]) },
    ).container.querySelector(".sess-activity"),
  ).toBeNull();
});
test("a write tool's session still gets the done perimeter once idle, with no chip", () => {
  const blocks: Turn["blocks"] = [
    {
      kind: "tools",
      id: "b1",
      rows: [
        {
          id: "r1",
          tool: "Write",
          row: { head: "Write", rest: "", tail: "" },
          done: true,
          isError: false,
          summary: "",
          preview: "",
        },
      ],
    },
  ];
  const { container } = mount({ ...emptyView("s1"), turns: [turn({ blocks })] });
  expect(container.querySelector(".sess-activity")?.getAttribute("data-activity")).toBe("done");
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector(".m")?.textContent).toBe("live · idle");
});
test("legacy callers retain their breathing dot without a perimeter", () => {
  const { container } = render(
    <SessionList
      groups={[{ label: "Today", rows: [row] }]}
      other={[]}
      selectedId={null}
      liveIds={new Set(["s1"])}
      nowMs={Date.now()}
      onOpen={vi.fn()}
    />,
  );
  expect(container.querySelector(".sess-activity")).toBeNull();
  expect(container.querySelector(".sess-dot.breathe")).not.toBeNull();
});
