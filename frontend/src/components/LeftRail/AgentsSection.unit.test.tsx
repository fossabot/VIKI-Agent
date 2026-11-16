/**
 * Unit tests for the roster rows' profile toggle — the profile is all a row expands to, runs
 * being the right rail's run list (components/RightRail/AgentRuns.tsx) — plus a case
 * agents.accept.test.tsx leaves implicit: two `.claude/agents/*.md` files could declare the same
 * `name` in frontmatter, producing a roster with a duplicate name — the row list must still
 * reconcile correctly across a rails refresh.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { agent, localIso } from "../../accept/m3b/fakes";
import type { AgentInfo } from "../../protocol";
import { AgentsSection } from "./AgentsSection";

afterEach(cleanup);

const NOW = new Date(2026, 8, 12, 18, 14).getTime();

function statusesOf(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll<HTMLElement>(".agent .s")].map((s) => s.textContent);
}

test("two agent files declaring the same name still reconcile correctly across a refresh", () => {
  // Mirrors the reorder shape that reproduces React's duplicate-key defect (see
  // SearchPalette.unit.test.tsx): a run of same-keyed rows plus
  // one differently-named row that jumps position between renders.
  const first: AgentInfo[] = [
    agent({ name: "researcher", last_run: localIso(2026, 9, 12, 8, 0) }),
    agent({ name: "researcher", last_run: localIso(2026, 9, 12, 9, 0) }),
    agent({ name: "curator", last_run: "" }),
  ];
  const { container, rerender } = render(
    <AgentsSection agents={first} running={[]} vikiOnline={false} nowMs={NOW} />,
  );
  expect(statusesOf(container)).toEqual(["idle · 08:00", "idle · 09:00", "never"]);

  const second: AgentInfo[] = [
    agent({ name: "curator", last_run: "" }),
    agent({ name: "researcher", last_run: localIso(2026, 9, 12, 10, 0) }),
    agent({ name: "researcher", last_run: localIso(2026, 9, 12, 11, 0) }),
  ];
  rerender(<AgentsSection agents={second} running={[]} vikiOnline={false} nowMs={NOW} />);
  const rows = [...container.querySelectorAll<HTMLElement>(".agent")];
  expect(rows.map((r) => r.getAttribute("data-agent"))).toEqual([
    "VIKI",
    "curator",
    "researcher",
    "researcher",
  ]);
  expect(statusesOf(container)).toEqual(["never", "idle · 10:00", "idle · 11:00"]);
});

test("a roster row toggles its profile open and closed, independently of the others", () => {
  const researcher = agent({
    name: "researcher",
    description: "Surveys sources and writes notes.",
    model: "opus",
    tools: ["WebFetch", "Read"],
    last_run: localIso(2026, 9, 12, 18, 14),
  });
  const connector = agent({ name: "connector" });
  const { container } = render(
    <AgentsSection
      agents={[researcher, connector]}
      running={["researcher"]}
      vikiOnline={false}
      nowMs={NOW}
    />,
  );
  const row = container.querySelector('[data-agent="researcher"]') as HTMLElement;
  expect(row.getAttribute("aria-expanded")).toBe("false");
  const detailsId = row.getAttribute("aria-controls");
  expect(detailsId).toBeTruthy();
  expect(container.querySelector(`#${detailsId}`)).toBeNull();

  fireEvent.click(row);
  expect(row.getAttribute("aria-expanded")).toBe("true");
  const details = container.querySelector(`#${detailsId}`) as HTMLElement;
  expect(details).not.toBeNull();
  expect(details.textContent).toContain("Surveys sources and writes notes.");
  expect(details.textContent).toContain("Model opus · Tools WebFetch, Read · Last run 18:14");

  // Expanding researcher does not touch connector's own, independent state.
  const connectorRow = container.querySelector('[data-agent="connector"]') as HTMLElement;
  expect(connectorRow.getAttribute("aria-expanded")).toBe("false");

  fireEvent.click(row);
  expect(row.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector(`#${detailsId}`)).toBeNull();
});

test("an expanded row with nothing set falls back to the section's own copy", () => {
  const { container } = render(
    <AgentsSection
      agents={[agent({ name: "connector" })]}
      running={[]}
      vikiOnline={false}
      nowMs={NOW}
    />,
  );
  fireEvent.click(container.querySelector('[data-agent="connector"]') as HTMLElement);
  const details = container.querySelector(".agent-details") as HTMLElement;
  expect(details.textContent).toContain("No description.");
  expect(details.textContent).toContain("Model inherit · Tools all tools · Last run never");
});

test("an expanded row shows its profile only", () => {
  const { container } = render(
    <AgentsSection
      agents={[agent({ name: "researcher", description: "Surveys sources and writes notes." })]}
      running={["researcher"]}
      vikiOnline={false}
      nowMs={NOW}
    />,
  );
  fireEvent.click(container.querySelector('[data-agent="researcher"]') as HTMLElement);
  const details = container.querySelector(".agent-details") as HTMLElement;
  // The description and the Model · Tools · Last run line, and nothing after them: a running
  // agent's runs and their Jump belong to the run list, not to this row.
  expect([...details.children].map((child) => child.textContent)).toEqual([
    "Surveys sources and writes notes.",
    "Model inherit · Tools all tools · Last run never",
  ]);
  expect(container.querySelectorAll("button")).toHaveLength(1); // the researcher row itself
});
