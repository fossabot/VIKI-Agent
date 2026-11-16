import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import { ICONS, Icon, type IconName } from "./Icon";

test.each(Object.keys(ICONS) as IconName[])("%s renders one aria-hidden svg.ico", (name) => {
  for (const size of [14, 20] as const) {
    const { container, unmount } = render(<Icon name={name} size={size} />);
    const svgs = container.querySelectorAll('svg.ico[aria-hidden="true"]');
    expect(svgs).toHaveLength(1);
    const svg = svgs[0] as SVGElement;
    expect(svg.getAttribute("data-icon")).toBe(name);
    expect(svg.getAttribute("viewBox")).toBe("0 0 16 16");
    expect(svg.getAttribute("width")).toBe(String(size));
    expect(svg.getAttribute("height")).toBe(String(size));
    const paths = svg.querySelectorAll("path");
    expect(paths).toHaveLength(1);
    expect(paths[0]?.getAttribute("d")).toBe(ICONS[name]);
    unmount();
  }
});

test("shield renders the outlined current-color path", () => {
  const { container } = render(<Icon name="shield" />);
  const svg = container.querySelector('svg.ico[aria-hidden="true"]');
  expect(svg?.getAttribute("data-icon")).toBe("shield");
  expect(svg?.getAttribute("viewBox")).toBe("0 0 16 16");
  expect(svg?.querySelector("path")?.getAttribute("d")).toBe(
    "M8 1.5 13.5 4v3.5c0 3.5-2.3 5.7-5.5 7-3.2-1.3-5.5-3.5-5.5-7V4z M5.5 7.5 7.3 9.3 10.8 5.8",
  );
});

test("check renders the shared 16-unit stroke glyph", () => {
  const { container } = render(<Icon name="check" />);
  const svg = container.querySelector('svg.ico[aria-hidden="true"]');
  expect(svg?.getAttribute("data-icon")).toBe("check");
  expect(svg?.getAttribute("viewBox")).toBe("0 0 16 16");
  expect(svg?.getAttribute("width")).toBe("16");
  expect(svg?.getAttribute("height")).toBe("16");
  expect(svg?.querySelector("path")?.getAttribute("d")).toBe("M3 8.5l3 3 7-7");
});
