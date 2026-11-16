import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { BotIcon } from "./BotIcon";
import { LED } from "./botIconDesign";

afterEach(cleanup);

it("draws a decorative icon that names its design", () => {
  const { container } = render(<BotIcon seed="p1" name="Chief" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("aria-hidden")).toBe("true");
  expect(svg?.getAttribute("focusable")).toBe("false");
  expect(svg?.getAttribute("data-seat")).toBe("chief");
  expect(svg?.getAttribute("data-emblem")).toBe("door");
  expect(svg?.getAttribute("data-icon")).toBe("door");
  expect(svg?.getAttribute("data-hue")).toBe("orange");
  expect(container.querySelector("g[filter]")?.getAttribute("style")).toContain(LED.orange);
});

it("draws an icon override without changing the seat hue", () => {
  const { container } = render(<BotIcon seed="p1" name="Chief" icon="star" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-icon")).toBe("star");
  expect(svg?.getAttribute("data-emblem")).toBe("star");
  expect(svg?.getAttribute("data-hue")).toBe("orange");
});

it("falls back to the generated emblem for an unknown icon override", () => {
  const { container } = render(<BotIcon seed="p1" name="Chief" icon="unknown" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-icon")).toBe("door");
  expect(svg?.getAttribute("data-emblem")).toBe("door");
});

it("draws Builder's seat hue as the amber bot token", () => {
  const { container } = render(<BotIcon seed="p1" name="Builder" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-seat")).toBe("builder");
  expect(svg?.getAttribute("data-hue")).toBe("amber");
  expect(container.querySelector("g[filter]")?.getAttribute("style")).toContain(LED.amber);
});

it("draws Engineer as the amber wrench seat with block eyes", () => {
  const { container } = render(<BotIcon seed="p1" name="Engineer" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-seat")).toBe("engineer");
  expect(svg?.getAttribute("data-eyes")).toBe("block");
  expect(svg?.getAttribute("data-emblem")).toBe("wrench");
  expect(svg?.getAttribute("data-hue")).toBe("amber");
  expect(container.querySelector("g[filter]")?.getAttribute("style")).toContain(LED.amber);
});

it("a hue prop overrides the design's hue, in both the LED and data-hue", () => {
  const { container } = render(<BotIcon seed="p1" name="Chief" hue="pink" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-seat")).toBe("chief");
  expect(svg?.getAttribute("data-hue")).toBe("pink");
  expect(container.querySelector("g[filter]")?.getAttribute("style")).toContain(LED.pink);
});

it("draws a padded, lower-case Desk as the desk seat", () => {
  const { container } = render(<BotIcon seed="p1" name=" desk " />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.getAttribute("data-seat")).toBe("desk");
  expect(svg?.getAttribute("data-eyes")).toBe("square");
  expect(svg?.getAttribute("data-emblem")).toBe("tray");
  expect(svg?.getAttribute("data-hue")).toBe("teal");
});

it("draws as a dark object in either app theme", () => {
  const { container } = render(<BotIcon seed="p1" name="Planner" />);
  expect(container.querySelector("svg.bot-icon")?.getAttribute("data-theme")).toBe("dark");
});

it("leaves the seat off a Bot that is not named after one", () => {
  const { container } = render(<BotIcon seed="p1" name="Planner" />);
  const svg = container.querySelector("svg.bot-icon");
  expect(svg?.hasAttribute("data-seat")).toBe(false);
  expect(svg?.getAttribute("data-head")).toMatch(/^(orb|block|capsule)$/);
});

it("gives each icon on a page its own ids, and points every reference at its own", () => {
  const { container } = render(
    <>
      <BotIcon seed="p1" name="Chief" />
      <BotIcon seed="p2" name="Planner" />
    </>,
  );
  const ids = [...container.querySelectorAll("[id]")].map((element) => element.id);
  expect(new Set(ids).size).toBe(ids.length);
  for (const svg of container.querySelectorAll("svg")) {
    const own = new Set([...svg.querySelectorAll("[id]")].map((element) => element.id));
    const refs = [...svg.outerHTML.matchAll(/url\(#([^)]+)\)/g)].map((match) => match[1]);
    expect(refs.length).toBeGreaterThan(0);
    for (const ref of refs) expect(own.has(ref ?? "")).toBe(true);
  }
});

it("draws the same icon for the same profile", () => {
  const shapes = () =>
    [...render(<BotIcon seed="p9" name="Planner" />).container.querySelectorAll("path")].map(
      (path) => path.getAttribute("d"),
    );
  const first = shapes();
  cleanup();
  expect(shapes()).toEqual(first);
});
