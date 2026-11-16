import { afterEach, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import { codeTokens, createEditor, type EditorHandle, languageSupport } from "./codemirror";

let editor: EditorHandle | undefined;
afterEach(() => {
  editor?.destroy();
  editor = undefined;
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

test.each([
  ["file.js", 'function greet(name) { const value = "hello"; return name; }'],
  ["file.jsx", 'function greet(name) { return <div title="hello">{name}</div>; }'],
  ["file.ts", "function greet(name: string): string { return name; }"],
  ["file.tsx", 'function greet(name: string) { return <div title="hello">{name}</div>; }'],
  ["file.py", 'def greet(name):\n    value = "hello"\n    return name\n'],
])("%s uses its installed grammar for functions and variables", async (ref, text) => {
  const tokens = await codeTokens(ref, text);
  const functionName = tokens.find((token) => text.slice(token.from, token.to) === "greet");
  expect(functionName?.className).toContain("tok-function");
  expect(tokens.some((token) => token.className.includes("tok-variableName"))).toBe(true);
  expect(tokens.some((token) => token.className.includes("tok-keyword"))).toBe(true);
});

test("JSON keeps strings and numbers distinct", async () => {
  const text = '{"name":"hello","count":42}';
  const tokens = await codeTokens("package.json", text);
  expect(tokens.find((token) => text.slice(token.from, token.to) === '"hello"')?.className).toBe(
    "tok-string",
  );
  expect(tokens.find((token) => text.slice(token.from, token.to) === "42")?.className).toBe(
    "tok-number",
  );
});

test.each(["\n", "\r\n", "\r"])(
  "token offsets preserve original %j source, BOM and Unicode",
  async (newline) => {
    const text = [
      '\uFEFFconst value = "\u{1f642} caf\u00e9";',
      "/* comment spanning",
      "   two lines */",
      "function greet(name) { return value; }",
      "",
    ].join(newline);
    const tokens = await codeTokens("source.js", text);
    let end = 0;
    let restored = "";
    expect(tokens.length).toBeGreaterThan(0);
    expect(Object.isFrozen(tokens)).toBe(true);
    for (const token of tokens) {
      expect(Object.isFrozen(token)).toBe(true);
      expect(Number.isInteger(token.from) && Number.isInteger(token.to)).toBe(true);
      expect(token.from).toBeGreaterThanOrEqual(end);
      expect(token.to).toBeGreaterThan(token.from);
      expect(token.to).toBeLessThanOrEqual(text.length);
      expect(token.className).toMatch(/^tok-[\w]+(?: tok-[\w]+)*$/);
      restored += text.slice(end, token.from) + text.slice(token.from, token.to);
      end = token.to;
    }
    expect(restored + text.slice(end)).toBe(text);
    const functionName = tokens.find((token) => token.className.includes("tok-function"));
    expect(functionName?.from).toBe(text.indexOf("greet"));
    expect(tokens.some((token) => text.slice(token.from, token.to).includes(newline))).toBe(true);
  },
);

test("unknown suffixes stay plain, without changing their content", async () => {
  expect(await languageSupport(null)).toBeNull();
  expect(await codeTokens("notes.txt", "function greet() {}\r\n")).toEqual([]);
});

test("the highlighting ceiling leaves larger readable files plain", async () => {
  vi.spyOn(performance, "now").mockReturnValue(0);
  const text = `/*${"x".repeat((1 << 18) - 4)}*/`;
  const tokens = await codeTokens("large.js", text);
  expect(tokens).toHaveLength(1);
  expect(tokens[0]).toMatchObject({ from: 0, to: text.length, className: "tok-comment" });
  expect(await codeTokens("large.js", `${text}x`)).toEqual([]);
  expect(await codeTokens("large.js", "x".repeat(1 << 20))).toEqual([]);
});

test("the range cap returns all plain text rather than partial highlighting", async () => {
  vi.spyOn(performance, "now").mockReturnValue(0);
  expect(await codeTokens("tokens.js", "value;".repeat(4096))).toHaveLength(8192);
  expect(await codeTokens("tokens.js", "value;".repeat(4097))).toEqual([]);
});

async function jsonParser() {
  const support = await languageSupport("json");
  if (!support) throw new Error("Missing installed JSON grammar");
  return support.language.parser;
}

test("a parser failure has the same plain fallback", async () => {
  const parser = await jsonParser();
  vi.spyOn(parser, "startParse").mockImplementation(() => {
    throw new Error("Synthetic grammar failure");
  });
  expect(await codeTokens("file.json", "{}")).toEqual([]);
});

test("the parse-step cap stops even when an injected clock never advances", async () => {
  const parser = await jsonParser();
  const advance = vi.fn(() => null);
  vi.spyOn(parser, "startParse").mockReturnValue({
    advance,
    parsedPos: 0,
    stoppedAt: null,
    stopAt: vi.fn(),
  });
  vi.spyOn(performance, "now").mockReturnValue(0);
  expect(await codeTokens("file.json", "{}")).toEqual([]);
  expect(advance).toHaveBeenCalledTimes(32768);
});

test("the cooperative time budget stops before another parse advance", async () => {
  const parser = await jsonParser();
  const advance = vi.fn(() => null);
  vi.spyOn(parser, "startParse").mockReturnValue({
    advance,
    parsedPos: 0,
    stoppedAt: null,
    stopAt: vi.fn(),
  });
  vi.spyOn(performance, "now").mockReturnValueOnce(0).mockReturnValueOnce(0).mockReturnValue(21);
  expect(await codeTokens("file.json", "{}")).toEqual([]);
  expect(advance).toHaveBeenCalledOnce();
});

test("a budget exhausted during highlighting discards already collected ranges", async () => {
  const parser = await jsonParser();
  const tree = parser.parse('{"name":42}');
  vi.spyOn(parser, "startParse").mockReturnValue({
    advance: () => tree,
    parsedPos: tree.length,
    stoppedAt: null,
    stopAt: vi.fn(),
  });
  vi.spyOn(performance, "now")
    .mockReturnValueOnce(0)
    .mockReturnValueOnce(0)
    .mockReturnValueOnce(0)
    .mockReturnValue(21);
  expect(await codeTokens("file.json", '{"name":42}')).toEqual([]);
});

test("abort before load or during parsing leaves no tokens", async () => {
  const stopped = new AbortController();
  stopped.abort();
  expect(await codeTokens("file.json", "{}", stopped.signal)).toEqual([]);
  const loading = new AbortController();
  const pending = codeTokens("file.json", "{}", loading.signal);
  loading.abort();
  expect(await pending).toEqual([]);
  const parser = await jsonParser();
  const controller = new AbortController();
  const advance = vi.fn(() => {
    controller.abort();
    return null;
  });
  vi.spyOn(parser, "startParse").mockReturnValue({
    advance,
    parsedPos: 0,
    stoppedAt: null,
    stopAt: vi.fn(),
  });
  expect(await codeTokens("file.json", "{}", controller.signal)).toEqual([]);
  expect(advance).toHaveBeenCalledOnce();
});

test("the full editor colors functions and variables while preserving save and dirty behavior", async () => {
  const parent = document.createElement("div");
  document.body.append(parent);
  const text = "function greet(name) { return name; }";
  const dirty = vi.fn();
  const save = vi.fn();
  editor = await createEditor({
    parent,
    text,
    base: text,
    ref: "file.js",
    wrap: false,
    fontSize: 13,
    label: "Synthetic editor",
    onDirty: dirty,
    onSave: save,
  });
  expect(parent.querySelector(".tok-function")?.textContent).toBe("greet");
  expect(parent.querySelector(".tok-variableName:not(.tok-function)")?.textContent).toBe("name");
  editor.setText(`${text}\n`);
  expect(dirty).toHaveBeenCalledExactlyOnceWith(true);
  parent
    .querySelector(".cm-content")
    ?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "s", ctrlKey: true, bubbles: true, cancelable: true }),
    );
  expect(save).toHaveBeenCalledOnce();
  expect(editor.text()).toBe(`${text}\n`);
  editor.setBase(`${text}\n`);
  expect(dirty).toHaveBeenLastCalledWith(false);
});

const css = readCss(import.meta.url, "../../styles/tokens.css");
const syntaxCss = readCss(import.meta.url, "codemirror.css");
const declarations = (block: string) =>
  new Map([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2]]));
function luminance(color: string): number {
  const rgb = [1, 3, 5].map((start) => Number.parseInt(color.slice(start, start + 2), 16) / 255);
  const linear = rgb.map((value) =>
    value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return (linear[0] ?? 0) * 0.2126 + (linear[1] ?? 0) * 0.7152 + (linear[2] ?? 0) * 0.0722;
}

test.each(["dark", "light"])(
  "%s syntax roles clear 4.5:1 on code surfaces in both contrast modes",
  (theme) => {
    const paletteBlock =
      theme === "dark"
        ? css.match(/\.code-syntax,\s*\.cm-editor\s*\{([^}]+)\}/)?.[1]
        : css.match(
            /\[data-theme="light"\] \.code-syntax,\s*\[data-theme="light"\] \.cm-editor\s*\{([^}]+)\}/,
          )?.[1];
    expect(paletteBlock).toBeDefined();
    const palette = declarations(paletteBlock ?? "");
    const base = declarations(
      css.match(new RegExp(`\\[data-theme="${theme}"\\]\\s*\\{([^}]+)\\}`))?.[1] ?? "",
    );
    const nativeSelection = syntaxCss.match(
      /:is\(\.cm-editor,\s*\.code-syntax\) ::selection\s*\{([^}]+)\}/,
    )?.[1];
    expect(nativeSelection).toBeDefined();
    expect(nativeSelection).toMatch(/color:\s*var\(--t0\)/);
    expect(nativeSelection).toMatch(/background:\s*var\(--code-selection\)/);
    for (const mode of ["normal", "more"]) {
      const selectedText = luminance(base.get("--t0") ?? "");
      const selectedFill = luminance(palette.get("--code-selection") ?? "");
      expect(
        (Math.max(selectedText, selectedFill) + 0.05) /
          (Math.min(selectedText, selectedFill) + 0.05),
        `${theme}/${mode}/native-selection`,
      ).toBeGreaterThanOrEqual(4.5);
      // More contrast changes borders/text, while these fills and the scoped selection stay put.
      for (const background of [
        base.get("--s0"),
        base.get("--fill-hi"),
        palette.get("--code-selection"),
      ]) {
        expect(background).toMatch(/^#[\da-f]{6}$/i);
        for (const role of [
          "keyword",
          "function",
          "variable",
          "string",
          "number",
          "type",
          "meta",
          "invalid",
          "comment",
        ]) {
          const color = palette.get(`--code-${role}`);
          expect(color).toMatch(/^#[\da-f]{6}$/i);
          const foreground = luminance(color ?? "");
          const under = luminance(background ?? "");
          const ratio = (Math.max(foreground, under) + 0.05) / (Math.min(foreground, under) + 0.05);
          expect(ratio, `${theme}/${mode}/${role}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
    expect(syntaxCss).toMatch(
      /\.code-syntax \.code-line-selected\s*\{\s*background:\s*var\(--code-selection\)/,
    );
    expect(syntaxCss).toMatch(/box-shadow:\s*inset 2px 0 0 var\(--or\)/);
    expect(syntaxCss).toContain(".tok-variableName");
    expect(syntaxCss).toContain(".tok-function");
  },
);
