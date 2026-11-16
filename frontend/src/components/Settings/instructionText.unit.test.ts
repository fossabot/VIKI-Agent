import { describe, expect, it } from "vitest";
import { contentFor, editableText, hiddenLines, visibleText } from "./instructionText";

describe("editableText and contentFor", () => {
  it.each([
    ["LF", "a\nb\n", { bom: false, newline: "lf", text: "a\nb\n" }],
    ["CRLF", "a\r\nb\r\n", { bom: false, newline: "crlf", text: "a\nb\n" }],
    ["no break", "abc", { bom: false, newline: "none", text: "abc" }],
    ["BOM and CRLF", "\uFEFF# A\r\nB", { bom: true, newline: "crlf", text: "# A\nB" }],
    ["BOM only", "\uFEFF", { bom: true, newline: "none", text: "" }],
    ["empty", "", { bom: false, newline: "none", text: "" }],
    ["tab", "a\tb\n", { bom: false, newline: "lf", text: "a\tb\n" }],
  ])("%s round-trips byte-exact", (_name, source, expected) => {
    const base = editableText(source);
    expect(base).toEqual(expected);
    if ("reason" in base) throw new Error("expected editable");
    expect(contentFor(base, base.text)).toBe(source);
  });

  it("applies the source's own breaks and BOM to an edit", () => {
    const base = editableText("\uFEFFa\r\nb");
    if ("reason" in base) throw new Error("expected editable");
    expect(contentFor(base, "a\nb\nc")).toBe("\uFEFFa\r\nb\r\nc");
    const lf = editableText("a\n");
    if ("reason" in lf) throw new Error("expected editable");
    expect(contentFor(lf, "x\ny")).toBe("x\ny");
  });

  it.each([
    ["a lone CR", "a\rb", "line_endings"],
    ["mixed breaks", "a\r\nb\nc", "line_endings"],
    ["a CR before a CR", "a\r\r\nb", "line_endings"],
    ["BEL", "a\u0007", "control"],
    ["NUL", "a\u0000", "control"],
    ["a control and a lone CR", "a\u0001\rb", "control"],
  ])("%s is not editable", (_name, source, reason) => {
    expect(editableText(source)).toEqual({ reason });
  });
});

describe("visibleText", () => {
  it.each([
    ["a right-to-left override", "never \u202Eask", "never <U+202E>ask"],
    ["an isolate", "\u2066b\u2069", "<U+2066>b<U+2069>"],
    ["an Arabic letter mark", "a\u061Cb", "a<U+061C>b"],
    ["a left-to-right mark", "a\u200Eb", "a<U+200E>b"],
    ["a zero-width space", "de\u200Blete", "de<U+200B>lete"],
    ["a joiner between ASCII letters", "de\u200Dlete", "de<U+200D>lete"],
    ["a joiner at the end", "م\u200C", "م<U+200C>"],
    ["a lone CR", "a\rb", "a<U+000D>b"],
    ["a line separator", "a\u2028b", "a<U+2028>b"],
    ["a Unicode tag", "x\u{E0041}", "x<U+E0041>"],
  ])("writes %s as its code", (_name, text, shown) => {
    expect(visibleText(text)).toEqual({ text: shown, hidden: true });
  });

  it.each([
    ["CRLF breaks", "a\r\nb\r\n"],
    ["a non-joiner in Persian", "می\u200Cخواهم"],
    ["a joiner in an emoji sequence", "\u{1F469}\u200D\u{1F4BB}"],
    ["an emoji's presentation selector", "\u2764\uFE0F"],
    ["a tab", "a\tb"],
  ])("leaves %s as written", (_name, text) => {
    expect(visibleText(text)).toEqual({ text, hidden: false });
  });

  it("numbers only the lines that hold one, up to the limit", () => {
    const text = "ok\nnever \u202Eask\nok\n\u200B\n\u200B";
    expect(hiddenLines(text, 2)).toEqual([
      { line: 2, text: "never <U+202E>ask" },
      { line: 4, text: "<U+200B>" },
    ]);
    expect(hiddenLines("plain\ntext", 5)).toEqual([]);
  });
});
