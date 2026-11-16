/**
 * visibleText: which characters a decision surface writes out as codes, and which it leaves as
 * they are. Every hidden character below is built from its code point, so this file itself holds
 * none: the last test reads the two source files to keep it that way.
 */
import { describe, expect, test } from "vitest";
import { visibleLine, visibleParts, visibleText } from "./visibleText";
import source from "./visibleText.ts?raw";
import own from "./visibleText.unit.test.ts?raw";

const at = (...points: number[]): string => String.fromCodePoint(...points);

const RLO = at(0x202e);
const PDF = at(0x202c);
const LRI = at(0x2066);
const PDI = at(0x2069);
const ZWSP = at(0x200b);
const ZWJ = at(0x200d);
const VS16 = at(0xfe0f);

describe("visibleText", () => {
  test("ordinary text comes back unchanged, tabs and line breaks included", () => {
    const command = "git status --short\n\tgit log --oneline -5 && echo 'done'";
    expect(visibleText(command)).toEqual({ text: command, hidden: false });
    expect(visibleText("")).toEqual({ text: "", hidden: false });
  });

  test("text in another script keeps its own spaces and marks", () => {
    // A full-width space and a no-break space paint as the gaps they are, so neither is hidden.
    const message = `git commit -m "修正${at(0x3000)}テスト" --author "A${at(0xa0)}B"`;
    expect(visibleText(message)).toEqual({ text: message, hidden: false });
  });

  test("bidi controls are written out where they sit, so the command reads in the order it runs", () => {
    // Painted raw, this reads `echo "safe text ; rm -rf ~/notes #"`: one quoted string.
    const command = `echo "safe text${RLO}"${LRI} ; rm -rf ~/notes #${PDI}${PDF}`;
    expect(visibleText(command)).toEqual({
      text: 'echo "safe text<U+202E>"<U+2066> ; rm -rf ~/notes #<U+2069><U+202C>',
      hidden: true,
    });
  });

  test.each([
    ["a zero-width space in a path", `rm -rf ./build${ZWSP}/../x`, "rm -rf ./build<U+200B>/../x"],
    ["a zero-width joiner between letters", `a${ZWJ}b`, "a<U+200D>b"],
    ["a soft hyphen", `co${at(0xad)}op`, "co<U+00AD>op"],
    ["a byte order mark", `${at(0xfeff)}echo`, "<U+FEFF>echo"],
    ["a word joiner", `a${at(0x2060)}b`, "a<U+2060>b"],
    ["a left-to-right mark", `a${at(0x200e)}b`, "a<U+200E>b"],
    ["an Arabic letter mark", `a${at(0x61c)}b`, "a<U+061C>b"],
    ["a Hangul filler", `a${at(0x3164)}b`, "a<U+3164>b"],
    ["a variation selector on a letter", `tmp${at(0xfe00)}/x`, "tmp<U+FE00>/x"],
    ["tag characters above U+FFFF", `x${at(0xe0041, 0xe0042)}y`, "x<U+E0041><U+E0042>y"],
    ["a line separator", `a${at(0x2028)}b`, "a<U+2028>b"],
    ["a paragraph separator", `a${at(0x2029)}b`, "a<U+2029>b"],
    ["a NUL", `a${at(0)}b`, "a<U+0000>b"],
    ["a carriage return", "a\r\nb", "a<U+000D>\nb"],
    ["an escape character", `a${at(0x1b)}[31mb`, "a<U+001B>[31mb"],
    ["a delete character", `a${at(0x7f)}b`, "a<U+007F>b"],
    ["a C1 control", `a${at(0x85)}b`, "a<U+0085>b"],
  ])("%s is written out as its code", (_name, raw, shown) => {
    expect(visibleText(raw)).toEqual({ text: shown, hidden: true });
  });

  test("an emoji keeps its own presentation selector; the same selector anywhere else is written out", () => {
    const warning = `git commit -m "${at(0x26a0)}${VS16} fix"`;
    expect(visibleText(warning)).toEqual({ text: warning, hidden: false });
    const bin = `${at(0x1f5d1)}${VS16}`;
    expect(visibleText(bin)).toEqual({ text: bin, hidden: false });
    expect(visibleText(`tmp${VS16}/x`)).toEqual({ text: "tmp<U+FE0F>/x", hidden: true });
    // Only the first selector belongs to the pictograph.
    expect(visibleText(`${at(0x26a0)}${VS16}${VS16}`)).toEqual({
      text: `${at(0x26a0)}${VS16}<U+FE0F>`,
      hidden: true,
    });
  });

  test("the sequences everyday emoji are made of are left alone", () => {
    const keycap = `1${VS16}${at(0x20e3)}`;
    const technologist = at(0x1f469, 0x200d, 0x1f4bb);
    const family = at(0x1f468, 0x200d, 0x1f469, 0x200d, 0x1f467);
    const toned = `${at(0x1f926, 0x1f3fd)}${ZWJ}${at(0x2642)}${VS16}`;
    const flag = `${at(0x1f3f3)}${VS16}${ZWJ}${at(0x1f308)}`;
    for (const emoji of [keycap, technologist, family, toned, flag]) {
      expect(visibleText(`step ${emoji} done`)).toEqual({
        text: `step ${emoji} done`,
        hidden: false,
      });
    }
  });

  test("a joiner or a selector that only borrows an emoji is still written out", () => {
    const woman = at(0x1f469);
    // A joiner with a pictograph on one side only joins nothing a reader could see.
    expect(visibleText(`${woman}${ZWJ}rm`)).toEqual({ text: `${woman}<U+200D>rm`, hidden: true });
    expect(visibleText(`rm${ZWJ}${woman}`)).toEqual({ text: `rm<U+200D>${woman}`, hidden: true });
    expect(visibleText(`${woman}${ZWJ}${ZWJ}${woman}`).hidden).toBe(true);
    // A keycap's selector without its cap, and the selectors above U+FFFF, which can carry data.
    expect(visibleText(`1${VS16}x`)).toEqual({ text: "1<U+FE0F>x", hidden: true });
    expect(visibleText(`${woman}${VS16}${at(0xe0100, 0xe0101)}`)).toEqual({
      text: `${woman}${VS16}<U+E0100><U+E0101>`,
      hidden: true,
    });
    // A bidi control is one wherever it sits.
    expect(visibleText(`${woman}${RLO}x`)).toEqual({ text: `${woman}<U+202E>x`, hidden: true });
  });

  test("what it returns has nothing left to write out", () => {
    const hostile = `a${RLO}b${ZWSP}c${at(0)}d${at(0x2028)}e${at(0xe0041)}f`;
    const once = visibleText(hostile);
    expect(once.hidden).toBe(true);
    expect(visibleText(once.text)).toEqual({ text: once.text, hidden: false });
  });
});

describe("visibleParts", () => {
  test("a code this app wrote is told apart from the same letters typed into the command", () => {
    // The first `<U+202E>` is typed text, a shell redirect; the second stands for the character.
    expect(visibleParts(`cat <U+202E> out${RLO}`)).toEqual([
      { text: "cat <U+202E> out", code: false },
      { text: "<U+202E>", code: true },
    ]);
  });

  test("the pieces join to exactly what visibleText returns", () => {
    for (const raw of ["", "plain", `${RLO}${PDF}`, `a${LRI}b${PDI}c`, `x${ZWSP}${ZWSP}y\n`]) {
      const parts = visibleParts(raw);
      expect(parts.length).toBeGreaterThan(0);
      expect(parts.map((part) => part.text).join("")).toBe(visibleText(raw).text);
      expect(parts.some((part) => part.code)).toBe(visibleText(raw).hidden);
    }
  });

  test("text with nothing hidden is one plain piece", () => {
    expect(visibleParts("git status")).toEqual([{ text: "git status", code: false }]);
  });
});

describe("visibleLine", () => {
  test("a line break is drawn, so a second command does not read as arguments of the first", () => {
    expect(visibleLine("echo ok\nrm -rf ./build")).toBe("echo ok ↵ rm -rf ./build");
    expect(visibleLine("git status")).toBe("git status");
  });

  test("hidden characters are written out as in visibleText", () => {
    expect(visibleLine(`a${RLO}b\nc`)).toBe("a<U+202E>b ↵ c");
  });
});

test("neither source file holds a hidden character of its own", () => {
  // An escape that an editor or a tool turned into the character itself would still pass every
  // test above, and would put an invisible character into the very code that looks for them.
  for (const text of [source, own]) {
    expect(visibleText(text.replaceAll("\r\n", "\n"))).toEqual({
      text: text.replaceAll("\r\n", "\n"),
      hidden: false,
    });
  }
});
