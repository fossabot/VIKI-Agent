import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { LARGE_TILES, SMALL_TILES, TileWordmark } from "./TileWordmark";

afterEach(cleanup);

const tileCount = (rows: readonly string[]) => rows.join("").split("#").length - 1;

test.each([
  ["small", SMALL_TILES, 7, 86],
  ["large", LARGE_TILES, 14, 159],
] as const)("the %s grid is a clean rectangle of tiles", (_, rows, height, width) => {
  expect(rows).toHaveLength(height);
  expect(new Set(rows.map((line) => line.length))).toEqual(new Set([width]));
  expect(rows.join("")).toMatch(/^[#.]+$/);
  // No empty margin: the lettering starts in the first column and ends in the last.
  expect(rows.some((line) => line.startsWith("#"))).toBe(true);
  expect(rows.some((line) => line.endsWith("#"))).toBe(true);
});

test("the large grid keeps the film's tiles, with both I stems three tiles wide", () => {
  expect(tileCount(LARGE_TILES)).toBe(886);
  const stems = (from: number, to: number) =>
    LARGE_TILES.slice(0, 13).every((line) => line.slice(from, to) === "###");
  expect(stems(21, 24)).toBe(true);
  expect(stems(49, 52)).toBe(true);
});

test("a title names the lettering as an image without drawing any text", () => {
  const { container, getByRole } = render(<TileWordmark size="small" title="VIKI Agent" />);
  const svg = getByRole("img", { name: "VIKI Agent" });
  expect(svg.getAttribute("aria-hidden")).toBeNull();
  expect(svg.querySelector("title")?.textContent).toBe("VIKI Agent");
  expect(svg.querySelectorAll("text")).toHaveLength(0);
  expect(container.textContent).toBe("VIKI Agent");
});

test.each(["small", "large"] as const)("the %s lettering is one decorative path", (size) => {
  const { container } = render(<TileWordmark size={size} />);
  const svg = container.querySelector("svg");
  const rows = size === "small" ? SMALL_TILES : LARGE_TILES;
  expect(svg?.getAttribute("aria-hidden")).toBe("true");
  expect(svg?.getAttribute("class")).toBe(`tile-wordmark tile-wordmark-${size}`);
  expect(svg?.getAttribute("viewBox")).toBe(`0 0 ${rows[0]?.length} ${rows.length}`);
  expect(svg?.textContent).toBe("");
  const path = svg?.querySelector("path")?.getAttribute("d") ?? "";
  expect(path.split("M").length - 1).toBe(tileCount(rows));
});
