/** Acceptance (frozen): the agents section and the geometric agent faces. */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { AgentFace } from "../../components/Face/AgentFace";
import { AgentsSection } from "../../components/LeftRail/AgentsSection";
import { agent, localIso } from "./fakes";

afterEach(cleanup);

const NOW = new Date(2026, 8, 12, 18, 14).getTime();
const agents = [
  agent({ name: "connector" }),
  agent({ name: "curator", last_run: localIso(2026, 9, 12, 8, 0) }),
  agent({ name: "librarian", last_run: localIso(2026, 8, 31, 8, 0) }),
  agent({ name: "researcher", last_run: localIso(2026, 9, 12, 18, 14) }),
];

test("VIKI first with presence, then the agents in order with their status", () => {
  const { container } = render(
    <AgentsSection agents={agents} running={["researcher"]} vikiOnline={true} nowMs={NOW} />,
  );
  const rows = [...container.querySelectorAll<HTMLElement>(".agent")];
  expect(rows.map((r) => r.getAttribute("data-agent"))).toEqual([
    "VIKI",
    "connector",
    "curator",
    "librarian",
    "researcher",
  ]);
  expect(rows.map((r) => r.querySelector(".s")?.textContent ?? null)).toEqual([
    "here now",
    "never",
    "idle · 08:00",
    "idle · 08-31",
    "here now",
  ]);
  expect(rows[0]?.querySelector(".s")?.classList.contains("s-on")).toBe(true);
  expect(rows[4]?.querySelector(".s")?.classList.contains("s-on")).toBe(true);
  expect(rows[1]?.querySelector(".s")?.classList.contains("s-on")).toBe(false);
  expect(rows[0]?.querySelector(".face-presence")).toBeNull(); // no default portrait
  expect(rows[4]?.querySelector(".face-presence")).not.toBeNull(); // the running agent's
  expect(rows[2]?.querySelector(".face-presence")).toBeNull();
});

test("VIKI offline: no status on her row, and no dot", () => {
  const { container } = render(
    <AgentsSection agents={[]} running={[]} vikiOnline={false} nowMs={NOW} />,
  );
  const rows = [...container.querySelectorAll<HTMLElement>(".agent")];
  expect(rows).toHaveLength(1);
  expect(rows[0]?.textContent).toBe("VIKI");
  expect(rows[0]?.querySelector(".s")).toBeNull();
  expect(rows[0]?.querySelector(".face-presence")).toBeNull();
});

test("AgentFace shapes: the four vault agents by name, neutral for anyone else", () => {
  const shapes: Record<string, string> = {};
  for (const name of [
    "researcher",
    "librarian",
    "curator",
    "connector",
    "Explore",
    "general-purpose",
  ]) {
    const { container, unmount } = render(<AgentFace name={name} />);
    shapes[name] = container.querySelector(".face")?.getAttribute("data-shape") ?? "";
    expect(container.querySelector("svg")?.getAttribute("width")).toBe("24");
    expect(container.querySelector(".face")?.getAttribute("aria-hidden")).toBe("true");
    unmount();
  }
  expect(shapes).toEqual({
    researcher: "circle",
    librarian: "square",
    curator: "hexagon",
    connector: "diamond",
    Explore: "neutral",
    "general-purpose": "neutral",
  });
});

test("an active face carries the breathing presence dot; an inactive one does not", () => {
  const { container } = render(<AgentFace name="researcher" active size={32} />);
  expect(container.querySelector(".face-presence")?.classList.contains("breathe")).toBe(true);
  expect(container.querySelector("svg")?.getAttribute("width")).toBe("32");
  cleanup();
  const idle = render(<AgentFace name="researcher" />);
  expect(idle.container.querySelector(".face-presence")).toBeNull();
});
