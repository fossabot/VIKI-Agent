import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { CodeFile } from "../../clients/code/codeApi";
import { type CodeToken, codeTokens } from "./codemirror";
import { FileView } from "./FileView";

vi.mock("./codemirror", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./codemirror")>();
  return { ...actual, codeTokens: vi.fn(actual.codeTokens) };
});

const highlight = vi.mocked(codeTokens);
let pending: Promise<readonly CodeToken[]>[] = [];

beforeEach(async () => {
  const actual = await vi.importActual<typeof import("./codemirror")>("./codemirror");
  pending = [];
  highlight.mockReset().mockImplementation((...args) => {
    const result = actual.codeTokens(...args);
    pending.push(result);
    return result;
  });
  // Real grammars run here; elapsed-time fallback is covered by the helper's own tests.
  vi.spyOn(performance, "now").mockReturnValue(0);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function file(path: string, text: string, over: Partial<CodeFile> = {}): CodeFile {
  return {
    path,
    text,
    size: new TextEncoder().encode(text).length,
    sha256: "x",
    encoding: "utf-8",
    newline: "lf",
    reason: null,
    ...over,
  };
}

function setup(value: CodeFile) {
  const props = { canQuote: true, onQuote: vi.fn(), onOpen: vi.fn() };
  return { ...render(<FileView file={value} {...props} />), props };
}

async function colored() {
  await act(async () => {
    await vi.dynamicImportSettled();
    await Promise.all(pending);
  });
}

const lineTexts = (container: HTMLElement) =>
  [...container.querySelectorAll(".code-text")].map((line) => line.textContent);
const coloredTexts = (container: HTMLElement, className: string) =>
  [...container.querySelectorAll(`.code-text .${className}`)].map((span) => span.textContent);

test.each([
  ["greet.py", 'def greet(name):\n\tmessage = "café 😀"\n\treturn message\n', "def"],
  [
    "greet.ts",
    'function greet(name: string) {\r\n\tconst message = "café 😀";\r\n\treturn message;\r\n}\r\n',
    "function",
  ],
])(
  "%s colors real functions, variables, keywords and strings without changing lines",
  async (path, text, keyword) => {
    const { container } = setup(file(path, text));
    await colored();
    expect(coloredTexts(container, "tok-function")).toContain("greet");
    expect(coloredTexts(container, "tok-variableName")).toContain("message");
    expect(coloredTexts(container, "tok-keyword")).toContain(keyword);
    expect(coloredTexts(container, "tok-string")).toContain('"café 😀"');
    expect(lineTexts(container)).toEqual(text.split(/\r\n|\r|\n/).slice(0, -1));
    expect(container.querySelector(".code-file")?.classList.contains("code-syntax")).toBe(true);
    expect(container.querySelector(".cm-editor")).toBeNull();
    expect(highlight).toHaveBeenCalledTimes(1);
    expect(highlight.mock.calls[0]?.slice(0, 2)).toEqual([path, text]);
  },
);

test("JSON uses its real grammar and retains exact escaped string and number text", async () => {
  const text = '{"message": "café 😀", "escaped": "a\\nb", "count": 2, "ready": true}\n';
  const { container } = setup(file("config.json", text));
  await colored();
  expect(coloredTexts(container, "tok-string")).toContain('"café 😀"');
  expect(coloredTexts(container, "tok-number")).toEqual(["2"]);
  expect(lineTexts(container)).toEqual([text.slice(0, -1)]);
});

test("empty final source lines remain numbered and Unicode identifiers retain their text", async () => {
  const text = 'const café = "😀";\n\n';
  const { container } = setup(file("unicode.ts", text));
  await colored();
  expect(lineTexts(container)).toEqual(['const café = "😀";', ""]);
  expect(coloredTexts(container, "tok-variableName")).toEqual(["café"]);
  expect(coloredTexts(container, "tok-string")).toEqual(['"😀"']);
  expect(container.querySelectorAll(".code-line")).toHaveLength(2);
});

test("BOM, tabs, CRLF, CR and Unicode offsets keep multiline string text exact", async () => {
  const text =
    '\ufeffdef greet(name):\r\n\tmessage = """café 😀\rsecond\nthird"""\r\n\treturn message\r';
  const { container } = setup(file("mixed.py", text, { newline: "mixed", encoding: "utf-8-bom" }));
  await colored();
  expect(lineTexts(container)).toEqual([
    "\ufeffdef greet(name):",
    '\tmessage = """café 😀',
    "second",
    'third"""',
    "\treturn message",
  ]);
  expect(coloredTexts(container, "tok-string")).toEqual(['"""café 😀', "second", 'third"""']);
  expect(coloredTexts(container, "tok-keyword")).toContain("return");
  expect(screen.getByText("Mixed")).toBeTruthy();
  expect(screen.getByText("utf-8-bom")).toBeTruthy();
});

test("markup inside a colored string stays text with no active DOM or network calls", async () => {
  const network = vi.spyOn(globalThis, "fetch");
  const text = 'const markup = "<script>alert(1)</script><img src=x onerror=alert(2)>";';
  const { container } = setup(file("markup.ts", text));
  await colored();
  expect(lineTexts(container)).toEqual([text]);
  expect(coloredTexts(container, "tok-string")).toEqual([
    '"<script>alert(1)</script><img src=x onerror=alert(2)>"',
  ]);
  expect(container.querySelector("script, img, iframe, .cm-editor")).toBeNull();
  expect(network).not.toHaveBeenCalled();
});

test("clicking tokens and line keys retain exact quote/editor arguments without reparsing", async () => {
  const text = 'def greet(name):\n\tmessage = "hello"\n\treturn message\n';
  const { container, props } = setup(file("src/greet.py", text));
  await colored();
  const token = container.querySelector(".tok-function") as HTMLElement;
  fireEvent.click(token);
  const area = screen.getByRole("listbox", { name: "src/greet.py" });
  expect(document.activeElement).toBe(area);
  fireEvent.keyDown(area, { key: "End", shiftKey: true });
  expect(
    [...container.querySelectorAll(".code-line-selected")].map((row) => row.textContent),
  ).toEqual(["1def greet(name):", '2\tmessage = "hello"', "3\treturn message"]);
  fireEvent.click(screen.getByRole("button", { name: "Quote in chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(props.onQuote).toHaveBeenCalledExactlyOnceWith("src/greet.py", 1, 3);
  expect(props.onOpen).toHaveBeenCalledExactlyOnceWith(1);
  await colored();
  expect(highlight).toHaveBeenCalledTimes(1);
  expect(container.querySelector(".tok-function")).toBe(token);
  expect(container.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
});

test("an inactive folder still cannot quote colored lines", async () => {
  const props = { canQuote: false, onQuote: vi.fn(), onOpen: vi.fn() };
  const { container } = render(
    <FileView file={file("greet.py", "def greet(): pass")} {...props} />,
  );
  await colored();
  fireEvent.click(container.querySelector(".tok-function") as HTMLElement);
  const quote = screen.getByRole("button", { name: "Quote in chat" });
  expect(quote.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(quote);
  expect(props.onQuote).not.toHaveBeenCalled();
});

test("unknown extensions stay plain and do not invoke the lazy coloring helper", async () => {
  const text = "<script>plain()</script>\r\n\t😀\r";
  const { container } = setup(file("crlf.txt", text));
  await colored();
  expect(lineTexts(container)).toEqual(["<script>plain()</script>", "\t😀"]);
  expect(container.querySelector(".code-text span, script")).toBeNull();
  expect(highlight).not.toHaveBeenCalled();
});

test("a supported file above the coloring budget retains its entire original text", async () => {
  const text = `const message = "${"x".repeat(1 << 18)}";`;
  const { container } = setup(file("large.ts", text));
  await colored();
  expect(lineTexts(container)).toEqual([text]);
  expect(container.querySelector(".code-text span")).toBeNull();
  expect(container.querySelector(".code-reason")).toBeNull();
});

test.each(["path", "text"])(
  "late grammar result after changing %s cannot color or reset the current selection",
  async (change) => {
    let resolve!: (tokens: readonly CodeToken[]) => void;
    highlight.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const original = file("first.py", "def original(): pass");
    const next = file(change === "path" ? "second.py" : "first.py", 'value = "next"');
    const { container, rerender, props } = setup(original);
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    const oldSignal = highlight.mock.calls[0]?.[2];
    rerender(<FileView file={next} {...props} />);
    await colored();
    fireEvent.click(screen.getByText("1"));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      resolve([{ from: 0, to: 3, className: "tok-function" }]);
    });
    expect(lineTexts(container)).toEqual(['value = "next"']);
    expect(coloredTexts(container, "tok-function")).toEqual([]);
    expect(coloredTexts(container, "tok-variableName")).toContain("value");
    expect(container.querySelector(".code-line-selected")?.textContent).toBe('1value = "next"');
    fireEvent.click(screen.getByRole("button", { name: "Quote in chat" }));
    expect(props.onQuote).toHaveBeenCalledExactlyOnceWith(next.path, 1, 1);
  },
);

test("unmount aborts pending coloring and ignores its later result", async () => {
  let resolve!: (tokens: readonly CodeToken[]) => void;
  highlight.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { container, unmount, props } = setup(file("pending.py", "def pending(): pass"));
  await act(async () => {
    await vi.dynamicImportSettled();
  });
  const signal = highlight.mock.calls[0]?.[2];
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    resolve([{ from: 0, to: 3, className: "tok-function" }]);
  });
  expect(container.childNodes).toHaveLength(0);
  expect(props.onQuote).not.toHaveBeenCalled();
  expect(props.onOpen).not.toHaveBeenCalled();
});
