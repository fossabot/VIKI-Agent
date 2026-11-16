import { describe, expect, it } from "vitest";
import {
  BOT_HUES,
  BOT_ICON_IDS,
  type BotDesign,
  buildGeometry,
  designFor,
  dotMap,
  EYE_SHAPES,
  FREE_EMBLEMS,
  HUES,
  isBotIconId,
  LED,
  pixelMap,
  SEAT_NAMES,
} from "./botIconDesign";

const SEATS = {
  chief: { eyes: "ring", emblem: "door", hue: "orange" },
  ops: { eyes: "round", emblem: "loop", hue: "green" },
  checker: { eyes: "narrow", emblem: "check", hue: "blue" },
  research: { eyes: "round", emblem: "lens", hue: "purple" },
  writer: { eyes: "arc", emblem: "nib", hue: "red" },
  engineer: { eyes: "block", emblem: "wrench", hue: "amber" },
  builder: { eyes: "block", emblem: "wrench", hue: "amber" },
  desk: { eyes: "square", emblem: "tray", hue: "teal" },
} as const;

const lit = (rows: readonly string[]) => rows.join("").split("#").length - 1;
const designs = Array.from({ length: 200 }, (_, i) => designFor(`profile-${i}`, "Planner"));
const kinds = (key: keyof BotDesign) => new Set(designs.map((design) => design[key]));

describe("designFor", () => {
  it("exposes the team icons in team order followed by every free icon", () => {
    expect(BOT_ICON_IDS).toEqual([
      "door",
      "loop",
      "lens",
      "nib",
      "wrench",
      "tray",
      "check",
      ...FREE_EMBLEMS,
    ]);
    expect(BOT_ICON_IDS).toHaveLength(27);
    expect(new Set(BOT_ICON_IDS).size).toBe(27);
    expect(BOT_ICON_IDS.every(isBotIconId)).toBe(true);
    expect(BOT_ICON_IDS.map((icon) => designFor("p1", "Planner", icon).emblem)).toEqual(
      BOT_ICON_IDS,
    );
    expect(isBotIconId("builder")).toBe(false);
    expect(isBotIconId("unknown")).toBe(false);
    expect(isBotIconId(null)).toBe(false);
  });

  it("gives each team seat its own design, whatever its profile id or letter case", () => {
    expect([...SEAT_NAMES].sort()).toEqual(Object.keys(SEATS).sort());
    for (const [seat, parts] of Object.entries(SEATS)) {
      const title = `${seat.charAt(0).toUpperCase()}${seat.slice(1)}`;
      for (const name of [seat, seat.toUpperCase(), `  ${title} `]) {
        expect(designFor(`profile-${name}`, name)).toMatchObject({
          seat,
          head: "orb",
          visor: "pill",
          gear: "none",
          ...parts,
        });
      }
    }
  });

  it("keeps a Bot's design for its profile", () => {
    expect(designFor("p1", "Planner")).toEqual(designFor("p1", "Planner"));
    expect(buildGeometry(designFor("p1", "Planner"))).toEqual(
      buildGeometry(designFor("p1", "Planner")),
    );
  });

  it("overrides only the emblem for a known icon and ignores unknown values", () => {
    const base = designFor("p1", "Planner");
    expect(designFor("p1", "Planner", "door")).toEqual({ ...base, emblem: "door" });
    expect(designFor("p1", "Planner", "unknown")).toEqual(base);
    expect(designFor("p1", "Builder")).toMatchObject({ seat: "builder", emblem: "wrench" });
  });

  it("varies every other Bot's shapes and colour by its profile", () => {
    expect(designs.every((design) => design.seat === null)).toBe(true);
    expect(kinds("head")).toEqual(new Set(["orb", "block", "capsule"]));
    expect(kinds("visor")).toEqual(new Set(["pill", "band", "round", "goggles"]));
    expect(kinds("gear")).toEqual(new Set(["none", "antenna", "twin", "ear"]));
    expect(kinds("eyes")).toEqual(new Set(EYE_SHAPES));
    expect(kinds("emblem")).toEqual(new Set(FREE_EMBLEMS));
    expect(kinds("hue")).toEqual(new Set(HUES));
    const looks = new Set(
      designs.map((d) => [d.head, d.visor, d.gear, d.eyes, d.emblem, d.hue].join("/")),
    );
    expect(looks.size).toBeGreaterThan(190);
  });

  it("keeps the seats' emblems for the seats", () => {
    const seatEmblems = Object.values(SEATS).map((parts) => parts.emblem);
    expect(FREE_EMBLEMS.filter((emblem) => (seatEmblems as string[]).includes(emblem))).toEqual([]);
  });
});

describe("the random pool", () => {
  it("keeps the emblems, eye shapes and hues a profile can draw", () => {
    expect(FREE_EMBLEMS).toEqual(
      "bolt star heart leaf bell note bubble key flag flask shield globe pin cloud book chart spark moon gear target".split(
        " ",
      ),
    );
    expect(EYE_SHAPES).toEqual(
      "round ring narrow arc block square heart plus line diamond dot star crescent bars".split(
        " ",
      ),
    );
    expect(HUES).toEqual(["blue", "green", "orange", "purple", "red", "teal"]);
    expect(designFor("profile-0", "Planner")).toEqual({
      seat: null,
      head: "block",
      visor: "pill",
      gear: "antenna",
      eyes: "bars",
      emblem: "moon",
      hue: "purple",
      grain: 30960034,
    });
  });

  it("adds two assigned-only hues, on bot-only tokens, outside the drawn pool", () => {
    expect(LED.amber).toBe("var(--bot-amber)");
    expect(LED.pink).toBe("var(--bot-pink)");
    expect(BOT_HUES).toEqual(["blue", "green", "orange", "purple", "red", "teal", "amber", "pink"]);
    expect(HUES).not.toContain("amber");
    expect(HUES).not.toContain("pink");
  });
});

describe("buildGeometry", () => {
  it("lights the eyes and the emblem in the design's LED colour, a label token", () => {
    for (const hue of HUES) {
      expect(buildGeometry({ ...designFor("p1", "Planner"), hue }).led).toBe(LED[hue]);
      expect(LED[hue]).toMatch(/^var\(--label-(blue|green|orange|purple|red|slate)\)$/);
    }
    expect(LED.teal).toBe("var(--label-slate)");
  });

  it("draws one dot per lit cell in each eye and one pixel per lit emblem cell", () => {
    for (const design of designs.slice(0, 60)) {
      const geometry = buildGeometry(design);
      expect(geometry.eyesLit.match(/M/g)).toHaveLength(2 * lit(dotMap(design.eyes)));
      expect(geometry.emblem.match(/M/g)).toHaveLength(lit(pixelMap(design.emblem)));
    }
  });

  it("gives every lit dot a hot centre and shades every emblem cube on its four edges", () => {
    for (const design of designs.slice(0, 60)) {
      const geometry = buildGeometry(design);
      const cubes = lit(pixelMap(design.emblem));
      expect(geometry.eyesHot.match(/M/g)).toHaveLength(2 * lit(dotMap(design.eyes)));
      expect(geometry.emblemLight.match(/M/g)).toHaveLength(2 * cubes);
      expect(geometry.emblemShade.match(/M/g)).toHaveLength(2 * cubes);
    }
  });

  it("frames every visor glass and lights only the head's tiles that face up", () => {
    for (const design of designs.slice(0, 60)) {
      const geometry = buildGeometry(design);
      expect(geometry.frame).toHaveLength(geometry.visor.length);
      expect(geometry.visor).toHaveLength(design.visor === "goggles" ? 2 : 1);
      const glints = Object.entries(geometry.glints);
      expect(glints.length).toBeGreaterThan(0);
      for (const [opacity] of glints) expect(Number(opacity)).toBeGreaterThanOrEqual(0.1);
    }
  });

  it("keeps every path finite and within reach of the view box", () => {
    for (const design of [...designs.slice(0, 60), ...SEAT_NAMES.map((s) => designFor(s, s))]) {
      const geometry = buildGeometry(design);
      const paths = [
        geometry.head,
        ...Object.values(geometry.tiles),
        ...Object.values(geometry.glints),
        ...Object.values(geometry.loose),
        geometry.looseLight,
        ...geometry.visor,
        ...geometry.frame,
        geometry.streak,
        geometry.gloss,
        geometry.eyesLit,
        geometry.eyesHot,
        geometry.eyesDim,
        geometry.emblem,
        geometry.emblemLight,
        geometry.emblemShade,
        ...geometry.sparks.map((spark) => spark.d),
        geometry.gear.stems,
        geometry.gear.balls,
        geometry.gear.ballsHot,
        geometry.gear.ear,
        geometry.gear.earLight,
      ].join(" ");
      expect(paths).not.toMatch(/NaN|Infinity|undefined/);
      const numbers = (paths.match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
      expect(numbers.every((n) => n > -70 && n < 170)).toBe(true);
    }
  });
});

it("draws every dot and pixel map on a 7 by 7 grid", () => {
  const emblems = [...FREE_EMBLEMS, ...Object.values(SEATS).map((parts) => parts.emblem)];
  for (const rows of [...EYE_SHAPES.map(dotMap), ...emblems.map(pixelMap)]) {
    expect(rows).toHaveLength(7);
    for (const row of rows) expect(row).toMatch(/^[.#]{7}$/);
  }
});
