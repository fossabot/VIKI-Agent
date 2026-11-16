import { describe, expect, it } from "vitest";
import { type DisplayMetadata, parseDisplayMetadata } from "./noteMetadata";

const absent: DisplayMetadata = {
  state: "absent",
  frontmatter_state: "absent",
  heading_state: "absent",
  display_title: null,
  title_source: null,
  tags: [],
  type: null,
  status: null,
};
const header = (changes: Record<string, unknown> = {}) => ({
  ...absent,
  state: "ready",
  frontmatter_state: "ready",
  display_title: "SKILL",
  title_source: "title",
  ...changes,
});

describe("display metadata wire", () => {
  it("preserves authored generic titles, Japanese, literal markup and ordered case-sensitive tags", () => {
    for (const [display_title, title_source] of [
      ["SKILL", "title"],
      ["  名前 é\u2028  ", "name"],
      [" **名** <img>literal</img> # ", "h1"],
      ["\ufeff", "title"],
    ]) {
      const value = header({
        display_title,
        title_source,
        heading_state: "ready",
        tags: ["Alpha", "alpha", " 名 "],
        type: "Note",
        status: "approved",
      });
      const original = JSON.stringify(value);
      expect(parseDisplayMetadata(value)).toBe(value);
      expect(JSON.stringify(value)).toBe(original);
    }
  });
  it("accepts every extractor structure pairing with its independent header coverage", () => {
    for (const heading_state of ["ready", "absent", "unsupported", "truncated"]) {
      const state = heading_state === "absent" ? "ready" : heading_state;
      expect(parseDisplayMetadata(header({ heading_state, state }))).not.toBeNull();
      const empty: Record<string, unknown> = {
        ...absent,
        frontmatter_state: "ready",
        heading_state,
        state: heading_state,
      };
      if (heading_state === "ready") {
        empty.display_title = "Body";
        empty.title_source = "h1";
      }
      expect(parseDisplayMetadata(empty)).not.toBeNull();
      expect(parseDisplayMetadata({ ...empty, frontmatter_state: "absent" })).not.toBeNull();
    }
    for (const state of ["unsupported", "truncated"]) {
      expect(
        parseDisplayMetadata({
          ...absent,
          state,
          frontmatter_state: state,
          heading_state: state,
          tags: null,
        }),
      ).not.toBeNull();
    }
    for (const field of ["tags", "type", "status"]) {
      const value = {
        ...absent,
        state: "ready",
        frontmatter_state: "ready",
        [field]: field === "tags" ? ["a"] : "a",
      };
      expect(parseDisplayMetadata(value)).not.toBeNull();
    }
  });
  it("uses Unicode code points for exact title and scalar bounds", () => {
    expect(parseDisplayMetadata(header({ display_title: "🧠".repeat(512) }))).not.toBeNull();
    expect(parseDisplayMetadata(header({ display_title: "🧠".repeat(513) }))).toBeNull();
    for (const field of ["type", "status"]) {
      expect(parseDisplayMetadata(header({ [field]: "🧠".repeat(64) }))).not.toBeNull();
      expect(parseDisplayMetadata(header({ [field]: "🧠".repeat(65) }))).toBeNull();
    }
    expect(
      parseDisplayMetadata(header({ tags: Array.from({ length: 16 }, (_, i) => `t${i}`) })),
    ).not.toBeNull();
    expect(
      parseDisplayMetadata(header({ tags: Array.from({ length: 17 }, (_, i) => `t${i}`) })),
    ).toBeNull();
    expect(parseDisplayMetadata(header({ tags: ["🧠".repeat(64)] }))).not.toBeNull();
    expect(parseDisplayMetadata(header({ tags: ["🧠".repeat(65)] }))).toBeNull();
  });
  it("rejects decoded controls, DEL, C1 controls, isolated surrogates and blank strings", () => {
    for (const invalid of [
      "",
      " \u2003",
      "a\u0000",
      "a\t",
      "a\n",
      "a\r",
      "a\u007f",
      "a\u0085",
      "a\u009f",
      "\ud800",
      "\udfff",
    ]) {
      for (const field of ["display_title", "type", "status", "tags"]) {
        const value = JSON.parse(
          JSON.stringify(header({ [field]: field === "tags" ? [invalid] : invalid })),
        );
        expect(parseDisplayMetadata(value)).toBeNull();
      }
    }
  });
  it("requires exact keys and types rather than stripping unsupported data", () => {
    for (const key of Object.keys(absent)) {
      const missing: Record<string, unknown> = { ...absent };
      delete missing[key];
      expect(parseDisplayMetadata(missing)).toBeNull();
    }
    for (const value of [null, [], "ready", 1, { ...absent, extra: true }])
      expect(parseDisplayMetadata(value)).toBeNull();
    for (const changes of [
      { state: "unknown" },
      { frontmatter_state: "unknown" },
      { heading_state: "unknown" },
      { display_title: 1 },
      { title_source: "path" },
      { type: false },
      { status: [] },
      { tags: null },
      { tags: "a" },
      { tags: [1] },
      { tags: ["a", "a"] },
    ])
      expect(parseDisplayMetadata(header(changes))).toBeNull();
  });
});

describe("display metadata correlations", () => {
  it("rejects contradictions that could incorrectly win title precedence", () => {
    for (const changes of [
      { state: "absent" },
      { state: "unsupported" },
      { state: "truncated" },
      { display_title: null },
      { title_source: null },
      { frontmatter_state: "absent" },
      { title_source: "h1", heading_state: "absent" },
      { title_source: "h1", heading_state: "unsupported", state: "unsupported" },
      { title_source: "h1", heading_state: "truncated", state: "truncated" },
      {
        frontmatter_state: "unsupported",
        heading_state: "unsupported",
        state: "unsupported",
        tags: null,
      },
    ])
      expect(parseDisplayMetadata(header(changes))).toBeNull();
    for (const changes of [
      { state: "ready" },
      { heading_state: "ready", state: "ready" },
      { type: "note", state: "ready" },
      { status: "active", state: "ready" },
      { tags: ["a"], state: "ready" },
    ])
      expect(parseDisplayMetadata({ ...absent, ...changes })).toBeNull();
    for (const state of ["unsupported", "truncated"]) {
      const blocked = {
        ...absent,
        state,
        frontmatter_state: state,
        heading_state: state,
        tags: null,
      };
      for (const changes of [
        { heading_state: "absent" },
        { tags: [] },
        { type: "note" },
        { status: "active" },
        { display_title: "Body", title_source: "h1" },
      ]) {
        expect(parseDisplayMetadata({ ...blocked, ...changes })).toBeNull();
      }
    }
    expect(parseDisplayMetadata(header({ heading_state: "truncated", state: "ready" }))).toBeNull();
    expect(
      parseDisplayMetadata(header({ heading_state: "unsupported", state: "truncated" })),
    ).toBeNull();
  });
});
