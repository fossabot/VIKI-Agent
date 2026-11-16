/**
 * Unit tests pinning AgentFace's stroke colouring that agents.accept.test.tsx leaves implicit:
 * the neutral face's dim stroke is the plain var(--t3), not the dashed diamond's own, fainter
 * var(--face-dim), and it still brightens like every other shape when active.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { AgentFace } from "./AgentFace";

afterEach(cleanup);

function strokeOf(container: HTMLElement): string | null {
  return container.querySelector("g")?.getAttribute("stroke") ?? null;
}

test("an inactive neutral face strokes with the dim token, not the diamond's own, fainter grey", () => {
  const { container } = render(<AgentFace name="Explore" />);
  expect(strokeOf(container)).toBe("var(--t3)");
});

test("an inactive diamond (connector) strokes with its own, fainter var(--face-dim)", () => {
  const { container } = render(<AgentFace name="connector" />);
  expect(strokeOf(container)).toBe("var(--face-dim)");
  expect(container.querySelector("g")?.getAttribute("stroke-dasharray")).toBe("2 1.5");
});

test("an active neutral face brightens to the same active stroke as every other shape", () => {
  const { container } = render(<AgentFace name="Explore" active />);
  expect(strokeOf(container)).toBe("var(--t2)");
});

test("an agent named like an Object.prototype member still gets the neutral face", () => {
  for (const name of ["constructor", "toString", "valueOf"]) {
    const { container, unmount } = render(<AgentFace name={name} />);
    expect(container.querySelector(".face")?.getAttribute("data-shape")).toBe("neutral");
    unmount();
  }
});
