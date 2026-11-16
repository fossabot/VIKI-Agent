import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Block, RoutingBlock, ToolBlock, Turn } from "../../state/view";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { Reply } from "./Conversation";

afterEach(cleanup);

function turn(blocks: Block[], status: Turn["status"] = "running"): Turn {
  return {
    id: "turn",
    user: null,
    blocks,
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

const TOOLS: ToolBlock = {
  kind: "tools",
  id: "tools",
  rows: [
    {
      id: "read",
      tool: "Read",
      row: { head: "Read", rest: "notes/example.md", tail: "" },
      done: false,
      isError: false,
      summary: "",
      preview: "",
    },
  ],
};

const AGENT: RoutingBlock = {
  kind: "routing",
  id: "agent",
  agent: "researcher",
  model: "sonnet",
  description: "Inspect synthetic examples",
  reason: "",
  status: "running",
  tools: [],
};

test("a running reply names no activity", () => {
  const { container } = render(<Reply turn={turn([TOOLS])} onOpenUrl={vi.fn()} />);
  expect(container.querySelector(".reply")?.className).toBe("reply reply-has-activity");
  expect(container.querySelector(".reply-presence")).toBeNull();
  expect(container.querySelector(".activity-orb")).toBeNull();
});

test.each(["success", "cancelled", "error"] as const)(
  "an ended %s reply has no activity even if a background agent remains",
  (status) => {
    const { container } = render(<Reply turn={turn([AGENT], status)} onOpenUrl={vi.fn()} />);
    expect(container.querySelector(".reply-presence")).toBeNull();
    expect(container.querySelector(".activity-orb")).toBeNull();
    expect(container.querySelector(".routing-meta")?.textContent).toContain(
      "Running in background",
    );
  },
);

test("a configured profile retains its name, revision and quiet presence dot", () => {
  const { container } = render(
    <IdentityContext.Provider
      value={{
        ...DEFAULT_IDENTITY,
        assistantName: "Planner",
        profile: {
          name: "Planner",
          avatarId: null,
          accent: "slate",
          shape: "circle",
          revision: 3,
        },
      }}
    >
      <Reply turn={turn([TOOLS])} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".reply-name")?.textContent).toBe("Planner");
  expect([...container.querySelectorAll(".chip")].map((chip) => chip.textContent)).toEqual([
    "r3",
    "Profile",
  ]);
  expect(container.querySelector(".face-presence")).not.toBeNull();
  expect(container.querySelector(".activity-orb")).toBeNull();
});

test("unresolved identity stays neutral while observed work remains visible", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, unresolved: true }}>
      <Reply turn={turn([TOOLS])} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".reply-name")).toBeNull();
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector(".face-neutral")).not.toBeNull();
  expect(container.querySelector(".activity-orb")).toBeNull();
});
