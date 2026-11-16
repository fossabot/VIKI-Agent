import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { RailSection } from "./RailSection";

afterEach(cleanup);

test("the count is only shown when given", () => {
  const { container, rerender } = render(<RailSection title="Routines" />);
  expect(container.querySelector(".rail-section-count")).toBeNull();

  rerender(<RailSection title="Routines" count={3} />);
  expect(container.querySelector(".rail-section-count")?.textContent).toBe("3");
});

test("dim renders the count in the dim tone, not orange", () => {
  const { container, rerender } = render(<RailSection title="Routines" count="09:00" />);
  const defaultCount = container.querySelector(".rail-section-count");
  expect(defaultCount?.classList.contains("rail-section-count--dim")).toBe(false);

  rerender(<RailSection title="Routines" count="09:00" dim />);
  const dimCount = container.querySelector(".rail-section-count");
  expect(dimCount?.classList.contains("rail-section-count--dim")).toBe(true);
});

test("className extends, rather than replaces, the section's own class", () => {
  const { container } = render(<RailSection title="Routines" className="rail-section--routines" />);
  const section = container.querySelector("section");
  expect(section?.classList.contains("rail-section")).toBe(true);
  expect(section?.classList.contains("rail-section--routines")).toBe(true);
});
