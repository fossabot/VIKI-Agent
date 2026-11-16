import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import { ContextSummary } from "./ContextSummary";
import { RightRail } from "./RightRail";

afterEach(cleanup);

test("context stays outside both rail scrollers and distinguishes unknown from observed zero", () => {
  const onOpen = vi.fn();
  const { container, rerender } = render(
    <RightRail summary={<p>Native Skills</p>} footer={<ContextSummary onOpen={onOpen} />}>
      <p>Overview details</p>
    </RightRail>,
  );
  const footer = container.querySelector(".right-rail-footer");
  expect(footer?.textContent).toContain("Not reported");
  expect(container.querySelector(".right-rail-details")?.contains(footer)).toBe(false);
  expect(container.querySelector(".right-rail-summary")?.contains(footer)).toBe(false);
  expect(screen.queryByRole("meter")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Context & loadout: Not reported" }));
  expect(onOpen).toHaveBeenCalledOnce();
  rerender(<ContextSummary percent={0} onOpen={onOpen} />);
  const meter = screen.getByRole("meter", { name: "Context window used" });
  expect(meter.getAttribute("aria-valuenow")).toBe("0");
  expect(meter.getAttribute("aria-valuemin")).toBe("0");
  expect(meter.getAttribute("aria-valuemax")).toBe("100");
  expect(screen.getByText("0% used")).toBeTruthy();
  rerender(<ContextSummary percent={37} />);
  expect(screen.getByRole("meter").getAttribute("aria-valuenow")).toBe("37");
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.getByText("Context & loadout unavailable")).toBeTruthy();
});

test.each([
  [-10, 0],
  [150, 100],
])("the meter keeps its native range clamping for %s%%", (percent, expected) => {
  const { container } = render(<ContextSummary percent={percent} />);
  expect(screen.getByRole("meter").getAttribute("aria-valuenow")).toBe(String(expected));
  const fill = container.querySelector(".context-summary-fill") as HTMLElement;
  expect(fill.style.width).toBe(`${expected}%`);
});

test("reported usage updates the same meter in both directions", () => {
  const { rerender } = render(<ContextSummary percent={5} />);
  const meter = screen.getByRole("meter", { name: "Context window used" });
  for (const percent of [30, 75, 20, 100, 0]) {
    rerender(<ContextSummary percent={percent} />);
    expect(screen.getByRole("meter", { name: "Context window used" })).toBe(meter);
    expect(meter.getAttribute("aria-valuenow")).toBe(String(percent));
    expect((meter.firstElementChild as HTMLElement).style.width).toBe(`${percent}%`);
    expect(screen.getByText(`${percent}% used`)).toBeTruthy();
  }
});

test("the context footer does not shrink while rail details and Skills scroll", () => {
  const css = readCss(import.meta.url, "RightRail.css");
  expect(css.match(/\.right-rail-footer\s*\{[^}]*\}/)?.[0]).toMatch(/flex:\s*none/);
});
