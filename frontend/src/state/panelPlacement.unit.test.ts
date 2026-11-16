import { expect, test } from "vitest";
import { defaultPanelPlacement, placePanel } from "./panelPlacement";

test("the existing four panels keep their order and optional Skills follows Routines", () => {
  expect(defaultPanelPlacement()).toEqual({
    left: ["sessions", "agents"],
    right: ["approvals", "routines", "skills"],
  });
  const first = defaultPanelPlacement();
  first.left = [];
  expect(defaultPanelPlacement().left).toEqual(["sessions", "agents"]);
});

test("move side and insert before identify panels without mutating the prior placement", () => {
  const initial = defaultPanelPlacement();
  const right = placePanel(initial, "sessions", "right", "routines");
  expect(right).toEqual({
    left: ["agents"],
    right: ["approvals", "sessions", "routines", "skills"],
  });
  const reordered = placePanel(right, "skills", "right", "approvals");
  expect(reordered.right).toEqual(["skills", "approvals", "sessions", "routines"]);
  expect(initial).toEqual(defaultPanelPlacement());
});

test("invalid targets, own target and an unchanged final position are inert", () => {
  const initial = defaultPanelPlacement();
  expect(placePanel(initial, "sessions", "left", "routines")).toBe(initial);
  expect(placePanel(initial, "sessions", "left", "sessions")).toBe(initial);
  expect(placePanel(initial, "agents", "left")).toBe(initial);
  expect(placePanel(initial, "skills", "right")).toBe(initial);
});

test("reset produces the canonical order after repeated cross-host moves", () => {
  const moved = placePanel(
    placePanel(defaultPanelPlacement(), "agents", "right"),
    "skills",
    "left",
  );
  expect(moved.left).toEqual(["sessions", "skills"]);
  expect(moved.right).toEqual(["approvals", "routines", "agents"]);
  expect(defaultPanelPlacement()).toEqual({
    left: ["sessions", "agents"],
    right: ["approvals", "routines", "skills"],
  });
});
