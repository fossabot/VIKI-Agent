import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

// jsdom applies no stylesheet, so the source is pinned.
const css = readCss(import.meta.url, "Kanban.css");

test("the lanes are a wrapping grid with no horizontal scroll", () => {
  const lanes = ruleIn(css, ".kanban-lanes");
  expect(lanes).toMatch(/grid-template-columns:\s*repeat\(auto-fit, minmax\(200px, 1fr\)\)/);
  expect(lanes).not.toMatch(/overflow-x|grid-auto-flow/);
});

test("a lane has a 3px top rule in its own colour, neutral by default", () => {
  const lane = ruleIn(css, ".kanban-lane");
  expect(lane).toMatch(/--kanban-lane:\s*var\(--t3\)/);
  expect(lane).toMatch(/border-top:\s*3px solid var\(--kanban-lane\)/);
});

test.each(["todo", "working", "blocked", "review", "done"])(
  "the %s lane takes its status colour",
  (stage) => {
    expect(ruleIn(css, `.kanban-lane[data-stage="${stage}"]`)).toMatch(
      new RegExp(`--kanban-lane:\\s*var\\(--kanban-${stage}\\)`),
    );
  },
);

test("the glyph and the count chip use the lane colour", () => {
  expect(ruleIn(css, ".kanban-lane-glyph")).toMatch(/color:\s*var\(--kanban-lane\)/);
  const chip = ruleIn(css, ".kanban-count");
  expect(chip).toMatch(/border:\s*1px solid var\(--kanban-lane\)/);
  expect(chip).toMatch(/background:\s*color-mix\(in srgb, var\(--kanban-lane\) 7%, transparent\)/);
  expect(chip).toMatch(/color:\s*var\(--t1\)/);
});

test("narrow windows stack the lanes in one column", () => {
  const narrow = narrowBlock(css);
  expect(ruleIn(narrow, ".kanban-lanes")).toMatch(/grid-template-columns:\s*1fr/);
  expect(ruleIn(narrow, ".kanban-lane")).toMatch(/min-height:\s*0/);
});

test("forced colours use system colours for the rule, glyph, chip and focus", () => {
  const forced = narrowBlock(css, "@media (forced-colors: active) {");
  expect(ruleIn(forced, ".kanban-lane")).toMatch(/border-top-color:\s*CanvasText/);
  expect(ruleIn(forced, ".kanban-lane-glyph")).toMatch(/color:\s*CanvasText/);
  expect(ruleIn(forced, ".kanban-count")).toMatch(/border-color:\s*CanvasText/);
  expect(ruleIn(forced, ".kanban-card:focus-within")).toMatch(/outline:\s*2px solid Highlight/);
});

test("the sheet has no animation or transition", () => {
  expect(css).not.toMatch(/animation|transition/);
});

test("owner names on cards stay neutral, not the Bot's hue", () => {
  expect(ruleIn(css, ".kanban-card .bot-tag-name")).toMatch(/color:\s*var\(--t1\)/);
});

test("the empty state keeps its buttons at their own width", () => {
  expect(ruleIn(css, ".kanban-empty")).toMatch(/justify-items:\s*start/);
});
