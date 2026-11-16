import { describe, expect, it } from "vitest";
import {
  cropForKey,
  initialCrop,
  moveCrop,
  type Rotation,
  rotateBy,
  rotatedSize,
  transformCrop,
  zoomCrop,
} from "./crop";

describe("rotatedSize", () => {
  it("keeps a 300x200 image as 300x200 at rotation 0", () => {
    expect(rotatedSize(300, 200, 0)).toEqual({ width: 300, height: 200 });
  });

  it("swaps a 300x200 image to 200x300 at rotation 90", () => {
    expect(rotatedSize(300, 200, 90)).toEqual({ width: 200, height: 300 });
  });

  it("keeps a 300x200 image as 300x200 at rotation 180", () => {
    expect(rotatedSize(300, 200, 180)).toEqual({ width: 300, height: 200 });
  });

  it("swaps a 300x200 image to 200x300 at rotation 270", () => {
    expect(rotatedSize(300, 200, 270)).toEqual({ width: 200, height: 300 });
  });
});

describe("initialCrop", () => {
  it("centres a 200 edge square at x 50 in a 300x200 landscape image", () => {
    expect(initialCrop(300, 200)).toEqual({ x: 50, y: 0, edge: 200 });
  });

  it("centres a 200 edge square at y 50 in a 200x301 portrait image", () => {
    expect(initialCrop(200, 301)).toEqual({ x: 0, y: 50, edge: 200 });
  });

  it("fills a square image exactly", () => {
    expect(initialCrop(250, 250)).toEqual({ x: 0, y: 0, edge: 250 });
  });
});

describe("moveCrop", () => {
  it("moves the crop by the given offset", () => {
    expect(moveCrop({ x: 50, y: 50, edge: 100 }, 10, -5, 400, 400)).toEqual({
      x: 60,
      y: 45,
      edge: 100,
    });
  });

  it("does not move past the left edge", () => {
    expect(moveCrop({ x: 0, y: 50, edge: 100 }, -10, 0, 400, 400)).toEqual({
      x: 0,
      y: 50,
      edge: 100,
    });
  });

  it("does not move past the right edge", () => {
    expect(moveCrop({ x: 300, y: 50, edge: 100 }, 10, 0, 400, 400)).toEqual({
      x: 300,
      y: 50,
      edge: 100,
    });
  });

  it("does not move past the top edge", () => {
    expect(moveCrop({ x: 50, y: 0, edge: 100 }, 0, -5, 400, 400)).toEqual({
      x: 50,
      y: 0,
      edge: 100,
    });
  });

  it("does not move past the bottom edge", () => {
    expect(moveCrop({ x: 50, y: 300, edge: 100 }, 0, 5, 400, 400)).toEqual({
      x: 50,
      y: 300,
      edge: 100,
    });
  });
});

describe("zoomCrop", () => {
  it("keeps the centre when zooming in", () => {
    expect(zoomCrop({ x: 50, y: 50, edge: 100 }, 1, 400, 400)).toEqual({ x: 55, y: 55, edge: 90 });
  });

  it("grows the edge by the step when zooming out", () => {
    expect(zoomCrop({ x: 50, y: 50, edge: 100 }, -1, 400, 400)).toEqual({
      x: 45,
      y: 45,
      edge: 110,
    });
  });

  it("keeps the edge at 1 when zooming in at the smallest edge", () => {
    expect(zoomCrop({ x: 0, y: 0, edge: 1 }, 1, 100, 100)).toEqual({ x: 0, y: 0, edge: 1 });
  });

  it("keeps the edge at the smallest image side when zooming out to full size", () => {
    expect(zoomCrop({ x: 0, y: 0, edge: 100 }, -1, 100, 100)).toEqual({ x: 0, y: 0, edge: 100 });
  });

  it("clamps x back inside when zooming out near the right edge", () => {
    expect(zoomCrop({ x: 300, y: 100, edge: 100 }, -1, 400, 400)).toEqual({
      x: 290,
      y: 95,
      edge: 110,
    });
  });

  it("clamps y back inside when zooming out near the bottom edge", () => {
    expect(zoomCrop({ x: 100, y: 300, edge: 100 }, -1, 400, 400)).toEqual({
      x: 95,
      y: 290,
      edge: 110,
    });
  });
});

describe("rotateBy", () => {
  it("steps by 90 degrees through the full cycle with turn 1", () => {
    let rotation: Rotation = 0;
    const visits: Rotation[] = [];
    for (let i = 0; i < 4; i++) {
      rotation = rotateBy(rotation, 1);
      visits.push(rotation);
    }
    expect(visits).toEqual([90, 180, 270, 0]);
  });

  it("steps by 90 degrees through the full cycle backwards with turn -1", () => {
    let rotation: Rotation = 0;
    const visits: Rotation[] = [];
    for (let i = 0; i < 4; i++) {
      rotation = rotateBy(rotation, -1);
      visits.push(rotation);
    }
    expect(visits).toEqual([270, 180, 90, 0]);
  });
});

describe("cropForKey", () => {
  const crop = { x: 50, y: 50, edge: 100 };

  it("moves left by one with ArrowLeft", () => {
    expect(cropForKey("ArrowLeft", false, crop, 400, 400)).toEqual({ x: 49, y: 50, edge: 100 });
  });

  it("moves left by ten with ArrowLeft and shift", () => {
    expect(cropForKey("ArrowLeft", true, crop, 400, 400)).toEqual({ x: 40, y: 50, edge: 100 });
  });

  it("moves right by one with ArrowRight", () => {
    expect(cropForKey("ArrowRight", false, crop, 400, 400)).toEqual({ x: 51, y: 50, edge: 100 });
  });

  it("moves right by ten with ArrowRight and shift", () => {
    expect(cropForKey("ArrowRight", true, crop, 400, 400)).toEqual({ x: 60, y: 50, edge: 100 });
  });

  it("moves up by one with ArrowUp", () => {
    expect(cropForKey("ArrowUp", false, crop, 400, 400)).toEqual({ x: 50, y: 49, edge: 100 });
  });

  it("moves up by ten with ArrowUp and shift", () => {
    expect(cropForKey("ArrowUp", true, crop, 400, 400)).toEqual({ x: 50, y: 40, edge: 100 });
  });

  it("moves down by one with ArrowDown", () => {
    expect(cropForKey("ArrowDown", false, crop, 400, 400)).toEqual({ x: 50, y: 51, edge: 100 });
  });

  it("moves down by ten with ArrowDown and shift", () => {
    expect(cropForKey("ArrowDown", true, crop, 400, 400)).toEqual({ x: 50, y: 60, edge: 100 });
  });

  it("zooms in with +", () => {
    expect(cropForKey("+", false, crop, 400, 400)).toEqual({ x: 55, y: 55, edge: 90 });
  });

  it("zooms in with =", () => {
    expect(cropForKey("=", false, crop, 400, 400)).toEqual({ x: 55, y: 55, edge: 90 });
  });

  it("zooms out with -", () => {
    expect(cropForKey("-", false, crop, 400, 400)).toEqual({ x: 45, y: 45, edge: 110 });
  });

  it("zooms out with _", () => {
    expect(cropForKey("_", false, crop, 400, 400)).toEqual({ x: 45, y: 45, edge: 110 });
  });

  it("returns null for an unrelated key", () => {
    expect(cropForKey("a", false, crop, 400, 400)).toBeNull();
  });
});

describe("transformCrop", () => {
  it("maps the square crop onto its rectangle for the transform request", () => {
    expect(transformCrop({ x: 3, y: 4, edge: 8 })).toEqual({ x: 3, y: 4, width: 8, height: 8 });
  });
});
